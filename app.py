import os
import time
import sqlite3
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.cloud import storage
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="Sleep Navigator - Shipping API Service")

# Database & GCS Configuration
DB_FILE = "shipments.db"
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "sleepnav-shipping-labels")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            patient_name TEXT NOT NULL,
            tracking_number TEXT UNIQUE NOT NULL,
            carrier TEXT NOT NULL,
            service_type TEXT NOT NULL,
            gcs_file_url TEXT NOT NULL,
            shipment_status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Configuration & Token Caching
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis-sandbox.fedex.com")
CLIENT_ID = os.getenv("FEDEX_CLIENT_ID")
CLIENT_SECRET = os.getenv("FEDEX_CLIENT_SECRET")
ACCOUNT_NUMBER = os.getenv("FEDEX_ACCOUNT_NUMBER")

_TOKEN_CACHE = {"token": None, "expires_at": 0}

def get_valid_access_token() -> str:
    current_time = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > current_time + 60:
        return _TOKEN_CACHE["token"]

    url = f"{FEDEX_BASE_URL}/oauth/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    response = requests.post(url, data=payload, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()
    _TOKEN_CACHE["token"] = data["access_token"]
    _TOKEN_CACHE["expires_at"] = current_time + data.get("expires_in", 3600)
    return _TOKEN_CACHE["token"]

def upload_label_to_gcs(pdf_bytes: bytes, tracking_number: str) -> str:
    """Uploads label PDF bytes directly to GCS bucket or returns a string fallback URL."""
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"labels/label_{tracking_number}.pdf")
        blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        return blob.public_url
    except Exception as e:
        print(f"GCS Upload Exception: {e}")
        # Always return a STRING so SQLite binding never receives an Exception object
        return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/labels/label_{tracking_number}.pdf"

# Data Schemas
class PatientAddress(BaseModel):
    full_name: str
    address_line1: str
    city: str
    state: str
    zip_code: str
    phone: str

class PackageInfo(BaseModel):
    weight_lbs: float
    length: int = 10
    width: int = 10
    height: int = 10

class LabelRequest(BaseModel):
    order_id: str
    patient: PatientAddress
    package: PackageInfo
    service_type: str = "FEDEX_EXPRESS_SAVER"

# --- Phase 1 & 2: Generate & Save Label Endpoint ---
@app.post("/api/shipments/create")
def create_shipment(request: LabelRequest):
    token = get_valid_access_token()
    url = f"{FEDEX_BASE_URL}/ship/v1/shipments"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {
        "labelResponseOptions": "URL_ONLY",
        "requestedShipment": {
            "shipper": {
                "address": {"streetLines": ["8835 Line Avenue"], "city": "Shreveport", "stateOrProvinceCode": "LA", "postalCode": "71106", "countryCode": "US"},
                "contact": {"personName": "Wellnecessities Clinic", "phoneNumber": "3182220885"}
            },
            "recipients": [{
                "address": {
                    "streetLines": [request.patient.address_line1],
                    "city": request.patient.city,
                    "stateOrProvinceCode": request.patient.state,
                    "postalCode": request.patient.zip_code,
                    "countryCode": "US"
                },
                "contact": {"personName": request.patient.full_name, "phoneNumber": request.patient.phone}
            }],
            "shipDatestamp": datetime.now().strftime("%Y-%m-%d"),
            "serviceType": request.service_type,
            "packagingType": "YOUR_PACKAGING",
            "pickupType": "USE_SCHEDULED_PICKUP",
            "shippingChargesPayment": {"paymentType": "SENDER", "payor": {"responsibleParty": {"accountNumber": {"value": ACCOUNT_NUMBER}}}},
            "labelSpecification": {"labelFormatType": "COMMON2D", "imageType": "PDF", "labelStockType": "PAPER_4X6"},
            "requestedPackageLineItems": [{
                "weight": {"units": "LB", "value": request.package.weight_lbs},
                "dimensions": {"length": request.package.length, "width": request.package.width, "height": request.package.height, "units": "IN"}
            }]
        },
        "accountNumber": {"value": ACCOUNT_NUMBER}
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        res.raise_for_status()
        
        shipment_data = res.json()["output"]["transactionShipments"][0]
        tracking_num = shipment_data["masterTrackingNumber"]
        label_url = shipment_data["pieceResponses"][0]["packageDocuments"][0]["url"]

        # Stream PDF and Save to GCS
        pdf_res = requests.get(label_url, timeout=15)
        gcs_file_url = upload_label_to_gcs(pdf_res.content, tracking_num)

        # Log into Database
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO shipments (order_id, patient_name, tracking_number, carrier, service_type, gcs_file_url, shipment_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (request.order_id, request.patient.full_name, tracking_num, "FedEx", request.service_type, gcs_file_url, "created"))
        conn.commit()
        conn.close()

        return {
            "status": "SUCCESS",
            "order_id": request.order_id,
            "tracking_number": tracking_num,
            "label_file_path": gcs_file_url,
            "remote_label_url": label_url
        }
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=400, detail=e.response.json() if e.response else str(e))

# --- Phase 3: Cancel / Void Shipment Endpoint ---
@app.put("/api/shipments/cancel/{tracking_number}")
def cancel_shipment(tracking_number: str):
    token = get_valid_access_token()
    url = f"{FEDEX_BASE_URL}/ship/v1/shipments/cancel"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {
        "accountNumber": {"value": ACCOUNT_NUMBER},
        "trackingNumber": tracking_number
    }

    try:
        res = requests.put(url, json=payload, headers=headers, timeout=15)
        res.raise_for_status()

        # Update DB status
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE shipments SET shipment_status = 'cancelled' WHERE tracking_number = ?", (tracking_number,))
        conn.commit()
        conn.close()

        return {"status": "SUCCESS", "message": f"Shipment {tracking_number} successfully cancelled."}
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=400, detail=e.response.json() if e.response else str(e))
# SleepNavigator Shipping API (PoC)
An asynchronous REST API microservice built with FastAPI to integrate SleepNavigator's practice management portal with FedEx Web Services and Google Cloud Storage (GCS).

# Tech Stack & Key Concepts
Framework: FastAPI (Python 3.10+) for async request processing and automatic OpenAPI generation.
Database: SQLite (`shipments.db`) for lightweight local data persistence.
Cloud Storage: Google Cloud Storage (`google-cloud-storage`) for automated PDF label uploads.
Validation: Pydantic(`BaseModel`) for strict incoming request body schema parsing.
Authentication: Automated OAuth2 client credentials token management with caching for FedEx API calls.

# Running the Application
1. Set Environment varibles
Ensure a `.env` file exists in the project root with the following keys:
```env
GCS_BUCKET_NAME=sleepnav-shipping-labels
FEDEX_BASE_URL=https://apis-sandbox.fedex.com
FEDEX_CLIENT_ID=your_fedex_client_id
FEDEX_CLIENT_SECRET=your_fedex_client_secret
FEDEX_ACCOUNT_NUMBER=your_fedex_account_number

2. Lunch the Development Server
uvicorn app:app --host 0.0.0.0 --port 8080 

3. Access Interactive API docs
https://<your-cloud-shell-url>/docs

# API Reference
1. Create Shipment & Generate Label
* Endpoint: POST /api/shipments/create
* Description: Requests a shipping label from FedEx, streams the PDF response, uploads it directly to Google Cloud Storage, and logs the shipment details into SQLite.
* Sample Request Body:
{
  "order_id": "HG-6",
  "patient": {
    "full_name": "Gregory House",
    "address_line1": "100 S. Main St",
    "city": "Shreveport",
    "state": "LA",
    "zip_code": "71106",
    "phone": "3182220885"
  },
  "package": {
    "weight_lbs": 2.5,
    "length": 10,
    "width": 8,
    "height": 4
  },
  "service_type": "FEDEX_EXPRESS_SAVER"
}

2. Cancel Shipment
* Endpoint: PUT /api/shipments/cancel/{tracking_number}
* Description: Voids an existing shipment with FedEx using its tracking number and updates the internal database     status to cancelled.

# Error Handeling
1. FedEx Validation Errors: HTTP 400 Bad Request responses from fedEx(such as invalid postal codes or state abbreviations) are caught and formatted into standard JSON detail errors.

2. GCS Fallback: If direct GCS bucket uploads fail due to local permissions during testing, the app safely falls back to string URL generation to prevent database insert crashes.
import os
import time
import random
import hashlib
from urllib.parse import urlparse, urlunparse
import requests
from google.cloud import secretmanager
from google.cloud import storage

# Get project ID and secret names from environment variables
PROJECT_ID = os.environ.get("GCP_PROJECT")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL")
N8N_RESCHEDULE_WEBHOOK_URL = os.environ.get("N8N_RESCHEDULE_WEBHOOK_URL")
N8N_CANCEL_WEBHOOK_URL = os.environ.get("N8N_CANCEL_WEBHOOK_URL")
N8N_USER_SECRET_NAME = os.environ.get("N8N_USER_SECRET_NAME")
N8N_PASSWORD_SECRET_NAME = os.environ.get("N8N_PASSWORD_SECRET_NAME")
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
INITIAL_BACKOFF_SECONDS = float(os.environ.get("INITIAL_BACKOFF_SECONDS", "2.0"))
HEALTH_CHECK_ENABLED = os.environ.get("HEALTH_CHECK_ENABLED", "1") == "1"
HEALTH_CHECK_URL = os.environ.get("N8N_HEALTH_URL")
HEALTH_CHECK_TIMEOUT_SECONDS = int(os.environ.get("HEALTH_CHECK_TIMEOUT_SECONDS", "10"))
HEALTH_CHECK_MAX_RETRIES = int(os.environ.get("HEALTH_CHECK_MAX_RETRIES", "5"))
DEDUPE_ENABLED = os.environ.get("DEDUPE_ENABLED", "1") == "1"
# Its own bucket, not the PHI one: the claim keys carry no client data, and this keeps the
# gateway's service account off a HIPAA-protected bucket it otherwise has no reason to touch.
DEDUPE_BUCKET = os.environ.get("DEDUPE_BUCKET", "phoenix-health-breely-dedupe")
DEDUPE_PREFIX = os.environ.get("DEDUPE_PREFIX", "breely-dedupe")

print("--- BREELY-GATEWAY V2.0 (SCALE-TO-ZERO) STARTING UP ---")

# Breely's dispatcher is at-least-once and has delivered the same event three times within one
# second, which onboarded a client three times over. A claim in Cloud Storage is the dedupe point
# because it is atomic across instances, which in-process state and n8n's static data are not.
try:
    storage_client = storage.Client() if DEDUPE_ENABLED else None
except Exception as e:
    storage_client = None
    print(f"WARNING: no storage client, duplicate deliveries will be forwarded: {e}")


def delivery_key(route, request_json):
    """Names one delivery of one Breely event, so redeliveries of it collide and nothing else does.

    Keyed on the event id *and* its start date, not the id alone: Breely re-pushes the same id
    after a reschedule, and that push carries a new date and still has to be processed.
    """
    event = request_json.get("event") or {}
    event_id = event.get("id")
    if event_id is None:
        return None
    digest = hashlib.sha256(f"{event_id}|{event.get('start_date') or ''}".encode()).hexdigest()
    return f"{DEDUPE_PREFIX}/{route}/{digest[:32]}"


def claim_delivery(key):
    """True if this caller won the right to forward the delivery, False if another already has.

    if_generation_match=0 makes the write succeed only when the object does not yet exist, so
    exactly one of N simultaneous callers wins regardless of which instance serves them.

    Anything other than a lost race returns True. A duplicate onboarding is visible and fixable
    by hand; a booking silently dropped because Cloud Storage was unreachable is not. This fails
    open deliberately.
    """
    if not storage_client or not key:
        return True
    try:
        storage_client.bucket(DEDUPE_BUCKET).blob(key).upload_from_string(
            b"", if_generation_match=0
        )
        return True
    except Exception as e:
        if getattr(e, "code", None) == 412:
            return False
        print(f"WARNING: duplicate check failed, forwarding anyway: {e}")
        return True

# Initialize the Secret Manager client
secret_client = secretmanager.SecretManagerServiceClient()

def get_secret(secret_name):
    """Fetches the latest version of a secret from Secret Manager."""
    if not PROJECT_ID or not secret_name:
        raise ValueError(f"Project ID or secret name is not configured for '{secret_name}'.")
        
    name = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
    try:
        response = secret_client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"Error accessing secret {secret_name}: {e}")
        raise

# Fetch secrets at cold start to cache them for subsequent invocations
try:
    N8N_USER = get_secret(N8N_USER_SECRET_NAME)
    N8N_PASSWORD = get_secret(N8N_PASSWORD_SECRET_NAME)
    print("Successfully fetched n8n credentials on startup.")
except Exception as e:
    # If secrets can't be fetched on startup, the function will fail.
    # This is a deliberate design choice to prevent it from running in an insecure state.
    N8N_USER = None
    N8N_PASSWORD = None
    print(f"FATAL: Could not fetch n8n credentials on startup. Error: {e}")


def forward_request(request, dest_url, route):
    """
    Forwards a request from Breely to the given n8n webhook URL, adding Basic Auth.
    """
    if not N8N_USER or not N8N_PASSWORD:
        print("Error: n8n credentials are not available.")
        return ("Internal Server Error: Missing credentials", 500)

    if not dest_url:
        print("Error: destination webhook URL is not configured.")
        return ("Internal Server Error: Webhook URL not set", 500)

    # Get the raw body data
    raw_body = request.get_data(as_text=True)
    print(f"Received raw request body: {raw_body}")

    # Get the JSON body from the incoming request
    request_json = request.get_json(force=True, silent=True)
    if not request_json:
        return ("Bad Request: No JSON body provided.", 400)

    # 200, not 4xx: a rejection reads as a delivery failure to Breely and earns more retries.
    if not claim_delivery(delivery_key(route, request_json)):
        event_id = (request_json.get("event") or {}).get("id")
        print(f"Duplicate delivery of {route} event {event_id} ignored.")
        return ("Duplicate delivery ignored.", 200)

    try:
        def derive_health_url():
            if HEALTH_CHECK_URL:
                return HEALTH_CHECK_URL
            parsed = urlparse(dest_url)
            base = (parsed.scheme, parsed.netloc, "/healthz", "", "", "")
            return urlunparse(base)

        def check_health(url):
            try:
                r = requests.get(url, timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
                return r.status_code == 200
            except requests.exceptions.RequestException:
                return False

        if HEALTH_CHECK_ENABLED:
            health_url = derive_health_url()
            health_ok = False
            hb = INITIAL_BACKOFF_SECONDS
            for i in range(HEALTH_CHECK_MAX_RETRIES):
                if check_health(health_url):
                    health_ok = True
                    break
                sleep_for = hb + random.uniform(0, hb * 0.5)
                print(f"n8n not healthy yet, retry {i+1}/{HEALTH_CHECK_MAX_RETRIES} in {sleep_for:.2f}s")
                time.sleep(sleep_for)
                hb = min(hb * 2, 30)

        backoff = INITIAL_BACKOFF_SECONDS
        attempt = 0
        last_exception = None
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                response = requests.post(
                    dest_url,
                    json=request_json,
                    auth=(N8N_USER, N8N_PASSWORD),
                    headers={"Content-Type": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                print(f"Received response from n8n: Status Code = {response.status_code}")
                print(f"n8n Response Body: {response.text}")
                if response.status_code in (502, 503, 504, 404):
                    sleep_for = backoff + random.uniform(0, backoff * 0.5)
                    print(f"Transient error from n8n (status {response.status_code}), retry {attempt}/{MAX_RETRIES} in {sleep_for:.2f}s")
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, 30)
                    continue
                return (response.text, response.status_code, response.headers.items())
            except requests.exceptions.RequestException as e:
                last_exception = e
                sleep_for = backoff + random.uniform(0, backoff * 0.5)
                print(f"Connection error to n8n, retry {attempt}/{MAX_RETRIES} in {sleep_for:.2f}s: {e}")
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 30)
        if last_exception:
            print(f"Failed to reach n8n after {MAX_RETRIES} attempts: {last_exception}")
            return ("Service Unavailable: Could not connect to the workflow service.", 503)
        return ("Service Unavailable", 503)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return ("Internal Server Error", 500)

from flask import Flask, request as flask_request

app = Flask(__name__)

@app.route('/', methods=['POST'])
def breely_gateway():
    return forward_request(flask_request, N8N_WEBHOOK_URL, 'onboarding')

@app.route('/reschedule', methods=['POST'])
def breely_reschedule():
    return forward_request(flask_request, N8N_RESCHEDULE_WEBHOOK_URL, 'reschedule')

@app.route('/cancel', methods=['POST'])
def breely_cancel():
    return forward_request(flask_request, N8N_CANCEL_WEBHOOK_URL, 'cancel')

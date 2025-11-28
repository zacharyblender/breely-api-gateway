import os
import requests
from google.cloud import secretmanager

# Get project ID and secret names from environment variables
PROJECT_ID = os.environ.get("GCP_PROJECT")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL")
N8N_USER_SECRET_NAME = os.environ.get("N8N_USER_SECRET_NAME")
N8N_PASSWORD_SECRET_NAME = os.environ.get("N8N_PASSWORD_SECRET_NAME")

print("--- BREELY-GATEWAY V2.0 (SCALE-TO-ZERO) STARTING UP ---")

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


def forward_request(request):
    """
    Forwards a request from Breely to the n8n webhook, adding Basic Auth.
    """
    if not N8N_USER or not N8N_PASSWORD:
        print("Error: n8n credentials are not available.")
        return ("Internal Server Error: Missing credentials", 500)

    if not N8N_WEBHOOK_URL:
        print("Error: N8N_WEBHOOK_URL is not configured.")
        return ("Internal Server Error: Webhook URL not set", 500)

    # Get the raw body data
    raw_body = request.get_data(as_text=True)
    print(f"Received raw request body: {raw_body}")

    # Get the JSON body from the incoming request
    request_json = request.get_json(force=True, silent=True)
    if not request_json:
        return ("Bad Request: No JSON body provided.", 400)

    try:
        # Forward the request to the n8n webhook with Basic Auth
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=request_json,
            auth=(N8N_USER, N8N_PASSWORD),
            headers={"Content-Type": "application/json"}
        )
        
        # Log the response from n8n for debugging
        print(f"Received response from n8n: Status Code = {response.status_code}")
        print(f"n8n Response Body: {response.text}")

        # Return the response from n8n to the original caller
        return (response.text, response.status_code, response.headers.items())

    except requests.exceptions.RequestException as e:
        print(f"Error forwarding request to n8n: {e}")
        return ("Service Unavailable: Could not connect to the workflow service.", 503)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return ("Internal Server Error", 500)

from flask import Flask, request as flask_request

app = Flask(__name__)

@app.route('/', methods=['POST'])
def breely_gateway():
    """
    Cloud Function entry point.
    """
    return forward_request(flask_request)
# breely-api-gateway

This is the API gateway for forwarding Breely webhooks to the n8n client onboarding workflow.

## Security Hardening Instructions

To protect the n8n service from direct public access and mitigate vulnerability scanning, the following infrastructure changes are required. Please execute these commands in your Google Cloud Shell.

### 1. Restrict n8n Service Ingress

This change will make the n8n Cloud Run service private, allowing only internal traffic from sources like a Load Balancer or VPC Connector.

```bash
# Replace 'n8n' with your actual n8n service name if different
N8N_SERVICE_NAME="n8n"
GCP_REGION="us-central1"

gcloud run services update ${N8N_SERVICE_NAME} \
  --region=${GCP_REGION} \
  --ingress=internal-and-cloud-load-balancing
```

### 2. Set up Serverless VPC Connector

A VPC connector is required to allow the `breely-api-gateway` (a Cloud Run service) to communicate with the now-internal `n8n` service.

**Note:** This is a billable component.

```bash
# Create the VPC Connector
CONNECTOR_NAME="n8n-connector"
VPC_NETWORK="default" # Or your custom VPC network name
IP_CIDR_RANGE="10.8.0.0" # Must be an unused /28 CIDR range

gcloud compute networks vpc-access connectors create ${CONNECTOR_NAME} \
  --region=${GCP_REGION} \
  --network=${VPC_NETWORK} \
  --range=${IP_CIDR_RANGE}

# Update the breely-api-gateway to use the connector
GATEWAY_SERVICE_NAME="breely-api-gateway"

gcloud run services update ${GATEWAY_SERVICE_NAME} \
  --region=${GCP_REGION} \
  --vpc-connector=${CONNECTOR_NAME}
```

### 3. Deploy API Gateway with Cloud Armor WAF

Instead of directly exposing the `breely-api-gateway`, we will place it behind an API Gateway with a Web Application Firewall (WAF) enabled. This will provide an enterprise-grade security layer to block common attacks.

**Step 3.1: Create an API Config**

First, create an OpenAPI specification file named `openapi.yaml` with the following content:

```yaml
swagger: "2.0"
info:
  title: Breely API Gateway
  description: Proxies requests to the secure n8n webhook.
  version: 1.0.0
schemes:
  - https
produces:
  - application/json
paths:
  /onboard:
    post:
      summary: Forwards client onboarding requests.
      operationId: forwardOnboardingRequest
      x-google-backend:
        address: https://<YOUR_GATEWAY_URL> # Replace with your breely-api-gateway URL
      responses:
        '200':
          description: A successful response
          schema:
            type: string
```

**Step 3.2: Create a Cloud Armor Security Policy**

```bash
# Create a default security policy to block common attacks
SECURITY_POLICY_NAME="breely-gateway-policy"

gcloud compute security-policies create ${SECURITY_POLICY_NAME} \
  --description="Default WAF policy for the Breely gateway."

# Add the preconfigured OWASP Top 10 rule
gcloud compute security-policies rules create 1000 \
  --security-policy=${SECURITY_POLICY_NAME} \
  --expression="evaluatePreconfiguredExpr('owasp-crs-v3.3-stable')" \
  --action="deny-403"
```

**Step 3.3: Create and Deploy the API Gateway**

```bash
# Create the API
gcloud api-gateway apis create breely-onboarding-api

# Create the API Config using the openapi.yaml file
gcloud api-gateway api-configs create breely-v1-config \
  --api=breely-onboarding-api --openapi-spec=openapi.yaml

# Deploy the Gateway
gcloud api-gateway gateways create breely-onboarding-gateway \
  --api=breely-onboarding-api --api-config=breely-v1-config \
  --location=${GCP_REGION}

# (Optional but Recommended) Update the gateway to use the Cloud Armor policy
# This requires getting the backend service name of the created gateway
# and is a more advanced step. Refer to GCP documentation for associating
# Cloud Armor with an API Gateway backend.
```


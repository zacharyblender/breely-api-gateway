"""Drives the gateway's real Flask routes, with the hop to n8n stubbed out.

Proves the duplicate is stopped at the route level and that a first delivery still forwards.
"""
import os
import sys
import uuid

RUN = uuid.uuid4().hex[:12]
os.environ["DEDUPE_PREFIX"] = f"breely-dedupe-test/{RUN}"
os.environ["HEALTH_CHECK_ENABLED"] = "0"
# Pinned to a bucket that already exists; production's default bucket is created at deploy time.
os.environ["DEDUPE_BUCKET"] = "phoenix-health-hipaa-protected-data"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402

# Stand in for n8n and for the credentials the real thing loads from Secret Manager.
main.N8N_USER, main.N8N_PASSWORD = "u", "p"
main.N8N_WEBHOOK_URL = "https://n8n.example.invalid/webhook/onboarding"
main.HEALTH_CHECK_ENABLED = False

forwarded = []


class FakeResponse:
    status_code = 200
    text = '{"message":"Workflow was started"}'
    headers = {}


def fake_post(url, **kwargs):
    forwarded.append(kwargs.get("json"))
    return FakeResponse()


main.requests.post = fake_post

BODY = {"event": {"id": 467349, "start_date": "Aug 18, 2026"}, "submission": {"name": "test"}}
RESCHEDULED = {"event": {"id": 467349, "start_date": "Aug 25, 2026"}}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


client = main.app.test_client()

r1 = client.post("/", json=BODY)
check("first delivery returns 200 and forwards to n8n",
      r1.status_code == 200 and len(forwarded) == 1, f"status={r1.status_code} forwarded={len(forwarded)}")

r2 = client.post("/", json=BODY)
check("redelivery returns 200 (so Breely does not escalate retries)", r2.status_code == 200, f"status={r2.status_code}")
check("redelivery is NOT forwarded to n8n", len(forwarded) == 1, f"forwarded={len(forwarded)}")
check("redelivery says so in the body", b"Duplicate" in r2.data, r2.data[:60].decode())

r3 = client.post("/", json=BODY)
check("a third delivery is also stopped", r3.status_code == 200 and len(forwarded) == 1, f"forwarded={len(forwarded)}")

r4 = client.post("/", json=RESCHEDULED)
check("a genuine reschedule of the same event still forwards", len(forwarded) == 2, f"forwarded={len(forwarded)}")

r5 = client.post("/", json={"submission": {}})
check("a payload with no event id still forwards (fails open)", len(forwarded) == 3, f"forwarded={len(forwarded)}")

r6 = client.post("/", data="not json", content_type="application/json")
check("a malformed body is still rejected with 400", r6.status_code == 400, f"status={r6.status_code}")

r7 = client.get("/")
check("GET is still refused", r7.status_code == 405, f"status={r7.status_code}")

deleted = 0
for b in main.storage_client.list_blobs(main.DEDUPE_BUCKET, prefix=f"breely-dedupe-test/{RUN}"):
    b.delete()
    deleted += 1
print(f"\ncleaned up {deleted} test object(s)")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)

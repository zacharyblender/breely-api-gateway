"""Drives the gateway's real Flask routes, with the hops to n8n and the Scheduler stubbed out.

Proves the duplicate is stopped at the route level, that a first delivery still forwards,
and that nothing the Scheduler copy can do changes what Breely sees.
"""
import os
import sys
import time
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
main.N8N_RESCHEDULE_WEBHOOK_URL = "https://n8n.example.invalid/webhook/reschedule"
main.N8N_CANCEL_WEBHOOK_URL = "https://n8n.example.invalid/webhook/cancel"
main.HEALTH_CHECK_ENABLED = False
# Secret Manager is not reachable from a test run; the fan-out reads the cache, not the API.
main._scheduler_secret = "test-secret"

forwarded = []


class FakeResponse:
    status_code = 200
    text = '{"message":"Workflow was started"}'
    headers = {}


def fake_post(url, **kwargs):
    """Records (url, json, timeout) for every hop — the URL is how n8n forwards and
    Scheduler copies are told apart, which a json-only recorder could not do."""
    forwarded.append((url, kwargs.get("json"), kwargs.get("timeout")))
    return FakeResponse()


main.requests.post = fake_post

BODY = {"event": {"id": 467349, "start_date": "Aug 18, 2026"}, "submission": {"name": "test"}}
RESCHEDULED = {"event": {"id": 467349, "start_date": "Aug 25, 2026"}}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def n8n_hops():
    return [f for f in forwarded if "n8n.example.invalid" in f[0]]


def scheduler_hops():
    return [f for f in forwarded if "scheduler.example.invalid" in f[0]]


def fresh(event_id):
    """A body nothing has claimed yet, so each case starts from a won claim."""
    return {"event": {"id": event_id, "start_date": "Aug 18, 2026"}, "submission": {"name": "test"}}


client = main.app.test_client()

# --- dedupe and the n8n forward, with the Scheduler copy switched off -------------------
main.SCHEDULER_INGEST_URL = None

r1 = client.post("/", json=BODY)
check("first delivery returns 200 and forwards to n8n",
      r1.status_code == 200 and len(n8n_hops()) == 1, f"status={r1.status_code} forwarded={len(n8n_hops())}")

r2 = client.post("/", json=BODY)
check("redelivery returns 200 (so Breely does not escalate retries)", r2.status_code == 200, f"status={r2.status_code}")
check("redelivery is NOT forwarded to n8n", len(n8n_hops()) == 1, f"forwarded={len(n8n_hops())}")
check("redelivery says so in the body", b"Duplicate" in r2.data, r2.data[:60].decode())

r3 = client.post("/", json=BODY)
check("a third delivery is also stopped", r3.status_code == 200 and len(n8n_hops()) == 1, f"forwarded={len(n8n_hops())}")

r4 = client.post("/", json=RESCHEDULED)
check("a genuine reschedule of the same event still forwards", len(n8n_hops()) == 2, f"forwarded={len(n8n_hops())}")

r5 = client.post("/", json={"submission": {}})
check("a payload with no event id still forwards (fails open)", len(n8n_hops()) == 3, f"forwarded={len(n8n_hops())}")

r6 = client.post("/", data="not json", content_type="application/json")
check("a malformed body is still rejected with 400", r6.status_code == 400, f"status={r6.status_code}")

r7 = client.get("/")
check("GET is still refused", r7.status_code == 405, f"status={r7.status_code}")

check("no Scheduler copy is attempted while SCHEDULER_INGEST_URL is unset", len(scheduler_hops()) == 0,
      f"copies={len(scheduler_hops())}")

# --- the /reschedule and /cancel routes, which had no coverage -------------------------
r8 = client.post("/reschedule", json=fresh(467400))
check("/reschedule forwards to the reschedule webhook",
      r8.status_code == 200 and n8n_hops()[-1][0] == main.N8N_RESCHEDULE_WEBHOOK_URL, n8n_hops()[-1][0])

r9 = client.post("/cancel", json=fresh(467401))
check("/cancel forwards to the cancel webhook",
      r9.status_code == 200 and n8n_hops()[-1][0] == main.N8N_CANCEL_WEBHOOK_URL, n8n_hops()[-1][0])

# The claim key is namespaced by route, so the same event id on a different route is a
# different delivery — otherwise a cancel would be swallowed by its own booking's claim.
r10 = client.post("/cancel", json=fresh(467400))
check("the same event id on a different route is not treated as a duplicate",
      r10.status_code == 200 and b"Duplicate" not in r10.data, r10.data[:60].decode())

r11 = client.post("/reschedule", json=fresh(467400))
check("a redelivery on the same route IS still a duplicate", b"Duplicate" in r11.data, r11.data[:60].decode())

# --- the Scheduler copy ---------------------------------------------------------------
main.SCHEDULER_INGEST_URL = "https://scheduler.example.invalid/api/internal/breely-import"

before = len(n8n_hops())
r12 = client.post("/", json=fresh(467500))
copy = scheduler_hops()[-1] if scheduler_hops() else (None, None)
check("a delivery is copied to the Scheduler as well as n8n",
      len(scheduler_hops()) == 1 and len(n8n_hops()) == before + 1,
      f"copies={len(scheduler_hops())} n8n={len(n8n_hops())}")
check("the copy carries the route name and the untouched Breely payload",
      copy[1] == {"route": "onboarding", "payload": fresh(467500)}, str(copy[1])[:120])
check("the copy goes to the Scheduler, not to n8n", copy[0] == main.SCHEDULER_INGEST_URL, str(copy[0]))
# The bound is what makes a hanging Scheduler survivable at all: without it the copy would
# inherit requests' default of no timeout and hold Breely's connection open indefinitely.
check("the copy is bounded by a short timeout, well under n8n's own",
      copy[2] is not None and 0 < copy[2] <= 10 and copy[2] < main.REQUEST_TIMEOUT_SECONDS, str(copy[2]))
check("the copy happens before the n8n forward (the board sees it first)",
      forwarded[-2][0] == main.SCHEDULER_INGEST_URL, forwarded[-2][0])
check("Breely still gets n8n's own status and body", r12.status_code == 200 and b"Workflow was started" in r12.data,
      f"status={r12.status_code}")

r13 = client.post("/cancel", json=fresh(467501))
check("the copy names the route it arrived on", scheduler_hops()[-1][1]["route"] == "cancel",
      scheduler_hops()[-1][1]["route"])


def post_that_fails_the_scheduler(exc):
    """n8n keeps working; only the Scheduler hop misbehaves."""
    def _post(url, **kwargs):
        if url == main.SCHEDULER_INGEST_URL:
            raise exc
        return fake_post(url, **kwargs)
    return _post


main.requests.post = post_that_fails_the_scheduler(main.requests.exceptions.ConnectionError("refused"))
before = len(n8n_hops())
r14 = client.post("/", json=fresh(467600))
check("a Scheduler that refuses the connection leaves the n8n response untouched",
      r14.status_code == 200 and b"Workflow was started" in r14.data and len(n8n_hops()) == before + 1,
      f"status={r14.status_code} n8n={len(n8n_hops())}")

main.requests.post = post_that_fails_the_scheduler(main.requests.exceptions.Timeout("timed out"))
before = len(n8n_hops())
started = time.monotonic()
r15 = client.post("/", json=fresh(467601))
elapsed = time.monotonic() - started
check("a Scheduler that hangs until timeout leaves the n8n response untouched",
      r15.status_code == 200 and b"Workflow was started" in r15.data and len(n8n_hops()) == before + 1,
      f"status={r15.status_code} n8n={len(n8n_hops())}")
check("and costs Breely no retry latency of its own", elapsed < 2, f"{elapsed:.2f}s")

main.requests.post = post_that_fails_the_scheduler(RuntimeError("secret manager unavailable"))
before = len(n8n_hops())
r16 = client.post("/", json=fresh(467602))
check("a Scheduler copy raising a non-request error is swallowed too",
      r16.status_code == 200 and len(n8n_hops()) == before + 1, f"status={r16.status_code}")

# A missing secret must disable the copy, not the gateway.
main.requests.post = fake_post
main._scheduler_secret = None
original_get_secret = main.get_secret
main.get_secret = lambda name: (_ for _ in ()).throw(RuntimeError("no such secret"))
before = len(n8n_hops())
r17 = client.post("/", json=fresh(467603))
main.get_secret = original_get_secret
main._scheduler_secret = "test-secret"
check("an unfetchable Scheduler secret still forwards to n8n",
      r17.status_code == 200 and len(n8n_hops()) == before + 1, f"status={r17.status_code}")

deleted = 0
for b in main.storage_client.list_blobs(main.DEDUPE_BUCKET, prefix=f"breely-dedupe-test/{RUN}"):
    b.delete()
    deleted += 1
print(f"\ncleaned up {deleted} test object(s)")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)

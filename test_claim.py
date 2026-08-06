"""Exercises the gateway's duplicate-delivery claim against live Cloud Storage.

Writes only under a throwaway prefix and deletes what it writes. Read the summary at the end:
every check must say PASS.
"""
import os
import sys
import uuid
import concurrent.futures as cf

RUN = uuid.uuid4().hex[:12]
os.environ["DEDUPE_PREFIX"] = f"breely-dedupe-test/{RUN}"
os.environ["HEALTH_CHECK_ENABLED"] = "0"
# Pinned to a bucket that already exists; production's default bucket is created at deploy time.
os.environ["DEDUPE_BUCKET"] = "phoenix-health-hipaa-protected-data"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


BOOKING = {"event": {"id": 467349, "start_date": "Aug 18, 2026"}}
RESCHEDULED = {"event": {"id": 467349, "start_date": "Aug 25, 2026"}}

k1 = main.delivery_key("onboarding", BOOKING)
k2 = main.delivery_key("onboarding", dict(BOOKING))
k3 = main.delivery_key("onboarding", RESCHEDULED)
k4 = main.delivery_key("reschedule", BOOKING)

check("a redelivery of the same event+date maps to the same key", k1 == k2)
check("the same event at a new date is a different key (reschedules still pass)", k1 != k3)
check("routes do not collide", k1 != k4)
check("a payload with no event id yields no key", main.delivery_key("onboarding", {}) is None)
check("no client identifiers appear in the key", "467349" not in k1 and "Aug" not in k1, k1)

# Sequential: first caller wins, redelivery loses.
check("first delivery is claimed", main.claim_delivery(k1) is True)
check("second delivery of the same event is rejected", main.claim_delivery(k1) is False)
check("third delivery is still rejected", main.claim_delivery(k1) is False)
check("the rescheduled push is NOT blocked by the original", main.claim_delivery(k3) is True)

# The real test: simultaneous callers, which is what Breely produced at 0-1s spacing.
race_key = main.delivery_key("onboarding", {"event": {"id": 999001, "start_date": "Sep 1, 2026"}})
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    won = list(ex.map(lambda _: main.claim_delivery(race_key), range(12)))
check("exactly one of 12 simultaneous claims wins", sum(won) == 1, f"winners={sum(won)}/12")

# Fail-open: an unreachable bucket must forward rather than drop a booking.
real_bucket = main.DEDUPE_BUCKET
main.DEDUPE_BUCKET = f"nonexistent-bucket-{RUN}"
check("an unreachable bucket forwards instead of dropping the booking",
      main.claim_delivery(main.delivery_key("onboarding", {"event": {"id": 999002, "start_date": "x"}})) is True)
main.DEDUPE_BUCKET = real_bucket

# Clean up everything this run wrote.
deleted = 0
try:
    for b in main.storage_client.list_blobs(main.DEDUPE_BUCKET, prefix=f"breely-dedupe-test/{RUN}"):
        b.delete()
        deleted += 1
except Exception as e:
    print(f"cleanup error: {e}")
print(f"\ncleaned up {deleted} test object(s) under breely-dedupe-test/{RUN}")

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)

"""Call-end webhook dedup (_pick_call_log_for): the auto-log must FOLD into an existing
CALL_LOG (agent dispositioned first, or the webhook fired twice) instead of duplicating,
for BOTH inbound and outbound — without merging a different call or crossing directions.
DB-free — exercises the pure matcher. Run::

    python tests/test_call_autolog_dedup.py     # or: pytest tests/test_call_autolog_dedup.py
"""
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.leadService as L  # noqa: E402


def _a(details, mins_ago=0):
    return SimpleNamespace(uid="a", details=details,
                           created_at=datetime.now(timezone.utc) - timedelta(minutes=mins_ago))


def test_exact_call_id_wins():
    items = [_a({"direction": "inbound", "call_id": "X", "disposition": "Connected"})]
    assert L._pick_call_log_for(items, "X", "inbound") is items[0]


def test_different_call_id_not_merged():
    items = [_a({"direction": "inbound", "call_id": "Y"}, mins_ago=1)]
    assert L._pick_call_log_for(items, "X", "inbound") is None      # other call -> fresh row


def test_fresh_inbound_disposition_matched():
    # reverse race: agent's inbound disposition has no call_id, is fresh
    items = [_a({"direction": "inbound", "disposition": "Connected · Order Booked"}, mins_ago=1)]
    assert L._pick_call_log_for(items, "X", "inbound") is items[0]


def test_fresh_outbound_disposition_matched():
    # if the outbound webhook DOES fire, its fresh no-call_id disposition folds in too
    items = [_a({"direction": "outbound", "disposition": "Connected · Order Booked"}, mins_ago=1)]
    assert L._pick_call_log_for(items, "X", "outbound") is items[0]


def test_cross_direction_not_matched():
    items = [_a({"direction": "inbound"}, mins_ago=1)]
    assert L._pick_call_log_for(items, None, "outbound") is None    # different leg type


def test_stale_not_matched():
    items = [_a({"direction": "inbound"}, mins_ago=120)]
    assert L._pick_call_log_for(items, None, "inbound") is None     # too old to be this call


def test_no_direction_no_fallback():
    items = [_a({"direction": "inbound"}, mins_ago=1)]
    assert L._pick_call_log_for(items, None, None) is None          # nothing to key the fallback on


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all call-autolog dedup checks passed")

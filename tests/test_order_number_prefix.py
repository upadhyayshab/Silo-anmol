"""Order-number prefix + backfill rewrite (ORD-CRM- for in-house CRM orders).

Run with::
    python tests/test_order_number_prefix.py
    # or: pytest tests/test_order_number_prefix.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import path_setup  # noqa: F401,E402  (puts SharedBackend on the path)
from routers.v1.orders import generate_order_number  # noqa: E402

# crm_order_number lives in the backfill script (scripts/), load it by path.
_bf_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "migrations",
                        "backfill_order_attribution.py")
_spec = importlib.util.spec_from_file_location("_bf", _bf_path)
_bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bf)
crm_order_number = _bf.crm_order_number


def test_generate_prefix():
    assert generate_order_number(is_crm=True).startswith("ORD-CRM-")
    on = generate_order_number(is_crm=False)
    assert on.startswith("ORD-") and not on.startswith("ORD-CRM-")


def test_rewrite_plain_to_crm():
    assert crm_order_number("ORD-20260704-ABCD") == "ORD-CRM-20260704-ABCD"


def test_rewrite_is_idempotent():
    assert crm_order_number("ORD-CRM-20260704-ABCD") is None  # already prefixed


def test_rewrite_ignores_non_ord():
    # Non-ORD- numbers are left alone. (Medusa ORD-STORE- orders never reach this
    # function — the backfill query excludes them by uid LIKE 'order_%'.)
    assert crm_order_number("FOO-1") is None
    assert crm_order_number(None) is None
    assert crm_order_number("") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")

"""Numeric filter values against text columns must bind as text, not BIGINT.

filtering_dependency coerces all-digit query values to int; asyncpg binds those as
BIGINT and Postgres has no `character varying = bigint` operator, so a phone number
typed into the Order Number box 500s the whole orders list. ERPGenericManager._filter
repairs it, because that's the only layer that knows the column's type.

Run with::
    python tests/test_text_col_int_coercion.py
    # or: pytest tests/test_text_col_int_coercion.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import path_setup  # noqa: F401,E402  (puts SharedBackend on the path)
import sqlalchemy as db  # noqa: E402
from managers.erpManagers import ERPGenericManager, CustomerOrderSchema  # noqa: E402


def _sql(filters):
    q = asyncio.run(ERPGenericManager._filter(
        db.select(CustomerOrderSchema), filters, CustomerOrderSchema,
    ))
    return str(q.compile(compile_kwargs={"literal_binds": True}))


def test_scalar_int_binds_as_text():
    # The actual prod 500: 9784468467 typed into the order-number filter.
    assert "'9784468467'" in _sql({"order_number": 9784468467})


def test_operator_dict_and_in_list_bind_as_text():
    assert "'9784468467'" in _sql({"order_number": {"$neq": 9784468467}})
    assert "'9784468467'" in _sql({"order_number": {"$in": [9784468467, 123]}})
    assert "'123'" in _sql({"order_number": {"$in": [9784468467, 123]}})


def test_other_text_columns_covered_not_just_order_number():
    # The point of fixing it in the manager: no per-column allowlist to maintain.
    assert "'9784468467'" in _sql({"customer_phone": 9784468467})
    assert "'560001'" in _sql({"pincode": 560001})


def test_non_text_columns_untouched():
    # Numeric/bool columns must keep binding as numbers, or every amount filter breaks.
    sql = _sql({"total_amount": 500})
    assert "'500'" not in sql and "500" in sql


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall passed")

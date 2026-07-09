"""The order and lead classifiers must agree on which sources are Lead Gen.

A source added to one side only files a lead under Lead Gen while its order lands in
Organic -- silently inflating Lead Gen's conversion rate and moving revenue between
sections. This happened with 'telecaller': 178 leads and 127 orders in July, 6% of the
month. June had 1 lead and 0 orders, so a data check against June could never see it.

Pure / DB-free: loads the module file directly, never through app/services/__init__.py.
"""
import importlib.util
import os
import re
import sys

_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "services", "tracker_queries.py")
_spec = importlib.util.spec_from_file_location("tracker_queries", _PATH)
tq = importlib.util.module_from_spec(_spec)
sys.modules["tracker_queries"] = tq
_spec.loader.exec_module(tq)

LEADGEN = frozenset(s.strip().lower() for s in tq._LEADGEN_SOURCES.split(",") if s.strip())


def _strip_sql_comments(sql):
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _in_clauses(sql):
    """Every `IN (...)` literal list in the executable SQL, as a set of quoted literals."""
    body = _strip_sql_comments(sql)
    return [frozenset(x.strip().lower() for x in m.group(1).split(",") if x.strip())
            for m in re.finditer(r"\bIN\s*\(([^)]*)\)", body, re.I)]


def test_both_classifiers_use_the_shared_lead_gen_list_verbatim():
    for name, sql in (("order", tq._ORDER_SQL), ("lead", tq._LEAD_SQL)):
        assert LEADGEN in _in_clauses(sql), f"{name} classifier does not use _LEADGEN_SOURCES verbatim"


def test_neither_classifier_extends_the_shared_list():
    """Catches `IN ({_LEADGEN_SOURCES},'telecaller')` -- the exact original bug."""
    for name, sql in (("order", tq._ORDER_SQL), ("lead", tq._LEAD_SQL)):
        for clause in _in_clauses(sql):
            assert not clause > LEADGEN, \
                f"{name} classifier extends the shared list with {sorted(clause - LEADGEN)}"


def test_telecaller_appears_only_in_comments():
    for sql in (tq._ORDER_SQL, tq._LEAD_SQL):
        assert "telecaller" not in _strip_sql_comments(sql).lower()


def test_order_status_predicates_are_not_confused_for_source_lists():
    """`NOT IN ('DELIVERED','CANCELLED')` must not trip the superset check."""
    clauses = _in_clauses(tq._ORDER_SQL)
    assert frozenset({"'delivered'", "'cancelled'"}) in clauses

"""Pins the advanced lead-filter translator (leadFilterService).

DB-free: builds SQLAlchemy clauses from filter trees and asserts on the compiled
SQL + on validation errors. No engine/connection needed — the translator is pure.

    python tests/test_lead_filter.py     # or: pytest tests/test_lead_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.leadFilterService as F  # noqa: E402


def s(clause):
    """Structural SQL (bind params as placeholders)."""
    return str(clause)


def lit(clause):
    """SQL with literal values inlined (for value assertions)."""
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def _raises(fn):
    try:
        fn()
    except F.FilterValidationError:
        return True
    return False


def rule(field, operator, value=None):
    return {"type": "rule", "field": field, "operator": operator, "value": value}


# --- translation -------------------------------------------------------------

def test_simple_enum_rule():
    c = F.build_filter_clause(rule("stage", "eq", "New Lead"))
    assert "leads.stage" in s(c)
    assert "New Lead" in lit(c)


def test_nested_and_or():
    tree = {"type": "group", "op": "AND", "children": [
        rule("stage", "eq", "Engaged"),
        {"type": "group", "op": "OR", "children": [
            rule("city", "contains", "pune"),
            rule("state", "eq", "MH"),
        ]},
    ]}
    text = s(F.build_filter_clause(tree))
    assert " AND " in text and " OR " in text
    assert "leads.city" in text and "leads.state" in text


def test_number_between():
    assert "BETWEEN" in s(F.build_filter_clause(rule("order_count", "between", [1, 5]))).upper()


def test_text_contains_escapes_and_binds():
    c = F.build_filter_clause(rule("first_name", "contains", "a%b"))
    assert "LIKE" in s(c).upper()
    assert "a\\%b" in lit(c)   # the user-supplied % is escaped, not treated as a wildcard


def test_in_operator():
    assert "IN" in s(F.build_filter_clause(rule("stage", "in", ["New Lead", "Engaged"]))).upper()


def test_boolean_is_true():
    assert "do_not_call" in s(F.build_filter_clause(rule("do_not_call", "is_true")))


def test_owner_in():
    text = s(F.build_filter_clause(rule("owner_id", "in", ["u1", "u2"])))
    assert "owner_id" in text and "IN" in text.upper()


def test_json_path_field():
    assert "campaign_data" in s(F.build_filter_clause(rule("campaign_utm_source", "eq", "fb")))


def test_date_on_uses_date_func():
    assert "date(" in s(F.build_filter_clause(rule("created_at", "on", "2026-07-01"))).lower()


# --- linked-table (EXISTS subquery) fields -----------------------------------

def test_linked_order_status_eq_is_exists():
    # order_status persists the enum NAME, so match on the name ("DELIVERED").
    c = F.build_filter_clause(rule("order_status", "eq", "DELIVERED"))
    text = s(c)
    assert "EXISTS" in text.upper()
    assert "customer_orders" in text          # subquery hits the orders table
    assert "leads.uid" in text                # correlated back to the lead
    assert "DELIVERED" in lit(c)              # the enum NAME is bound as the value


def test_linked_enum_not_in_is_not_exists():
    # not_in => NOT EXISTS any matching linked row ("has no such order").
    text = s(F.build_filter_clause(rule("order_status", "not_in", ["CANCELLED"]))).upper()
    assert "NOT" in text and "EXISTS" in text


def test_linked_call_outcome_exists_over_activities():
    # outcome stores the CallOutcome *value*; activity restricted to call_log rows.
    c = F.build_filter_clause(rule("call_outcome", "eq", "answered"))
    text = s(c)
    assert "EXISTS" in text.upper()
    assert "lead_activities" in text
    assert "answered" in lit(c)


def test_linked_field_nested_in_group():
    tree = {"type": "group", "op": "AND", "children": [
        rule("stage", "eq", "Engaged"),
        {"type": "group", "op": "OR", "children": [
            rule("order_status", "eq", "DELIVERED"),
            rule("last_call_on", "after", "2026-06-01"),
        ]},
    ]}
    text = s(F.build_filter_clause(tree))
    assert " AND " in text and " OR " in text
    assert "EXISTS" in text.upper()
    assert "customer_orders" in text and "lead_activities" in text


def test_linked_date_between_compiles():
    c = F.build_filter_clause(rule("order_placed_on", "between", ["2026-06-01", "2026-06-30"]))
    text = s(c).upper()
    assert "EXISTS" in text and "BETWEEN" in text


def test_linked_payment_method_enum_names():
    # payment_method also persists the NAME ("UPI"), not the value ("upi").
    assert "UPI" in lit(F.build_filter_clause(rule("order_payment_method", "eq", "UPI")))


def test_linked_enum_membership_rejects_bogus():
    # enum-NAME membership validation still guards linked enum fields.
    assert _raises(lambda: F.build_filter_clause(rule("order_status", "eq", "delivered")))  # value, not NAME
    assert _raises(lambda: F.build_filter_clause(rule("order_status", "eq", "TOTALLY_BOGUS")))
    assert _raises(lambda: F.build_filter_clause(rule("call_outcome", "in", ["nope"])))


def test_is_empty_text():
    # text is_empty => NULL OR '' ; assert both branches present
    text = s(F.build_filter_clause(rule("email", "is_empty"))).upper()
    assert "IS NULL" in text and "OR" in text


def test_empty_group_is_none():
    assert F.build_filter_clause({"type": "group", "op": "AND", "children": []}) is None
    assert F.build_filter_clause(None) is None


# --- validation / safety -----------------------------------------------------

def test_unknown_field_raises():
    assert _raises(lambda: F.build_filter_clause(rule("ssn", "eq", "x")))


def test_operator_not_allowed_for_type_raises():
    # 'contains' is a text op; not valid on an enum field
    assert _raises(lambda: F.build_filter_clause(rule("stage", "contains", "New")))


def test_enum_membership_enforced():
    assert _raises(lambda: F.build_filter_clause(rule("stage", "eq", "Totally Bogus")))


def test_between_needs_pair():
    assert _raises(lambda: F.build_filter_clause(rule("order_count", "between", [1])))


def test_in_needs_nonempty_list():
    assert _raises(lambda: F.build_filter_clause(rule("stage", "in", "New Lead")))
    assert _raises(lambda: F.build_filter_clause(rule("stage", "in", [])))


def test_bad_node_type_raises():
    assert _raises(lambda: F.build_filter_clause({"type": "wat"}))
    assert _raises(lambda: F.build_filter_clause({"type": "group", "op": "XOR", "children": []}))


def test_depth_cap():
    node = rule("do_not_call", "is_true")
    for _ in range(F.MAX_DEPTH + 2):
        node = {"type": "group", "op": "AND", "children": [node]}
    assert _raises(lambda: F.build_filter_clause(node))


def test_node_cap():
    tree = {"type": "group", "op": "AND",
            "children": [rule("do_not_call", "is_true") for _ in range(F.MAX_NODES + 5)]}
    assert _raises(lambda: F.build_filter_clause(tree))


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"all {passed} lead_filter checks passed")

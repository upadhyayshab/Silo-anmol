"""Advanced lead filter engine — the LSQ-style query builder's backend.

Translates a nested AND/OR filter tree (sent by the frontend query builder) into
a single SQLAlchemy boolean clause that plugs into ``LeadManager.search_leads``.

Filter tree shape (recursive)::

    node = {"type": "group", "op": "AND"|"OR", "children": [node, ...]}
         | {"type": "rule", "field": <key>, "operator": <op>, "value": <any>}

Security: only fields/operators present in ``FIELD_CATALOG`` are accepted — the
catalog is the whitelist. Field identifiers come from the server registry (never
from the request), and all values are bound as parameters, so a tree can't inject
SQL. LIKE wildcards in user text are escaped. Node-count and depth caps bound abuse.

Phase 1 covers lead columns (incl. the denormalized ``order_count`` / ``order_value``
/ ``last_activity_at``) and ``campaign_data`` JSON paths. Phase 2 adds linked-table
conditions via ``source: "linked"`` fields, translated to correlated ``EXISTS`` /
``NOT EXISTS`` subqueries over ``customer_orders`` and ``lead_activities``.
"""
from datetime import date, datetime, timezone, timedelta

from sqlalchemy import and_, or_, func, exists, select

from managers import LeadSchema, CustomerOrderSchema, LeadActivitySchema
from utils.crm_enums import LeadStage, LeadActivityType, CallOutcome
from utils.crm_constants import LeadSource
from utils.constants import OrderStatus, PaymentMethod
from utils.timeutils import ist_date


class FilterValidationError(ValueError):
    """Raised for a malformed tree, unknown field, or bad operator/value shape.
    The router maps this to HTTP 422."""


# --- Operator sets per field type (also the catalog's per-field whitelist) ---
TEXT_OPS = ["contains", "not_contains", "eq", "neq", "starts_with", "ends_with", "is_empty", "is_not_empty"]
NUMBER_OPS = ["eq", "neq", "gt", "gte", "lt", "lte", "between", "is_empty"]
DATE_OPS = ["on", "before", "after", "between", "in_last_days", "is_empty"]
ENUM_OPS = ["eq", "neq", "in", "not_in", "is_empty"]
BOOL_OPS = ["is_true", "is_false"]
OWNER_OPS = ["in", "not_in"]

_OPS_BY_TYPE = {
    "text": TEXT_OPS, "number": NUMBER_OPS, "date": DATE_OPS, "datetime": DATE_OPS,
    "enum": ENUM_OPS, "boolean": BOOL_OPS, "owner": OWNER_OPS,
}

MAX_NODES = 200   # total rules+groups in one tree
MAX_DEPTH = 10    # nesting depth


def _f(key, label, group, ftype, **extra):
    d = {"key": key, "label": label, "group": group, "type": ftype,
         "operators": _OPS_BY_TYPE[ftype], "source": extra.pop("source", "column")}
    d.update(extra)
    return d


# The field catalog: single source of truth for what the UI offers AND what the
# translator accepts. Keep labels/groups human-facing; `source`/`col`/`json_*` are
# internal resolution hints stripped before the catalog is sent to the client.
FIELD_CATALOG = [
    _f("first_name", "First Name", "Contact", "text"),
    _f("last_name", "Last Name", "Contact", "text"),
    _f("mobile", "Mobile", "Contact", "text"),
    _f("phone", "Phone", "Contact", "text"),
    _f("email", "Email", "Contact", "text"),

    _f("lead_number", "Lead Number", "Lead", "text"),
    _f("stage", "Lead Stage", "Lead", "enum", options=[{"value": e.value, "label": e.value} for e in LeadStage]),
    _f("source", "Lead Source", "Lead", "enum", options=[{"value": e.value, "label": e.value} for e in LeadSource]),
    _f("owner_id", "Lead Owner", "Lead", "owner"),
    _f("lead_score", "Lead Score", "Lead", "number"),
    _f("created_at", "Created Date", "Lead", "datetime"),

    _f("order_count", "Order Count", "Orders", "number"),
    _f("order_value", "Order Value", "Orders", "number"),

    _f("last_activity_at", "Last Activity", "Activity", "datetime"),
    _f("follow_up_at", "Follow-up Date", "Activity", "datetime"),

    _f("city", "City", "Address", "text"),
    _f("district", "District", "Address", "text"),
    _f("taluk", "Taluk", "Address", "text"),
    _f("state", "State", "Address", "text"),
    _f("pincode", "Pincode", "Address", "text"),
    _f("country", "Country", "Address", "text"),

    _f("do_not_call", "Do Not Call", "Compliance", "boolean"),
    _f("do_not_sms", "Do Not SMS", "Compliance", "boolean"),
    _f("do_not_email", "Do Not Email", "Compliance", "boolean"),

    _f("campaign_utm_source", "UTM Source", "Campaign", "text", source="json", json_col="campaign_data", json_key="utm_source"),
    _f("campaign_utm_medium", "UTM Medium", "Campaign", "text", source="json", json_col="campaign_data", json_key="utm_medium"),
    _f("campaign_utm_campaign", "UTM Campaign", "Campaign", "text", source="json", json_col="campaign_data", json_key="utm_campaign"),
    _f("campaign_page_id", "FB Page Id", "Campaign", "text", source="json", json_col="campaign_data", json_key="page_id"),

    # --- Linked-table conditions (EXISTS over a child table) — Phase 2 ---
    # Orders: EXISTS over customer_orders (customer_orders.lead_id == leads.uid).
    # order_status / payment_method are db.Enum(...) columns WITHOUT values_callable,
    # so SQLAlchemy persists the enum *member NAME* (e.g. "DELIVERED", "UPI"). The
    # option `value` therefore MUST be e.name, not e.value — otherwise a match on the
    # stored name never fires. (crm_enums._enum_values only flips this for lead_activities.)
    _f("order_status", "Order Status", "Orders", "enum",
       source="linked", linked_schema=CustomerOrderSchema, linked_col="order_status",
       options=[{"value": e.name, "label": e.value} for e in OrderStatus]),
    _f("order_payment_method", "Order Payment Method", "Orders", "enum",
       source="linked", linked_schema=CustomerOrderSchema, linked_col="payment_method",
       options=[{"value": e.name, "label": e.value} for e in PaymentMethod]),
    _f("order_placed_on", "Order Placed On", "Orders", "date",
       source="linked", linked_schema=CustomerOrderSchema, linked_col="created_at"),

    # Activity (call logs): EXISTS over lead_activities restricted to CALL_LOG rows.
    # activity_type uses values_callable=_enum_values (persists .value "call_log"), and
    # `outcome` is a plain String(50) that stores the CallOutcome *value* ("answered",
    # ...), so call_outcome options use e.value.
    _f("call_outcome", "Call Outcome", "Activity", "enum",
       source="linked", linked_schema=LeadActivitySchema, linked_col="outcome",
       linked_where=(LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG),
       options=[{"value": e.value, "label": e.value} for e in CallOutcome]),
    _f("last_call_on", "Last Call On", "Activity", "date",
       source="linked", linked_schema=LeadActivitySchema, linked_col="created_at",
       linked_where=(LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG)),
    # direction lives in details JSON (not a plain column), so this linked field carries
    # linked_json_col/linked_json_key instead of linked_col — _linked_clause resolves it
    # the same way _resolve_col does for LEAD json fields.
    _f("call_direction", "Call Direction", "Activity", "enum",
       source="linked", linked_schema=LeadActivitySchema,
       linked_where=(LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG),
       linked_json_col="details", linked_json_key="direction",
       options=[{"value": "inbound", "label": "Inbound"}, {"value": "outbound", "label": "Outbound"}]),

    # Calls Attempted: a COUNT threshold rather than an EXISTS check — "source": "count"
    # short-circuits in _apply to a correlated scalar subquery (mirrors
    # leadService.call_counts, which tallies CALL_LOG rows per lead the same way).
    _f("calls_attempted", "Calls Attempted", "Activity", "number",
       source="count", count_schema=LeadActivitySchema,
       count_where=(LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG)),
]
_CATALOG_BY_KEY = {f["key"]: f for f in FIELD_CATALOG}


def catalog_for_api():
    """The catalog as the UI needs it — internal resolution hints stripped."""
    return [
        {"key": f["key"], "label": f["label"], "group": f["group"],
         "type": f["type"], "operators": f["operators"], "options": f.get("options")}
        for f in FIELD_CATALOG
    ]


# --- value coercion / helpers ------------------------------------------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise FilterValidationError(f"expected a number, got {v!r}")
    return int(f) if f.is_integer() else f


def _date(v):
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise FilterValidationError(f"expected a date (YYYY-MM-DD), got {v!r}")


def _pair(v):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise FilterValidationError(f"'between' needs a [from, to] pair, got {v!r}")
    return v[0], v[1]


def _strlist(v):
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def _like_escape(s):
    # Neutralise user-supplied LIKE wildcards; paired with escape="\\" below.
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_col(field):
    if field["source"] == "json":
        return getattr(LeadSchema, field["json_col"])[field["json_key"]].as_string()
    return getattr(LeadSchema, field.get("col", field["key"]))


def _date_clause(col, operator, value):
    """The date/datetime operator logic, factored out so linked-table date fields
    reuse the exact same comparisons on a child-table column. Returns None if the
    operator isn't a date operator (caller decides how to error).

    Every "date"/"datetime" catalog field maps to a timestamptz column
    (created_at, follow_up_at, last_activity_at, or a linked table's
    created_at) — shift to the IST calendar day before comparing, else the
    session's UTC TimeZone buckets a pick like "on Jul 14" onto the wrong
    day for anything after 18:30 IST."""
    dcol = ist_date(col)
    if operator == "on":
        return dcol == _date(value)
    if operator == "before":
        return dcol < _date(value)
    if operator == "after":
        return dcol > _date(value)
    if operator == "between":
        lo, hi = _pair(value)
        return dcol.between(_date(lo), _date(hi))
    if operator == "in_last_days":
        return col >= (datetime.now(timezone.utc) - timedelta(days=int(_num(value))))
    return None


def _linked_clause(field, operator, value):
    """Translate a ``source: "linked"`` rule into an EXISTS / NOT EXISTS subquery
    over a child table (customer_orders / lead_activities).

    Shape::

        [NOT] EXISTS (SELECT * FROM <child>
                      WHERE <child>.lead_id = leads.uid
                        AND <fixed extra, e.g. activity_type = 'call_log'>
                        AND <inner positive op clause on the linked column>)

    Enum semantics (documented choice): the inner clause is ALWAYS built as a
    *positive* match, and the polarity is applied at the EXISTS level —
      * eq / in      -> EXISTS a linked row matching        ("has such a row")
      * neq / not_in -> NOT EXISTS any linked row matching  ("has no such row")
      * is_empty     -> NOT EXISTS any linked row of this kind at all
    This differs from column enum fields (where neq/not_in negate the column
    comparison); over a one-to-many link, "not equal" only makes sense as the
    absence of a matching child row."""
    LinkedSchema = field["linked_schema"]
    if field.get("linked_json_col"):
        # JSON path on the child table (e.g. lead_activities.details->>'direction'),
        # resolved the same way _resolve_col does for LEAD-level json fields.
        linked_col = getattr(LinkedSchema, field["linked_json_col"])[field["linked_json_key"]].as_string()
    else:
        linked_col = getattr(LinkedSchema, field["linked_col"])
    conds = [LinkedSchema.lead_id == LeadSchema.uid]
    if field.get("linked_where") is not None:
        conds.append(field["linked_where"])

    ftype = field["type"]
    negate = False
    inner = None

    if ftype == "enum":
        if operator == "eq":
            inner = linked_col == str(value)
        elif operator == "in":
            inner = linked_col.in_(_strlist(value))
        elif operator == "neq":
            inner, negate = linked_col == str(value), True
        elif operator == "not_in":
            inner, negate = linked_col.in_(_strlist(value)), True
        elif operator == "is_empty":
            negate = True  # no linked row of this kind exists at all
        else:
            raise FilterValidationError(f"operator {operator!r} not supported for {field['key']!r}")
    elif ftype in ("date", "datetime"):
        if operator == "is_empty":
            negate = True  # no linked row of this kind exists at all
        else:
            inner = _date_clause(linked_col, operator, value)
            if inner is None:
                raise FilterValidationError(f"operator {operator!r} not supported for {field['key']!r}")
    else:
        raise FilterValidationError(f"linked field type {ftype!r} not supported for {field['key']!r}")

    if inner is not None:
        conds.append(inner)
    clause = exists().where(and_(*conds))
    return ~clause if negate else clause


def _count_clause(field, operator, value):
    """Translate a ``source: "count"`` rule into a NUMBER-operator comparison against a
    correlated ``COUNT(*)`` scalar subquery over a child table (e.g. "Calls Attempted"
    counts lead_activities rows restricted to CALL_LOG) — mirrors leadService.call_counts.
    ``is_empty`` has no NULL to check against a count, so it's treated as ``== 0``."""
    count_schema = field["count_schema"]
    conds = [count_schema.lead_id == LeadSchema.uid]
    if field.get("count_where") is not None:
        conds.append(field["count_where"])
    sub = select(func.count()).select_from(count_schema).where(and_(*conds)).scalar_subquery()

    if operator == "is_empty":
        return sub == 0
    if operator == "between":
        lo, hi = _pair(value)
        return sub.between(_num(lo), _num(hi))
    n = _num(value)
    return {"eq": sub == n, "neq": sub != n, "gt": sub > n,
            "gte": sub >= n, "lt": sub < n, "lte": sub <= n}[operator]


def _apply(field, operator, value):
    ftype = field["type"]

    # Linked (EXISTS-subquery) fields short-circuit before column resolution: their
    # comparison lives inside a correlated subquery, not on a leads column.
    if field.get("source") == "linked":
        return _linked_clause(field, operator, value)

    # Count (scalar-subquery) fields likewise short-circuit: there's no leads column to
    # resolve, `col` is a correlated COUNT(*) over the child table.
    if field.get("source") == "count":
        return _count_clause(field, operator, value)

    col = _resolve_col(field)

    # value-less operators
    if operator == "is_empty":
        return or_(col.is_(None), col == "") if ftype == "text" else col.is_(None)
    if operator == "is_not_empty":
        return and_(col.isnot(None), col != "") if ftype == "text" else col.isnot(None)
    if operator == "is_true":
        return col.is_(True)
    if operator == "is_false":
        return col.is_(False)

    if ftype == "text":
        if operator == "contains":
            return col.ilike(f"%{_like_escape(value)}%", escape="\\")
        if operator == "not_contains":
            return ~col.ilike(f"%{_like_escape(value)}%", escape="\\")
        if operator == "starts_with":
            return col.ilike(f"{_like_escape(value)}%", escape="\\")
        if operator == "ends_with":
            return col.ilike(f"%{_like_escape(value)}", escape="\\")
        if operator == "eq":
            return col == str(value)
        if operator == "neq":
            return col != str(value)

    elif ftype == "number":
        if operator == "between":
            lo, hi = _pair(value)
            return col.between(_num(lo), _num(hi))
        n = _num(value)
        return {"eq": col == n, "neq": col != n, "gt": col > n,
                "gte": col >= n, "lt": col < n, "lte": col <= n}[operator]

    elif ftype in ("date", "datetime"):
        clause = _date_clause(col, operator, value)
        if clause is not None:
            return clause

    elif ftype == "enum":
        if operator == "eq":
            return col == str(value)
        if operator == "neq":
            return col != str(value)
        if operator == "in":
            return col.in_(_strlist(value))
        if operator == "not_in":
            return ~col.in_(_strlist(value))

    elif ftype == "owner":
        if operator == "in":
            return col.in_(_strlist(value))
        if operator == "not_in":
            return ~col.in_(_strlist(value))

    raise FilterValidationError(f"operator {operator!r} not supported for {field['key']!r}")


def _validate_value(field, operator, value):
    if operator in ("is_empty", "is_not_empty", "is_true", "is_false"):
        return
    if value is None:
        raise FilterValidationError(f"{field['key']}.{operator} needs a value")
    if operator == "between":
        _pair(value)
    if operator in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            raise FilterValidationError(f"{field['key']}.{operator} needs a non-empty list")
    if field["type"] == "enum" and operator in ("eq", "neq", "in", "not_in"):
        allowed = {o["value"] for o in field.get("options", [])}
        bad = [v for v in _strlist(value) if v not in allowed]
        if bad:
            raise FilterValidationError(f"invalid value(s) for {field['key']}: {bad}")


def _build(node, depth, counter):
    if depth > MAX_DEPTH:
        raise FilterValidationError("filter nesting too deep")
    counter["n"] += 1
    if counter["n"] > MAX_NODES:
        raise FilterValidationError("filter has too many conditions")
    if not isinstance(node, dict):
        raise FilterValidationError("invalid filter node")

    ntype = node.get("type")
    if ntype == "group":
        op = (node.get("op") or "AND").upper()
        if op not in ("AND", "OR"):
            raise FilterValidationError(f"invalid group op {op!r}")
        clauses = [c for c in (_build(ch, depth + 1, counter) for ch in (node.get("children") or [])) if c is not None]
        if not clauses:
            return None
        return and_(*clauses) if op == "AND" else or_(*clauses)

    if ntype == "rule":
        field = _CATALOG_BY_KEY.get(node.get("field"))
        if not field:
            raise FilterValidationError(f"unknown field {node.get('field')!r}")
        operator = node.get("operator")
        if operator not in field["operators"]:
            raise FilterValidationError(f"operator {operator!r} not allowed for {field['key']!r}")
        value = node.get("value")
        _validate_value(field, operator, value)
        return _apply(field, operator, value)

    raise FilterValidationError(f"invalid node type {ntype!r}")


def build_filter_clause(tree):
    """Translate a filter tree into one SQLAlchemy clause (or None for an empty
    tree). Raises FilterValidationError on anything malformed."""
    if not tree:
        return None
    return _build(tree, 0, {"n": 0})

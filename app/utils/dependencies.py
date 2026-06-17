from fastapi import Request, HTTPException
from typing import Any
from config import get_settings
from datetime import datetime, date

settings = get_settings()


def master_key_dependency(request: "Request"):
    api_key = request.headers.get("X-API-Key", "")
    if api_key != settings.master_api_key:
        raise HTTPException(status_code=403, detail="Forbidden")

def _coerce_scalar(value: str):
    """coerce a string query value to int/float/bool if appropriate, otherwise return the original string """
    v = value
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
        try:
            return int(v)
        except Exception:
            pass
    try:
        if "." in v:
            return float(v)
    except Exception:
        pass

    datetime_formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",       # 2024-12-19T10:37:56.340248Z
        "%Y-%m-%dT%H:%M:%SZ",          # 2024-12-19T10:37:56Z
        "%Y-%m-%dT%H:%M:%S",           # 2024-12-19T10:37:56
        "%Y-%m-%d %H:%M:%S",           # 2024-12-19 10:37:56
        "%Y-%m-%d",                    # 2024-12-19
    ]
    for fmt in datetime_formats:
        try:
            parsed = datetime.strptime(v, fmt)
            # A date-only value (no time component) is returned as a `date` so the
            # managers match it against the whole day (via func.date) rather than an
            # exact-midnight timestamp. This makes range filters inclusive of the
            # end day instead of silently excluding it.
            return parsed.date() if fmt == "%Y-%m-%d" else parsed
        except (ValueError, TypeError):
            continue
    return value

def _set_range(filters: dict[str, Any], field: str, op: str, value: Any) -> None:
    """Merge a range operator into a field's condition.

    Without this, a request carrying both ``field:gte`` and ``field:lte`` would
    have the second bound overwrite the first (both target ``filters[field]``),
    silently dropping one side of the range. Merging keeps both bounds so the
    filter behaves as a closed interval.
    """
    existing = filters.get(field)
    if isinstance(existing, dict):
        existing[op] = value
    else:
        filters[field] = {op: value}


def sorting_dependency(request: "Request"):
    sorts = []
    for key, value in request.query_params.items():
        if key.endswith(":asc"):
            sorts.append(f"+{key.removesuffix(':asc')}")
        elif key.endswith(":desc"):
            sorts.append(f"-{key.removesuffix(':desc')}")
    return sorts

def filtering_dependency(request: Request):
    """
    Supported Operators:
    -------------------
    Case-Sensitive :
        :eq       - Equals
        :neq      - Not equals
        :lt       - Less than
        :lte      - Less than or equal
        :gt       - Greater than
        :gte      - Greater than or equal
        :in       - In list (comma-separated)
        :nin      - Not in list (comma-separated)
        :between  - Between two values (comma-separated)
        :like     - SQL LIKE pattern (case-sensitive)
    Case-Insensitive:
        :ieq      - Case-insensitive equals
        :ilike    - Case-insensitive LIKE pattern
        :iin      - Case-insensitive IN list (comma-separated)
    -------------------
    """
    filters: dict[str, Any] = {}
    for key, value in request.query_params.items():
        if key.endswith(":eq"):
            filters[key.removesuffix(":eq")] = _coerce_scalar(value)
        elif key.endswith(":neq"):
            filters[key.removesuffix(":neq")] = {"$neq": _coerce_scalar(value)}
        elif key.endswith(":lt"):
            _set_range(filters, key.removesuffix(":lt"), "$lt", _coerce_scalar(value))
        elif key.endswith(":lte"):
            _set_range(filters, key.removesuffix(":lte"), "$lte", _coerce_scalar(value))
        elif key.endswith(":gt"):
            _set_range(filters, key.removesuffix(":gt"), "$gt", _coerce_scalar(value))
        elif key.endswith(":gte"):
            _set_range(filters, key.removesuffix(":gte"), "$gte", _coerce_scalar(value))
        elif key.endswith(":in"):
            field = key.removesuffix(":in")
            values = [v.strip() for v in value.split(",") if v.strip()]
            filters[field] = {"$in": [_coerce_scalar(v) for v in values]}
        elif key.endswith(":nin"):
            field = key.removesuffix(":nin")
            values = [v.strip() for v in value.split(",") if v.strip()]
            filters[field] = {"$nin": [_coerce_scalar(v) for v in values]}
        elif key.endswith(":between"):
            field = key.removesuffix(":between")
            values = [v.strip() for v in value.split(",") if v.strip()]
            filters[field] = {"$between": [_coerce_scalar(v) for v in values]}
        elif key.endswith(":like"):
            filters[key.removesuffix(":like")] = {"$like": f"%{str(value)}%"}
        elif key.endswith(":ieq"):
            field = key.removesuffix(":ieq")
            coerced = _coerce_scalar(value)
            if isinstance(coerced, str):
                filters[field] = {"$ieq": coerced}
            else:
                filters[field] = coerced
        elif key.endswith(":iin"):
            field = key.removesuffix(":iin")
            values = [v.strip() for v in value.split(",") if v.strip() != ""]
            coerced = [_coerce_scalar(v) for v in values]
            if coerced and all(isinstance(v, str) for v in coerced):
                filters[field] = {"$in_ci": coerced}
            else:
                filters[field] = {"$in": coerced}
        elif key.endswith(":ilike"):
            field = key.removesuffix(":ilike")
            filters[field] = {"$ilike": f"%{str(value)}%"}
    return filters
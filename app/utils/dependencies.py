from fastapi import Request, HTTPException
from typing import Any  
from config import get_settings
from datetime import datetime

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
            return datetime.strptime(v, fmt)
        except (ValueError, TypeError):
            continue
    return value

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
            filters[key.removesuffix(":eq")] = value
        elif key.endswith(":neq"):
            filters[key.removesuffix(":neq")] = {"$neq": value}
        elif key.endswith(":lt"):
            field = key.removesuffix(":lt")
            filters[field] = {"$lt": _coerce_scalar(value)}
        elif key.endswith(":lte"):
            field = key.removesuffix(":lte")
            filters[field] = {"$lte": _coerce_scalar(value)}
        elif key.endswith(":gt"):
            field = key.removesuffix(":gt")
            filters[field] = {"$gt": _coerce_scalar(value)}
        elif key.endswith(":gte"):
            field = key.removesuffix(":gte")
            filters[field] = {"$gte": _coerce_scalar(value)}
        elif key.endswith(":in"):
            filters[key.removesuffix(":in")] = {"$in": value.split(",")}
        elif key.endswith(":nin"):
            filters[key.removesuffix(":nin")] = {"$nin": value.split(",")}
        elif key.endswith(":between"):
            field = key.removesuffix(":between")
            values = [v.strip() for v in value.split(",") if v.strip()]
            filters[field] = {"$between": [_coerce_scalar(v) for v in values]}
        elif key.endswith(":like"):
            filters[key.removesuffix(":like")] = {"$like": f"%{value}%"}
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
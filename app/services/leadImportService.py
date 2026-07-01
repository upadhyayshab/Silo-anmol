"""CSV bulk lead import (checkpoint 2.4).

Parses a LeadSquared lead export (or a native-format CSV) with the stdlib
``csv`` module — no pandas — maps headers tolerantly to our lead fields, and
runs each row through the shared create-or-merge path (``leadService.create_lead``)
so duplicates are merged, not re-inserted. Returns a per-file summary with
row-level errors.

Header mapping is grounded in a real LSQ lead export. Notably the contact
number lives in **"Phone Number"** (LSQ's "Mobile Number" column is usually
empty), so both map to our ``mobile``. Attribution columns (LeadGen/Ad/Campaign
IDs, UTM-ish fields) are collected into ``campaign_data``; the rich
animal-husbandry questionnaire columns and any other populated columns are
preserved verbatim in ``custom_fields`` so nothing is lost. LSQ opt-out flags
(Do Not Call/SMS/Email) are honoured.

Imported leads are system-owned so they round-robin across telecallers; they
are NOT all attributed to the admin who ran the import.
"""
import csv
import io
import logging
from typing import Dict, Any, List, Optional

from models import LeadCreateRequest
from utils.crm_constants import LeadSource
from services import leadService

logger = logging.getLogger(__name__)

# Cap to protect the worker from a runaway upload; surfaced in the summary.
MAX_ROWS = 50_000

# Lowercased/trimmed source header -> our LeadCreateRequest scalar field. Covers
# both LSQ export headers and our native names. Unmapped, non-attribution
# columns are preserved into ``custom_fields``.
_HEADER_ALIASES = {
    # names
    "first name": "first_name", "firstname": "first_name", "first_name": "first_name",
    "last name": "last_name", "lastname": "last_name", "last_name": "last_name",
    # phone — LSQ puts the real number in "Phone Number"; "Mobile Number" is
    # usually empty. Both map to mobile (first non-empty in column order wins).
    "phone number": "mobile", "phone": "mobile", "mobile": "mobile",
    "mobile number": "mobile", "mobile_number": "mobile", "mobilenumber": "mobile",
    "primary phone": "mobile",
    "secondary phone": "phone", "alternate phone": "phone", "landline": "phone",
    # email
    "email": "email", "email address": "email", "emailaddress": "email",
    # address
    "address": "address_line", "address 1": "address_line", "address1": "address_line",
    "address line 1": "address_line", "mx_street1": "address_line", "street1": "address_line",
    "address 2": "address_line_2", "address2": "address_line_2",
    "address line 2": "address_line_2", "mx_street2": "address_line_2", "street2": "address_line_2",
    "city": "city", "mx_city": "city",
    "district": "district", "mx_district": "district",
    "state": "state", "mx_state": "state",
    "pincode": "pincode", "pin code": "pincode", "zip": "pincode", "zip code": "pincode",
    "postal code": "pincode", "postcode": "pincode", "mx_zip": "pincode", "mx_postalcode": "pincode",
    "country": "country", "mx_country": "country",
    # source / scoring
    "lead source": "source", "source": "source", "leadsource": "source",
    "lead score": "lead_score", "lead_score": "lead_score",
    # compliance (LSQ opt-outs)
    "do not call": "do_not_call", "do not sms": "do_not_sms", "do not email": "do_not_email",
    # free text
    "notes": "notes", "note": "notes", "comments": "notes",
}

# Lowercased/trimmed header -> key inside the ``campaign_data`` JSON blob.
_CAMPAIGN_ALIASES = {
    "leadgen id": "leadgen_id",
    "ad id": "ad_id",
    "adset id": "adset_id",
    "campaign id": "campaign_id",
    "source campaign": "source_campaign",
    "source medium": "source_medium",
    "source content": "source_content",
    "conversion referrer url": "conversion_referrer_url",
    "source referrer url": "source_referrer_url",
    "source ip address": "source_ip",
    "latitude": "latitude",
    "longitude": "longitude",
}

_TRUE = {"yes", "y", "true", "t", "1", "on"}
_FALSE = {"no", "n", "false", "f", "0", "off"}


def _coerce_source(value: Optional[str]) -> Optional[LeadSource]:
    """Map free-text to a LeadSource enum (case-insensitive), else None."""
    if not value:
        return None
    v = value.strip().lower()
    for member in LeadSource:
        if member.value.lower() == v:
            return member
    return None


def _coerce_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _coerce_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _build_payload(data: Dict[str, str], campaign: Dict[str, Any],
                   custom: Dict[str, Any]) -> LeadCreateRequest:
    source_raw = data.get("source")
    source_enum = _coerce_source(source_raw)
    if source_raw and source_enum is None:
        custom["source_raw"] = source_raw  # keep the original label, don't 422

    first = data.get("first_name") or data.get("last_name")
    last = data.get("last_name") if data.get("first_name") else None
    return LeadCreateRequest(
        first_name=first,
        last_name=last,
        mobile=data["mobile"],
        phone=data.get("phone"),
        email=data.get("email"),
        address_line=data.get("address_line"),
        address_line_2=data.get("address_line_2"),
        city=data.get("city"),
        district=data.get("district"),
        state=data.get("state"),
        pincode=data.get("pincode"),
        country=data.get("country"),
        source=source_enum,
        lead_score=_coerce_int(data.get("lead_score")),
        do_not_call=_coerce_bool(data.get("do_not_call")),
        do_not_sms=_coerce_bool(data.get("do_not_sms")),
        do_not_email=_coerce_bool(data.get("do_not_email")),
        notes=data.get("notes"),
        custom_fields=custom or None,
        campaign_data=campaign or None,
    )


async def import_leads_csv(engine, file_bytes: bytes, by_user_id: str) -> Dict[str, Any]:
    """Import leads from CSV bytes. Returns a summary dict (see ``LeadImportSummary``)."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return {"total_rows": 0, "created": 0, "merged": 0, "skipped": 0,
                "errors": [{"row": 0, "reason": "empty file or missing header row"}]}

    # Resolve each source header once into a scalar field or a campaign_data key.
    field_for: Dict[str, str] = {}
    campaign_for: Dict[str, str] = {}
    for header in reader.fieldnames:
        key = (header or "").strip().lower()
        if key in _HEADER_ALIASES:
            field_for[header] = _HEADER_ALIASES[key]
        elif key in _CAMPAIGN_ALIASES:
            campaign_for[header] = _CAMPAIGN_ALIASES[key]

    created = merged = skipped = total = 0
    errors: List[Dict[str, Any]] = []

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        if total >= MAX_ROWS:
            errors.append({"row": i, "reason": f"row cap {MAX_ROWS} reached; remaining rows skipped"})
            break
        total += 1
        try:
            data: Dict[str, str] = {}
            campaign: Dict[str, Any] = {}
            custom: Dict[str, Any] = {}
            for header, raw in row.items():
                value = (raw or "").strip()
                if not value:
                    continue
                if header in field_for:
                    target = field_for[header]
                    if target not in data:
                        data[target] = value
                    else:
                        custom[header] = value  # duplicate-mapped column -> keep raw
                elif header in campaign_for:
                    campaign[campaign_for[header]] = value
                else:
                    custom[header] = value

            if not (data.get("first_name") or data.get("last_name")) or not data.get("mobile"):
                skipped += 1
                errors.append({"row": i, "reason": "missing required name or mobile"})
                continue

            payload = _build_payload(data, campaign, custom)
            # System-owned -> round-robin assigned (not the importing admin).
            # Bulk CSV import must NOT trigger re-engagement (a backfill of historic
            # leads would spuriously reopen/reassign/notify across the whole team).
            _, was_created = await leadService.create_lead(
                engine, payload, by_user_id="system", source_label="CSV import",
                reengage_on_merge=False,
            )
            if was_created:
                created += 1
            else:
                merged += 1
        except Exception as e:
            skipped += 1
            errors.append({"row": i, "reason": str(e)[:200]})

    logger.info(f"[import] {total} rows: {created} created, {merged} merged, "
                f"{skipped} skipped (by {by_user_id})")
    return {"total_rows": total, "created": created, "merged": merged,
            "skipped": skipped, "errors": errors[:500]}

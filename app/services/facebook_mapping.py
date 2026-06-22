"""Configurable Meta-field -> lead-field mapping (LSQ-style "Default Mapping").

Replaces the old hardcoded ``_FIELD_ALIASES`` + ``campaign_data`` block in
``facebook_leads.py`` with a DB-backed mapping:

  - a global **default** mapping (``fb_field_mappings`` scope="default"), seeded
    on first use to match today's behavior, and
  - optional **per-form** overrides (scope="form").

Two field kinds: ``marketing`` (attribution fields from the leadgen object —
ad/adset/campaign ids+names, platform, is_organic, ...) and ``question`` (the
form's question fields). Marketing fields land in ``campaign_data`` by default;
question fields in lead columns or ``custom_fields``. Unmapped fields are still
preserved (questions -> custom_fields, marketing -> campaign_data) so no data is
ever lost; ``target=None`` is an explicit "ignore".

The pure ``apply_resolved`` has no DB dependency and is unit-tested.
"""
import logging
import time
from typing import Dict, Any, List, Optional, Tuple

from models import LeadCreateRequest
from utils.crm_constants import LeadSource
from utils.dedup_utils import normalize_mobile

logger = logging.getLogger(__name__)

# Lead columns a mapping may write to (LeadCreateRequest scalar fields).
LEAD_COLUMNS = {
    "first_name", "last_name", "mobile", "phone", "email",
    "address_line", "address_line_2", "city", "district", "state",
    "pincode", "country", "lead_score", "notes",
    "do_not_call", "do_not_sms", "do_not_email",
}

# Default form-question mapping (mirrors the legacy _FIELD_ALIASES; target_kind=lead).
DEFAULT_QUESTION_MAP: List[Tuple[str, str]] = [
    ("phone_number", "mobile"), ("phone", "mobile"), ("email", "email"),
    ("first_name", "first_name"), ("last_name", "last_name"),
    ("city", "city"), ("state", "state"), ("province", "state"),
    ("street_address", "address_line"),
    ("post_code", "pincode"), ("zip_code", "pincode"), ("zip", "pincode"),
]

# Default marketing/attribution mapping (target_kind=campaign_data). Matches the
# LSQ screenshot, incl. campaign name -> source_campaign.
DEFAULT_MARKETING_MAP: List[Tuple[str, str]] = [
    ("leadgen_id", "leadgen_id"), ("created_time", "created_time"),
    ("ad_id", "ad_id"), ("ad_name", "ad_name"),
    ("adset_id", "adset_id"), ("adset_name", "adset_name"),
    ("campaign_id", "campaign_id"), ("campaign_name", "source_campaign"),
    ("form_id", "form_id"), ("form_name", "form_name"),
    ("platform", "platform"), ("is_organic", "is_organic"),
    ("partner_name", "partner_name"),
]

# Human labels for the UI (meta_field key -> display label).
LABELS: Dict[str, str] = {
    "leadgen_id": "Id (LeadGen ID)", "created_time": "Created Time",
    "ad_id": "Ad Id", "ad_name": "Ad Name",
    "adset_id": "Adset Id", "adset_name": "Adset name",
    "campaign_id": "Campaign Id", "campaign_name": "Campaign name",
    "form_id": "Form Id", "form_name": "Form Name",
    "platform": "Platform", "is_organic": "Is Organic", "partner_name": "Partner name",
}

# The marketing fields we read off the leadgen object (drives the Graph fetch too).
MARKETING_KEYS = [m for m, _ in DEFAULT_MARKETING_MAP]

# Single source of truth for the Graph fields requested per lead (fetch + backfill).
# (leadgen_id -> "id", form_name comes from our DB, so they're not requested here.)
GRAPH_LEAD_FIELDS = (
    "id,created_time,form_id,ad_id,ad_name,adset_id,adset_name,"
    "campaign_id,campaign_name,platform,is_organic,partner_name,field_data"
)

# Short-TTL caches (invalidated on edits via clear_cache()).
_TTL = 60.0
_cache: Dict[str, Tuple[dict, float]] = {}        # form_id -> resolved mapping
_form_cache: Dict[str, Tuple[Any, float]] = {}    # form_id -> form row (or None)


def clear_cache() -> None:
    _cache.clear()
    _form_cache.clear()


# --------------------------------------------------------------------------
# Pure application (no DB) — unit-tested
# --------------------------------------------------------------------------

def apply_resolved(resolved: Dict[Tuple[str, str], dict],
                   marketing: Dict[str, Any],
                   questions: Dict[str, Any]) -> Tuple[dict, dict, dict]:
    """Split incoming values into (lead-columns, campaign_data, custom_fields).

    ``resolved`` maps ``(field_kind, meta_field) -> {target, target_kind, is_active}``.
    Unmapped marketing -> campaign_data by its own name; unmapped/ignored questions
    -> custom_fields (never drop a customer's answer). Explicitly-ignored marketing
    is dropped (attribution noise).
    """
    data: dict = {}
    campaign_data: dict = {}
    custom_fields: dict = {}

    def _write(row: dict, value: Any) -> None:
        tk, tgt = row.get("target_kind"), row.get("target")
        if tk == "lead":
            # A lead target that isn't a real column (typo/unknown) is preserved
            # in custom_fields rather than silently misdirected to campaign_data.
            (data if tgt in LEAD_COLUMNS else custom_fields)[tgt] = value
        elif tk == "custom":
            custom_fields[tgt] = value
        else:  # campaign_data
            campaign_data[tgt] = value

    for key, value in marketing.items():
        if value in (None, ""):
            continue
        row = resolved.get(("marketing", key))
        if row is None:
            campaign_data[key] = value                # preserve unmapped attribution
        elif row.get("is_active") and row.get("target"):
            _write(row, value)
        # else: explicitly ignored -> drop

    for name, value in questions.items():
        if value in (None, ""):
            continue
        row = resolved.get(("question", name))
        if row is None:
            custom_fields[name] = value               # preserve (today's behavior)
        elif row.get("is_active") and row.get("target"):
            _write(row, value)
        else:
            custom_fields[name] = value               # ignored target, keep the answer

    return data, campaign_data, custom_fields


# --------------------------------------------------------------------------
# DB-backed resolution + seeding
# --------------------------------------------------------------------------

async def seed_default_mapping(engine) -> int:
    """Insert the default mapping rows if none exist yet. Idempotent."""
    from managers import FbFieldMappingManager, FbFieldMappingSchema
    mgr = FbFieldMappingManager(engine)
    existing = await mgr.fetch_all(filters={"scope": "default"}, limit=1)
    if existing.items:
        return 0
    rows = (
        [("question", m, t, "lead") for m, t in DEFAULT_QUESTION_MAP]
        + [("marketing", m, t, "campaign_data") for m, t in DEFAULT_MARKETING_MAP]
    )
    n = 0
    for kind, meta, target, tkind in rows:
        try:
            await mgr.create(FbFieldMappingSchema(
                scope="default", form_id=None, field_kind=kind,
                meta_field=meta, target=target, target_kind=tkind, is_active=True))
            n += 1
        except Exception:
            pass  # unique constraint — another worker seeded concurrently
    clear_cache()
    logger.info(f"[fb-map] seeded {n} default mapping rows")
    return n


async def resolve(engine, form_id: Optional[str] = None) -> Dict[Tuple[str, str], dict]:
    """Resolved mapping for a form: global defaults overlaid by per-form rows."""
    from managers import FbFieldMappingManager
    key = form_id or "__default__"
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[1]) < _TTL:
        return hit[0]

    mgr = FbFieldMappingManager(engine)
    defaults = await mgr.fetch_all(filters={"scope": "default"}, limit=500)
    if not defaults.items:
        await seed_default_mapping(engine)
        defaults = await mgr.fetch_all(filters={"scope": "default"}, limit=500)

    out: Dict[Tuple[str, str], dict] = {}
    for r in defaults.items:
        out[(r.field_kind, r.meta_field)] = {
            "target": r.target, "target_kind": r.target_kind, "is_active": r.is_active}
    if form_id:
        forms = await mgr.fetch_all(filters={"scope": "form", "form_id": form_id}, limit=500)
        for r in forms.items:
            out[(r.field_kind, r.meta_field)] = {
                "target": r.target, "target_kind": r.target_kind, "is_active": r.is_active}

    _cache[key] = (out, time.monotonic())
    return out


async def _form_row(engine, form_id: Optional[str]):
    """The form's DB row, briefly cached (so a backfill loop doesn't re-query per lead)."""
    if not form_id:
        return None
    hit = _form_cache.get(form_id)
    if hit and (time.monotonic() - hit[1]) < _TTL:
        return hit[0]
    from managers import FbLeadgenFormManager
    rows = await FbLeadgenFormManager(engine).fetch_all(filters={"form_id": form_id})
    row = rows.items[0] if rows.items else None
    _form_cache[form_id] = (row, time.monotonic())
    return row


async def form_status(engine, form_id: Optional[str]) -> Optional[str]:
    """A form's ingestion status ('active'/'inactive'), or None if not synced yet."""
    row = await _form_row(engine, form_id)
    return row.status if row else None


# --------------------------------------------------------------------------
# Build a LeadCreateRequest from a Graph leadgen object
# --------------------------------------------------------------------------

def _extract(lead_json: Dict[str, Any], form_name: Optional[str]) -> Tuple[dict, dict]:
    """Pull the marketing dict + the question dict out of a leadgen object."""
    marketing = {k: lead_json.get(k) for k in MARKETING_KEYS}
    marketing["leadgen_id"] = lead_json.get("id")
    marketing["form_id"] = lead_json.get("form_id")
    marketing["form_name"] = form_name

    questions: dict = {}
    for entry in lead_json.get("field_data", []):
        name = entry.get("name")
        values = entry.get("values") or []
        if name:
            questions[name] = values[0] if values else None

    # Split a single full_name into first/last so the question mapping can place them.
    if questions.get("full_name") and not questions.get("first_name"):
        parts = str(questions["full_name"]).strip().split(" ", 1)
        questions["first_name"] = parts[0]
        questions.setdefault("last_name", parts[1] if len(parts) > 1 else None)

    return marketing, questions


async def build_lead_request(engine, lead_json: Dict[str, Any]) -> LeadCreateRequest:
    """Map a Graph leadgen object onto a LeadCreateRequest via the configured mapping."""
    form_id = lead_json.get("form_id")
    resolved = await resolve(engine, form_id)
    row = await _form_row(engine, form_id)
    form_name = row.form_name if row else None

    marketing, questions = _extract(lead_json, form_name)
    data, campaign_data, custom_fields = apply_resolved(resolved, marketing, questions)

    data.setdefault("first_name", "Facebook Lead")
    data["mobile"] = normalize_mobile(data.get("mobile")) or ""
    data["source"] = LeadSource.FB_LEAD_ADS
    data["campaign_data"] = campaign_data or None
    data["custom_fields"] = custom_fields or None

    allowed = set(LeadCreateRequest.model_fields)
    return LeadCreateRequest(**{k: v for k, v in data.items() if k in allowed})

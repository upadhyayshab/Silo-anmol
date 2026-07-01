"""Lead deduplication engine (Feature 1 — checkpoint 2.3).

Shared by every lead-create path — the Facebook webhook, CSV import, and the
manual ``POST /leads`` endpoint — so a person who already exists in the CRM is
*merged* into their existing record instead of spawning a duplicate.

Soft deletes are respected: a lead that has been soft-deleted (``deleted_at``
set) is NEVER a dedup match. If the only existing record for a phone/email was
deleted, the incoming lead is treated as brand new — we do not resurrect or
merge into an intentionally-removed lead.

The pure helpers (``normalize_mobile``, ``normalize_email``,
``mobile_candidates``, ``compute_backfill``) have no DB/app dependencies so they
are unit-testable in isolation; the manager/schema imports are done lazily
inside the async DB helpers.
"""
import logging
import re
from typing import Optional, Dict, Any, List

import sqlalchemy as db

logger = logging.getLogger(__name__)


# Contact/profile fields we attempt to backfill onto an existing lead when a
# duplicate is merged. Identity/workflow fields (owner, stage, lead_number,
# follow_up, notes) are intentionally excluded — merging must never disturb
# ownership, pipeline position, or a telecaller's scratch note.
_MERGEABLE_FIELDS = (
    "first_name", "last_name", "phone", "email",
    "address_line", "address_line_2", "city", "district", "state",
    "pincode", "country", "source", "lead_score",
)


# --------------------------------------------------------------------------
# Pure normalization helpers (no DB)
# --------------------------------------------------------------------------

def normalize_mobile(value: Optional[str]) -> Optional[str]:
    """Strip punctuation and the India country/trunk prefix to a bare number.

    ``"+91 98765-43210"``, ``"098765 43210"`` and ``"9876543210"`` all
    normalize to ``"9876543210"``. Returns ``None`` for empty/garbage input.
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits or None


def normalize_email(value: Optional[str]) -> Optional[str]:
    """Trim + lowercase an email; empty becomes ``None``."""
    if not value:
        return None
    cleaned = str(value).strip().lower()
    return cleaned or None


def mobile_candidates(value: Optional[str]) -> List[str]:
    """Canonical **digit-only** forms a number might take once punctuation is stripped.

    ``find_duplicate`` strips non-digits from the stored ``mobile``/``phone`` at
    the DB layer (``regexp_replace(col, '\\D', '', 'g')``) and compares against
    these forms, so a stored value with internal spaces/dashes — e.g. a
    LeadSquared export ``"+91 98765-43210"`` or a manually-typed ``"098765 43210"``
    — still matches. We cover the bare national number plus the ``91`` country
    and ``0`` trunk prefixes. Heads off the known "+91 vs bare number" mismatch.
    """
    norm = normalize_mobile(value)
    if not norm:
        return []
    # Digit-only variants (no '+', since the stored value is digit-stripped too).
    forms = [norm, f"91{norm}", f"0{norm}"]
    seen, out = set(), []
    for f in forms:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def compute_backfill(existing_values: Dict[str, Any],
                     incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Decide which fields to backfill onto an existing lead from incoming data.

    Pure function (no DB) so it is directly unit-testable. Only *empty* fields
    are filled — a populated value is never overwritten. ``custom_fields`` is
    shallow-merged (incoming fills gaps only).

    ``campaign_data`` is handled differently: it is NOT gap-filled. The flat
    top-level keys stay first-touch (never overwritten, never extended); instead
    each subsequent non-empty incoming ``campaign_data`` dict is appended as a
    touch onto a ``touches`` list, so re-submissions from later ads/forms are
    recorded without disturbing first-touch attribution. A retry carrying the
    same ``leadgen_id`` as an existing touch is skipped (idempotent).
    """
    updates: Dict[str, Any] = {}

    for field in _MERGEABLE_FIELDS:
        new_val = incoming.get(field)
        if new_val is None:
            continue
        cur = existing_values.get(field)
        cur_cmp = cur.value if hasattr(cur, "value") else cur
        if cur_cmp in (None, ""):
            updates[field] = new_val

    blob = incoming.get("custom_fields")
    if isinstance(blob, dict) and blob:
        cur = existing_values.get("custom_fields") or {}
        merged = dict(cur)
        for k, v in blob.items():
            merged.setdefault(k, v)
        if merged != cur:
            updates["custom_fields"] = merged

    # ponytail: touches[] holds 2nd+ ad/web touches; flat keys stay first-touch. Promote to a lead_touches table if attribution needs SQL joins.
    incoming_campaign = incoming.get("campaign_data")
    if isinstance(incoming_campaign, dict) and incoming_campaign:
        cur = dict(existing_values.get("campaign_data") or {})
        touches = list(cur.get("touches", []))
        incoming_leadgen = incoming_campaign.get("leadgen_id")
        already = incoming_leadgen is not None and any(
            isinstance(t, dict) and t.get("leadgen_id") == incoming_leadgen
            for t in touches
        )
        if not already:
            touches.append(incoming_campaign)
            cur["touches"] = touches
            updates["campaign_data"] = cur

    return updates


def _jsonable(value):
    """Coerce a value into something safe to store in the JSON details column."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):          # enum -> its value
        return value.value
    return str(value)


# --------------------------------------------------------------------------
# DB helpers (managers imported lazily so the pure helpers stay importable)
# --------------------------------------------------------------------------

async def find_duplicate(engine, *, mobile: Optional[str] = None,
                         email: Optional[str] = None,
                         exclude_lead_id: Optional[str] = None):
    """Return the oldest **non-deleted** lead matching this mobile/phone/email.

    Matches ``mobile``/``phone`` against every candidate form, OR a
    case-insensitive ``email`` match. Returns ``None`` when nothing matches.
    """
    from managers import LeadManager, LeadSchema

    cands = mobile_candidates(mobile)
    norm_email = normalize_email(email)
    if not cands and not norm_email:
        return None

    match_conds = []
    if cands:
        # Strip non-digits from the stored values at the DB layer so formatting
        # differences ("+91 98765-43210" vs "9876543210") never defeat the match.
        # NB: this is a functional expression, so the plain mobile/phone indexes
        # don't apply — fine at CRM scale; add a functional index if volume grows.
        digits_mobile = db.func.regexp_replace(LeadSchema.mobile, r"\D", "", "g")
        digits_phone = db.func.regexp_replace(LeadSchema.phone, r"\D", "", "g")
        match_conds.append(digits_mobile.in_(cands))
        match_conds.append(digits_phone.in_(cands))
    if norm_email:
        match_conds.append(db.func.trim(db.func.lower(LeadSchema.email)) == norm_email)

    mgr = LeadManager(engine)
    async with mgr.session_factory() as session:
        query = (
            db.select(LeadSchema)
            .where(LeadSchema.deleted_at.is_(None), db.or_(*match_conds))
            .order_by(LeadSchema.created_at.asc(), LeadSchema.uid.asc())
            .limit(1)
        )
        if exclude_lead_id:
            query = query.where(LeadSchema.uid != exclude_lead_id)
        result = await session.execute(query)
        return result.scalars().first()


async def merge_into_existing(engine, existing, incoming: Dict[str, Any], *,
                              source_label: str, by_user_id: Optional[str] = "system"):
    """Backfill empty fields on ``existing`` from ``incoming`` and log a merge note.

    Never overwrites a populated field. Returns the refreshed lead.
    """
    from managers import LeadManager
    from utils.crm_enums import LeadActivityType
    from services import leadService

    existing_values = {f: getattr(existing, f, None) for f in _MERGEABLE_FIELDS}
    existing_values["custom_fields"] = getattr(existing, "custom_fields", None)
    existing_values["campaign_data"] = getattr(existing, "campaign_data", None)
    updates = compute_backfill(existing_values, incoming)

    mgr = LeadManager(engine)
    if updates:
        await mgr.update(existing.uid, updates)

    await leadService.record_activity(
        engine, existing.uid, LeadActivityType.NOTE,
        user_id=by_user_id,
        body=f"Deduplicated from {source_label}",
        details={
            "merged_fields": list(updates.keys()),
            "incoming_mobile": _jsonable(incoming.get("mobile")),
            "incoming_email": _jsonable(incoming.get("email")),
        },
    )
    logger.info(f"[dedup] merged duplicate into lead {existing.uid} "
                f"from {source_label}; backfilled {list(updates.keys())}")
    return await mgr.fetch(existing.uid)

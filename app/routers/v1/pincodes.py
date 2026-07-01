"""CRM pincode lookup — resolve an Indian pincode to its possible
(state, district, taluk) options so the lead form can auto-fill geography.

Queries the seeded `pincodes` table first (the curated GeoNames extract), then
falls back to the pypinindia dataset for any code not present.
"""
from typing import List, Optional

import sqlalchemy as db
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import get_settings, get_engine
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/pincodes", tags=["CRM - Pincodes"])


class PincodeOption(BaseModel):
    state: str
    district: Optional[str] = None
    taluk: Optional[str] = None


class PincodeLookupResponse(BaseModel):
    pincode: str
    options: List[PincodeOption]


def _tc(s):
    """Title-case a place name; blanks -> None."""
    s = (s or "").strip()
    return s.title() if s else None


def _dedupe(options):
    """Distinct (state, district, taluk) tuples, first-seen order preserved."""
    seen, out = set(), []
    for opt in options:
        key = tuple(x or "" for x in opt)
        if key in seen:
            continue
        seen.add(key)
        out.append(opt)
    return out


def _from_pypinindia(pin_data):
    """Map pypinindia get_pincode_info() rows -> normalized, deduped tuples."""
    out = []
    for info in pin_data or []:
        state = _tc(info.get("statename"))
        district = _tc(info.get("districtname") or info.get("district"))
        taluk = _tc(info.get("taluk"))
        if state:
            out.append((state, district, taluk))
    return _dedupe(out)


@router.get("/{pincode}", response_model=PincodeLookupResponse)
async def lookup_pincode(
    pincode: str,
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ)),
):
    pin = pincode.strip()
    if not pin.isdigit() or len(pin) != 6:
        raise HTTPException(status_code=400, detail="Pincode must be 6 digits")

    sp = f"{settings.name}." if settings.supports_schema else ""

    # 1. Seeded table (authoritative).
    async with engine.connect() as conn:
        rows = (await conn.execute(
            db.text(f"SELECT DISTINCT state, district, taluk FROM {sp}pincodes WHERE pincode = :pin"),
            {"pin": pin},
        )).fetchall()
    options = _dedupe([(_tc(r[0]), _tc(r[1]), _tc(r[2])) for r in rows])

    # 2. Fallback to pypinindia for codes the table doesn't cover.
    if not options:
        from pypinindia import get_pincode_info
        try:
            options = _from_pypinindia(get_pincode_info(pin))
        except Exception:
            options = []

    if not options:
        raise HTTPException(status_code=404, detail="Pincode not found")

    return PincodeLookupResponse(
        pincode=pin,
        options=[PincodeOption(state=s, district=d, taluk=t) for (s, d, t) in options],
    )

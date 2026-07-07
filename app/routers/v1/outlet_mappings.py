"""
CRUD API endpoints for managing outlet mappings.
This replaces the Google Sheets macro for auto-assigning outlets.

Available routes:
- GET /outlet-mappings - List all mappings (with pagination)
- GET /outlet-mappings/{uid} - Get a specific mapping
- POST /outlet-mappings - Create a new mapping
- PUT /outlet-mappings/{uid} - Update a mapping
- DELETE /outlet-mappings/{uid} - Delete a mapping
- POST /outlet-mappings/bulk - Bulk create/update mappings
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from config import get_settings, get_engine
from managers import OutletMappingManager, OutletManager, OutletSchema ,OutletMappingSchema
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)
outlet_mapping_manager = OutletMappingManager(engine)
outlet_manager = OutletManager(engine)

# Lazily initialised; loads pypinindia's dataset once on first call.
_pinindia = None

def _get_pinindia():
    global _pinindia
    if _pinindia is None:
        from pypinindia import PincodeData
        _pinindia = PincodeData()
    return _pinindia


def _validate_location(
    state: str,
    district: str,
    taluk: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validate state / district / taluk against the pypinindia dataset.

    Returns None when everything is valid.
    Returns a dict with ``valid`` values and ``suggestions`` when something
    doesn't match, so the caller can include that in the HTTP error response.
    """
    pd = _get_pinindia()

    # --- state ---
    valid_states = [s.upper() for s in pd.get_states()]
    if state.upper() not in valid_states:
        suggestions = pd.suggest_states(state, n=5)
        return {
            "field": "state",
            "provided": state,
            "message": f"'{state}' is not a recognised state.",
            "suggestions": suggestions,
        }

    # pypinindia stores states in uppercase; look up the canonical casing for
    # downstream calls.
    canonical_state = next(s for s in pd.get_states() if s.upper() == state.upper())

    # --- district ---
    valid_districts_lower = {d.lower(): d for d in pd.get_districts(state_name=canonical_state)}
    if district.lower() not in valid_districts_lower:
        suggestions = pd.suggest_districts(district, state_name=canonical_state, n=5)
        return {
            "field": "district",
            "provided": district,
            "message": f"'{district}' is not a recognised district in {canonical_state}.",
            "valid_districts": sorted(valid_districts_lower.values()),
            "suggestions": suggestions,
        }

    canonical_district = valid_districts_lower[district.lower()]

    # --- taluk (optional) ---
    if taluk:
        valid_taluks_lower = {
            t.lower(): t
            for t in pd.get_unique_taluks(
                state_name=canonical_state,
                district_name=canonical_district,
            )
            if t and t != "nan"
        }
        if taluk.lower() not in valid_taluks_lower:
            return {
                "field": "taluk",
                "provided": taluk,
                "message": (
                    f"'{taluk}' is not a recognised taluk in "
                    f"{canonical_district}, {canonical_state}."
                ),
                "valid_taluks": sorted(valid_taluks_lower.values()),
            }

    return None  # all good


router = APIRouter(prefix="/outlet-mappings", tags=["Outlet Mappings"])


# Pydantic schemas
class OutletMappingCreate(BaseModel):
    state: str = Field(..., description="State name (e.g., 'Karnataka')")
    district: str = Field(..., description="District name")
    taluk: Optional[str] = Field(None, description="Taluk name (optional)")
    pincode: Optional[str] = Field(None, description="Pincode mapping (optional)")
    outlet_id: str = Field(..., description="UUID of the outlet to assign")
    is_active: bool = Field(default=True, description="Whether this mapping is active")


class OutletMappingUpdate(BaseModel):
    state: Optional[str] = Field(None, description="State name")
    district: Optional[str] = Field(None, description="District name")
    taluk: Optional[str] = Field(None, description="Taluk name")
    pincode: Optional[str] = Field(None, description="Pincode mapping")
    outlet_id: Optional[str] = Field(None, description="UUID of the outlet")
    is_active: Optional[bool] = Field(None, description="Whether this mapping is active")


class OutletMappingResponse(BaseModel):
    uid: str
    state: str
    district: str
    taluk: Optional[str]
    pincode: Optional[str]
    outlet_id: str
    outlet_name: Optional[str]
    is_active: bool
    created_at: str


class BulkMappingRequest(BaseModel):
    mappings: List[OutletMappingCreate]


class BulkMappingResponse(BaseModel):
    created: int
    skipped: int
    errors: List[str]


# Helper function
def map_to_response(mapping, outlet_name=None):
    """Convert database model to response schema"""
    return OutletMappingResponse(
        uid=mapping.uid,
        state=mapping.state,
        district=mapping.district,
        taluk=mapping.taluk,
        pincode=mapping.pincode,
        outlet_id=mapping.outlet_id,
        outlet_name=outlet_name,
        is_active=mapping.is_active,
        created_at=mapping.created_at.isoformat() if mapping.created_at else None
    )



@router.get("/lookup")
async def lookup_pincode(pincode: str = Query(..., description="Pincode to lookup")):
    """
    Resolve a pincode to a location and find the assigned outlet.
    Returns the resolved location (state, district, taluk) and the mapped outlet.
    Checks the database outlet_mappings table first before falling back to pypinindia.
    """
    pin_str = str(pincode).strip()
    
    # 1. Check direct pincode mapping in DB first. Use fetch_all (not fetch_one, which
    # raises on no match) — an unmapped pincode must fall through to the pypinindia
    # fallback below, not 500.
    records = await outlet_mapping_manager.fetch_all(
        1, filters={"pincode": pin_str, "is_active": True}
    )
    db_mapping = records.items[0] if records.items else None
    if db_mapping:
        outlet = await outlet_manager.fetch(db_mapping.outlet_id)
        if outlet and outlet.is_active:
            # Capitalize geographical details for output styling
            return {
                "resolved_location": {
                    "state": db_mapping.state.title(),
                    "district": db_mapping.district.title(),
                    "taluk": db_mapping.taluk.title() if db_mapping.taluk else "",
                    "pincode": pin_str
                },
                "outlet": outlet.model_dump()
            }

    # 2. Fallback to pypinindia lookup
    from pypinindia import get_pincode_info
    try:
        # Use get_pincode_info which is already imported in some contexts or available in pypinindia
        pin_data = get_pincode_info(pincode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Pincode lookup failed: {str(e)}")

    if not pin_data or not isinstance(pin_data, list) or len(pin_data) == 0:
        raise HTTPException(status_code=404, detail="Pincode not found")

    # Get the first result
    info = pin_data[0]
    res_district = info.get('districtname') or info.get('district')
    res_taluk = info.get('taluk')
    res_state = info.get('statename')

    if not res_district:
        raise HTTPException(status_code=404, detail="Could not resolve district for this pincode")

    from utils.outlet_assignment import auto_assign_outlet
    
    # Get the mapped outlet (without assigning it to an order)
    outlet = await auto_assign_outlet(
        engine,
        order_id=None,
        district=res_district,
        pincode=pincode,
        state=res_state,
        taluk=res_taluk
    )

    return {
        "resolved_location": {
            "state": res_state,
            "district": res_district,
            "taluk": res_taluk,
            "pincode": pincode
        },
        "outlet": outlet.model_dump() if outlet else None
    }


@router.get("/locations")
async def get_locations():
    """
    Get all mapped locations in a hierarchical structure:
    state -> districts -> taluks (with mapped outlet_id per entry).
    """
    import sqlalchemy as db

    async with engine.connect() as conn:
        query = db.select(
            OutletMappingSchema.state,
            OutletMappingSchema.district,
            OutletMappingSchema.taluk,
            OutletMappingSchema.outlet_id,
        ).where(OutletMappingSchema.is_active == True).order_by(
            OutletMappingSchema.state,
            OutletMappingSchema.district,
            OutletMappingSchema.taluk,
        )
        result = await conn.execute(query)
        rows = result.fetchall()

    # Build hierarchy: { state: { district: [ {taluk, outlet_id} ] } }
    hierarchy: dict = {}
    for state, district, taluk, outlet_id in rows:
        if not state:
            continue
        if state not in hierarchy:
            hierarchy[state] = {}
        if district not in hierarchy[state]:
            hierarchy[state][district] = []
        hierarchy[state][district].append({
            "taluk": taluk,
            "outlet_id": outlet_id,
        })

    # Serialize into a list for easy frontend consumption
    return {
        "locations": [
            {
                "state": state,
                "districts": [
                    {
                        "district": district,
                        "taluks": sorted(taluks, key=lambda t: t["taluk"] or ""),
                    }
                    for district, taluks in sorted(districts.items())
                ],
            }
            for state, districts in sorted(hierarchy.items())
        ]
    }


@router.get("")
async def list_mappings(
    state: Optional[str] = Query(None, description="Filter by state"),
    district: Optional[str] = Query(None, description="Filter by district"),
    taluk: Optional[str] = Query(None, description="Filter by taluk"),
    pincode: Optional[str] = Query(None, description="Filter by pincode"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    limit: int = Query(100, ge=0, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset")
):
    """List all outlet mappings with optional filters (case-insensitive)"""
    filters = {}
    if state:
        filters["state"] = state.lower()
    if district:
        filters["district"] = district.lower()
    if taluk is not None:
        filters["taluk"] = taluk.lower()
    if pincode:
        filters["pincode"] = pincode.strip()
    if is_active is not None:
        filters["is_active"] = is_active

    mappings = await outlet_mapping_manager.fetch_all(
        filters=filters,
        limit=limit,
        offset=offset,
        joins=[OutletMappingSchema.outlet]
    )

    return mappings.model_dump()


@router.get("/{uid}", response_model=OutletMappingResponse)
async def get_mapping(uid: str):
    """Get a specific outlet mapping by ID"""
    mapping = await outlet_mapping_manager.fetch(uid)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    outlet = await outlet_manager.fetch(mapping.outlet_id)
    return map_to_response(mapping, outlet.outlet_name if outlet else None)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_mapping(
    payload: OutletMappingCreate,
    _: AuthContext = Depends(require_permission(Permission.OUTLETS_WRITE))
):
    """
    Create a new outlet mapping.

    State, district, and taluk are validated against the pypinindia dataset.
    If a pincode is provided, geographic validation is bypassed.
    """
    # --- geographic validation ---
    if not payload.pincode:
        location_error = _validate_location(payload.state, payload.district, payload.taluk)
        if location_error:
            raise HTTPException(status_code=422, detail=location_error)

    # --- outlet exists ---
    outlet = await outlet_manager.fetch(payload.outlet_id)
    if not outlet:
        raise HTTPException(status_code=400, detail="Outlet not found")

    mapping_data = OutletMappingSchema(
        state = payload.state.lower(),
        district = payload.district.lower(),
        taluk = payload.taluk.lower() if payload.taluk else None,
        pincode = payload.pincode.strip() if payload.pincode else None,
        outlet_id = payload.outlet_id,
        is_active = payload.is_active
    )

    mapping = await outlet_mapping_manager.create(mapping_data)
    return map_to_response(mapping, outlet.outlet_name)


@router.put("/{uid}", response_model=OutletMappingResponse)
async def update_mapping(
    uid: str,
    payload: OutletMappingUpdate,
    _: AuthContext = Depends(require_permission(Permission.OUTLETS_WRITE))
):
    """Update an existing outlet mapping"""
    mapping = await outlet_mapping_manager.fetch(uid)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    update_data = {}
    if payload.state is not None:
        update_data["state"] = payload.state.lower()
    if payload.district is not None:
        update_data["district"] = payload.district.lower()
    if payload.taluk is not None:
        update_data["taluk"] = payload.taluk.lower() if payload.taluk else None
    if payload.pincode is not None:
        update_data["pincode"] = payload.pincode.strip() if payload.pincode else None
    if payload.outlet_id is not None:
        # Verify outlet exists
        outlet = await outlet_manager.fetch(payload.outlet_id)
        if not outlet:
            raise HTTPException(status_code=400, detail="Outlet not found")
        update_data["outlet_id"] = payload.outlet_id
    if payload.is_active is not None:
        update_data["is_active"] = payload.is_active

    mapping = await outlet_mapping_manager.update(uid, update_data)
    outlet = await outlet_manager.fetch(mapping.outlet_id)
    return map_to_response(mapping, outlet.outlet_name if outlet else None)


@router.delete("/{uid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(
    uid: str,
    _: AuthContext = Depends(require_permission(Permission.OUTLETS_WRITE))
):
    """Delete an outlet mapping"""
    mapping = await outlet_mapping_manager.fetch(uid)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    await outlet_mapping_manager.delete(uid)
    return None


@router.post("/bulk", response_model=BulkMappingResponse)
async def bulk_upsert_mappings(
    payload: BulkMappingRequest,
    _: AuthContext = Depends(require_permission(Permission.OUTLETS_WRITE))
):
    """Bulk create or update outlet mappings. Replaces all mappings for matching criteria."""
    created = 0
    skipped = 0
    errors = []

    # Clear existing mappings for the states being imported
    states_to_clear = set()
    for m in payload.mappings:
        states_to_clear.add(m.state)

    # Clear existing mappings for these states
    if states_to_clear:
        for state in states_to_clear:
            existing = await outlet_mapping_manager.fetch_all(
                filters={"state": state.lower()}
            )
            for item in existing.items:
                try:
                    await outlet_mapping_manager.delete(item.uid)
                except Exception as e:
                    errors.append(f"Failed to delete existing mapping {item.uid}: {str(e)}")

    # Create new mappings
    for mapping_data in payload.mappings:
        try:
            # Verify outlet exists
            outlet = await outlet_manager.fetch(mapping_data.outlet_id)
            if not outlet:
                skipped += 1
                errors.append(f"Outlet not found: {mapping_data.outlet_id}")
                continue

            mapping = await outlet_mapping_manager.create({
                "state": mapping_data.state.lower(),
                "district": mapping_data.district.lower(),
                "taluk": mapping_data.taluk.lower() if mapping_data.taluk else None,
                "outlet_id": mapping_data.outlet_id,
                "is_active": mapping_data.is_active
            })
            created += 1

        except Exception as e:
            skipped += 1
            errors.append(f"Failed to create mapping for {mapping_data.district}/{mapping_data.taluk}: {str(e)}")

    return BulkMappingResponse(
        created=created,
        skipped=skipped,
        errors=errors[:20]  # Limit errors to first 20
    )

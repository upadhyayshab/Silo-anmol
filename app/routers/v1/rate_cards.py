from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Any, Dict
from config import get_settings, get_engine
from managers import RateCardManager, RateCardSchema, OutletManager
from models import RateCardCreateRequest, RateCardUpdateRequest, RateCardResponse, ListResponse, StatusResponse
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission
from utils import dependencies as D

settings = get_settings()
engine = get_engine(settings.name)
rate_card_manager = RateCardManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/rate-cards", tags=["Rider Rate Cards"])

@router.post("", response_model=RateCardResponse, status_code=status.HTTP_201_CREATED)
async def create_rate_card(
    payload: RateCardCreateRequest,
    _ctx: AuthContext = Depends(require_permission(Permission.DRIVER_PAY_WRITE))
):
    """Create a new rate card for an outlet"""
    # Check if outlet exists
    try:
        await outlet_manager.fetch(payload.outlet_id)
    except:
        raise HTTPException(status_code=404, detail="Outlet not found")
    
    # Check if rate card already exists for this outlet
    existing = await rate_card_manager.fetch_all(filters={"outlet_id": payload.outlet_id})
    if existing.items:
        raise HTTPException(status_code=400, detail="Rate card already exists for this outlet. Use PUT to update.")

    new_rate_card = RateCardSchema(**payload.dict())
    return await rate_card_manager.create(new_rate_card, joins=[RateCardSchema.outlet])

@router.get("", response_model=ListResponse[RateCardResponse])
async def list_rate_cards(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    limit: int = 50,
    offset: int = 0,
    _ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """List all rate cards"""
    return await rate_card_manager.fetch_all(
        limit=limit, 
        offset=offset, 
        sorts=sorts, 
        filters=filters,
        joins=[RateCardSchema.outlet]
    )

@router.get("/{uid}", response_model=RateCardResponse)
async def get_rate_card(
    uid: str,
    _ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """Get a specific rate card"""
    try:
        return await rate_card_manager.fetch(uid, joins=[RateCardSchema.outlet])
    except:
        raise HTTPException(status_code=404, detail="Rate card not found")

@router.put("/{uid}", response_model=RateCardResponse)
async def update_rate_card(
    uid: str,
    payload: RateCardUpdateRequest,
    _ctx: AuthContext = Depends(require_permission(Permission.DRIVER_PAY_WRITE))
):
    """Update a rate card"""
    updates = payload.dict(exclude_unset=True)
    try:
        return await rate_card_manager.update(uid, updates, joins=[RateCardSchema.outlet])
    except:
        raise HTTPException(status_code=404, detail="Rate card not found")

@router.delete("/{uid}", response_model=StatusResponse)
async def delete_rate_card(
    uid: str,
    _ctx: AuthContext = Depends(require_permission(Permission.DRIVER_PAY_WRITE))
):
    """Delete a rate card"""
    try:
        await rate_card_manager.delete(uid)
        return StatusResponse(status="success", message="Rate card deleted")
    except:
        raise HTTPException(status_code=404, detail="Rate card not found")

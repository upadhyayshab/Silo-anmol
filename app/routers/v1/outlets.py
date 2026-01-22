from fastapi import APIRouter, HTTPException, Depends, status

from config import get_settings, get_engine
from managers import OutletManager
from models import (
    OutletCreateRequest, OutletUpdateRequest, OutletResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/outlets", tags=["Outlet Management"])


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/{outlet_id}", response_model=OutletResponse)
async def get_outlet(
    outlet_id: str,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER))
):
    """Get specific outlet details"""
    try:
        outlet = await outlet_manager.fetch(outlet_id)
        
        return OutletResponse(
            uid=outlet.uid,
            outlet_name=outlet.outlet_name,
            address=outlet.address,
            city=outlet.city,
            state=outlet.state,
            pincode=outlet.pincode,
            phone=outlet.phone,
            email=outlet.email,
            manager_id=outlet.manager_id,
            is_active=outlet.is_active,
            created_at=outlet.created_at,
            last_updated=outlet.last_updated
        )
    
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlet: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[OutletResponse])
async def list_outlets(
    is_active: bool = None,
    city: str = None,
    state: str = None,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, 
        UserRole.WAREHOUSE_MANAGER, UserRole.ACCOUNTANT
    ))
):
    """
    List all outlets with optional filters
    Requires: super_admin, admin, warehouse_manager, or accountant role
    """
    try:
        filters = {}
        if is_active is not None:
            filters["is_active"] = is_active
        if city:
            filters["city"] = city
        if state:
            filters["state"] = state
        
        outlets = await outlet_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        outlet_responses = [
            OutletResponse(
                uid=outlet.uid,
                outlet_name=outlet.outlet_name,
                outlet_code=outlet.outlet_code,
                address=outlet.address,
                city=outlet.city,
                state=outlet.state,
                pincode=outlet.pincode,
                phone=outlet.phone,
                email=outlet.email,
                gstin=outlet.gstin,
                state_code=outlet.state_code,
                pan=outlet.pan,
                manager_id=outlet.manager_id,
                is_active=outlet.is_active,
                created_at=outlet.created_at
            )
            for outlet in outlets.items
        ]
        
        return ListResponse(items=outlet_responses, count=len(outlet_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlets: {str(e)}"
        )


# Duplicate /{outlet_id} route removed - moved to top of file
    """
    Get outlet by ID
    Requires: super_admin, admin, warehouse_manager, outlet_manager, or accountant role
    """
    try:
        outlet = await outlet_manager.fetch(outlet_id)
        
        return OutletResponse(
            uid=outlet.uid,
            outlet_name=outlet.outlet_name,
            outlet_code=outlet.outlet_code,
            address=outlet.address,
            city=outlet.city,
            state=outlet.state,
            pincode=outlet.pincode,
            phone=outlet.phone,
            email=outlet.email,
            gstin=outlet.gstin,
            state_code=outlet.state_code,
            pan=outlet.pan,
            manager_id=outlet.manager_id,
            is_active=outlet.is_active,
            created_at=outlet.created_at
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Outlet not found: {str(e)}"
        )




@router.get("/{outlet_id}", response_model=OutletResponse)
async def get_outlet(
    outlet_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """Get outlet by ID"""
    try:
        outlet = await outlet_manager.fetch(outlet_id)
        return outlet
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Outlet not found: {str(e)}"
        )


@router.put("/{outlet_id}", response_model=OutletResponse)
async def update_outlet(
    outlet_id: str,
    payload: OutletUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """Update outlet details"""
    try:
        updates = payload.dict(exclude_unset=True)
        updated_outlet = await outlet_manager.update(outlet_id, updates)
        return updated_outlet
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update outlet: {str(e)}"
        )


@router.post("", response_model=OutletResponse, status_code=status.HTTP_201_CREATED)
async def create_outlet(
    payload: OutletCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Create new outlet
    Requires: super_admin or admin role
    """
    try:
        # Check if outlet_code already exists
        existing = await outlet_manager.fetch_all(filters={"outlet_code": payload.outlet_code})
        if existing.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Outlet code already exists"
            )
        
        # Create outlet
        from managers import OutletSchema
        outlet = OutletSchema(
            outlet_name=payload.outlet_name,
            outlet_code=payload.outlet_code,
            address=payload.address,
            city=payload.city,
            state=payload.state,
            pincode=payload.pincode,
            phone=payload.phone,
            email=payload.email,
            gstin=payload.gstin,
            state_code=payload.state_code,
            pan=payload.pan,
            manager_id=payload.manager_id,
            is_active=True
        )
        
        created_outlet = await outlet_manager.create(outlet)
        
        return OutletResponse(
            uid=created_outlet.uid,
            outlet_name=created_outlet.outlet_name,
            outlet_code=created_outlet.outlet_code,
            address=created_outlet.address,
            city=created_outlet.city,
            state=created_outlet.state,
            pincode=created_outlet.pincode,
            phone=created_outlet.phone,
            email=created_outlet.email,
            gstin=created_outlet.gstin,
            state_code=created_outlet.state_code,
            pan=created_outlet.pan,
            manager_id=created_outlet.manager_id,
            is_active=created_outlet.is_active,
            created_at=created_outlet.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create outlet: {str(e)}"
        )


@router.patch("/{outlet_id}", response_model=OutletResponse)
async def update_outlet(
    outlet_id: str,
    payload: OutletUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Update outlet details
    Requires: super_admin or admin role
    """
    try:
        updates = payload.dict(exclude_unset=True)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        updated_outlet = await outlet_manager.update(outlet_id, updates)
        
        return OutletResponse(
            uid=updated_outlet.uid,
            outlet_name=updated_outlet.outlet_name,
            outlet_code=updated_outlet.outlet_code,
            address=updated_outlet.address,
            city=updated_outlet.city,
            state=updated_outlet.state,
            pincode=updated_outlet.pincode,
            phone=updated_outlet.phone,
            email=updated_outlet.email,
            gstin=updated_outlet.gstin,
            state_code=updated_outlet.state_code,
            pan=updated_outlet.pan,
            manager_id=updated_outlet.manager_id,
            is_active=updated_outlet.is_active,
            created_at=updated_outlet.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update outlet: {str(e)}"
        )


@router.delete("/{outlet_id}", response_model=StatusResponse)
async def deactivate_outlet(
    outlet_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    """
    Deactivate outlet (soft delete)
    Requires: super_admin role only
    """
    try:
        await outlet_manager.update(outlet_id, {"is_active": False})
        return StatusResponse(status="ok", message="Outlet deactivated successfully")
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate outlet: {str(e)}"
        )

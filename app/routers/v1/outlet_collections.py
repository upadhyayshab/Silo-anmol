from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Optional
from datetime import datetime, date
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    OutletDailyCollectionManager, OutletManager,
    OutletDailyCollectionSchema
)
from models import (
    OutletCollectionCreateRequest, OutletCollectionStatusUpdateRequest,
    OutletCollectionResponse, ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OutletCollectionStatus

settings = get_settings()
engine = get_engine(settings.name)

collection_manager = OutletDailyCollectionManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/outlet-collections", tags=["Outlet Collections"])


@router.post("", response_model=OutletCollectionResponse)
async def create_collection(
    payload: OutletCollectionCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Create a new outlet daily collection record
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Validate outlet exists
        try:
            await outlet_manager.fetch(payload.outlet_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outlet not found: {payload.outlet_id}"
            )
        
        # Create collection record
        collection_data = OutletDailyCollectionSchema(
            date=payload.collection_date,
            outlet_id=payload.outlet_id,
            amount=payload.amount,
            payment_mode=payload.payment_mode,
            payment_sub_mode=payload.payment_sub_mode,
            transaction_id=payload.transaction_id,
            remarks=payload.remarks,
            confirmation_status=OutletCollectionStatus.PENDING,
            confirmed_by=None,
            confirmed_at=None
        )
        
        collection = await collection_manager.create(collection_data)
        
        return OutletCollectionResponse(
            uid=collection.uid,
            collection_date=collection.date,
            outlet_id=collection.outlet_id,
            amount=collection.amount,
            payment_mode=collection.payment_mode,
            payment_sub_mode=collection.payment_sub_mode,
            transaction_id=collection.transaction_id,
            remarks=collection.remarks,
            confirmation_status=collection.confirmation_status,
            confirmed_by=collection.confirmed_by,
            confirmed_at=collection.confirmed_at,
            created_at=collection.created_at,
            updated_at=collection.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create collection: {str(e)}"
        )


@router.get("", response_model=ListResponse[OutletCollectionResponse])
async def list_collections(
    outlet_id: Optional[str] = Query(None, description="Filter by outlet"),
    date_from: Optional[date] = Query(None, description="Filter from date"),
    date_to: Optional[date] = Query(None, description="Filter to date"),
    confirmation_status: Optional[OutletCollectionStatus] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    List outlet collections with filters
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Build filters
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if confirmation_status:
            filters["confirmation_status"] = confirmation_status
        
        # Date range filtering - use proper SharedBackend syntax
        if date_from and date_to:
            # For date range, we need to use between operator
            filters["date"] = {"between": [date_from, date_to]}
        elif date_from:
            filters["date"] = {">": date_from}
        elif date_to:
            filters["date"] = {"<": date_to}
        
        # Fetch collections
        collections = await collection_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None,
            sorts=["-date", "-created_at"]
        )
        
        # Convert to response models
        items = [
            OutletCollectionResponse(
                uid=c.uid,
                collection_date=c.date,
                outlet_id=c.outlet_id,
                amount=c.amount,
                payment_mode=c.payment_mode,
                payment_sub_mode=c.payment_sub_mode,
                transaction_id=c.transaction_id,
                remarks=c.remarks,
                confirmation_status=c.confirmation_status,
                confirmed_by=c.confirmed_by,
                confirmed_at=c.confirmed_at,
                created_at=c.created_at,
                updated_at=c.updated_at
            )
            for c in collections.items
        ]
        
        return ListResponse(items=items, count=collections.count)
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch collections: {str(e)}"
        )


@router.get("/{collection_id}", response_model=OutletCollectionResponse)
async def get_collection(
    collection_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Get a single collection record
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        collection = await collection_manager.fetch(collection_id)
        
        return OutletCollectionResponse(
            uid=collection.uid,
            collection_date=collection.date,
            outlet_id=collection.outlet_id,
            amount=collection.amount,
            payment_mode=collection.payment_mode,
            payment_sub_mode=collection.payment_sub_mode,
            transaction_id=collection.transaction_id,
            remarks=collection.remarks,
            confirmation_status=collection.confirmation_status,
            confirmed_by=collection.confirmed_by,
            confirmed_at=collection.confirmed_at,
            created_at=collection.created_at,
            updated_at=collection.updated_at
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection not found: {str(e)}"
        )


@router.patch("/{collection_id}/status", response_model=OutletCollectionResponse)
async def update_collection_status(
    collection_id: str,
    payload: OutletCollectionStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Update collection confirmation status
    
    Status transitions:
    - PENDING -> CONFIRMED
    - PENDING -> NOT_RECEIVED
    - NOT_RECEIVED -> CONFIRMED
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Fetch current collection
        collection = await collection_manager.fetch(collection_id)
        
        # Validate status transition
        current_status = collection.confirmation_status
        new_status = payload.confirmation_status
        
        valid_transitions = {
            OutletCollectionStatus.PENDING: [
                OutletCollectionStatus.CONFIRMED,
                OutletCollectionStatus.NOT_RECEIVED
            ],
            OutletCollectionStatus.NOT_RECEIVED: [
                OutletCollectionStatus.CONFIRMED
            ],
            OutletCollectionStatus.CONFIRMED: []  # Cannot change from CONFIRMED
        }
        
        if new_status not in valid_transitions.get(current_status, []):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition: {current_status.value} -> {new_status.value}"
            )
        
        # Prepare updates
        updates = {
            "confirmation_status": new_status
        }
        
        # Set confirmed_by and confirmed_at when status becomes CONFIRMED
        if new_status == OutletCollectionStatus.CONFIRMED:
            updates["confirmed_by"] = current_user_id
            updates["confirmed_at"] = datetime.utcnow()
        
        # Update collection
        await collection_manager.update(
            collection_id,
            updates
        )
        
        # Fetch fresh copy to avoid session detachment issues
        updated_collection = await collection_manager.fetch(collection_id)
        
        return OutletCollectionResponse(
            uid=updated_collection.uid,
            collection_date=updated_collection.date,
            outlet_id=updated_collection.outlet_id,
            amount=updated_collection.amount,
            payment_mode=updated_collection.payment_mode,
            payment_sub_mode=updated_collection.payment_sub_mode,
            transaction_id=updated_collection.transaction_id,
            remarks=updated_collection.remarks,
            confirmation_status=updated_collection.confirmation_status,
            confirmed_by=updated_collection.confirmed_by,
            confirmed_at=updated_collection.confirmed_at,
            created_at=updated_collection.created_at,
            updated_at=updated_collection.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update collection status: {str(e)}"
        )


@router.delete("/{collection_id}", response_model=StatusResponse)
async def delete_collection(
    collection_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Delete a collection record
    
    Use this if a collection was created by mistake.
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Fetch collection to get details for response
        collection = await collection_manager.fetch(collection_id)
        
        # Delete using direct session delete (to avoid SharedBackend bug)
        async with collection_manager.session_factory() as session:
            import sqlalchemy as db
            query = db.select(OutletDailyCollectionSchema).filter_by(uid=collection_id)
            result = await session.execute(query)
            collection_to_delete = result.scalar_one_or_none()
            
            if not collection_to_delete:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection not found"
                )
            
            await session.delete(collection_to_delete)
            await session.commit()
        
        return StatusResponse(
            status="ok",
            message=f"Collection record for {collection.date} deleted successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete collection: {str(e)}"
        )

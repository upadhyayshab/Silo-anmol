from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Dict, Any
from datetime import datetime, date

from config import get_settings, get_engine
from managers import ActivityLogManager, UserManager, ActivityLogSchema
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission
from utils.functions import ensure_date
from pydantic import BaseModel

settings = get_settings()
engine = get_engine(settings.name)
activity_manager = ActivityLogManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/activity-logs", tags=["Activity Logs"])


# Response Models
class ActivityLogResponse(BaseModel):
    uid: str
    user_id: str
    user_name: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str]
    details: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/user/{user_id}", response_model=List[ActivityLogResponse])
async def get_user_activity_logs(
    user_id: str,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    _: AuthContext = Depends(require_permission(Permission.AUDIT_READ))
):
    """Get activity logs for a specific user"""
    try:
        # Build filters
        filters = {"user_id": user_id}
        
        if action:
            filters["action"] = action
        if start_date:
            filters["created_at__gte"] = start_date
        if end_date:
            filters["created_at__lte"] = end_date
        
        # Fetch logs
        logs = await activity_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        # Enhance with user names
        log_responses = []
        for log in logs.items:
            try:
                user = await user_manager.fetch(log.user_id)
                user_name = f"{user.first_name} {user.last_name}".strip()
            except:
                user_name = "Unknown User"
            
            log_responses.append(ActivityLogResponse(
                uid=log.uid,
                user_id=log.user_id,
                user_name=user_name,
                action=log.action,
                entity_type=log.entity_type,
                entity_id=log.entity_id,
                details=log.details,
                ip_address=log.ip_address,
                created_at=log.created_at
            ))
        
        return log_responses
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user activity logs: {str(e)}"
        )


@router.get("/entity/{entity_type}/{entity_id}", response_model=List[ActivityLogResponse])
async def get_entity_activity_logs(
    entity_type: str,
    entity_id: str,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    _: AuthContext = Depends(require_permission(Permission.AUDIT_READ))
):
    """Get activity logs for a specific entity"""
    try:
        # Build filters
        filters = {
            "entity_type": entity_type,
            "entity_id": entity_id
        }
        
        if action:
            filters["action"] = action
        if start_date:
            filters["created_at__gte"] = start_date
        if end_date:
            filters["created_at__lte"] = end_date
        
        # Fetch logs
        logs = await activity_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        # Enhance with user names
        log_responses = []
        for log in logs.items:
            try:
                user = await user_manager.fetch(log.user_id)
                user_name = f"{user.first_name} {user.last_name}".strip()
            except:
                user_name = "Unknown User"
            
            log_responses.append(ActivityLogResponse(
                uid=log.uid,
                user_id=log.user_id,
                user_name=user_name,
                action=log.action,
                entity_type=log.entity_type,
                entity_id=log.entity_id,
                details=log.details,
                ip_address=log.ip_address,
                created_at=log.created_at
            ))
        
        return log_responses
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch entity activity logs: {str(e)}"
        )


@router.get("/summary")
async def get_activity_summary(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    _: AuthContext = Depends(require_permission(Permission.AUDIT_READ))
):
    """Get activity summary statistics"""
    try:
        # Build filters
        filters = {}
        if start_date:
            filters["created_at__gte"] = start_date
        if end_date:
            filters["created_at__lte"] = end_date
        
        # Fetch all logs for the period
        logs = await activity_manager.fetch_all(filters=filters)
        
        # Calculate summary statistics
        total_activities = len(logs.items)
        
        # Group by action
        action_counts = {}
        user_counts = {}
        entity_counts = {}
        
        for log in logs.items:
            # Count by action
            action_counts[log.action] = action_counts.get(log.action, 0) + 1
            
            # Count by user
            user_counts[log.user_id] = user_counts.get(log.user_id, 0) + 1
            
            # Count by entity type
            entity_counts[log.entity_type] = entity_counts.get(log.entity_type, 0) + 1
        
        return {
            "period": {
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None
            },
            "summary": {
                "total_activities": total_activities,
                "unique_users": len(user_counts),
                "unique_entity_types": len(entity_counts)
            },
            "breakdown": {
                "by_action": action_counts,
                "by_user": dict(list(user_counts.items())[:10]),  # Top 10 users
                "by_entity_type": entity_counts
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate activity summary: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=List[ActivityLogResponse])
async def get_activity_logs(
    user_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 100,
    offset: int = 0,
    _: AuthContext = Depends(require_permission(Permission.AUDIT_READ))
):
    """
    Get activity logs with filters
    Requires: audit:read permission
    """
    try:
        filters = {}
        
        if user_id:
            filters["user_id"] = user_id
        if entity_type:
            filters["entity_type"] = entity_type
        if entity_id:
            filters["entity_id"] = entity_id
        if action:
            filters["action"] = action
        
        # Note: Date filtering would need custom SQL query implementation
        # For now, we'll filter in Python (not optimal for large datasets)
        
        logs = await activity_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        # Get user names for better display
        user_cache = {}
        log_responses = []
        
        for log in sorted(logs.items, key=lambda x: x.created_at, reverse=True):
            # Get user name if not cached
            user_name = None
            if log.user_id not in user_cache:
                try:
                    user = await user_manager.fetch(log.user_id)
                    user_cache[log.user_id] = user.full_name
                except:
                    user_cache[log.user_id] = "Unknown User"
            
            user_name = user_cache[log.user_id]
            
            # Apply date filtering if specified
            log_date = ensure_date(log.created_at)
            if start_date and log_date < start_date:
                continue
            if end_date and log_date > end_date:
                continue
            
            log_responses.append(ActivityLogResponse(
                uid=log.uid,
                user_id=log.user_id,
                user_name=user_name,
                action=log.action,
                entity_type=log.entity_type,
                entity_id=log.entity_id,
                details=log.details,
                ip_address=log.ip_address,
                created_at=log.created_at
            ))
        
        return log_responses
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch activity logs: {str(e)}"
        )


# Utility function to log activities
async def log_activity(
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None
):
    """
    Utility function to log user activities
    Used by other modules to create audit trails
    """
    try:
        activity_log = ActivityLogSchema(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip_address=ip_address
        )
        
        await activity_manager.create(activity_log)
        return True
    except Exception:
        return False


# Export utility function
__all__ = ["router", "log_activity"]
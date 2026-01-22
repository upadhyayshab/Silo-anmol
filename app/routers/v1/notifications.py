from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime

from config import get_settings, get_engine
from managers import NotificationManager, NotificationSchema
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole
from pydantic import BaseModel

settings = get_settings()
engine = get_engine(settings.name)
notification_manager = NotificationManager(engine)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# Response Models
class NotificationResponse(BaseModel):
    uid: str
    title: str
    message: str
    type: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationCreateRequest(BaseModel):
    user_id: str
    title: str
    message: str
    type: str = "info"  # info, warning, error, success


class UnreadCountResponse(BaseModel):
    unread_count: int


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    current_user_id: str = Depends(get_current_user_id)
):
    """Get count of unread notifications for current user"""
    try:
        notifications = await notification_manager.fetch_all(
            filters={
                "user_id": current_user_id,
                "is_read": False
            }
        )
        
        return UnreadCountResponse(unread_count=len(notifications.items))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unread count: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=List[NotificationResponse])
async def get_user_notifications(
    is_read: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    current_user_id: str = Depends(get_current_user_id)
):
    """
    Get notifications for current user
    """
    try:
        filters = {"user_id": current_user_id}
        if is_read is not None:
            filters["is_read"] = is_read
        
        notifications = await notification_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters
        )
        
        return [
            NotificationResponse(
                uid=notif.uid,
                title=notif.title,
                message=notif.message,
                type=notif.type,
                is_read=notif.is_read,
                created_at=notif.created_at
            )
            for notif in sorted(notifications.items, key=lambda x: x.created_at, reverse=True)
        ]
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch notifications: {str(e)}"
        )


# Duplicate unread-count route removed - moved to top of file for proper ordering


@router.put("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: str,
    current_user_id: str = Depends(get_current_user_id)
):
    """
    Mark specific notification as read
    """
    try:
        # Verify notification belongs to current user
        notification = await notification_manager.fetch(notification_id)
        
        if notification.user_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this notification"
            )
        
        # Mark as read
        updated_notification = await notification_manager.update(
            notification_id, 
            {"is_read": True}
        )
        
        return NotificationResponse(
            uid=updated_notification.uid,
            title=updated_notification.title,
            message=updated_notification.message,
            type=updated_notification.type,
            is_read=updated_notification.is_read,
            created_at=updated_notification.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark notification as read: {str(e)}"
        )


@router.put("/mark-all-read")
async def mark_all_notifications_read(
    current_user_id: str = Depends(get_current_user_id)
):
    """
    Mark all notifications as read for current user
    """
    try:
        # Get all unread notifications for user
        unread_notifications = await notification_manager.fetch_all(
            filters={"user_id": current_user_id, "is_read": False}
        )
        
        # Mark each as read
        for notification in unread_notifications.items:
            await notification_manager.update(notification.uid, {"is_read": True})
        
        return {
            "status": "ok",
            "message": f"Marked {len(unread_notifications.items)} notifications as read"
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark all notifications as read: {str(e)}"
        )


@router.post("", response_model=NotificationResponse)
async def create_notification(
    payload: NotificationCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Create notification for a user (Admin function)
    """
    try:
        notification = NotificationSchema(
            user_id=payload.user_id,
            title=payload.title,
            message=payload.message,
            type=payload.type,
            is_read=False
        )
        
        created_notification = await notification_manager.create(notification)
        
        return NotificationResponse(
            uid=created_notification.uid,
            title=created_notification.title,
            message=created_notification.message,
            type=created_notification.type,
            is_read=created_notification.is_read,
            created_at=created_notification.created_at
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create notification: {str(e)}"
        )


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    current_user_id: str = Depends(get_current_user_id)
):
    """
    Delete notification (user can delete their own notifications)
    """
    try:
        # Verify notification belongs to current user
        notification = await notification_manager.fetch(notification_id)
        
        if notification.user_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this notification"
            )
        
        # Delete notification (soft delete via is_active = False)
        await notification_manager.update(notification_id, {"is_active": False})
        
        return {"status": "ok", "message": "Notification deleted successfully"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete notification: {str(e)}"
        )


# Utility function to create system notifications
async def create_system_notification(
    user_id: str,
    title: str,
    message: str,
    notification_type: str = "info"
):
    """
    Utility function to create system notifications
    Used by other modules to send notifications
    """
    try:
        notification = NotificationSchema(
            user_id=user_id,
            title=title,
            message=message,
            type=notification_type,
            is_read=False
        )
        
        await notification_manager.create(notification)
        return True
    except Exception:
        return False


# Export utility function
__all__ = ["router", "create_system_notification"]
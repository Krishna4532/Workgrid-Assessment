import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user
from app.db import get_db_connection
from app.models import (
    NotificationItem,
    NotificationListResponse,
    NotificationUnreadCountResponse,
    User,
)
from app.repositories.notifications_repo import (
    get_notification_by_id,
    get_unread_count,
    list_user_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

logger = logging.getLogger("workgrid.notifications_router")

router = APIRouter(tags=["Notifications"])


@router.get(
    "/notifications",
    summary="List My Notifications",
    response_model=NotificationListResponse,
    response_description="User notifications feed with keyset cursor pagination and unread count",
)
def get_my_notifications(
    unread_only: bool = Query(False, description="Filter for unread notifications only"),
    cursor: Optional[str] = Query(None, description="Opaque keyset pagination cursor"),
    limit: int = Query(20, ge=1, le=100, description="Page limit"),
    current_user: User = Depends(get_current_user),
):
    """Retrieve notifications strictly scoped to the authenticated caller."""
    with get_db_connection() as conn:
        items, next_cursor, has_more = list_user_notifications(
            conn=conn,
            user_id=current_user.id,
            unread_only=unread_only,
            cursor=cursor,
            limit=limit,
        )
        unread_cnt = get_unread_count(conn, current_user.id)
        return NotificationListResponse(
            notifications=items,
            unread_count=unread_cnt,
            next_cursor=next_cursor,
            has_more=has_more,
        )


@router.get(
    "/notifications/unread-count",
    summary="Get Unread Notification Count",
    response_model=NotificationUnreadCountResponse,
    response_description="Total count of unread notifications for the caller",
)
def get_my_unread_count(current_user: User = Depends(get_current_user)):
    """Retrieve count of unread notifications for the authenticated caller."""
    with get_db_connection() as conn:
        cnt = get_unread_count(conn, current_user.id)
        return NotificationUnreadCountResponse(unread_count=cnt)


@router.post(
    "/notifications/{id}/read",
    summary="Mark Notification as Read",
    response_model=NotificationItem,
    response_description="Updated notification object",
)
def mark_read_endpoint(
    id: UUID,
    current_user: User = Depends(get_current_user),
):
    """Mark a notification as read idempotently.
    
    Returns HTTP 404 if the notification does not exist or belongs to another user.
    """
    with get_db_connection() as conn:
        with conn.transaction():
            marked = mark_notification_read(conn, current_user.id, id)
            if not marked:
                # Information hiding: Non-owned or non-existent notification returns 404
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Notification not found",
                )
            notif = get_notification_by_id(conn, current_user.id, id)
            if not notif:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Notification not found",
                )
            return notif


@router.post(
    "/notifications/read-all",
    summary="Mark All Notifications as Read",
    response_description="Confirmation and count of marked notifications",
)
def mark_all_read_endpoint(
    current_user: User = Depends(get_current_user),
):
    """Mark all unread notifications for the current user as read."""
    with get_db_connection() as conn:
        with conn.transaction():
            count = mark_all_notifications_read(conn, current_user.id)
            return {"status": "ok", "marked_read_count": count}

import base64
from datetime import datetime, timezone
import logging
from typing import List, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status
import psycopg
from psycopg.rows import dict_row

from app.models import NotificationItem
from app.sse import emit_pg_notify

logger = logging.getLogger("workgrid.notifications")


def _encode_notification_cursor(created_at: datetime, notification_id: UUID) -> str:
    """Encode (created_at, id) tuple into a stable, opaque base64 cursor."""
    raw = f"{created_at.isoformat()}|{notification_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _decode_notification_cursor(cursor: str) -> Tuple[datetime, UUID]:
    """Decode base64 cursor into (created_at, id) tuple."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        ts_str, id_str = raw.split("|")
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid notification pagination cursor format",
        )


def create_notification(
    conn: psycopg.Connection,
    user_id: UUID,
    event_id: Optional[UUID],
    dedupe_key: str,
    notification_type: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[UUID] = None,
) -> Optional[NotificationItem]:
    """Persist an in-app notification with database-enforced deduplication.
    
    If the dedupe_key already exists (e.g. from a retried or redelivered outbox job),
    ON CONFLICT (dedupe_key) DO NOTHING prevents duplicate notification generation.
    Returns the created NotificationItem, or None if already recorded.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO notifications (
                user_id, event_id, dedupe_key, notification_type,
                title, message, entity_type, entity_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id, user_id, event_id, dedupe_key, notification_type,
                      title, message, entity_type, entity_id, read_at, created_at;
            """,
            (
                user_id,
                event_id,
                dedupe_key,
                notification_type,
                title,
                message,
                entity_type,
                entity_id,
            ),
        )
        row = cur.fetchone()
        if not row:
            logger.info(
                "Duplicate notification suppressed by dedupe_key: %s", dedupe_key
            )
            return None

        item = NotificationItem(**row)
        emit_pg_notify(
            conn,
            {
                "id": str(item.id),
                "event_type": "notification_created",
                "recipient_id": str(user_id),
            },
        )
        return item


def get_unread_count(conn: psycopg.Connection, user_id: UUID) -> int:
    """Fast indexed query for count of unread notifications for a user."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM notifications
            WHERE user_id = %s AND read_at IS NULL;
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def list_user_notifications(
    conn: psycopg.Connection,
    user_id: UUID,
    unread_only: bool = False,
    cursor: Optional[str] = None,
    limit: int = 20,
) -> Tuple[List[NotificationItem], Optional[str], bool]:
    """Retrieve notifications scoped to user_id using keyset pagination."""
    limit = min(max(1, limit), 100)
    clauses = ["user_id = %s"]
    params: List[object] = [user_id]

    if unread_only:
        clauses.append("read_at IS NULL")

    if cursor:
        c_ts, c_id = _decode_notification_cursor(cursor)
        clauses.append("(created_at, id) < (%s, %s)")
        params.extend([c_ts, c_id])

    sql = f"""
        SELECT id, user_id, event_id, dedupe_key, notification_type,
               title, message, entity_type, entity_id, read_at, created_at
        FROM notifications
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT %s;
    """
    params.append(limit + 1)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    has_more = len(rows) > limit
    results = [NotificationItem(**r) for r in rows[:limit]]

    next_cursor = None
    if has_more and results:
        last = results[-1]
        next_cursor = _encode_notification_cursor(last.created_at, last.id)

    return results, next_cursor, has_more


def get_notification_by_id(
    conn: psycopg.Connection,
    user_id: UUID,
    notification_id: UUID,
) -> Optional[NotificationItem]:
    """Retrieve notification ensuring strict ownership isolation (returns None if not owned)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, user_id, event_id, dedupe_key, notification_type,
                   title, message, entity_type, entity_id, read_at, created_at
            FROM notifications
            WHERE id = %s AND user_id = %s;
            """,
            (notification_id, user_id),
        )
        row = cur.fetchone()
        return NotificationItem(**row) if row else None


def mark_notification_read(
    conn: psycopg.Connection,
    user_id: UUID,
    notification_id: UUID,
) -> bool:
    """Mark a notification as read idempotently.
    
    Returns True if notification exists and belongs to user_id (whether newly marked or already read).
    Returns False if notification does not exist or belongs to another user (404 trigger).
    """
    with conn.cursor() as cur:
        # First check existence and ownership
        cur.execute(
            "SELECT id, read_at FROM notifications WHERE id = %s AND user_id = %s;",
            (notification_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return False

        if row[1] is None:
            cur.execute(
                "UPDATE notifications SET read_at = NOW() WHERE id = %s AND user_id = %s;",
                (notification_id, user_id),
            )
            emit_pg_notify(
                conn,
                {
                    "id": str(notification_id),
                    "event_type": "notification_read",
                    "recipient_id": str(user_id),
                },
            )
        return True


def mark_all_notifications_read(
    conn: psycopg.Connection,
    user_id: UUID,
) -> int:
    """Mark all unread notifications for a user as read."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE notifications
            SET read_at = NOW()
            WHERE user_id = %s AND read_at IS NULL;
            """,
            (user_id,),
        )
        count = cur.rowcount
        if count > 0:
            emit_pg_notify(
                conn,
                {
                    "event_type": "notification_read",
                    "recipient_id": str(user_id),
                },
            )
        return count

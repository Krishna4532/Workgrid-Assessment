import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import settings
from app.models import OutboxEvent
from app.sse import emit_pg_notify

logger = logging.getLogger("workgrid.outbox")


def enqueue_outbox_event(
    conn: psycopg.Connection,
    event_type: str,
    payload: Dict[str, Any],
    max_attempts: Optional[int] = None,
) -> UUID:
    """Insert a new business event into the transactional outbox.
    
    Must participate in the caller's ongoing database transaction.
    Does NOT commit the transaction.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO outbox (event_type, payload, status, attempts, max_attempts, next_attempt_at)
            VALUES (%s, %s, 'pending', 0, %s, NOW())
            RETURNING id;
            """,
            (
                event_type,
                Jsonb(payload),
                max_attempts if max_attempts is not None else settings.WORKER_MAX_RETRIES,
            ),
        )
        row = cur.fetchone()
        event_id = row["id"]
        logger.debug("Enqueued outbox event %s (type: %s)", event_id, event_type)

        # Emit PostgreSQL NOTIFY on 'workgrid_events' channel within same transaction.
        # Dispatched by PostgreSQL only upon successful transaction commit.
        notify_data = {
            "id": str(event_id),
            "event_type": event_type,
            "team_id": str(payload.get("team_id")) if payload.get("team_id") else None,
            "scope_type": payload.get("scope_type"),
            "scope_id": str(payload.get("scope_id")) if payload.get("scope_id") else None,
            "recipient_id": str(payload.get("assignee_id")) if payload.get("assignee_id") else (str(payload.get("user_id")) if payload.get("user_id") else None),
            "item_id": str(payload.get("item_id")) if payload.get("item_id") else None,
            "user_id": str(payload.get("user_id")) if payload.get("user_id") else None,
        }
        emit_pg_notify(conn, notify_data)

        return event_id


def claim_pending_events(
    conn: psycopg.Connection,
    worker_id: str,
    batch_size: int = 20,
    lease_timeout_seconds: int = 60,
    event_ids: Optional[List[UUID]] = None,
) -> List[OutboxEvent]:
    """Atomically claim a batch of eligible outbox events with fencing tokens.
    
    Eligible events are:
    1. 'pending' events whose next_attempt_at has arrived.
    2. 'processing' events whose lock lease has expired (worker crash recovery).
    
    Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent lock contention between
    concurrent workers. Generates a fresh lease_token for each claimed row to
    prevent stale workers from overwriting results after lease expiration.
    """
    if event_ids is not None and not event_ids:
        return []

    event_filter = "AND id = ANY(%s)" if event_ids is not None else ""
    params: List[Any] = [lease_timeout_seconds]
    if event_ids is not None:
        params.append(event_ids)
    params.append(batch_size)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id
            FROM outbox
            WHERE (
                (status = 'pending' AND next_attempt_at <= NOW())
                OR (status = 'processing' AND locked_at < NOW() - make_interval(secs => %s))
            )
               {event_filter}
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED;
            """,
            params,
        )
        rows = cur.fetchall()
        if not rows:
            return []

        claimed_ids = [r["id"] for r in rows]
        new_lease_tokens = {e_id: uuid4() for e_id in claimed_ids}

        claimed_events: List[OutboxEvent] = []
        for e_id, lease_token in new_lease_tokens.items():
            cur.execute(
                """
                UPDATE outbox
                SET status = 'processing',
                    locked_at = NOW(),
                    locked_by = %s,
                    lease_token = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, event_type, payload, status, attempts, max_attempts,
                          next_attempt_at, locked_at, locked_by, lease_token,
                          last_error, created_at, updated_at, completed_at;
                """,
                (worker_id, lease_token, e_id),
            )
            updated_row = cur.fetchone()
            if updated_row:
                claimed_events.append(OutboxEvent(**updated_row))

        logger.info(
            "Worker '%s' claimed %d outbox event(s)", worker_id, len(claimed_events)
        )
        return claimed_events


def lock_event_lease(
    conn: psycopg.Connection,
    event_id: UUID,
    lease_token: UUID,
) -> bool:
    """Lock an event only while the supplied token owns its active processing lease."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM outbox
            WHERE id = %s AND status = 'processing' AND lease_token = %s
            FOR UPDATE;
            """,
            (event_id, lease_token),
        )
        return cur.fetchone() is not None


def mark_event_completed(
    conn: psycopg.Connection,
    event_id: UUID,
    lease_token: UUID,
) -> bool:
    """Mark an event as successfully completed using lease fencing.
    
    Returns True if updated, or False if lease was stolen/expired.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE outbox
            SET status = 'completed',
                completed_at = NOW(),
                updated_at = NOW(),
                locked_at = NULL,
                locked_by = NULL,
                lease_token = NULL
            WHERE id = %s AND status = 'processing' AND lease_token = %s;
            """,
            (event_id, lease_token),
        )
        success = cur.rowcount > 0
        if not success:
            logger.warning(
                "Stale worker lease rejected for event %s (lease token %s)",
                event_id,
                lease_token,
            )
        return success


def mark_event_failed(
    conn: psycopg.Connection,
    event_id: UUID,
    lease_token: UUID,
    error_message: str,
    max_retries: int = 5,
    backoff_base: float = 2.0,
    max_backoff: float = 60.0,
    permanent: bool = False,
) -> bool:
    """Record event failure with exponential backoff or terminal dead_letter state.
    
    Returns True if updated, or False if lease was stolen/expired.
    """
    clean_error = error_message[:1000] if error_message else "Unknown error"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT attempts, max_attempts
            FROM outbox
            WHERE id = %s AND status = 'processing' AND lease_token = %s
            FOR UPDATE;
            """,
            (event_id, lease_token),
        )
        row = cur.fetchone()
        if not row:
            logger.warning(
                "Cannot mark event %s failed: lease token %s is no longer valid",
                event_id,
                lease_token,
            )
            return False

        new_attempts = row["attempts"] + 1
        effective_max = row["max_attempts"] or max_retries

        if permanent or new_attempts >= effective_max:
            # Transition to terminal dead_letter state
            cur.execute(
                """
                UPDATE outbox
                SET status = 'dead_letter',
                    attempts = %s,
                    last_error = %s,
                    updated_at = NOW(),
                    locked_at = NULL,
                    locked_by = NULL,
                    lease_token = NULL
                WHERE id = %s AND status = 'processing' AND lease_token = %s;
                """,
                (new_attempts, clean_error, event_id, lease_token),
            )
            logger.error(
                "Event %s moved to dead_letter after %d attempts. Error: %s",
                event_id,
                new_attempts,
                clean_error,
            )
        else:
            # Calculate exponential backoff delay
            delay_seconds = min(max_backoff, (backoff_base ** new_attempts))
            cur.execute(
                """
                UPDATE outbox
                SET status = 'pending',
                    attempts = %s,
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s,
                    updated_at = NOW(),
                    locked_at = NULL,
                    locked_by = NULL,
                    lease_token = NULL
                WHERE id = %s AND status = 'processing' AND lease_token = %s;
                """,
                (new_attempts, delay_seconds, clean_error, event_id, lease_token),
            )
            logger.info(
                "Event %s retry #%d scheduled in %.1fs. Error: %s",
                event_id,
                new_attempts,
                delay_seconds,
                clean_error,
            )

        return cur.rowcount > 0


def retry_dead_letter_event(
    conn: psycopg.Connection,
    event_id: UUID,
) -> Optional[OutboxEvent]:
    """Administrative action: reset a dead_letter event to pending for immediate reprocessing."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE outbox
            SET status = 'pending',
                attempts = 0,
                next_attempt_at = NOW(),
                last_error = NULL,
                updated_at = NOW(),
                locked_at = NULL,
                locked_by = NULL,
                lease_token = NULL,
                completed_at = NULL
            WHERE id = %s AND status = 'dead_letter'
            RETURNING id, event_type, payload, status, attempts, max_attempts,
                      next_attempt_at, locked_at, locked_by, lease_token,
                      last_error, created_at, updated_at, completed_at;
            """,
            (event_id,),
        )
        row = cur.fetchone()
        return OutboxEvent(**row) if row else None


def get_outbox_event_by_id(
    conn: psycopg.Connection,
    event_id: UUID,
) -> Optional[OutboxEvent]:
    """Retrieve an outbox event record by its primary key."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, event_type, payload, status, attempts, max_attempts,
                   next_attempt_at, locked_at, locked_by, lease_token,
                   last_error, created_at, updated_at, completed_at
            FROM outbox
            WHERE id = %s;
            """,
            (event_id,),
        )
        row = cur.fetchone()
        return OutboxEvent(**row) if row else None

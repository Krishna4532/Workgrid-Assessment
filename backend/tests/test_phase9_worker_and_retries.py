import uuid
from unittest.mock import patch
import pytest

from app.db import get_db_connection
from app.notifications.dispatcher import PermanentEventError
from app.repositories.identity_repo import create_user
from app.repositories.notifications_repo import get_unread_count
from app.repositories.outbox_repo import (
    enqueue_outbox_event,
    get_outbox_event_by_id,
    mark_event_failed,
    retry_dead_letter_event,
)
from app.worker import OutboxWorker


# ============================================================================
# 1. Normal Outbox Event Processing and Notification Generation
# ============================================================================

def test_worker_processes_event_and_generates_notification():
    """Verify worker discovers, claims, completes events, and generates in-app notifications."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"wrk-{uuid.uuid4().hex[:8]}@example.com", display_name="Worker User")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Fix Pipeline Worker",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Team Lead",
                    "assignee_id": str(user.id),
                },
            )

    worker = OutboxWorker(worker_id="test-p9-worker", batch_size=10)
    processed = worker.run_once([event_id])
    assert processed >= 1

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None
        assert event.status == "completed"
        assert event.completed_at is not None

        # Verify notification was generated for recipient
        unread = get_unread_count(conn, user.id)
        assert unread == 1


# ============================================================================
# 2. Transient Failure and Exponential Backoff Retries
# ============================================================================

def test_worker_transient_failure_records_backoff_and_subsequent_success():
    """Verify transient error records failure with backoff schedule, and subsequent retry succeeds."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"wrk-trans-{uuid.uuid4().hex[:8]}@example.com", display_name="Trans User")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Transient Bug",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Lead",
                    "assignee_id": str(user.id),
                },
            )

    worker = OutboxWorker(worker_id="test-trans-worker", max_retries=5, backoff_base=1.0)

    # 1. Simulate transient failure on first execution attempt
    with patch("app.worker.dispatch_outbox_event", side_effect=ConnectionError("Transient connection drop")):
        worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "pending"
        assert event.attempts == 1
        assert "ConnectionError" in (event.last_error or "")
        assert event.next_attempt_at is not None

    # Reset next_attempt_at to past in database so worker can reclaim it immediately
    with get_db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE outbox SET next_attempt_at = NOW() - INTERVAL '1 second' WHERE id = %s;", (event_id,))

    # 2. Second attempt succeeds without patch
    worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "completed"
        assert event.completed_at is not None


# ============================================================================
# 3. Terminal Failure and Dead-Lettering after Max Retries
# ============================================================================

def test_worker_dead_letters_after_max_retries():
    """Verify event is transitioned to dead_letter state when attempts exceed max_retries."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Fatal Bug"},
            )
            # Simulate 4 prior failed attempts
            with conn.cursor() as cur:
                cur.execute("UPDATE outbox SET attempts = 4 WHERE id = %s;", (event_id,))

    worker = OutboxWorker(worker_id="test-dl-worker", max_retries=5)

    # 5th attempt fails
    with patch("app.worker.dispatch_outbox_event", side_effect=RuntimeError("Persistent crash")):
        worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "dead_letter"
        assert event.attempts == 5
        assert "RuntimeError" in (event.last_error or "")


# ============================================================================
# 4. Administrative Resurrection of Dead-Lettered Events
# ============================================================================

def test_dead_letter_administrative_resurrection():
    """Verify dead-lettered events can be resurrected via retry_dead_letter_event and reprocessed."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"resurrect-{uuid.uuid4().hex[:8]}@example.com", display_name="Resurrect User")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Resurrect Me",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Lead",
                    "assignee_id": str(user.id),
                },
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE outbox SET status = 'dead_letter', attempts = 5, last_error = 'Fatal' WHERE id = %s;",
                    (event_id,),
                )

    # 1. Administratively resurrect event
    with get_db_connection() as conn:
        with conn.transaction():
            resurrected = retry_dead_letter_event(conn, event_id)
            assert resurrected is not None
            assert resurrected.status == "pending"

        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "pending"
        assert event.attempts == 0

    # 2. Worker claims and completes the resurrected event
    worker = OutboxWorker(worker_id="test-resurrect-worker")
    worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "completed"


# ============================================================================
# 5. Non-Retryable Permanent Event Errors
# ============================================================================

def test_permanent_event_error_dead_letters_immediately():
    """Verify non-retryable PermanentEventError transitions to dead_letter on first attempt."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Malformed Event"},
            )

    worker = OutboxWorker(worker_id="test-permanent-worker", max_retries=5)

    with patch("app.worker.dispatch_outbox_event", side_effect=PermanentEventError("Schema mismatch in payload")):
        worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event.status == "dead_letter"
        assert event.attempts == 1
        assert "PermanentEventError" in (event.last_error or "")


# ============================================================================
# 6. Database-Level Notification Deduplication (dedupe_key)
# ============================================================================

def test_notification_deduplication_prevents_duplicate_deliveries():
    """Verify database unique constraint ON CONFLICT (dedupe_key) DO NOTHING prevents duplicate notifications."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"dedupe-{uuid.uuid4().hex[:8]}@example.com", display_name="Dedupe User")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Dedupe Work Item",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Lead",
                    "assignee_id": str(user.id),
                },
            )

    worker = OutboxWorker(worker_id="test-dedupe-worker")
    worker.run_once([event_id])

    with get_db_connection() as conn:
        assert get_unread_count(conn, user.id) == 1

        # Simulate replay or duplicate delivery of the exact same event
        event = get_outbox_event_by_id(conn, event_id)
        with conn.transaction():
            from app.notifications.dispatcher import dispatch_outbox_event
            dispatch_outbox_event(conn, event)

        # Still exactly 1 notification, 0 duplicate rows
        assert get_unread_count(conn, user.id) == 1

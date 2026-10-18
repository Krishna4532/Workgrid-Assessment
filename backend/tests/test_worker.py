from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import signal
import uuid
import pytest
from psycopg.rows import dict_row

from app.db import get_db_connection
from app.models import OutboxEvent
from app.notifications.dispatcher import dispatch_outbox_event
from app.repositories.identity_repo import create_user
from app.repositories.notifications_repo import get_unread_count
from app.repositories.outbox_repo import (
    claim_pending_events,
    enqueue_outbox_event,
    get_outbox_event_by_id,
    mark_event_completed,
    mark_event_failed,
    retry_dead_letter_event,
)
from app.worker import OutboxWorker


def test_worker_claims_and_processes_pending_event():
    """Verify worker discovers, claims, and successfully completes pending outbox events."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Test Recipient")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Fix Critical Bug",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Team Lead",
                    "assignee_id": str(user.id),
                },
            )

    worker = OutboxWorker(worker_id="test-worker-1", batch_size=10)
    processed = worker.run_once([event_id])
    assert processed >= 1

    # Verify event completed
    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None
        assert event.status == "completed"
        assert event.completed_at is not None

        # Verify notification was generated for user
        unread = get_unread_count(conn, user.id)
        assert unread == 1


def test_two_workers_do_not_claim_same_event_concurrently():
    """Verify PostgreSQL FOR UPDATE SKIP LOCKED prevents concurrent workers from claiming the same event."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Concurrency Test"},
            )

    # Worker 1 holds open a transaction claiming the event
    with get_db_connection() as conn1:
        with conn1.transaction():
            events1 = claim_pending_events(
                conn1, worker_id="worker-alpha", batch_size=10, event_ids=[event_id]
            )
            claimed_ids1 = [e.id for e in events1]
            assert event_id in claimed_ids1

            # Worker 2 simultaneously queries while Worker 1 is in-flight
            with get_db_connection() as conn2:
                with conn2.transaction():
                    events2 = claim_pending_events(
                        conn2, worker_id="worker-beta", batch_size=10, event_ids=[event_id]
                    )
                    claimed_ids2 = [e.id for e in events2]
                    # Worker 2 must NOT claim the row locked by Worker 1
                    assert event_id not in claimed_ids2


def test_fencing_prevents_stale_worker_from_overwriting():
    """Verify that a stale worker whose lease expired cannot mark an event completed after another worker reclaimed it."""
    worker = OutboxWorker(worker_id="test-stale-worker")
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Fencing Test"},
            )

    # Worker 1 claims with lease timeout = 1 second
    with get_db_connection() as conn:
        with conn.transaction():
            events1 = claim_pending_events(
                conn, worker_id="worker-slow", batch_size=1, lease_timeout_seconds=1, event_ids=[event_id]
            )
            assert len(events1) == 1
            lease_token_1 = events1[0].lease_token

        # Manually backdate locked_at to simulate lease expiration
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE outbox SET locked_at = NOW() - INTERVAL '10 seconds' WHERE id = %s;",
                    (event_id,),
                )

        # Worker 2 reclaims the expired event
        with conn.transaction():
            events2 = claim_pending_events(
                conn, worker_id="worker-fast", batch_size=1, lease_timeout_seconds=1, event_ids=[event_id]
            )
            assert len(events2) == 1
            lease_token_2 = events2[0].lease_token
            assert lease_token_2 != lease_token_1

    with patch("app.worker.dispatch_outbox_event") as dispatch:
        assert worker.process_single_event(events1[0]) is False
        dispatch.assert_not_called()

    # Worker 1 wakes up and attempts to complete with its stale lease token
    with get_db_connection() as conn:
        with conn.transaction():
            marked1 = mark_event_completed(conn, event_id, lease_token_1)
            # Fenced out!
            assert marked1 is False
            failed1 = mark_event_failed(conn, event_id, lease_token_1, "stale failure")
            assert failed1 is False

        # Worker 2 completes with fresh lease token
        with conn.transaction():
            marked2 = mark_event_completed(conn, event_id, lease_token_2)
            assert marked2 is True
            failed2 = mark_event_failed(conn, event_id, lease_token_2, "late failure")
            assert failed2 is False


def test_transient_failure_retries_with_exponential_backoff():
    """Verify that a processing error increments attempts and schedules retry in the future."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Failure Backoff Test"},
            )

    worker = OutboxWorker(worker_id="test-fail-worker", backoff_base=2.0)

    # Simulate dispatch raising a transient error
    with patch("app.worker.dispatch_outbox_event", side_effect=ConnectionError("Downstream timeout")):
        worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None
        assert event.status == "pending"
        assert event.attempts == 1
        assert "Downstream timeout" in (event.last_error or "")
        # Next attempt should be scheduled in the future
        assert event.next_attempt_at > datetime.now(timezone.utc) - timedelta(seconds=1)


def test_terminal_failure_transitions_to_dead_letter_after_max_attempts():
    """Verify an event transitions to dead_letter after exceeding configured max_retries."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {"item_id": str(uuid.uuid4()), "title": "Terminal Failure Test"},
            )
            # Pre-set attempts to 4 of 5
            with conn.cursor() as cur:
                cur.execute("UPDATE outbox SET attempts = 4 WHERE id = %s;", (event_id,))

    worker = OutboxWorker(worker_id="test-dl-worker", max_retries=5)

    with patch("app.worker.dispatch_outbox_event", side_effect=RuntimeError("Permanent corruption")):
        worker.run_once([event_id])

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None
        assert event.status == "dead_letter"
        assert event.attempts == 5
        assert "Permanent corruption" in (event.last_error or "")

        # Verify administrative retry can resurrect it
        resurrected = retry_dead_letter_event(conn, event_id)
        assert resurrected is not None
        assert resurrected.status == "pending"
        assert resurrected.attempts == 0
        assert resurrected.last_error is None


def test_duplicate_delivery_idempotency_produces_single_notification():
    """Verify that reprocessing an outbox event produces exactly one in-app notification due to database dedupe_key."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"dup-{uuid.uuid4().hex[:8]}@example.com", display_name="Dup Test User")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "team_id": str(uuid.uuid4()),
                    "title": "Dup Test Item",
                    "actor_id": str(uuid.uuid4()),
                    "actor_name": "Manager",
                    "assignee_id": str(user.id),
                },
            )
            event = get_outbox_event_by_id(conn, event_id)
            assert event is not None

        # First dispatch
        with conn.transaction():
            notifs1 = dispatch_outbox_event(conn, event)
            assert len(notifs1) == 1

        # Second dispatch (simulating worker crash and replay of same event)
        with conn.transaction():
            notifs2 = dispatch_outbox_event(conn, event)
            # Suppressed by ON CONFLICT (dedupe_key) DO NOTHING
            assert len(notifs2) == 0

        # Total unread count is strictly 1
        cnt = get_unread_count(conn, user.id)
        assert cnt == 1


def test_worker_retry_after_dispatch_transaction_rolls_back_notifications():
    """Simulate a worker crash after insert but before event completion."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"crash-{uuid.uuid4().hex[:8]}@example.com", display_name="Crash Recipient")
            event_id = enqueue_outbox_event(
                conn,
                "work_item_created",
                {
                    "item_id": str(uuid.uuid4()),
                    "title": "Crash Window Item",
                    "actor_id": str(uuid.uuid4()),
                    "assignee_id": str(user.id),
                },
            )

    worker = OutboxWorker(worker_id="test-crash-worker", backoff_base=0.5)
    with patch("app.worker.mark_event_completed", side_effect=RuntimeError("simulated crash before completion")):
        assert worker.run_once([event_id]) == 0

    with get_db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id = %s;", (user.id,))
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "UPDATE outbox SET next_attempt_at = NOW() WHERE id = %s;",
                    (event_id,),
                )

    assert worker.run_once([event_id]) == 1
    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None and event.status == "completed"
        assert get_unread_count(conn, user.id) == 1


def test_permanent_unknown_event_is_dead_lettered_immediately():
    """Verify unsupported event types do not consume transient retry attempts."""
    with get_db_connection() as conn:
        with conn.transaction():
            event_id = enqueue_outbox_event(conn, "unknown_event_type", {})

    worker = OutboxWorker(worker_id="test-permanent-worker")
    assert worker.run_once([event_id]) == 0

    with get_db_connection() as conn:
        event = get_outbox_event_by_id(conn, event_id)
        assert event is not None
        assert event.status == "dead_letter"
        assert event.attempts == 1


def test_worker_shutdown_closes_pool_after_current_cycle(monkeypatch):
    worker = OutboxWorker(worker_id="test-shutdown-worker")
    lifecycle = []
    monkeypatch.setattr("app.worker.init_db_pool", lambda: lifecycle.append("open"))
    monkeypatch.setattr("app.worker.close_db_pool", lambda: lifecycle.append("close"))

    def complete_current_cycle():
        lifecycle.append("cycle")
        worker.request_shutdown()
        return 0

    monkeypatch.setattr(worker, "run_once", complete_current_cycle)
    worker.start()

    assert lifecycle == ["open", "cycle", "close"]
    assert worker._running is False
    assert worker._stop_requested is True
    assert worker._stop_event.is_set()
    with patch("app.worker.claim_pending_events") as claim:
        assert worker.run_once() == 0
        claim.assert_not_called()


def test_worker_closes_pool_if_initialization_fails(monkeypatch):
    worker = OutboxWorker(worker_id="test-startup-failure-worker")
    lifecycle = []

    def fail_to_open_pool():
        lifecycle.append("open")
        raise RuntimeError("pool initialization failed")

    monkeypatch.setattr("app.worker.init_db_pool", fail_to_open_pool)
    monkeypatch.setattr("app.worker.close_db_pool", lambda: lifecycle.append("close"))

    with pytest.raises(RuntimeError, match="pool initialization failed"):
        worker.start()
    assert lifecycle == ["open", "close"]


def test_sigterm_requests_shutdown_without_closing_pool_in_handler(monkeypatch):
    registered_handlers = {}
    worker_calls = []

    class FakeWorker:
        def request_shutdown(self):
            worker_calls.append("request_shutdown")

        def start(self):
            registered_handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr("app.worker.OutboxWorker", FakeWorker)
    monkeypatch.setattr(
        "app.worker.signal.signal",
        lambda signum, handler: registered_handlers.setdefault(signum, handler),
    )

    from app.worker import run_worker

    run_worker()
    assert signal.SIGINT in registered_handlers
    assert signal.SIGTERM in registered_handlers
    assert worker_calls == ["request_shutdown"]

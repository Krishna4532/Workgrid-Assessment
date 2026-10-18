import logging
import os
import signal
import threading
from typing import Optional
from uuid import UUID, uuid4

from app.config import settings
from app.db import close_db_pool, get_db_connection, init_db_pool
from app.models import OutboxEvent
from app.notifications.dispatcher import PermanentEventError, dispatch_outbox_event
from app.repositories.outbox_repo import (
    claim_pending_events,
    lock_event_lease,
    mark_event_completed,
    mark_event_failed,
)

# Configure worker logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Worker %(process)d] %(name)s: %(message)s",
)
logger = logging.getLogger("workgrid.worker")


class _LeaseLostError(Exception):
    pass


class OutboxWorker:
    """Standalone, resilient outbox worker daemon.
    
    Processes transactional outbox events using bounded PostgreSQL connection pools,
    lease fencing, exponential backoff retries, and terminal dead-lettering.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        poll_interval: Optional[float] = None,
        batch_size: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
        max_backoff: Optional[float] = None,
        lease_timeout: Optional[int] = None,
    ):
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid4().hex[:6]}"
        self.poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL_SECONDS
        self.batch_size = batch_size or settings.WORKER_BATCH_SIZE
        self.max_retries = max_retries or settings.WORKER_MAX_RETRIES
        self.backoff_base = backoff_base or settings.WORKER_BACKOFF_BASE_SECONDS
        self.max_backoff = max_backoff or settings.WORKER_MAX_BACKOFF_SECONDS
        self.lease_timeout = lease_timeout or settings.WORKER_LEASE_TIMEOUT_SECONDS
        self._running = False
        self._stop_requested = False
        self._stop_event = threading.Event()

    def process_single_event(self, event: OutboxEvent) -> bool:
        """Process a single claimed outbox event within its own isolated transaction."""
        if not event.lease_token:
            logger.error("Event %s claimed without lease token. Skipping.", event.id)
            return False

        try:
            with get_db_connection() as conn:
                with conn.transaction():
                    if not lock_event_lease(conn, event.id, event.lease_token):
                        logger.warning(
                            "Lease lost before processing event %s (worker: %s)",
                            event.id,
                            self.worker_id,
                        )
                        return False

                    # 1. Dispatch notifications with database deduplication
                    dispatch_outbox_event(conn, event)

                    # 2. Mark completed with lease verification
                    marked = mark_event_completed(conn, event.id, event.lease_token)
                    if not marked:
                        raise _LeaseLostError(f"Lease lost while processing event {event.id}")

            logger.info("Successfully processed event %s (type: %s)", event.id, event.event_type)
            return True

        except _LeaseLostError as exc:
            logger.warning("%s (worker: %s)", exc, self.worker_id)
            return False
        except Exception as exc:
            logger.warning(
                "Error processing event %s (type: %s): %s",
                event.id,
                event.event_type,
                type(exc).__name__,
            )

            # Record failure in a separate clean transaction
            try:
                with get_db_connection() as fail_conn:
                    with fail_conn.transaction():
                        mark_event_failed(
                            conn=fail_conn,
                            event_id=event.id,
                            lease_token=event.lease_token,
                            error_message=f"{type(exc).__name__}: {str(exc)}",
                            max_retries=self.max_retries,
                            backoff_base=self.backoff_base,
                            max_backoff=self.max_backoff,
                            permanent=isinstance(exc, PermanentEventError),
                        )
            except Exception as fail_err:
                logger.error(
                    "Failed to record failure state for event %s: %s",
                    event.id,
                    fail_err,
                )

            return False

    def run_once(self, event_ids: Optional[list[UUID]] = None) -> int:
        """Execute a single polling and processing cycle. Returns number of events processed."""
        if self._stop_requested:
            return 0
        events = []
        try:
            # 1. Claim batch inside a short transaction and commit immediately
            with get_db_connection() as conn:
                with conn.transaction():
                    events = claim_pending_events(
                        conn=conn,
                        worker_id=self.worker_id,
                        batch_size=self.batch_size,
                        lease_timeout_seconds=self.lease_timeout,
                        event_ids=event_ids,
                    )
        except Exception as claim_err:
            logger.error("Error claiming outbox events: %s", claim_err)
            return 0

        if not events:
            return 0

        # 2. Process each claimed event independently
        processed_count = 0
        for event in events:
            if self._stop_requested:
                break
            if self.process_single_event(event):
                processed_count += 1

        return processed_count

    def start(self) -> None:
        """Start the worker processing loop until interrupted."""
        self._running = True
        logger.info(
            "WorkGrid Background Worker '%s' started (poll: %.1fs, batch: %d, max_retries: %d, lease: %ds)",
            self.worker_id,
            self.poll_interval,
            self.batch_size,
            self.max_retries,
            self.lease_timeout,
        )

        try:
            init_db_pool()
            while self._running and not self._stop_requested:
                processed = self.run_once()
                if processed == 0 and self._running and not self._stop_requested:
                    self._stop_event.wait(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Worker interrupted by user.")
        finally:
            self.stop()

    def stop(self) -> None:
        """Gracefully stop worker and release connection pool resources."""
        self.request_shutdown()
        logger.info("WorkGrid Background Worker '%s' shutting down...", self.worker_id)
        close_db_pool()
        logger.info("Worker shutdown complete.")

    def request_shutdown(self) -> None:
        """Stop the processing loop; pool cleanup remains in the worker's finally block."""
        self._running = False
        self._stop_requested = True
        self._stop_event.set()


def run_worker() -> None:
    """Entrypoint for running the worker from command line."""
    worker = OutboxWorker()

    def _signal_handler(sig, frame):
        logger.info("Received termination signal %s. Requesting graceful shutdown...", sig)
        worker.request_shutdown()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        # Signals might not be fully available on all Windows configurations
        pass

    worker.start()


if __name__ == "__main__":
    run_worker()

import asyncio
from collections import deque
import json
import logging
import sys
import threading
from typing import Any, Dict, List, Optional, Set
from uuid import UUID, uuid4
import psycopg

from app.config import settings

logger = logging.getLogger("workgrid.sse")


class SSEBroadcaster:
    """Manages Server-Sent Events subscribers and bridges PostgreSQL LISTEN/NOTIFY.
    
    Architecture & Safeguards:
    - Exactly ONE asynchronous connection listens to PostgreSQL channel 'workgrid_events'.
    - Bounded memory queues (maxsize=100) per subscriber prevent memory leaks from slow consumers.
    - An in-memory ring buffer of the last 100 events enables instantaneous replay for reconnecting
      clients presenting Last-Event-ID.
    - Zero connections consumed from the main HTTP connection pool.
    """

    def __init__(self, buffer_size: int = 100):
        self._subscribers: Set[asyncio.Queue] = set()
        self._recent_events: deque = deque(maxlen=buffer_size)
        self._running: bool = False
        self._listener_task: Optional[asyncio.Task] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Launch the background PostgreSQL listener task."""
        if not self._running:
            self._running = True
            if sys.platform == "win32":
                # On Windows, psycopg async requires SelectorEventLoop instead of ProactorEventLoop.
                # Run the LISTEN loop in a dedicated background thread with WindowsSelectorEventLoopPolicy.
                self._listen_thread = threading.Thread(
                    target=self._run_windows_listener,
                    name="WorkGrid-SSE-PG-Listener",
                    daemon=True,
                )
                self._listen_thread.start()
            else:
                self._listener_task = asyncio.create_task(self._listen_loop())
            logger.info("SSE Broadcaster started.")

    def _run_windows_listener(self) -> None:
        """Background thread worker for Windows running SelectorEventLoop."""
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_loop = loop
        self._listen_task = loop.create_task(self._listen_loop())
        try:
            loop.run_until_complete(self._listen_task)
        except (asyncio.CancelledError, Exception) as exc:
            logger.debug("Windows SSE listener thread finished: %s", exc)
        finally:
            try:
                # Cancel all remaining tasks and shutdown asyncgens cleanly before closing loop
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

    async def stop(self) -> None:
        """Gracefully stop the broadcaster and close subscriber queues."""
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._thread_loop and self._thread_loop.is_running():
            def _stop():
                task = getattr(self, "_listen_task", None)
                if task and not task.done():
                    task.cancel()
                aconn = getattr(self, "_active_aconn", None)
                if aconn:
                    try:
                        aconn.pgconn.finish()
                    except Exception:
                        pass
                self._thread_loop.stop()

            self._thread_loop.call_soon_threadsafe(_stop)
            if self._listen_thread and self._listen_thread.is_alive():
                self._listen_thread.join(timeout=2.0)
            self._thread_loop = None
            self._listen_thread = None

        # Clean up any remaining subscriber queues
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._subscribers.clear()
        logger.info("SSE Broadcaster stopped.")

    async def subscribe(self) -> asyncio.Queue:
        """Register a new SSE client with a bounded event queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        try:
            q._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            q._event_loop = None
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe_sync(self, q: asyncio.Queue) -> None:
        """Synchronously unregister an SSE client queue upon disconnect."""
        with self._lock:
            self._subscribers.discard(q)

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Unregister an SSE client queue upon disconnect."""
        self.unsubscribe_sync(q)

    def publish_local(self, event_data: Dict[str, Any]) -> None:
        """Dispatch an event to all local subscribers and store in replay ring buffer."""
        if not event_data.get("id"):
            event_data["id"] = str(uuid4())

        with self._lock:
            self._recent_events.append(event_data)
            subs = list(self._subscribers)

        # Distribute non-blocking to subscriber queues with thread-safety
        dead_queues = []
        for q in subs:
            try:
                def _put():
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait(event_data)

                loop = getattr(q, "_event_loop", None) or getattr(q, "_loop", None)
                if loop and loop.is_running():
                    try:
                        running_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        running_loop = None

                    if running_loop is loop:
                        _put()
                    else:
                        _put()
                        loop.call_soon_threadsafe(lambda: None)
                else:
                    _put()
            except Exception:
                dead_queues.append(q)

        with self._lock:
            for q in dead_queues:
                self._subscribers.discard(q)

    def get_events_after(self, last_event_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return events occurring after last_event_id from the replay buffer.
        
        Returns:
            List of events if last_event_id was found in buffer.
            None if last_event_id is unknown/stale (signals need for full sync).
        """
        found_index = -1
        events_list = list(self._recent_events)
        for i, ev in enumerate(events_list):
            if ev.get("id") == last_event_id:
                found_index = i
                break

        if found_index != -1:
            return events_list[found_index + 1 :]
        return None

    async def _listen_loop(self) -> None:
        """Dedicated connection loop listening to PostgreSQL LISTEN workgrid_events."""
        while self._running:
            try:
                # Open a dedicated async connection specifically for LISTEN
                async with await psycopg.AsyncConnection.connect(
                    settings.DATABASE_URL, autocommit=True
                ) as aconn:
                    await aconn.execute("LISTEN workgrid_events;")
                    logger.info("PostgreSQL LISTEN active on 'workgrid_events'")
                    async for notify in aconn.notifies():
                        if not self._running:
                            break
                        try:
                            data = json.loads(notify.payload)
                            self.publish_local(data)
                        except Exception as e:
                            logger.warning("Failed to parse notify payload: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "SSE listener connection error (%s): %s. Reconnecting in 2s...",
                    type(exc).__name__,
                    exc,
                )
                try:
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    break


# Global broadcaster singleton
broadcaster = SSEBroadcaster()


def emit_pg_notify(conn: psycopg.Connection, event_data: Dict[str, Any]) -> None:
    """Send PostgreSQL NOTIFY within the active transaction.
    
    PostgreSQL guarantees this notification is dispatched ONLY when the transaction
    successfully commits. If the transaction rolls back, PostgreSQL automatically
    discards the notification.
    """
    try:
        with conn.cursor() as cur:
            payload = json.dumps(event_data, default=str)
            cur.execute("SELECT pg_notify(%s, %s);", ("workgrid_events", payload))
    except Exception as exc:
        logger.warning("Failed to emit pg_notify: %s", exc)

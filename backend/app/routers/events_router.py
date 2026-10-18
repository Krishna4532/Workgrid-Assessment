import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional, Set
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from app.auth import get_current_user, get_current_user_for_sse
from app.db import get_db_connection
from app.models import User
from app.repositories.dashboard_repo import get_user_accessible_team_ids
from app.repositories.identity_repo import get_user_by_id
from app.sse import broadcaster
from app.tickets import create_sse_ticket

logger = logging.getLogger("workgrid.events_router")

router = APIRouter(tags=["Live Updates & SSE"])


def _categorize_event(event_type: str) -> str:
    """Map internal business event types to clean client-facing SSE categories."""
    if event_type.startswith("work_item_") or event_type in ("comment_added", "work_item_updated"):
        return "work_item"
    if event_type.startswith("role_request_"):
        return "approval"
    if event_type.startswith("notification_"):
        return "notification"
    if event_type == "role_assignment_changed":
        return "role_change"
    return "message"


def _is_event_authorized(
    event: Dict[str, Any],
    user_id: UUID,
    accessible_team_ids: Set[UUID],
) -> bool:
    """Enforce authorization and tenant/scope isolation before delivering an event to a user.
    
    Guarantees:
    - User notifications are delivered ONLY to the matching recipient_id.
    - Team-scoped work items are delivered ONLY to users with active coverage in that team.
    - Role assignment changes are delivered to affected users or covering leadership.
    - Unknown/unmatched scopes are never disclosed (defense-in-depth).
    """
    rec_id = event.get("recipient_id")
    if rec_id:
        return rec_id == str(user_id)

    evt_team = event.get("team_id")
    if evt_team:
        try:
            return UUID(evt_team) in accessible_team_ids
        except (ValueError, TypeError):
            return False

    evt_type = event.get("event_type", "")
    if evt_type == "role_assignment_changed":
        # Delivered if this user is the subject
        if event.get("user_id") == str(user_id):
            return True

    if evt_type.startswith("role_request_"):
        # Role request events with scope_id in accessible teams
        scope_id = event.get("scope_id")
        if scope_id:
            try:
                return UUID(scope_id) in accessible_team_ids
            except (ValueError, TypeError):
                return False

    return False


@router.post(
    "/me/events/ticket",
    summary="Issue Single-Use SSE Ticket",
    response_description="Short-lived single-use ticket for establishing an SSE connection without exposing bearer tokens in URLs",
)
def issue_sse_ticket(current_user: User = Depends(get_current_user)):
    """Issue a 60-second single-use ticket for EventSource connection (WG-03).
    
    Guarantees:
    - Requires authenticated session (Authorization: Bearer <token>).
    - Ticket is valid for 60 seconds and can be consumed at most once.
    - Eliminates reusable bearer tokens from URLs.
    """
    ticket = create_sse_ticket(current_user.id, ttl_seconds=60)
    return {"ticket": ticket, "expires_in": 60}


@router.get(
    "/me/events",
    summary="Subscribe to Real-Time Server-Sent Events",
    response_description="Continuous text/event-stream of authorized live updates",
)
@router.get(
    "/events",
    summary="Subscribe to Real-Time Server-Sent Events (Alias)",
    include_in_schema=False,
)
async def sse_events_stream(
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    current_user: User = Depends(get_current_user_for_sse),
    limit: Optional[int] = Query(None, description="Optional maximum events before closing stream (testing/bounded streaming)"),
):
    """Establish an authenticated, scope-isolated Server-Sent Events stream.
    
    Features & Guarantees:
    - Derives caller identity from Bearer token header or single-use SSE ticket (WG-03).
    - Checks authorization against live database assignments; never leaks out-of-scope resources.
    - Zero connection pool starvation: uses a single dedicated background listener for PostgreSQL LISTEN.
    - Memory-safe bounded client queues (maxsize=100) preventing resource leaks.
    - Keep-alive comments (: keep-alive) every 15 seconds to prevent proxy/browser timeout.
    - Supports Last-Event-ID replay from in-memory ring buffer for clean reconnects (WG-08).
    - Fail-closed authorization freshness: role revocation or refresh errors terminate the stream immediately (WG-04).
    - Immediate unregister on client disconnect or component unmount.
    """
    # 1. Load initial accessible team IDs directly from database
    with get_db_connection() as conn:
        accessible_team_ids = set(get_user_accessible_team_ids(conn, current_user.id))

    # 2. Register with broadcaster
    queue = await broadcaster.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal accessible_team_ids
        events_sent = 0
        try:
            # Send initial connected handshake
            handshake = json.dumps({"user_id": str(current_user.id), "status": "connected"})
            yield f"event: connected\ndata: {handshake}\n\n"
            events_sent += 1
            if limit is not None and events_sent >= limit:
                return

            # Replay missed events if client supplied Last-Event-ID
            if last_event_id:
                replayed = broadcaster.get_events_after(last_event_id)
                if replayed is not None:
                    for ev in replayed:
                        if _is_event_authorized(ev, current_user.id, accessible_team_ids):
                            category = _categorize_event(ev.get("event_type", ""))
                            ev_id = ev.get("id", "")
                            yield f"id: {ev_id}\nevent: {category}\ndata: {json.dumps(ev)}\n\n"
                            events_sent += 1
                            if limit is not None and events_sent >= limit:
                                return
                else:
                    # Client was disconnected too long; request full query sync
                    sync_payload = json.dumps({"action": "full_refresh", "reason": "reconnect_cursor_expired"})
                    yield f"event: sync\ndata: {sync_payload}\n\n"
                    events_sent += 1
                    if limit is not None and events_sent >= limit:
                        return

            # Continuous event loop
            while True:
                try:
                    # Wait up to 15s for next event or send keep-alive
                    event_data = await asyncio.wait_for(queue.get(), timeout=15.0)

                    if event_data is None:
                        # Broadcaster shutdown sentinel
                        break

                    # WG-04: Re-check and refresh accessible scopes on role changes
                    if event_data.get("event_type") == "role_assignment_changed":
                        if event_data.get("user_id") == str(current_user.id):
                            try:
                                with get_db_connection() as conn:
                                    fresh_user = get_user_by_id(conn, current_user.id)
                                    if not fresh_user or fresh_user.status != "active":
                                        logger.info("User %s account is %s; terminating SSE stream", current_user.id, fresh_user.status if fresh_user else "not found")
                                        break
                                    accessible_team_ids = set(get_user_accessible_team_ids(conn, current_user.id))
                            except Exception as e:
                                logger.error("Failed to refresh accessible teams for %s; failing closed: %s", current_user.id, e)
                                # Fail closed: close stream immediately rather than delivering potentially unauthorized events
                                break

                    # Enforce strict scope isolation
                    if _is_event_authorized(event_data, current_user.id, accessible_team_ids):
                        category = _categorize_event(event_data.get("event_type", ""))
                        ev_id = event_data.get("id", "")
                        payload_json = json.dumps(event_data)
                        yield f"id: {ev_id}\nevent: {category}\ndata: {payload_json}\n\n"
                        events_sent += 1
                        if limit is not None and events_sent >= limit:
                            return

                except asyncio.TimeoutError:
                    # Periodic keep-alive comment to prevent proxy or socket timeout
                    yield ": keep-alive\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.unsubscribe_sync(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

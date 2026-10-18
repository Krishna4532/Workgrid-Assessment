import logging
import secrets
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple
from uuid import UUID

logger = logging.getLogger("workgrid.tickets")

# Thread-safe in-memory store for single-use SSE tickets: ticket -> (user_id, expires_at)
_tickets_lock = threading.Lock()
_tickets: Dict[str, Tuple[UUID, datetime]] = {}


def create_sse_ticket(user_id: UUID, ttl_seconds: int = 60) -> str:
    """Generate a short-lived, single-use ticket for establishing an SSE connection.
    
    Guarantees:
    - Cryptographically strong random token (32 bytes urlsafe).
    - Bound to specific user_id.
    - Short TTL (default 60 seconds).
    - Single-use: consumed upon connection verification.
    """
    ticket = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    with _tickets_lock:
        # Prune expired tickets opportunistically
        expired_keys = [k for k, (_, exp) in _tickets.items() if exp <= now]
        for k in expired_keys:
            del _tickets[k]

        _tickets[ticket] = (user_id, expires_at)

    logger.debug("Issued single-use SSE ticket for user %s (valid for %ds)", user_id, ttl_seconds)
    return ticket


def consume_sse_ticket(ticket: str) -> Optional[UUID]:
    """Atomically validate and consume a single-use SSE ticket.
    
    Returns:
        UUID of authenticated user if ticket is valid and not expired.
        None if ticket is invalid, expired, or already consumed.
    """
    if not ticket:
        return None

    now = datetime.now(timezone.utc)
    with _tickets_lock:
        entry = _tickets.pop(ticket, None)

    if not entry:
        logger.warning("Attempted to consume invalid or already consumed SSE ticket")
        return None

    user_id, expires_at = entry
    if now > expires_at:
        logger.warning("Attempted to consume expired SSE ticket for user %s", user_id)
        return None

    return user_id

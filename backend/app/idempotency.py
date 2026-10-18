import hashlib
import json
import logging
from typing import Any, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("workgrid.idempotency")


def compute_payload_hash(payload: Any, operation: str = "") -> str:
    """Hash the operation and JSON payload so keys cannot alias other mutations."""
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def get_stored_idempotency_response(
    conn: psycopg.Connection,
    user_id: UUID,
    idempotency_key: str,
    current_hash: str,
) -> Optional[Tuple[int, str]]:
    """Serialize requests using a key, then check for a previously stored response.
    
    If found and hash matches, returns (status_code, response_body).
    If found and hash mismatches, raises HTTP 422 Unprocessable Entity.
    If not found, returns None.

    Must be called inside the same transaction as the mutation; the transaction-scoped
    advisory lock keeps concurrent first attempts from both executing the mutation.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));",
            (f"{user_id}:{idempotency_key}",),
        )
        cur.execute(
            """
            SELECT request_hash, status_code, response_body
            FROM idempotency_keys
            WHERE user_id = %s AND idempotency_key = %s;
            """,
            (user_id, idempotency_key),
        )
        row = cur.fetchone()
        if not row:
            return None

        if row["request_hash"] != current_hash:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Idempotency-Key was previously used with a different request payload",
            )

        return row["status_code"], row["response_body"]


def record_idempotency_response(
    conn: psycopg.Connection,
    user_id: UUID,
    idempotency_key: str,
    request_hash: str,
    status_code: int,
    response_body: str,
) -> None:
    """Record an idempotency key in the identical transaction as the business mutation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO idempotency_keys (user_id, idempotency_key, request_hash, status_code, response_body)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (user_id, idempotency_key, request_hash, status_code, response_body),
        )

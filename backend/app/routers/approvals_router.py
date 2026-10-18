import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.db import get_db_connection
from app.idempotency import (
    compute_payload_hash,
    get_stored_idempotency_response,
    record_idempotency_response,
)
from app.models import (
    ApprovalDecisionRequest,
    RoleRequest,
    RoleRequestCreate,
    User,
)
from app.repositories.approvals_repo import (
    approve_role_request,
    cancel_role_request,
    create_role_request,
    get_approvals_inbox,
    get_user_role_requests,
    reject_role_request,
    require_role_request_decision_authority,
)

logger = logging.getLogger("workgrid.approvals_router")

router = APIRouter(tags=["Role Requests & Approvals"])


@router.post(
    "/role-requests",
    summary="Submit Role Request",
    response_model=RoleRequest,
    status_code=status.HTTP_201_CREATED,
)
def submit_request(
    dto: RoleRequestCreate,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            req_hash = compute_payload_hash(dto.model_dump(), operation="role_requests.submit")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            req = create_role_request(conn, current_user.id, dto)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    201,
                    req.model_dump_json(),
                )

            return req


@router.get(
    "/me/role-requests",
    summary="List My Submitted Role Requests",
    response_model=List[RoleRequest],
)
def get_my_requests(current_user: User = Depends(get_current_user)):
    with get_db_connection() as conn:
        return get_user_role_requests(conn, current_user.id)


@router.post(
    "/role-requests/{id}/cancel",
    summary="Cancel Pending Role Request",
    response_model=RoleRequest,
)
def cancel_request(
    id: UUID,
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            return cancel_role_request(conn, id, current_user.id)


@router.get(
    "/approvals/inbox",
    summary="Approvals Inbox",
    response_model=List[RoleRequest],
)
def get_inbox(current_user: User = Depends(get_current_user)):
    with get_db_connection() as conn:
        return get_approvals_inbox(conn, current_user.id)


@router.post(
    "/role-requests/{id}/approve",
    summary="Approve Role Request",
    response_model=Dict[str, Any],
)
def approve_request(
    id: UUID,
    dto: Optional[ApprovalDecisionRequest] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            req_hash = compute_payload_hash(dto.model_dump() if dto else {}, operation=f"role_requests.approve:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    require_role_request_decision_authority(conn, id, current_user.id)
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            comment = (dto.comment or dto.reason) if dto else None
            req, assignment = approve_role_request(
                conn, id, current_user.id, current_user.display_name, comment
            )

            result = {
                "status": "approved",
                "request": req.model_dump(),
                "assignment": assignment.model_dump(),
            }

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    json.dumps(result, default=str),
                )

            return result


@router.post(
    "/role-requests/{id}/reject",
    summary="Reject Role Request",
    response_model=Dict[str, Any],
)
def reject_request(
    id: UUID,
    dto: ApprovalDecisionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            req_hash = compute_payload_hash(dto.model_dump(), operation=f"role_requests.reject:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    require_role_request_decision_authority(conn, id, current_user.id)
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            reason = dto.reason or dto.comment
            req = reject_role_request(conn, id, current_user.id, reason)

            result = {
                "status": "rejected",
                "request": req.model_dump(),
            }

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    json.dumps(result, default=str),
                )

            return result

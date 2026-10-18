import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.authorization import (
    evaluate_work_item_transition,
    require_team_access,
    validate_assignee_eligibility,
)
from app.db import get_db_connection
from app.idempotency import (
    compute_payload_hash,
    get_stored_idempotency_response,
    record_idempotency_response,
)
from app.models import (
    ActivityItem,
    User,
    WorkItem,
    WorkItemAssignRequest,
    WorkItemClaimRequest,
    WorkItemCommentRequest,
    WorkItemCreate,
    WorkItemKind,
    WorkItemPriority,
    WorkItemStatus,
    WorkItemTransitionRequest,
    WorkItemUpdate,
)
from app.repositories.roles_repo import get_active_positions
from app.repositories.work_items_repo import (
    add_comment_to_item,
    assign_work_item,
    claim_work_item,
    create_work_item,
    get_item_activity,
    get_items_summary_counts,
    get_work_item_by_id,
    list_work_items,
    transition_work_item,
    update_work_item_fields,
)

logger = logging.getLogger("workgrid.work_items_router")

router = APIRouter(tags=["Work Items"])


def _get_accessible_team_ids(conn, user_id: UUID) -> List[UUID]:
    """Retrieve all team IDs where the user has active membership/coverage."""
    positions = get_active_positions(conn, user_id)
    team_ids = set()
    with conn.cursor() as cur:
        for pos in positions:
            if pos.scope_type == "team":
                team_ids.add(pos.scope_id)
            elif pos.scope_type == "department":
                cur.execute("SELECT id FROM teams WHERE department_id = %s;", (pos.scope_id,))
                for row in cur.fetchall():
                    team_ids.add(row[0])
            elif pos.scope_type == "organization":
                cur.execute(
                    """
                    SELECT t.id
                    FROM teams t
                    JOIN departments d ON t.department_id = d.id
                    WHERE d.organization_id = %s;
                    """,
                    (pos.scope_id,),
                )
                for row in cur.fetchall():
                    team_ids.add(row[0])
    return list(team_ids)


@router.post(
    "/work-items",
    summary="Create Work Item",
    response_model=WorkItem,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/items",
    include_in_schema=False,
    response_model=WorkItem,
    status_code=status.HTTP_201_CREATED,
)
def create_item_endpoint(
    dto: WorkItemCreate,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            # Check idempotency
            req_hash = compute_payload_hash(dto.model_dump(), operation="work_items.create")

            # Authorization is rechecked before returning any cached response.
            require_team_access(conn, current_user.id, dto.team_id, min_rank=1, resource_description="Team")

            if dto.assignee_id:
                validate_assignee_eligibility(conn, dto.assignee_id, dto.team_id)

            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            item = create_work_item(conn, current_user.id, current_user.display_name, dto)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    201,
                    item.model_dump_json(),
                )

            return item


@router.get(
    "/work-items",
    summary="List Accessible Work Items",
    response_model=Dict[str, Any],
)
@router.get(
    "/items",
    include_in_schema=False,
    response_model=Dict[str, Any],
)
def list_items_endpoint(
    team_id: Optional[UUID] = Query(None),
    view: str = Query("all"),
    status: Optional[WorkItemStatus] = Query(None),
    priority: Optional[WorkItemPriority] = Query(None),
    kind: Optional[WorkItemKind] = Query(None),
    assignee: Optional[UUID] = Query(None),
    q: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        accessible_teams = _get_accessible_team_ids(conn, current_user.id)
        if team_id:
            if team_id not in accessible_teams:
                # Information hiding (404)
                raise HTTPException(status_code=404, detail="Team not found or not accessible")
            target_teams = [team_id]
        else:
            target_teams = accessible_teams

        items, next_cursor, has_more = list_work_items(
            conn=conn,
            team_ids=target_teams,
            view=view,
            user_id=current_user.id,
            status_filter=status,
            priority_filter=priority,
            kind_filter=kind,
            assignee_id=assignee,
            q=q,
            cursor=cursor,
            limit=limit,
        )
        return {
            "items": [i.model_dump() for i in items],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }


@router.get(
    "/work-items/summary",
    summary="Work Items Summary Counts",
)
@router.get(
    "/items/summary",
    include_in_schema=False,
)
def get_summary_endpoint(
    team_id: Optional[UUID] = Query(None),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        accessible_teams = _get_accessible_team_ids(conn, current_user.id)
        if team_id:
            if team_id not in accessible_teams:
                raise HTTPException(status_code=404, detail="Team not found")
            target_teams = [team_id]
        else:
            target_teams = accessible_teams
        return get_items_summary_counts(conn, target_teams)


@router.get(
    "/work-items/{id}",
    summary="Retrieve Work Item",
    response_model=WorkItem,
)
@router.get(
    "/items/{id}",
    include_in_schema=False,
    response_model=WorkItem,
)
def get_item_endpoint(
    id: UUID,
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        item = get_work_item_by_id(conn, id)
        if not item:
            raise HTTPException(status_code=404, detail="Work item not found")

        # Information hiding: Non-members get 404
        require_team_access(conn, current_user.id, item.team_id, min_rank=0, resource_description="Work item")
        return item


@router.patch(
    "/work-items/{id}",
    summary="Update Work Item Fields",
    response_model=WorkItem,
)
@router.patch(
    "/items/{id}",
    include_in_schema=False,
    response_model=WorkItem,
)
def update_item_endpoint(
    id: UUID,
    dto: WorkItemUpdate,
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            item = get_work_item_by_id(conn, id)
            if not item:
                raise HTTPException(status_code=404, detail="Work item not found")

            # Must have at least rank 1 (member) in the team
            effective = require_team_access(conn, current_user.id, item.team_id, min_rank=1, resource_description="Work item")

            # Members can edit items they created or own. Managers (rank >= 2) can edit any item.
            if effective["role_rank"] < 2 and item.creator_id != current_user.id and item.assignee_id != current_user.id:
                raise HTTPException(
                    status_code=403,
                    detail="Members may only edit items they created or own",
                )

            return update_work_item_fields(conn, id, current_user.id, dto)


@router.post(
    "/work-items/{id}/claim",
    summary="Atomic Work Item Claim",
    response_model=WorkItem,
)
@router.post(
    "/items/{id}/claim",
    include_in_schema=False,
    response_model=WorkItem,
)
def claim_item_endpoint(
    id: UUID,
    dto: Optional[WorkItemClaimRequest] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            item = get_work_item_by_id(conn, id)
            if not item:
                raise HTTPException(status_code=404, detail="Work item not found")

            require_team_access(conn, current_user.id, item.team_id, min_rank=1, resource_description="Work item")

            req_hash = compute_payload_hash(dto.model_dump() if dto else {}, operation=f"work_items.claim:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            claimed = claim_work_item(conn, id, current_user.id, current_user.display_name)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    claimed.model_dump_json(),
                )

            return claimed


@router.post(
    "/work-items/{id}/assign",
    summary="Assign Work Item",
    response_model=WorkItem,
)
@router.post(
    "/items/{id}/assign",
    include_in_schema=False,
    response_model=WorkItem,
)
def assign_item_endpoint(
    id: UUID,
    dto: WorkItemAssignRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            item = get_work_item_by_id(conn, id)
            if not item:
                raise HTTPException(status_code=404, detail="Work item not found")

            # Assigning work requires team_leader or higher (rank >= 2)
            require_team_access(conn, current_user.id, item.team_id, min_rank=2, resource_description="Work item")

            if dto.assignee_id:
                validate_assignee_eligibility(conn, dto.assignee_id, item.team_id)

            req_hash = compute_payload_hash(dto.model_dump(), operation=f"work_items.assign:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            updated = assign_work_item(conn, id, current_user.id, dto.assignee_id, dto.version, dto.reason)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    updated.model_dump_json(),
                )

            return updated


@router.post(
    "/work-items/{id}/transition",
    summary="Transition Work Item Status",
    response_model=WorkItem,
)
@router.post(
    "/items/{id}/transition",
    include_in_schema=False,
    response_model=WorkItem,
)
def transition_item_endpoint(
    id: UUID,
    dto: WorkItemTransitionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            item = get_work_item_by_id(conn, id)
            if not item:
                raise HTTPException(status_code=404, detail="Work item not found")

            effective = require_team_access(conn, current_user.id, item.team_id, min_rank=1, resource_description="Work item")

            req_hash = compute_payload_hash(dto.model_dump(), operation=f"work_items.transition:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            # OCC Check: verify version before workflow rule evaluation
            if item.version != dto.version:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Item was modified concurrently (current version: {item.version}, expected: {dto.version})",
                )

            # Validate transition workflow rules
            valid, allowed, err_code, err_msg = evaluate_work_item_transition(
                item=item,
                caller_id=current_user.id,
                caller_rank=effective["role_rank"],
                to_status=dto.to_status,
                reason=dto.reason,
            )
            if not valid:
                raise HTTPException(status_code=err_code or 422, detail=err_msg)

            updated = transition_work_item(conn, id, current_user.id, dto.to_status, dto.version, dto.reason)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    updated.model_dump_json(),
                )

            return updated


@router.post(
    "/work-items/{id}/comments",
    summary="Add Comment to Work Item",
    response_model=ActivityItem,
)
@router.post(
    "/items/{id}/comments",
    include_in_schema=False,
    response_model=ActivityItem,
)
def add_comment_endpoint(
    id: UUID,
    dto: WorkItemCommentRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.transaction():
            item = get_work_item_by_id(conn, id)
            if not item:
                raise HTTPException(status_code=404, detail="Work item not found")

            require_team_access(conn, current_user.id, item.team_id, min_rank=1, resource_description="Work item")

            req_hash = compute_payload_hash(dto.model_dump(), operation=f"work_items.comment:{id}")
            if idempotency_key:
                stored = get_stored_idempotency_response(conn, current_user.id, idempotency_key, req_hash)
                if stored:
                    return JSONResponse(status_code=stored[0], content=json.loads(stored[1]))

            act = add_comment_to_item(conn, id, current_user.id, current_user.display_name, dto.comment)

            if idempotency_key:
                record_idempotency_response(
                    conn,
                    current_user.id,
                    idempotency_key,
                    req_hash,
                    200,
                    act.model_dump_json(),
                )

            return act


@router.get(
    "/work-items/{id}/activity",
    summary="Get Work Item Activity Timeline",
    response_model=List[ActivityItem],
)
@router.get(
    "/items/{id}/activity",
    include_in_schema=False,
    response_model=List[ActivityItem],
)
def get_activity_endpoint(
    id: UUID,
    current_user: User = Depends(get_current_user),
):
    with get_db_connection() as conn:
        item = get_work_item_by_id(conn, id)
        if not item:
            raise HTTPException(status_code=404, detail="Work item not found")

        # Viewers (rank >= 0) can read activity
        require_team_access(conn, current_user.id, item.team_id, min_rank=0, resource_description="Work item")
        return get_item_activity(conn, id)

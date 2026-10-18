import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query, status

from app.auth import get_current_user
from app.db import get_db_connection
from app.models import (
    DashboardResponse,
    OwnerDashboardResponse,
    TeamMembersListResponse,
    TodoListResponse,
    User,
)
from app.repositories.dashboard_repo import (
    get_dashboard_data,
    get_owner_dashboard_data,
    get_team_members,
    get_unified_todo,
)

logger = logging.getLogger("workgrid.dashboard_router")

router = APIRouter(tags=["Dashboard & Workspace"])


@router.get(
    "/me/dashboard",
    summary="Get Current User Dashboard Overview",
    response_model=DashboardResponse,
    response_description="Aggregated dashboard metrics, active positions, greeting, and summary counts",
)
def get_dashboard_overview(current_user: User = Depends(get_current_user)):
    """Retrieve operational dashboard overview for the authenticated user.
    
    Guarantees:
    - Derives identity exclusively from authenticated JWT session.
    - Counts only records within caller's authorized scope.
    - Open work items exclude resolved/closed items.
    - Missing due dates are never counted as overdue.
    - Efficient SQL aggregates without N+1 queries.
    """
    with get_db_connection() as conn:
        return get_dashboard_data(conn, current_user)


@router.get(
    "/me/owner-dashboard",
    summary="Get Organization Owner Dashboard Overview",
    response_model=OwnerDashboardResponse,
    response_description="Organization-wide metrics, team-level breakdown, and attention items",
)
@router.get(
    "/owner/overview",
    include_in_schema=False,
    response_model=OwnerDashboardResponse,
)
def get_owner_dashboard_endpoint(current_user: User = Depends(get_current_user)):
    """Retrieve comprehensive organization-wide operational dashboard for Organization Owner.
    
    Guarantees:
    - Strictly scoped to the authenticated user's authorized organization.
    - Requires active owner or organization_head role (403 if unauthorized).
    - Team-level visibility across all teams in the organization.
    - Consolidates work progress, blocked work, and overdue tasks.
    """
    with get_db_connection() as conn:
        return get_owner_dashboard_data(conn, current_user)


@router.get(
    "/me/todo",
    summary="Get Unified Actionable To-Do List",
    response_model=TodoListResponse,
    response_description="Consolidated actionable items: assigned work, eligible approvals, and role requests",
)
def get_todo_list(
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    limit: int = Query(20, ge=1, le=100, description="Page limit"),
    current_user: User = Depends(get_current_user),
):
    """Retrieve consolidated actionable queue for the caller.
    
    Guarantees:
    - Includes assigned open/in-progress work items.
    - Includes work items awaiting approval where caller is eligible manager (excluding self-approval).
    - Includes pending role requests where caller has decider authority (excluding self-approval).
    - Excludes completed or non-actionable items.
    - Server-side deterministic ordering: overdue first, priority, due date, created date, item ID.
    - Validated cursor pagination.
    """
    with get_db_connection() as conn:
        items, total_count, overdue_count, next_cursor, has_more = get_unified_todo(
            conn=conn,
            user=current_user,
            cursor=cursor,
            limit=limit,
        )
        return TodoListResponse(
            items=items,
            total_count=total_count,
            overdue_count=overdue_count,
            next_cursor=next_cursor,
            has_more=has_more,
        )


@router.get(
    "/teams/{team_id}/members",
    summary="List Active Team Members",
    response_model=TeamMembersListResponse,
    response_description="Members with active effective roles in the specified team",
)
def get_team_members_endpoint(
    team_id: UUID,
    current_user: User = Depends(get_current_user),
):
    """Retrieve active members and their effective roles within a team.
    
    Guarantees:
    - Requires caller to have active access to the team (Rule #9: Non-disclosure 404 for non-members).
    - Excludes suspended and deactivated accounts.
    """
    with get_db_connection() as conn:
        members = get_team_members(conn, team_id, current_user.id)
        return TeamMembersListResponse(team_id=team_id, members=members)

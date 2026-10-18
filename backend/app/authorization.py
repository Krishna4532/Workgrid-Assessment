import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status
import psycopg

from app.models import WorkItem, WorkItemStatus
from app.repositories.roles_repo import get_effective_role

logger = logging.getLogger("workgrid.authz")

ROLE_RANKS = {
    "viewer": 0,
    "member": 1,
    "team_leader": 2,
    "department_manager": 3,
    "organization_head": 4,
    "owner": 5,
}


def get_user_effective_role_in_team(
    conn: psycopg.Connection, user_id: UUID, team_id: UUID
) -> Optional[Dict[str, Any]]:
    """Retrieve user's active effective role for a given team from the effective_roles view."""
    return get_effective_role(conn, user_id, team_id)


def require_team_access(
    conn: psycopg.Connection,
    user_id: UUID,
    team_id: UUID,
    min_rank: int = 0,
    resource_description: str = "Resource",
) -> Dict[str, Any]:
    """Verify user has access to a team-scoped resource.
    
    Enforces Rule #9 from the Constitution:
    'What lies outside a person's scope is invisible to them: 404, never 403.'
    """
    effective = get_user_effective_role_in_team(conn, user_id, team_id)
    if not effective:
        # Information hiding: Non-members do not learn that the resource exists
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_description} not found",
        )

    user_rank = effective["role_rank"]
    if user_rank < min_rank:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Action requires minimum rank {min_rank}, but user has rank {user_rank} ({effective['role']})",
        )

    return effective


def validate_assignee_eligibility(
    conn: psycopg.Connection, assignee_id: UUID, team_id: UUID
) -> None:
    """Ensure the prospective assignee holds an active covering role of at least 'member' (rank >= 1)."""
    effective = get_user_effective_role_in_team(conn, assignee_id, team_id)
    if not effective or effective["role_rank"] < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Assignee is not an active member or manager in this team's scope",
        )


def evaluate_work_item_transition(
    item: WorkItem,
    caller_id: UUID,
    caller_rank: int,
    to_status: WorkItemStatus,
    reason: Optional[str] = None,
) -> Tuple[bool, List[str], Optional[int], Optional[str]]:
    """Validate a work item status transition according to Constitution §7 workflow rules.
    
    Returns:
        (is_valid, list_of_allowed_moves, http_error_code, error_message)
    """
    from_status = item.status
    is_assignee = item.assignee_id == caller_id
    is_creator = item.creator_id == caller_id
    is_manager = caller_rank >= 2

    allowed_moves: List[str] = []

    # Calculate allowed moves from current status
    if from_status == "open":
        if is_manager:
            allowed_moves.append("closed")
        if caller_rank >= 1:
            allowed_moves.append("in_progress")

    elif from_status == "in_progress":
        if is_assignee or is_manager:
            allowed_moves.append("open")
        if item.requires_approval:
            if is_assignee or is_creator or is_manager:
                allowed_moves.append("awaiting_approval")
        else:
            if is_assignee or is_creator or is_manager:
                allowed_moves.append("resolved")

    elif from_status == "awaiting_approval":
        # Rule #6: Nobody decides their own request or approves an item they own.
        if is_manager and not is_assignee and not is_creator:
            allowed_moves.append("resolved")
            allowed_moves.append("in_progress")

    elif from_status == "resolved":
        if is_assignee or is_manager:
            allowed_moves.append("closed")
            allowed_moves.append("in_progress")

    elif from_status == "closed":
        # Closed is final per Constitution §7
        pass

    # Verify if destination is reachable in the workflow
    if to_status not in allowed_moves:
        # Check if the move is fundamentally illegal for the current status (422)
        # or if the user specifically lacks the role/ownership authority (403)
        if from_status == "closed":
            return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, "Closed is final and cannot be transitioned"
        
        # Self-approval check on awaiting_approval (Rule #6: Nobody decides their own request or approves an item they own)
        if from_status == "awaiting_approval" and (is_assignee or is_creator) and to_status in ("resolved", "in_progress"):
            return False, allowed_moves, status.HTTP_403_FORBIDDEN, "Creators and assignees cannot approve or reject their own work item"

        if to_status == "closed" and from_status == "open" and not is_manager:
            return False, allowed_moves, status.HTTP_403_FORBIDDEN, "Only managers can close an open item directly"

        return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, f"Illegal transition from '{from_status}' to '{to_status}'. Allowed moves: {allowed_moves}"

    # Verify reason requirements
    reason_clean = (reason or "").strip()
    if to_status == "closed" and from_status in ("open", "resolved") and not reason_clean and from_status == "open":
        return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason is required to close an open item directly"

    if from_status == "in_progress" and to_status == "open" and not reason_clean:
        return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason is required to release an in-progress item back to open"

    if from_status == "awaiting_approval" and to_status == "in_progress" and not reason_clean:
        return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason is required to reject an item back to in-progress"

    if from_status == "resolved" and to_status == "in_progress" and not reason_clean:
        return False, allowed_moves, status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason is required to reopen a resolved item"

    return True, allowed_moves, None, None

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status
import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.models import RoleAssignment, RoleRequest, RoleRequestCreate, RoleType, ScopeType
from app.repositories.roles_repo import (
    _build_scope_fks,
    grant_or_update_role,
)
from app.repositories.outbox_repo import enqueue_outbox_event

logger = logging.getLogger("workgrid.approvals")

ROLE_RANKS = {
    "viewer": 0,
    "member": 1,
    "team_leader": 2,
    "department_manager": 3,
    "organization_head": 4,
    "owner": 5,
}


def get_approval_policy(
    conn: psycopg.Connection, requested_role: RoleType, scope_type: ScopeType
) -> Optional[Dict[str, Any]]:
    """Lookup decider requirement from approval_policies table."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT requested_role, scope_type, decider_role, decider_scope_type
            FROM approval_policies
            WHERE requested_role = %s AND scope_type = %s;
            """,
            (requested_role, scope_type),
        )
        return cur.fetchone()


def check_decider_authority(
    conn: psycopg.Connection,
    decider_id: UUID,
    request_row: Dict[str, Any],
) -> bool:
    """Verify decider holds active, covering role of sufficient rank at decision time.
    
    Rule #6: Nobody decides their own request.
    Authority is checked dynamically from active assignments, never from tokens or cached state.
    """
    if decider_id == request_row["user_id"]:
        return False

    decider_assignments = _get_scope_covering_assignments(conn, decider_id, request_row)
    if not decider_assignments:
        return False

    policy = get_approval_policy(conn, request_row["requested_role"], request_row["scope_type"])
    if not policy:
        # If no explicit policy row exists, fallback authority is owner of the organization
        min_rank = 5
    else:
        min_rank = ROLE_RANKS.get(policy["decider_role"], 5)


    max_decider_rank = max(ROLE_RANKS.get(a["role"], -1) for a in decider_assignments)
    return max_decider_rank >= min_rank or max_decider_rank >= 5


def _get_scope_covering_assignments(
    conn: psycopg.Connection,
    user_id: UUID,
    request_row: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return active assignments covering the scope node of a role request."""
    req_scope_type = request_row["scope_type"]
    req_org_id = request_row["organization_id"]
    req_dept_id = request_row["department_id"]
    req_team_id = request_row["team_id"]

    with conn.cursor(row_factory=dict_row) as cur:
        # Determine organization_id for fallback check
        if req_scope_type == "team":
            cur.execute(
                """
                SELECT d.organization_id, t.department_id
                FROM teams t
                JOIN departments d ON t.department_id = d.id
                WHERE t.id = %s;
                """,
                (req_team_id,),
            )
            meta = cur.fetchone()
            org_id = meta["organization_id"] if meta else None
            dept_id = meta["department_id"] if meta else None
        elif req_scope_type == "department":
            cur.execute("SELECT organization_id FROM departments WHERE id = %s;", (req_dept_id,))
            meta = cur.fetchone()
            org_id = meta["organization_id"] if meta else None
            dept_id = req_dept_id
        else:
            org_id = req_org_id
            dept_id = None

        if not org_id:
            return []

        # Query all active assignments held by the user that cover the target scope
        cur.execute(
            """
            SELECT role, scope_type, organization_id, department_id, team_id
            FROM role_assignments
            WHERE user_id = %s AND status = 'active'
              AND (
                  (scope_type = 'organization' AND organization_id = %s)
                  OR (scope_type = 'department' AND department_id = %s)
                  OR (scope_type = 'team' AND team_id = %s)
              );
            """,
            (user_id, org_id, dept_id, req_team_id),
        )
        return cur.fetchall()


def require_request_decision_authority(
    conn: psycopg.Connection,
    decider_id: UUID,
    request_row: Dict[str, Any],
) -> None:
    """Authorize a decision while hiding requests outside the caller's scope."""
    if request_row["user_id"] == decider_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nobody can decide their own request",
        )
    assignments = _get_scope_covering_assignments(conn, decider_id, request_row)
    if not assignments:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")

    policy = get_approval_policy(conn, request_row["requested_role"], request_row["scope_type"])
    min_rank = ROLE_RANKS.get(policy["decider_role"], 5) if policy else 5
    max_rank = max(ROLE_RANKS.get(a["role"], -1) for a in assignments)
    if max_rank < min_rank and max_rank < 5:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Caller does not have sufficient authority to decide this request",
        )


def require_role_request_decision_authority(
    conn: psycopg.Connection,
    request_id: UUID,
    decider_id: UUID,
) -> None:
    """Revalidate current authority before replaying a cached decision response."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM role_requests WHERE id = %s;", (request_id,))
        request_row = cur.fetchone()

    if not request_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")
    require_request_decision_authority(conn, decider_id, request_row)


def create_role_request(
    conn: psycopg.Connection,
    user_id: UUID,
    dto: RoleRequestCreate,
) -> RoleRequest:
    """Submit a new role request with unique pending constraint handling.
    
    Security & Boundary Invariants (WG-05):
    - Validates target scope existence.
    - Unassigned users (0 active roles) may only request entry-level roles ('viewer' or 'member').
    - Assigned users may not request roles outside their authorized organization boundary.
    """
    org_id, dept_id, team_id = _build_scope_fks(dto.scope_type, dto.scope_id)

    with conn.cursor() as cur:
        # 1. Verify target scope existence and resolve target organization
        target_org_id = None
        if dto.scope_type == "organization":
            cur.execute("SELECT id FROM organizations WHERE id = %s;", (dto.scope_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target organization not found")
            target_org_id = row[0]
        elif dto.scope_type == "department":
            cur.execute("SELECT organization_id FROM departments WHERE id = %s;", (dto.scope_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target department not found")
            target_org_id = row[0]
        elif dto.scope_type == "team":
            cur.execute(
                """
                SELECT d.organization_id
                FROM teams t
                JOIN departments d ON t.department_id = d.id
                WHERE t.id = %s;
                """,
                (dto.scope_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target team not found")
            target_org_id = row[0]

        # 2. Check active role assignments for caller
        cur.execute(
            """
            SELECT COUNT(*) AS cnt,
                   ARRAY_AGG(DISTINCT COALESCE(
                       ra.organization_id,
                       d.organization_id,
                       d2.organization_id
                   )) FILTER (WHERE ra.status = 'active') AS assigned_org_ids
            FROM role_assignments ra
            LEFT JOIN departments d ON ra.department_id = d.id
            LEFT JOIN teams t ON ra.team_id = t.id
            LEFT JOIN departments d2 ON t.department_id = d2.id
            WHERE ra.user_id = %s AND ra.status = 'active';
            """,
            (user_id,),
        )
        row = cur.fetchone()
        active_cnt = row[0] or 0
        assigned_orgs = set(row[1] or [])

        if active_cnt == 0:
            # Unassigned onboarding user: only entry-level roles allowed
            if dto.requested_role not in ("viewer", "member"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Unassigned users may only request entry-level roles ('viewer' or 'member'), not '{dto.requested_role}'",
                )
        else:
            # Assigned user: cannot request cross-organization access outside assigned organizations
            if target_org_id not in assigned_orgs:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot request roles outside your assigned organization",
                )

        # Check if user already holds the exact requested role actively at this scope
        with conn.cursor(row_factory=dict_row) as check_cur:
            if dto.scope_type == "team":
                check_cur.execute(
                    "SELECT role FROM role_assignments WHERE user_id = %s AND team_id = %s AND status = 'active';",
                    (user_id, team_id),
                )
            elif dto.scope_type == "department":
                check_cur.execute(
                    "SELECT role FROM role_assignments WHERE user_id = %s AND department_id = %s AND status = 'active';",
                    (user_id, dept_id),
                )
            else:
                check_cur.execute(
                    "SELECT role FROM role_assignments WHERE user_id = %s AND organization_id = %s AND status = 'active';",
                    (user_id, org_id),
                )
            existing_active = check_cur.fetchone()
            if existing_active and existing_active["role"] == dto.requested_role:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"You already hold the active '{dto.requested_role}' position in this {dto.scope_type}.",
                )

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO role_requests (
                    user_id, requested_role, scope_type, organization_id,
                    department_id, team_id, reason, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING id, user_id, requested_role, scope_type, organization_id,
                          department_id, team_id, reason, status, created_at, updated_at;
                """,
                (
                    user_id,
                    dto.requested_role,
                    dto.scope_type,
                    org_id,
                    dept_id,
                    team_id,
                    dto.reason,
                ),
            )
            row = cur.fetchone()
            scope_id = row["organization_id"] or row["department_id"] or row["team_id"]
            req_item = RoleRequest(
                id=row["id"],
                user_id=row["user_id"],
                requested_role=row["requested_role"],
                scope_type=row["scope_type"],
                scope_id=scope_id,
                reason=row["reason"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            enqueue_outbox_event(
                conn,
                "role_request_submitted",
                {
                    "request_id": str(req_item.id),
                    "user_id": str(req_item.user_id),
                    "actor_id": str(req_item.user_id),
                    "requested_role": req_item.requested_role,
                    "scope_type": req_item.scope_type,
                    "scope_id": str(req_item.scope_id),
                    "reason": req_item.reason,
                },
            )
            return req_item
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A pending request already exists for this user, scope, and requested role",
        )


def cancel_role_request(
    conn: psycopg.Connection, request_id: UUID, caller_id: UUID
) -> RoleRequest:
    """Cancel a pending role request. Only the requester may cancel, and only while pending."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM role_requests WHERE id = %s FOR UPDATE;", (request_id,))
        req = cur.fetchone()
        if not req:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")

        if req["user_id"] != caller_id:
            if not _get_scope_covering_assignments(conn, caller_id, req):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the requester can cancel their pending request",
            )

        if req["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel request in '{req['status']}' state",
            )

        cur.execute(
            """
            UPDATE role_requests
            SET status = 'cancelled', updated_at = NOW()
            WHERE id = %s
            RETURNING id, user_id, requested_role, scope_type, organization_id,
                      department_id, team_id, reason, status, created_at, updated_at;
            """,
            (request_id,),
        )
        updated = cur.fetchone()
        scope_id = updated["organization_id"] or updated["department_id"] or updated["team_id"]
        req_obj = RoleRequest(
            id=updated["id"],
            user_id=updated["user_id"],
            requested_role=updated["requested_role"],
            scope_type=updated["scope_type"],
            scope_id=scope_id,
            reason=updated["reason"],
            status=updated["status"],
            created_at=updated["created_at"],
            updated_at=updated["updated_at"],
        )
        enqueue_outbox_event(
            conn,
            "role_request_cancelled",
            {
                "request_id": str(req_obj.id),
                "user_id": str(req_obj.user_id),
                "actor_id": str(caller_id),
                "requested_role": req_obj.requested_role,
                "scope_type": req_obj.scope_type,
                "scope_id": str(req_obj.scope_id),
            },
        )
        return req_obj


def approve_role_request(
    conn: psycopg.Connection,
    request_id: UUID,
    decider_id: UUID,
    decider_name: str,
    comment: Optional[str] = None,
) -> Tuple[RoleRequest, RoleAssignment]:
    """Approve a role request in an atomic transaction:
    
    1. Lock request row FOR UPDATE.
    2. Confirm still pending (otherwise 409).
    3. Recheck decider authority inside lock and decider != requester (otherwise 403).
    4. Write approval record to approval_records.
    5. Update request status to 'approved'.
    6. Replace or grant assignment and write audit record.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM role_requests WHERE id = %s FOR UPDATE;", (request_id,))
        req = cur.fetchone()
        if not req:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")

        if req["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Request has already been decided (current status: {req['status']})",
            )

        require_request_decision_authority(conn, decider_id, req)

        # 1. Insert approval record
        cur.execute(
            """
            INSERT INTO approval_records (request_id, decider_id, outcome, comment)
            VALUES (%s, %s, 'approved', %s);
            """,
            (request_id, decider_id, comment),
        )

        # 2. Update role_requests row
        cur.execute(
            """
            UPDATE role_requests
            SET status = 'approved', updated_at = NOW()
            WHERE id = %s
            RETURNING id, user_id, requested_role, scope_type, organization_id,
                      department_id, team_id, reason, status, created_at, updated_at;
            """,
            (request_id,),
        )
        updated_req = cur.fetchone()

        scope_id = updated_req["organization_id"] or updated_req["department_id"] or updated_req["team_id"]

        # 3. Grant or update assignment and write audit entry in same transaction
        assignment = grant_or_update_role(
            conn,
            user_id=updated_req["user_id"],
            role=updated_req["requested_role"],
            scope_type=updated_req["scope_type"],
            scope_id=scope_id,
            actor_id=decider_id,
            reason=f"Approved request {request_id}: {comment or 'No comment'}",
            metadata={"request_id": str(request_id), "decider_name": decider_name},
        )

        req_obj = RoleRequest(
            id=updated_req["id"],
            user_id=updated_req["user_id"],
            requested_role=updated_req["requested_role"],
            scope_type=updated_req["scope_type"],
            scope_id=scope_id,
            reason=updated_req["reason"],
            status=updated_req["status"],
            created_at=updated_req["created_at"],
            updated_at=updated_req["updated_at"],
        )

        enqueue_outbox_event(
            conn,
            "role_request_approved",
            {
                "request_id": str(req_obj.id),
                "user_id": str(req_obj.user_id),
                "actor_id": str(decider_id),
                "actor_name": decider_name,
                "requested_role": req_obj.requested_role,
                "scope_type": req_obj.scope_type,
                "scope_id": str(req_obj.scope_id),
                "comment": comment,
            },
        )

        return req_obj, assignment


def reject_role_request(
    conn: psycopg.Connection,
    request_id: UUID,
    decider_id: UUID,
    reason: str,
) -> RoleRequest:
    """Reject a role request with reason required."""
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reason is required to reject a role request",
        )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM role_requests WHERE id = %s FOR UPDATE;", (request_id,))
        req = cur.fetchone()
        if not req:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role request not found")

        if req["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Request has already been decided (current status: {req['status']})",
            )

        require_request_decision_authority(conn, decider_id, req)

        cur.execute(
            """
            INSERT INTO approval_records (request_id, decider_id, outcome, comment)
            VALUES (%s, %s, 'rejected', %s);
            """,
            (request_id, decider_id, reason_clean),
        )

        cur.execute(
            """
            UPDATE role_requests
            SET status = 'rejected', updated_at = NOW()
            WHERE id = %s
            RETURNING id, user_id, requested_role, scope_type, organization_id,
                      department_id, team_id, reason, status, created_at, updated_at;
            """,
            (request_id,),
        )
        updated = cur.fetchone()
        scope_id = updated["organization_id"] or updated["department_id"] or updated["team_id"]
        req_rejected = RoleRequest(
            id=updated["id"],
            user_id=updated["user_id"],
            requested_role=updated["requested_role"],
            scope_type=updated["scope_type"],
            scope_id=scope_id,
            reason=updated["reason"],
            status=updated["status"],
            created_at=updated["created_at"],
            updated_at=updated["updated_at"],
        )

        enqueue_outbox_event(
            conn,
            "role_request_rejected",
            {
                "request_id": str(req_rejected.id),
                "user_id": str(req_rejected.user_id),
                "actor_id": str(decider_id),
                "requested_role": req_rejected.requested_role,
                "scope_type": req_rejected.scope_type,
                "scope_id": str(req_rejected.scope_id),
                "reason": reason_clean,
            },
        )

        return req_rejected


def get_user_role_requests(
    conn: psycopg.Connection, user_id: UUID
) -> List[RoleRequest]:
    """Retrieve all requests submitted by the current user."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT r.id, r.user_id, r.requested_role, r.scope_type,
                   r.organization_id, r.department_id, r.team_id,
                   r.reason, r.status, r.created_at, r.updated_at,
                   u.display_name AS user_name, u.email AS user_email,
                   CASE r.scope_type
                       WHEN 'organization' THEN o.name
                       WHEN 'department' THEN d.name
                       WHEN 'team' THEN t.name
                   END AS scope_name
            FROM role_requests r
            JOIN users u ON r.user_id = u.id
            LEFT JOIN organizations o ON r.organization_id = o.id
            LEFT JOIN departments d ON r.department_id = d.id
            LEFT JOIN teams t ON r.team_id = t.id
            WHERE r.user_id = %s
            ORDER BY r.created_at DESC;
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        results = []
        for r in rows:
            scope_id = r["organization_id"] or r["department_id"] or r["team_id"]
            results.append(
                RoleRequest(
                    id=r["id"],
                    user_id=r["user_id"],
                    user_name=r["user_name"],
                    user_email=r["user_email"],
                    requested_role=r["requested_role"],
                    scope_type=r["scope_type"],
                    scope_id=scope_id,
                    scope_name=r["scope_name"],
                    reason=r["reason"],
                    status=r["status"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return results


def get_approvals_inbox(
    conn: psycopg.Connection, decider_id: UUID
) -> List[RoleRequest]:
    """Retrieve all pending requests that the decider is currently eligible to decide."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT r.id, r.user_id, r.requested_role, r.scope_type,
                   r.organization_id, r.department_id, r.team_id,
                   r.reason, r.status, r.created_at, r.updated_at,
                   u.display_name AS user_name, u.email AS user_email,
                   CASE r.scope_type
                       WHEN 'organization' THEN o.name
                       WHEN 'department' THEN d.name
                       WHEN 'team' THEN t.name
                   END AS scope_name
            FROM role_requests r
            JOIN users u ON r.user_id = u.id
            LEFT JOIN organizations o ON r.organization_id = o.id
            LEFT JOIN departments d ON r.department_id = d.id
            LEFT JOIN teams t ON r.team_id = t.id
            WHERE r.status = 'pending' AND r.user_id != %s
            ORDER BY r.created_at ASC;
            """,
            (decider_id,),
        )
        candidates = cur.fetchall()

    eligible = []
    for c in candidates:
        if check_decider_authority(conn, decider_id, c):
            scope_id = c["organization_id"] or c["department_id"] or c["team_id"]
            eligible.append(
                RoleRequest(
                    id=c["id"],
                    user_id=c["user_id"],
                    user_name=c["user_name"],
                    user_email=c["user_email"],
                    requested_role=c["requested_role"],
                    scope_type=c["scope_type"],
                    scope_id=scope_id,
                    scope_name=c["scope_name"],
                    reason=c["reason"],
                    status=c["status"],
                    created_at=c["created_at"],
                    updated_at=c["updated_at"],
                )
            )
    return eligible

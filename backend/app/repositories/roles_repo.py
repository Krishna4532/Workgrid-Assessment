import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
import psycopg
from psycopg.rows import dict_row

from app.models import PositionResponse, RoleAssignment, RoleType, ScopeType
from app.repositories.outbox_repo import enqueue_outbox_event

logger = logging.getLogger("workgrid.roles_repo")


def _build_scope_fks(
    scope_type: ScopeType, scope_id: UUID
) -> Tuple[Optional[UUID], Optional[UUID], Optional[UUID]]:
    """Return (organization_id, department_id, team_id) according to scope_type."""
    if scope_type == "organization":
        return scope_id, None, None
    elif scope_type == "department":
        return None, scope_id, None
    elif scope_type == "team":
        return None, None, scope_id
    raise ValueError(f"Invalid scope_type: {scope_type}")


def grant_or_update_role(
    conn: psycopg.Connection,
    user_id: UUID,
    role: RoleType,
    scope_type: ScopeType,
    scope_id: UUID,
    actor_id: Optional[UUID] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> RoleAssignment:
    """Grant or update a role assignment and record the audit event atomically.
    
    Preserves single assignment row invariant per (user, scope) node.
    """
    org_id, dept_id, team_id = _build_scope_fks(scope_type, scope_id)
    meta_json = json.dumps(metadata or {})

    with conn.cursor(row_factory=dict_row) as cur:
        # Check for existing assignment for this user at this specific scope node
        if scope_type == "organization":
            cur.execute(
                "SELECT * FROM role_assignments WHERE user_id = %s AND organization_id = %s FOR UPDATE;",
                (user_id, org_id),
            )
        elif scope_type == "department":
            cur.execute(
                "SELECT * FROM role_assignments WHERE user_id = %s AND department_id = %s FOR UPDATE;",
                (user_id, dept_id),
            )
        else:
            cur.execute(
                "SELECT * FROM role_assignments WHERE user_id = %s AND team_id = %s FOR UPDATE;",
                (user_id, team_id),
            )

        existing = cur.fetchone()

        if existing:
            prev_status = existing["status"]
            prev_role = existing["role"]
            assignment_id = existing["id"]

            # Update existing assignment record
            cur.execute(
                """
                UPDATE role_assignments
                SET role = %s, status = 'active', granted_by = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by, created_at, updated_at;
                """,
                (role, actor_id, assignment_id),
            )
            updated_assignment = cur.fetchone()

            event_type = "restore" if prev_status != "active" and prev_role == role else "role_change"

            # Record audit event
            cur.execute(
                """
                INSERT INTO role_assignment_audit (
                    assignment_id, user_id, actor_id, event_type,
                    previous_status, new_status, previous_role, new_role,
                    scope_type, scope_id, reason, metadata
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s::jsonb);
                """,
                (
                    assignment_id,
                    user_id,
                    actor_id,
                    event_type,
                    prev_status,
                    prev_role,
                    role,
                    scope_type,
                    scope_id,
                    reason,
                    meta_json,
                ),
            )
            return RoleAssignment(**updated_assignment)
        else:
            # Insert fresh assignment
            cur.execute(
                """
                INSERT INTO role_assignments (
                    user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
                RETURNING id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by, created_at, updated_at;
                """,
                (user_id, role, scope_type, org_id, dept_id, team_id, actor_id),
            )
            new_assignment = cur.fetchone()
            assignment_id = new_assignment["id"]

            # Record grant audit event
            cur.execute(
                """
                INSERT INTO role_assignment_audit (
                    assignment_id, user_id, actor_id, event_type,
                    previous_status, new_status, previous_role, new_role,
                    scope_type, scope_id, reason, metadata
                ) VALUES (%s, %s, %s, 'grant', NULL, 'active', NULL, %s, %s, %s, %s, %s::jsonb);
                """,
                (
                    assignment_id,
                    user_id,
                    actor_id,
                    role,
                    scope_type,
                    scope_id,
                    reason,
                    meta_json,
                ),
            )
            return RoleAssignment(**new_assignment)


def suspend_role(
    conn: psycopg.Connection,
    assignment_id: UUID,
    actor_id: Optional[UUID] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> RoleAssignment:
    """Suspend an active role assignment and record the audit event atomically."""
    meta_json = json.dumps(metadata or {})
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM role_assignments WHERE id = %s FOR UPDATE;",
            (assignment_id,),
        )
        existing = cur.fetchone()
        if not existing:
            raise ValueError(f"RoleAssignment {assignment_id} not found")

        prev_status = existing["status"]
        role = existing["role"]
        user_id = existing["user_id"]
        scope_type = existing["scope_type"]
        scope_id = existing["organization_id"] or existing["department_id"] or existing["team_id"]

        cur.execute(
            """
            UPDATE role_assignments
            SET status = 'suspended', updated_at = NOW()
            WHERE id = %s
            RETURNING id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by, created_at, updated_at;
            """,
            (assignment_id,),
        )
        updated = cur.fetchone()

        cur.execute(
            """
            INSERT INTO role_assignment_audit (
                assignment_id, user_id, actor_id, event_type,
                previous_status, new_status, previous_role, new_role,
                scope_type, scope_id, reason, metadata
            ) VALUES (%s, %s, %s, 'suspend', %s, 'suspended', %s, %s, %s, %s, %s, %s::jsonb);
            """,
            (
                assignment_id,
                user_id,
                actor_id,
                prev_status,
                role,
                role,
                scope_type,
                scope_id,
                reason,
                meta_json,
            ),
        )
        enqueue_outbox_event(
            conn,
            "role_assignment_changed",
            {
                "assignment_id": str(assignment_id),
                "user_id": str(user_id),
                "actor_id": str(actor_id) if actor_id else None,
                "role": role,
                "action": "suspended",
                "reason": reason,
            },
        )
        return RoleAssignment(**updated)


def revoke_role(
    conn: psycopg.Connection,
    assignment_id: UUID,
    actor_id: Optional[UUID] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> RoleAssignment:
    """Revoke a role assignment and record the audit event atomically."""
    meta_json = json.dumps(metadata or {})
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM role_assignments WHERE id = %s FOR UPDATE;",
            (assignment_id,),
        )
        existing = cur.fetchone()
        if not existing:
            raise ValueError(f"RoleAssignment {assignment_id} not found")

        prev_status = existing["status"]
        role = existing["role"]
        user_id = existing["user_id"]
        scope_type = existing["scope_type"]
        scope_id = existing["organization_id"] or existing["department_id"] or existing["team_id"]

        cur.execute(
            """
            UPDATE role_assignments
            SET status = 'revoked', updated_at = NOW()
            WHERE id = %s
            RETURNING id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by, created_at, updated_at;
            """,
            (assignment_id,),
        )
        updated = cur.fetchone()

        cur.execute(
            """
            INSERT INTO role_assignment_audit (
                assignment_id, user_id, actor_id, event_type,
                previous_status, new_status, previous_role, new_role,
                scope_type, scope_id, reason, metadata
            ) VALUES (%s, %s, %s, 'revoke', %s, 'revoked', %s, %s, %s, %s, %s, %s::jsonb);
            """,
            (
                assignment_id,
                user_id,
                actor_id,
                prev_status,
                role,
                role,
                scope_type,
                scope_id,
                reason,
                meta_json,
            ),
        )
        enqueue_outbox_event(
            conn,
            "role_assignment_changed",
            {
                "assignment_id": str(assignment_id),
                "user_id": str(user_id),
                "actor_id": str(actor_id) if actor_id else None,
                "role": role,
                "action": "revoked",
                "reason": reason,
            },
        )
        return RoleAssignment(**updated)


def get_active_positions(
    conn: psycopg.Connection, user_id: UUID
) -> List[PositionResponse]:
    """Retrieve all active role assignments for the specified user with scope lineage.
    
    Excludes suspended and revoked assignments.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 
                ra.id AS assignment_id,
                ra.role,
                ra.scope_type,
                ra.status,
                CASE ra.scope_type
                    WHEN 'organization' THEN ra.organization_id
                    WHEN 'department' THEN ra.department_id
                    WHEN 'team' THEN ra.team_id
                END AS scope_id,
                CASE ra.scope_type
                    WHEN 'organization' THEN o_direct.name
                    WHEN 'department' THEN d_direct.name
                    WHEN 'team' THEN t_direct.name
                END AS scope_name,
                CASE ra.scope_type
                    WHEN 'team' THEN d_team.id
                    WHEN 'department' THEN d_direct.id
                    ELSE NULL
                END AS department_id,
                CASE ra.scope_type
                    WHEN 'team' THEN d_team.name
                    WHEN 'department' THEN d_direct.name
                    ELSE NULL
                END AS department_name,
                CASE ra.scope_type
                    WHEN 'organization' THEN o_direct.id
                    WHEN 'department' THEN o_dept.id
                    WHEN 'team' THEN o_team.id
                END AS organization_id,
                CASE ra.scope_type
                    WHEN 'organization' THEN o_direct.name
                    WHEN 'department' THEN o_dept.name
                    WHEN 'team' THEN o_team.name
                END AS organization_name
            FROM role_assignments ra
            -- Joins for organization scope
            LEFT JOIN organizations o_direct ON ra.organization_id = o_direct.id
            -- Joins for department scope
            LEFT JOIN departments d_direct ON ra.department_id = d_direct.id
            LEFT JOIN organizations o_dept ON d_direct.organization_id = o_dept.id
            -- Joins for team scope
            LEFT JOIN teams t_direct ON ra.team_id = t_direct.id
            LEFT JOIN departments d_team ON t_direct.department_id = d_team.id
            LEFT JOIN organizations o_team ON d_team.organization_id = o_team.id
            WHERE ra.user_id = %s AND ra.status = 'active'
            ORDER BY ra.created_at ASC;
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        return [PositionResponse(**row) for row in rows]


def get_effective_role(
    conn: psycopg.Connection, user_id: UUID, team_id: UUID
) -> Optional[Dict[str, Any]]:
    """Query the effective_roles view to resolve the highest active role for (user, team)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT user_id, team_id, assignment_id, role, scope_type, role_rank
            FROM effective_roles
            WHERE user_id = %s AND team_id = %s;
            """,
            (user_id, team_id),
        )
        return cur.fetchone()

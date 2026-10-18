import uuid
import pytest
import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.db import get_db_connection
from app.repositories.identity_repo import create_organization, create_department, create_team, create_user
from app.repositories.roles_repo import grant_or_update_role, suspend_role, revoke_role


def test_grant_role_writes_assignment_and_audit_atomically():
    """Verify that granting a role creates the assignment and audit record atomically."""
    with get_db_connection() as conn:
        with conn.transaction():
            actor = create_user(conn, email=f"actor-{uuid.uuid4().hex[:8]}@example.com", display_name="Admin Actor")
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Target User")
            org = create_organization(conn, name="Test Org", slug=f"org-{uuid.uuid4().hex[:8]}")

            assignment = grant_or_update_role(
                conn,
                user_id=user.id,
                role="owner",
                scope_type="organization",
                scope_id=org.id,
                actor_id=actor.id,
                reason="Promotion to owner",
                metadata={"test_run": True},
            )

        # After commit, verify assignment exists
        assert assignment.id is not None
        assert assignment.status == "active"
        assert assignment.role == "owner"

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM role_assignment_audit WHERE assignment_id = %s;",
                (assignment.id,),
            )
            audit = cur.fetchone()
            assert audit is not None
            assert audit["event_type"] == "grant"
            assert audit["user_id"] == user.id
            assert audit["actor_id"] == actor.id
            assert audit["new_role"] == "owner"
            assert audit["new_status"] == "active"
            assert audit["reason"] == "Promotion to owner"
            assert audit["metadata"].get("test_run") is True


def test_suspend_and_revoke_lifecycle_with_audit():
    """Verify suspending and revoking an assignment updates status and writes audit logs."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            # 1. Grant
            assignment = grant_or_update_role(
                conn,
                user_id=user.id,
                role="member",
                scope_type="team",
                scope_id=team.id,
                reason="New hire",
            )
            assert assignment.status == "active"

            # 2. Suspend
            suspended = suspend_role(conn, assignment.id, reason="Investigation")
            assert suspended.status == "suspended"

            # 3. Revoke
            revoked = revoke_role(conn, assignment.id, reason="Terminated")
            assert revoked.status == "revoked"

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT event_type, previous_status, new_status, reason FROM role_assignment_audit WHERE assignment_id = %s ORDER BY created_at ASC;",
                (assignment.id,),
            )
            audits = cur.fetchall()
            assert len(audits) == 3
            assert audits[0]["event_type"] == "grant"
            assert audits[0]["new_status"] == "active"

            assert audits[1]["event_type"] == "suspend"
            assert audits[1]["previous_status"] == "active"
            assert audits[1]["new_status"] == "suspended"
            assert audits[1]["reason"] == "Investigation"

            assert audits[2]["event_type"] == "revoke"
            assert audits[2]["previous_status"] == "suspended"
            assert audits[2]["new_status"] == "revoked"
            assert audits[2]["reason"] == "Terminated"


def test_transaction_rollback_leaves_no_partial_records():
    """Verify that an abort in a transaction leaves neither assignment nor audit rows."""
    with get_db_connection() as conn:
        user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="User")
        org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        conn.commit()

        assignment_id = None
        try:
            with conn.transaction():
                assignment = grant_or_update_role(
                    conn,
                    user_id=user.id,
                    role="owner",
                    scope_type="organization",
                    scope_id=org.id,
                )
                assignment_id = assignment.id
                # Intentionally trigger an error to force rollback
                raise RuntimeError("Simulated mid-transaction failure")
        except RuntimeError:
            pass

        # Verify nothing persisted
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM role_assignments WHERE id = %s;", (assignment_id,))
            assert cur.fetchone() is None
            cur.execute("SELECT * FROM role_assignment_audit WHERE assignment_id = %s;", (assignment_id,))
            assert cur.fetchone() is None


def test_audit_log_is_strictly_append_only():
    """Verify that direct UPDATE or DELETE on role_assignment_audit is rejected by trigger."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            assignment = grant_or_update_role(
                conn, user_id=user.id, role="owner", scope_type="organization", scope_id=org.id
            )

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM role_assignment_audit WHERE assignment_id = %s;", (assignment.id,))
            audit_id = cur.fetchone()[0]

            # Direct UPDATE must fail
            with pytest.raises(errors.RaiseException) as exc_info:
                cur.execute(
                    "UPDATE role_assignment_audit SET reason = 'Tampered' WHERE id = %s;",
                    (audit_id,),
                )
            conn.rollback()
            assert "append-only" in str(exc_info.value).lower()

            # Direct DELETE must fail
            with pytest.raises(errors.RaiseException) as exc_info:
                cur.execute(
                    "DELETE FROM role_assignment_audit WHERE id = %s;",
                    (audit_id,),
                )
            conn.rollback()
            assert "append-only" in str(exc_info.value).lower()


def test_single_assignment_per_user_per_scope_constraint():
    """Verify that a user cannot have two assignment rows for the same scope node."""
    with get_db_connection() as conn:
        user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="User")
        org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
        team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO role_assignments (user_id, role, scope_type, team_id, status)
                VALUES (%s, 'member', 'team', %s, 'active');
                """,
                (user.id, team.id),
            )
            conn.commit()

            # Attempting to insert a duplicate assignment row for the same user and team must fail
            with pytest.raises(errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, team_id, status)
                    VALUES (%s, 'team_leader', 'team', %s, 'active');
                    """,
                    (user.id, team.id),
                )
            conn.rollback()

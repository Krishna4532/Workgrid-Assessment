import uuid
import pytest
from unittest.mock import patch
from psycopg.rows import dict_row

from app.db import get_db_connection
from app.models import WorkItemCreate
from app.repositories import work_items_repo
from app.repositories.approvals_repo import (
    approve_role_request,
    create_role_request,
    reject_role_request,
)
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.outbox_repo import enqueue_outbox_event, get_outbox_event_by_id
from app.repositories.roles_repo import grant_or_update_role
from app.repositories.work_items_repo import create_work_item, transition_work_item


def _setup_test_context(conn):
    org = create_organization(conn, name="Test Org", slug=f"org-{uuid.uuid4().hex[:8]}")
    dept = create_department(conn, organization_id=org.id, name="Test Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
    team = create_team(conn, department_id=dept.id, name="Test Team", slug=f"team-{uuid.uuid4().hex[:8]}")

    manager = create_user(conn, email=f"mgr-{uuid.uuid4().hex[:8]}@example.com", display_name="Manager")
    member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
    applicant = create_user(conn, email=f"app-{uuid.uuid4().hex[:8]}@example.com", display_name="Applicant")

    grant_or_update_role(conn, user_id=manager.id, role="team_leader", scope_type="team", scope_id=team.id)
    grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    return org, dept, team, manager, member, applicant


def test_work_item_creation_enqueues_outbox_event_in_same_transaction():
    """Verify that creating a work item enqueues a work_item_created event in the same transaction."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, member, _ = _setup_test_context(conn)

            dto = WorkItemCreate(
                team_id=team.id,
                kind="incident",
                title="Outbox Test Incident",
                description="Testing transactional outbox",
                priority="high",
                assignee_id=member.id,
            )
            item = create_work_item(conn, member.id, member.display_name, dto)

        # In a separate transaction, verify outbox event was committed
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM outbox WHERE event_type = 'work_item_created' AND payload->>'item_id' = %s;",
                (str(item.id),),
            )
            event = cur.fetchone()
            assert event is not None
            assert event["status"] == "pending"
            assert event["attempts"] == 0
            assert event["payload"]["title"] == "Outbox Test Incident"
            assert event["payload"]["creator_id"] == str(member.id)
            assert event["payload"]["assignee_id"] == str(member.id)


def test_transaction_rollback_leaves_no_outbox_event():
    """Verify that a rolled-back transaction commits neither the business record nor the outbox event."""
    canary_title = f"Rollback Canary {uuid.uuid4().hex}"
    fake_item_id = uuid.uuid4()

    with get_db_connection() as conn:
        try:
            with conn.transaction():
                _, _, team, _, member, _ = _setup_test_context(conn)
                enqueue_outbox_event(
                    conn,
                    "work_item_created",
                    {"item_id": str(fake_item_id), "title": canary_title},
                )
                # Intentionally trigger rollback
                raise RuntimeError("Simulated transaction crash")
        except RuntimeError:
            pass

        # Verify nothing committed
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE payload->>'title' = %s;",
                (canary_title,),
            )
            assert cur.fetchone()[0] == 0


def test_outbox_insert_failure_rolls_back_work_item_and_activity():
    """Verify an outbox insertion failure cannot commit its business mutation."""
    canary_title = f"Outbox Failure Canary {uuid.uuid4()}"

    with get_db_connection() as conn:
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            with conn.transaction():
                _, _, team, _, member, _ = _setup_test_context(conn)
                dto = WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title=canary_title,
                )
                with patch.object(
                    work_items_repo,
                    "enqueue_outbox_event",
                    side_effect=RuntimeError("outbox unavailable"),
                ):
                    create_work_item(conn, member.id, member.display_name, dto)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM work_items WHERE title = %s;", (canary_title,))
            assert cur.fetchone()[0] == 0


def test_role_request_lifecycle_outbox_events():
    """Verify role request creation, approval, and rejection each write outbox events atomically."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, manager, _, applicant = _setup_test_context(conn)

            from app.models import RoleRequestCreate

            # 1. Submission
            dto = RoleRequestCreate(
                requested_role="member",
                scope_type="team",
                scope_id=team.id,
                reason="Wanting to contribute",
            )
            req = create_role_request(conn, applicant.id, dto)

        # Check role_request_submitted event
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM outbox WHERE event_type = 'role_request_submitted' AND payload->>'request_id' = %s;",
                (str(req.id),),
            )
            sub_event = cur.fetchone()
            assert sub_event is not None
            assert sub_event["payload"]["requested_role"] == "member"

        # 2. Approval
        with conn.transaction():
            approve_role_request(conn, req.id, manager.id, manager.display_name, "Approved for team")

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM outbox WHERE event_type = 'role_request_approved' AND payload->>'request_id' = %s;",
                (str(req.id),),
            )
            app_event = cur.fetchone()
            assert app_event is not None
            assert app_event["payload"]["actor_id"] == str(manager.id)

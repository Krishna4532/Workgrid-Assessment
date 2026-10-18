import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.models import WorkItemCreate, RoleRequestCreate
from app.repositories.approvals_repo import create_role_request
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import grant_or_update_role
from app.repositories.work_items_repo import create_work_item


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_hierarchy(conn):
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"Conc Org {suffix}", slug=f"conc-org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"Conc Dept {suffix}", slug=f"conc-dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"Conc Team {suffix}", slug=f"conc-team-{suffix}")
    return org, dept, team


# ============================================================================
# 1. Genuinely Concurrent Work-Item Claims
# ============================================================================

def test_concurrent_work_item_claims_exactly_one_winner(client):
    """Verify simultaneous claims on the same open item result in exactly one winner and one 409."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            member_a = create_user(conn, email=f"claim-a-{uuid.uuid4().hex[:8]}@example.com", display_name="Claimant A")
            member_b = create_user(conn, email=f"claim-b-{uuid.uuid4().hex[:8]}@example.com", display_name="Claimant B")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member_a.id, role="member", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member_b.id, role="member", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="incident", title="Critical Outage"),
            )

    barrier = Barrier(2)

    def do_claim(user_email: str):
        barrier.wait()
        return client.post(
            f"/work-items/{item.id}/claim",
            headers={"X-Dev-User-Email": user_email},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_a = executor.submit(do_claim, member_a.email)
        f_b = executor.submit(do_claim, member_b.email)
        res_a = f_a.result()
        res_b = f_b.result()

    statuses = [res_a.status_code, res_b.status_code]
    assert 200 in statuses, "At least one claim must succeed with 200"
    assert 409 in statuses, "The competing claim must fail with 409 Conflict"

    winner_res = res_a if res_a.status_code == 200 else res_b
    loser_res = res_b if res_a.status_code == 200 else res_a
    winner_email = member_a.email if res_a.status_code == 200 else member_b.email

    assert winner_res.json()["status"] == "in_progress"
    assert winner_res.json()["version"] == 2
    assert "already claimed" in loser_res.json()["detail"].lower()

    # Verify database state consistency
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, version, assignee_id FROM work_items WHERE id = %s;",
                (item.id,),
            )
            final_status, final_ver, final_assignee = cur.fetchone()
            assert final_status == "in_progress"
            assert final_ver == 2
            assert final_assignee in (member_a.id, member_b.id)

            # Exactly 2 activity records: 'created' + 1 'claimed' (0 activity rows for loser)
            cur.execute(
                "SELECT event_type, actor_id FROM activity WHERE work_item_id = %s ORDER BY created_at ASC;",
                (item.id,),
            )
            activities = cur.fetchall()
            assert len(activities) == 2
            assert activities[0][0] == "created"
            assert activities[1][0] == "claimed"
            assert activities[1][1] == final_assignee


# ============================================================================
# 2. Genuinely Concurrent Approval Decisions
# ============================================================================

def test_concurrent_approval_decisions_race_condition_safety(client):
    """Verify simultaneous decision requests on the same pending role request permit only one decision."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            owner = create_user(conn, email=f"own-{uuid.uuid4().hex[:8]}@example.com", display_name="Owner")
            dept_mgr = create_user(conn, email=f"dm-{uuid.uuid4().hex[:8]}@example.com", display_name="Dept Mgr")
            applicant = create_user(conn, email=f"app-{uuid.uuid4().hex[:8]}@example.com", display_name="Applicant")

            grant_or_update_role(conn, user_id=owner.id, role="owner", scope_type="organization", scope_id=org.id)
            grant_or_update_role(conn, user_id=dept_mgr.id, role="department_manager", scope_type="department", scope_id=dept.id)
            # Applicant must be an active member to request promotion to team_leader
            grant_or_update_role(conn, user_id=applicant.id, role="member", scope_type="team", scope_id=team.id)

            # Submit valid promotion role request for team leader
            req = create_role_request(
                conn,
                applicant.id,
                RoleRequestCreate(
                    requested_role="team_leader",
                    scope_type="team",
                    scope_id=team.id,
                    reason="Promotion request",
                ),
            )

    barrier = Barrier(2)

    def do_approve():
        barrier.wait()
        return client.post(
            f"/role-requests/{req.id}/approve",
            headers={"X-Dev-User-Email": dept_mgr.email},
            json={"comment": "Approved by DM"},
        )

    def do_reject():
        barrier.wait()
        return client.post(
            f"/role-requests/{req.id}/reject",
            headers={"X-Dev-User-Email": owner.email},
            json={"reason": "Rejected by Owner"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_appr = executor.submit(do_approve)
        f_rej = executor.submit(do_reject)
        res_appr = f_appr.result()
        res_rej = f_rej.result()

    statuses = [res_appr.status_code, res_rej.status_code]
    assert 200 in statuses, "One decision must succeed with 200"
    assert 409 in statuses, "The competing decision must fail with 409 Conflict"

    # Verify database state integrity
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM role_requests WHERE id = %s;", (req.id,))
            final_req_status = cur.fetchone()[0]
            assert final_req_status in ("approved", "rejected")

            cur.execute("SELECT count(*) FROM approval_records WHERE request_id = %s;", (req.id,))
            record_count = cur.fetchone()[0]
            assert record_count == 1, "Exactly one approval record must be created"


# ============================================================================
# 3. Optimistic Concurrency Control (OCC) and Stale Edits
# ============================================================================

def test_optimistic_concurrency_control_rejects_stale_edits(client):
    """Verify updating a work item with an outdated version returns 409 and prevents overwrites."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Original Title",
                    description="Original Description",
                    priority="normal",
                ),
            )
            assert item.version == 1

    headers = {"X-Dev-User-Email": lead.email}

    # Session 1 updates title successfully with version 1 -> advances to version 2
    res_s1 = client.patch(
        f"/work-items/{item.id}",
        headers=headers,
        json={"title": "Session 1 Updated Title", "version": 1},
    )
    assert res_s1.status_code == 200
    updated_item = res_s1.json()
    assert updated_item["title"] == "Session 1 Updated Title"
    assert updated_item["version"] == 2

    # Session 2 attempts update supplying stale version 1 -> 409 Conflict
    res_s2 = client.patch(
        f"/work-items/{item.id}",
        headers=headers,
        json={"description": "Session 2 Stale Description", "version": 1},
    )
    assert res_s2.status_code == 409
    assert res_s2.json()["code"] == "CONFLICT"
    assert "version mismatch" in res_s2.json()["detail"].lower()

    # Verify Session 1's title was preserved and Session 2's description was not applied
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, description, version FROM work_items WHERE id = %s;", (item.id,))
            title, desc, ver = cur.fetchone()
            assert title == "Session 1 Updated Title"
            assert desc == "Original Description"
            assert ver == 2

    # Session 2 refreshes to current version 2 and retries -> succeeds, advances to version 3
    res_s2_retry = client.patch(
        f"/work-items/{item.id}",
        headers=headers,
        json={"description": "Session 2 Fresh Description", "version": 2},
    )
    assert res_s2_retry.status_code == 200
    assert res_s2_retry.json()["version"] == 3
    assert res_s2_retry.json()["description"] == "Session 2 Fresh Description"


# ============================================================================
# 4. Stale State Transitions
# ============================================================================

def test_stale_version_transitions_are_rejected(client):
    """Verify attempting status transition with stale expected version returns 409 Conflict."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Transition OCC Test",
                ),
            )
            assert item.version == 1
            assert item.status == "open"

    headers = {"X-Dev-User-Email": lead.email}

    # 1. Attempt transition with stale version 0 or 99 -> 409 Conflict
    bad_res = client.post(
        f"/work-items/{item.id}/transition",
        headers=headers,
        json={"to_status": "in_progress", "version": 99},
    )
    assert bad_res.status_code == 409
    assert "modified concurrently" in bad_res.json()["detail"].lower()

    # 2. Transition with current version 1 -> 200 OK
    good_res = client.post(
        f"/work-items/{item.id}/transition",
        headers=headers,
        json={"to_status": "in_progress", "version": 1},
    )
    assert good_res.status_code == 200
    assert good_res.json()["version"] == 2
    assert good_res.json()["status"] == "in_progress"


# ============================================================================
# 5. Stale Work-Item Assignments
# ============================================================================

def test_stale_version_assignments_are_rejected(client):
    """Verify assigning a work item with an outdated version returns 409 Conflict."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Assignment OCC Test"),
            )

    headers = {"X-Dev-User-Email": lead.email}

    # Attempt assignment with stale version 42 -> 409 Conflict
    stale_res = client.post(
        f"/work-items/{item.id}/assign",
        headers=headers,
        json={"assignee_id": str(member.id), "version": 42},
    )
    assert stale_res.status_code == 409
    assert "modified concurrently" in stale_res.json()["detail"].lower()

    # Valid assignment with version 1 -> 200 OK
    valid_res = client.post(
        f"/work-items/{item.id}/assign",
        headers=headers,
        json={"assignee_id": str(member.id), "version": 1, "reason": "Initial assignment"},
    )
    assert valid_res.status_code == 200
    assert valid_res.json()["version"] == 2
    assert valid_res.json()["assignee_id"] == str(member.id)

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import get_effective_role, grant_or_update_role


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_hierarchy(conn):
    org = create_organization(conn, name="Acme Org", slug=f"org-{uuid.uuid4().hex[:8]}")
    dept = create_department(conn, organization_id=org.id, name="Eng Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
    team = create_team(conn, department_id=dept.id, name="Platform Team", slug=f"team-{uuid.uuid4().hex[:8]}")

    owner = create_user(conn, email=f"owner-{uuid.uuid4().hex[:8]}@example.com", display_name="Org Owner")
    dept_mgr = create_user(conn, email=f"dm-{uuid.uuid4().hex[:8]}@example.com", display_name="Dept Manager")
    team_lead = create_user(conn, email=f"tl-{uuid.uuid4().hex[:8]}@example.com", display_name="Team Leader")
    applicant = create_user(conn, email=f"app-{uuid.uuid4().hex[:8]}@example.com", display_name="Applicant")
    unrelated = create_user(conn, email=f"unrel-{uuid.uuid4().hex[:8]}@example.com", display_name="Unrelated Person")

    grant_or_update_role(conn, user_id=owner.id, role="owner", scope_type="organization", scope_id=org.id)
    grant_or_update_role(conn, user_id=dept_mgr.id, role="department_manager", scope_type="department", scope_id=dept.id)
    grant_or_update_role(conn, user_id=team_lead.id, role="team_leader", scope_type="team", scope_id=team.id)

    return org, dept, team, owner, dept_mgr, team_lead, applicant, unrelated


def test_submit_role_request_and_duplicate_prevention(client):
    """Verify role request creation and duplicate pending request prevention (409)."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, _, _, applicant, _ = _setup_hierarchy(conn)

    # 1. Submit request
    res = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={
            "requested_role": "member",
            "scope_type": "team",
            "scope_id": str(team.id),
            "reason": "Joining team",
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "pending"
    assert data["requested_role"] == "member"

    # 2. Duplicate submission while still pending must get 409 Conflict
    dup = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={
            "requested_role": "member",
            "scope_type": "team",
            "scope_id": str(team.id),
            "reason": "Duplicate attempt",
        },
    )
    assert dup.status_code == 409
    assert "pending request already exists" in dup.json()["detail"].lower()


def test_cancel_role_request_rules(client):
    """Verify requester can cancel own pending request, others cannot, and cannot cancel once decided."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, _, _, applicant, unrelated = _setup_hierarchy(conn)

    sub = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    req_id = sub.json()["id"]

    # 1. Out-of-scope user cannot discover the request
    res_un = client.post(
        f"/role-requests/{req_id}/cancel",
        headers={"X-Dev-User-Email": unrelated.email},
    )
    assert res_un.status_code == 404

    # 2. Requester cancels -> 200 OK
    res_c = client.post(
        f"/role-requests/{req_id}/cancel",
        headers={"X-Dev-User-Email": applicant.email},
    )
    assert res_c.status_code == 200
    assert res_c.json()["status"] == "cancelled"

    # 3. Cannot cancel an already cancelled request -> 409 Conflict
    res_again = client.post(
        f"/role-requests/{req_id}/cancel",
        headers={"X-Dev-User-Email": applicant.email},
    )
    assert res_again.status_code == 409


def test_self_approval_restriction_refused(client):
    """Verify Rule #6: Nobody decides their own request, even if they have authority."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, _, team_lead, _, _ = _setup_hierarchy(conn)

    # Team leader submits a request for viewer at department
    sub = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": team_lead.email},
        json={"requested_role": "viewer", "scope_type": "team", "scope_id": str(team.id)},
    )
    req_id = sub.json()["id"]

    # Team leader attempts to approve own request -> 403 Forbidden
    app_res = client.post(
        f"/role-requests/{req_id}/approve",
        headers={"X-Dev-User-Email": team_lead.email},
    )
    assert app_res.status_code == 403
    assert "nobody can decide their own request" in app_res.json()["detail"].lower()


def test_approval_routing_and_atomic_assignment_creation(client):
    """Verify approval follows policy table and creates active assignment atomically."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, owner, _, team_lead, applicant, unrelated = _setup_hierarchy(conn)

    # Applicant requests member at team
    sub = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    req_id = sub.json()["id"]

    # 1. Out-of-scope user cannot discover the request
    res_un = client.post(
        f"/role-requests/{req_id}/approve",
        headers={"X-Dev-User-Email": unrelated.email},
    )
    assert res_un.status_code == 404

    # 2. Verify applicant does NOT have access while pending
    with get_db_connection() as conn:
        eff_pending = get_effective_role(conn, applicant.id, team.id)
        assert eff_pending is None

    # 3. Authorized team leader approves request
    app_res = client.post(
        f"/role-requests/{req_id}/approve",
        headers={"X-Dev-User-Email": team_lead.email},
        json={"comment": "Welcome to platform team"},
    )
    assert app_res.status_code == 200
    data = app_res.json()
    assert data["status"] == "approved"
    assert data["assignment"]["status"] == "active"
    assert data["assignment"]["role"] == "member"

    # 4. Verify applicant now immediately has effective role
    with get_db_connection() as conn:
        eff_approved = get_effective_role(conn, applicant.id, team.id)
        assert eff_approved is not None
        assert eff_approved["role"] == "member"

    # 5. Competing approval on already-approved request returns 409 Conflict
    dup_app = client.post(
        f"/role-requests/{req_id}/approve",
        headers={"X-Dev-User-Email": owner.email},
    )
    assert dup_app.status_code == 409
    assert "already been decided" in dup_app.json()["detail"].lower()


def test_concurrent_approval_decisions_only_commit_one(client):
    """Verify row locking serializes simultaneous approve/reject decisions."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, owner, _, team_lead, applicant, _ = _setup_hierarchy(conn)

    submitted = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    assert submitted.status_code == 201
    request_id = submitted.json()["id"]
    barrier = Barrier(2)

    def approve():
        barrier.wait(timeout=10)
        return client.post(
            f"/role-requests/{request_id}/approve",
            headers={"X-Dev-User-Email": team_lead.email},
            json={"comment": "Approved concurrently"},
        )

    def reject():
        barrier.wait(timeout=10)
        return client.post(
            f"/role-requests/{request_id}/reject",
            headers={"X-Dev-User-Email": owner.email},
            json={"reason": "Rejected concurrently"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [executor.submit(approve), executor.submit(reject)]
        results = [future.result(timeout=20) for future in responses]

    assert sorted(response.status_code for response in results) == [200, 409]
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM approval_records WHERE request_id = %s;", (request_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT status FROM role_requests WHERE id = %s;", (request_id,))
            final_status = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM role_assignments WHERE user_id = %s AND team_id = %s;", (applicant.id, team.id))
            assignment_count = cur.fetchone()[0]
    assert final_status in ("approved", "rejected")
    assert assignment_count == (1 if final_status == "approved" else 0)


def test_idempotency_key_cannot_replay_approval_as_rejection(client):
    """Verify decision operation names are included in idempotency request identity."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, _, team_lead, applicant, _ = _setup_hierarchy(conn)

    submitted = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    request_id = submitted.json()["id"]
    headers = {
        "X-Dev-User-Email": team_lead.email,
        "Idempotency-Key": f"decision-{uuid.uuid4()}",
    }

    approved = client.post(
        f"/role-requests/{request_id}/approve",
        headers=headers,
        json={"comment": "Same decision comment"},
    )
    assert approved.status_code == 200

    rejected = client.post(
        f"/role-requests/{request_id}/reject",
        headers=headers,
        json={"comment": "Same decision comment"},
    )
    assert rejected.status_code == 422


def test_rejection_requires_reason(client):
    """Verify rejecting a role request requires a reason and does not grant permissions."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, _, team_lead, applicant, _ = _setup_hierarchy(conn)

    sub = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": applicant.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    req_id = sub.json()["id"]

    # Rejection without reason fails with 422
    fail_rej = client.post(
        f"/role-requests/{req_id}/reject",
        headers={"X-Dev-User-Email": team_lead.email},
        json={"reason": "   "},
    )
    assert fail_rej.status_code == 422

    # Rejection with reason succeeds
    rej = client.post(
        f"/role-requests/{req_id}/reject",
        headers={"X-Dev-User-Email": team_lead.email},
        json={"reason": "Position already filled"},
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"

    # Rejected applicant has no effective role
    with get_db_connection() as conn:
        assert get_effective_role(conn, applicant.id, team.id) is None

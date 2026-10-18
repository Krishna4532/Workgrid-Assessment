import uuid
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
from app.repositories.roles_repo import grant_or_update_role
from app.security import hash_password


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_e2e_hierarchy(conn):
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"E2E Org {suffix}", slug=f"e2e-org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"E2E Dept {suffix}", slug=f"e2e-dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"E2E Team {suffix}", slug=f"e2e-team-{suffix}")
    return org, dept, team


# ============================================================================
# 1. Complete End-to-End User Lifecycle & Work Management Flow
# ============================================================================

def test_complete_end_to_end_lifecycle_flow(client):
    """Verify full end-to-end user registration, approval, item creation, claim, review, resolution, and closure."""
    dept_mgr_pwd = "ManagerSecret123!"
    applicant_pwd = "ApplicantPass456!"

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_e2e_hierarchy(conn)

            # Department Manager setup
            dept_mgr = create_user(
                conn,
                email=f"dm-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Department Manager",
                password_hash=hash_password(dept_mgr_pwd),
            )
            grant_or_update_role(conn, user_id=dept_mgr.id, role="department_manager", scope_type="department", scope_id=dept.id)

    # ------------------------------------------------------------------------
    # Step 1: Real Account Registration
    # ------------------------------------------------------------------------
    reg_email = f"newuser-{uuid.uuid4().hex[:8]}@example.com"
    reg_res = client.post(
        "/auth/register",
        json={
            "email": reg_email,
            "password": applicant_pwd,
            "display_name": "New Registered Member",
            "target_team_id": str(team.id),
            "requested_role": "member",
        },
    )
    assert reg_res.status_code == 201
    reg_data = reg_res.json()
    new_user_id = reg_data["user"]["id"]
    new_user_token = reg_data["access_token"]
    role_req_id = reg_data["role_request"]["id"]

    # Before approval, user has 0 active positions
    pos_res_pre = client.get(
        "/me/positions",
        headers={"Authorization": f"Bearer {new_user_token}"},
    )
    assert pos_res_pre.status_code == 200
    assert len(pos_res_pre.json()) == 0

    # ------------------------------------------------------------------------
    # Step 2: Department Manager Approves Role Request
    # ------------------------------------------------------------------------
    # DM logs in
    dm_login_res = client.post("/auth/login", json={"email": dept_mgr.email, "password": dept_mgr_pwd})
    assert dm_login_res.status_code == 200
    dm_token = dm_login_res.json()["access_token"]
    dm_headers = {"Authorization": f"Bearer {dm_token}"}

    # DM inspects approvals inbox
    inbox_res = client.get("/approvals/inbox", headers=dm_headers)
    assert inbox_res.status_code == 200
    inbox_ids = [r["id"] for r in inbox_res.json()]
    assert role_req_id in inbox_ids

    # DM approves role request
    appr_res = client.post(
        f"/role-requests/{role_req_id}/approve",
        headers=dm_headers,
        json={"comment": "Welcome to the team!"},
    )
    assert appr_res.status_code == 200

    # ------------------------------------------------------------------------
    # Step 3: Approved User Verifies Role and Accesses Work Items
    # ------------------------------------------------------------------------
    user_headers = {"Authorization": f"Bearer {new_user_token}"}
    pos_res_post = client.get("/me/positions", headers=user_headers)
    assert pos_res_post.status_code == 200
    positions = pos_res_post.json()
    assert len(positions) == 1
    assert positions[0]["role"] == "member"
    assert positions[0]["scope_id"] == str(team.id)

    # ------------------------------------------------------------------------
    # Step 4: Member Creates Compliance Work Item Requiring Approval
    # ------------------------------------------------------------------------
    create_res = client.post(
        "/work-items",
        headers=user_headers,
        json={
            "team_id": str(team.id),
            "kind": "compliance",
            "title": "Quarterly Security Compliance Review",
            "description": "SOC2 evidence collection and verification",
            "priority": "high",
            "requires_approval": True,
        },
    )
    assert create_res.status_code == 201
    item = create_res.json()
    item_id = item["id"]
    assert item["status"] == "open"
    assert item["version"] == 1

    # ------------------------------------------------------------------------
    # Step 5: Member Claims Item (moves to in_progress)
    # ------------------------------------------------------------------------
    claim_res = client.post(f"/work-items/{item_id}/claim", headers=user_headers)
    assert claim_res.status_code == 200
    claimed = claim_res.json()
    assert claimed["status"] == "in_progress"
    assert claimed["assignee_id"] == str(new_user_id)
    assert claimed["version"] == 2

    # ------------------------------------------------------------------------
    # Step 6: Member Submits for Approval (moves to awaiting_approval)
    # ------------------------------------------------------------------------
    submit_res = client.post(
        f"/work-items/{item_id}/transition",
        headers=user_headers,
        json={"to_status": "awaiting_approval", "version": 2},
    )
    assert submit_res.status_code == 200
    assert submit_res.json()["status"] == "awaiting_approval"
    assert submit_res.json()["version"] == 3

    # Self-approval by member is strictly blocked (Rule #6)
    self_appr = client.post(
        f"/work-items/{item_id}/transition",
        headers=user_headers,
        json={"to_status": "resolved", "version": 3},
    )
    assert self_appr.status_code == 403

    # ------------------------------------------------------------------------
    # Step 7: Department Manager Reviews and Approves (moves to resolved)
    # ------------------------------------------------------------------------
    dm_appr_res = client.post(
        f"/work-items/{item_id}/transition",
        headers=dm_headers,
        json={"to_status": "resolved", "version": 3},
    )
    assert dm_appr_res.status_code == 200
    assert dm_appr_res.json()["status"] == "resolved"
    assert dm_appr_res.json()["version"] == 4

    # ------------------------------------------------------------------------
    # Step 8: Member Adds Resolution Comment
    # ------------------------------------------------------------------------
    comment_res = client.post(
        f"/work-items/{item_id}/comments",
        headers=user_headers,
        json={"comment": "Compliance evidence attached and verified."},
    )
    assert comment_res.status_code == 200
    assert comment_res.json()["event_type"] == "comment"

    # ------------------------------------------------------------------------
    # Step 9: Department Manager Closes Item
    # ------------------------------------------------------------------------
    close_res = client.post(
        f"/work-items/{item_id}/transition",
        headers=dm_headers,
        json={"to_status": "closed", "version": 4, "reason": "Completed and archived"},
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "closed"
    assert close_res.json()["version"] == 5

    # ------------------------------------------------------------------------
    # Step 10: Verify Full Append-Only Activity Timeline
    # ------------------------------------------------------------------------
    act_res = client.get(f"/work-items/{item_id}/activity", headers=user_headers)
    assert act_res.status_code == 200
    activities = act_res.json()
    event_types = [a["event_type"] for a in activities]
    assert event_types == [
        "created",
        "claimed",
        "status_changed",
        "status_changed",
        "comment",
        "status_changed",
    ]


# ============================================================================
# 2. Demo Persona Compatibility Flow
# ============================================================================

def test_demo_persona_flow_and_isolation(client):
    """Verify demo personas continue to work with their established roles without data leakage."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_e2e_hierarchy(conn)
            # Create standard demo persona users
            owner = create_user(conn, email=f"alex-{uuid.uuid4().hex[:8]}@example.com", display_name="Alex Vance")
            lead = create_user(conn, email=f"rahul-{uuid.uuid4().hex[:8]}@example.com", display_name="Rahul Patel")
            viewer = create_user(conn, email=f"jordan-{uuid.uuid4().hex[:8]}@example.com", display_name="Jordan Lee")

            grant_or_update_role(conn, user_id=owner.id, role="owner", scope_type="organization", scope_id=org.id)
            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=viewer.id, role="viewer", scope_type="team", scope_id=team.id)

    # 1. Owner can access owner-dashboard
    res_owner = client.get("/me/owner-dashboard", headers={"X-Dev-User-Email": owner.email})
    assert res_owner.status_code == 200
    assert res_owner.json()["organization_id"] == str(org.id)
    assert res_owner.json()["organization_name"] == org.name

    # 2. Team Lead can create and manage work items
    res_create = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": lead.email},
        json={"team_id": str(team.id), "kind": "task", "title": "Lead Created Task"},
    )
    assert res_create.status_code == 201
    item_id = res_create.json()["id"]

    # 3. Viewer can view task but cannot modify
    res_view = client.get(f"/work-items/{item_id}", headers={"X-Dev-User-Email": viewer.email})
    assert res_view.status_code == 200

    res_mut = client.post(f"/work-items/{item_id}/claim", headers={"X-Dev-User-Email": viewer.email})
    assert res_mut.status_code == 403

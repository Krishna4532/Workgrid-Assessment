import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.models import WorkItemCreate, WorkItemUpdate, WorkItemTransitionRequest
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import grant_or_update_role, suspend_role
from app.repositories.work_items_repo import create_work_item, transition_work_item
from app.security import create_access_token, hash_password


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_org_and_team(conn, suffix=None):
    uid = uuid.uuid4().hex[:8]
    s = f"{suffix}-{uid}" if suffix else uid
    org = create_organization(conn, name=f"Auth Org {s}", slug=f"auth-org-{s}")
    dept = create_department(conn, organization_id=org.id, name=f"Auth Dept {s}", slug=f"auth-dept-{s}")
    team = create_team(conn, department_id=dept.id, name=f"Auth Team {s}", slug=f"auth-team-{s}")
    return org, dept, team


# ============================================================================
# 1. Unauthenticated Requests Fail Closed Across All Endpoints (HTTP 401)
# ============================================================================

def test_unauthenticated_requests_fail_closed_across_all_endpoints(client):
    """Verify all protected API endpoints fail closed with HTTP 401 for unauthenticated callers."""
    fake_id = str(uuid.uuid4())

    endpoints = [
        ("GET", "/work-items", None),
        ("POST", "/work-items", {"team_id": fake_id, "kind": "task", "title": "Unauthorized"}),
        ("GET", f"/work-items/{fake_id}", None),
        ("PATCH", f"/work-items/{fake_id}", {"version": 1, "title": "New"}),
        ("POST", f"/work-items/{fake_id}/claim", None),
        ("POST", f"/work-items/{fake_id}/assign", {"version": 1}),
        ("POST", f"/work-items/{fake_id}/transition", {"to_status": "in_progress", "version": 1}),
        ("POST", f"/work-items/{fake_id}/comments", {"comment": "Unauthorized"}),
        ("GET", "/work-items/summary", None),
        ("POST", "/role-requests", {"requested_role": "member", "scope_type": "team", "scope_id": fake_id}),
        ("GET", "/me/role-requests", None),
        ("GET", "/approvals/inbox", None),
        ("POST", f"/role-requests/{fake_id}/approve", None),
        ("POST", f"/role-requests/{fake_id}/reject", {"reason": "Rejected"}),
        ("POST", f"/role-requests/{fake_id}/cancel", None),
        ("GET", "/me/dashboard", None),
        ("GET", "/me/owner-dashboard", None),
        ("GET", "/me/todo", None),
        ("GET", "/notifications", None),
        ("GET", "/notifications/unread-count", None),
        ("POST", "/notifications/read-all", None),
        ("POST", f"/notifications/{fake_id}/read", None),
        ("POST", "/me/events/ticket", None),
        ("GET", "/me/events", None),
        ("GET", "/me", None),
        ("GET", "/me/positions", None),
        ("GET", "/scopes", None),
    ]

    for method, path, payload in endpoints:
        if method == "GET":
            res = client.get(path)
        elif method == "POST":
            res = client.post(path, json=payload)
        elif method == "PATCH":
            res = client.patch(path, json=payload)
        else:
            continue

        assert res.status_code == 401, f"{method} {path} returned {res.status_code}, expected 401"
        data = res.json()
        assert data.get("code") == "UNAUTHORIZED"
        assert "message" in data


# ============================================================================
# 2. Viewer Role In-Scope Privilege Boundaries (HTTP 403 Forbidden)
# ============================================================================

def test_viewer_role_privilege_boundaries(client):
    """Verify viewer (rank 0) can read within team but is forbidden from mutating items."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_and_team(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            viewer = create_user(conn, email=f"vwr-{uuid.uuid4().hex[:8]}@example.com", display_name="Viewer")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=viewer.id, role="viewer", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Open Item"),
            )

    viewer_headers = {"X-Dev-User-Email": viewer.email}

    # 1. Viewer can read team work item detail
    read_res = client.get(f"/work-items/{item.id}", headers=viewer_headers)
    assert read_res.status_code == 200
    assert read_res.json()["id"] == str(item.id)

    # 2. Viewer cannot create work item (requires rank >= 1) -> 403
    create_res = client.post(
        "/work-items",
        headers=viewer_headers,
        json={"team_id": str(team.id), "kind": "task", "title": "Viewer Cannot Create"},
    )
    assert create_res.status_code == 403
    assert create_res.json()["code"] == "FORBIDDEN"

    # 3. Viewer cannot claim work item (requires rank >= 1) -> 403
    claim_res = client.post(f"/work-items/{item.id}/claim", headers=viewer_headers)
    assert claim_res.status_code == 403
    assert claim_res.json()["code"] == "FORBIDDEN"

    # 4. Viewer cannot update work item fields (requires rank >= 1) -> 403
    patch_res = client.patch(
        f"/work-items/{item.id}",
        headers=viewer_headers,
        json={"title": "Viewer Edit Attempt", "version": 1},
    )
    assert patch_res.status_code == 403
    assert patch_res.json()["code"] == "FORBIDDEN"

    # 5. Viewer cannot transition work item (requires rank >= 1) -> 403
    trans_res = client.post(
        f"/work-items/{item.id}/transition",
        headers=viewer_headers,
        json={"to_status": "in_progress", "version": 1},
    )
    assert trans_res.status_code == 403
    assert trans_res.json()["code"] == "FORBIDDEN"

    # 6. Viewer cannot add comments (requires rank >= 1) -> 403
    comment_res = client.post(
        f"/work-items/{item.id}/comments",
        headers=viewer_headers,
        json={"comment": "Viewer Comment Attempt"},
    )
    assert comment_res.status_code == 403
    assert comment_res.json()["code"] == "FORBIDDEN"


# ============================================================================
# 3. Member Role Privilege Boundaries (Rank 1 vs Rank 2+)
# ============================================================================

def test_member_role_privilege_boundaries(client):
    """Verify member (rank 1) cannot assign work, close open items directly, or edit unowned items."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_and_team(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            member_a = create_user(conn, email=f"ma-{uuid.uuid4().hex[:8]}@example.com", display_name="Member A")
            member_b = create_user(conn, email=f"mb-{uuid.uuid4().hex[:8]}@example.com", display_name="Member B")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member_a.id, role="member", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member_b.id, role="member", scope_type="team", scope_id=team.id)

            # Item created by Lead and not assigned to Member A
            lead_item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Lead Work"),
            )

    member_a_headers = {"X-Dev-User-Email": member_a.email}

    # 1. Member cannot assign work (requires rank >= 2, team_leader) -> 403
    assign_res = client.post(
        f"/work-items/{lead_item.id}/assign",
        headers=member_a_headers,
        json={"assignee_id": str(member_a.id), "version": 1},
    )
    assert assign_res.status_code == 403
    assert assign_res.json()["code"] == "FORBIDDEN"

    # 2. Member cannot close an open item directly (only managers rank >= 2) -> 403
    close_res = client.post(
        f"/work-items/{lead_item.id}/transition",
        headers=member_a_headers,
        json={"to_status": "closed", "version": 1, "reason": "Closing attempt"},
    )
    assert close_res.status_code == 403
    assert close_res.json()["code"] == "FORBIDDEN"

    # 3. Member cannot edit fields of an item they neither created nor own -> 403
    edit_res = client.patch(
        f"/work-items/{lead_item.id}",
        headers=member_a_headers,
        json={"title": "Member A Overwrite Attempt", "version": 1},
    )
    assert edit_res.status_code == 403
    assert "only edit items they created or own" in edit_res.json()["detail"].lower()


# ============================================================================
# 4. Constitution Rule #6: Self-Approval Restrictions
# ============================================================================

def test_self_approval_restrictions_constitution_rule_6(client):
    """Verify nobody approves an item they own or decides their own role request."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_and_team(conn)
            # Manager who is also creator and assignee
            mgr = create_user(conn, email=f"mgr-{uuid.uuid4().hex[:8]}@example.com", display_name="Manager")
            independent_lead = create_user(conn, email=f"lead2-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead 2")

            grant_or_update_role(conn, user_id=mgr.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=independent_lead.id, role="team_leader", scope_type="team", scope_id=team.id)

            # Item requiring approval where mgr is creator and assignee
            item = create_work_item(
                conn,
                creator_id=mgr.id,
                creator_name=mgr.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="compliance",
                    title="Self Approval Check",
                    requires_approval=True,
                    assignee_id=mgr.id,
                ),
            )
            # Transition to awaiting_approval
            item_awaiting = transition_work_item(conn, item.id, mgr.id, "in_progress", 1)
            item_awaiting = transition_work_item(conn, item.id, mgr.id, "awaiting_approval", 2)

    # 1. Manager creator/assignee cannot approve own item -> 403 Forbidden
    self_appr = client.post(
        f"/work-items/{item.id}/transition",
        headers={"X-Dev-User-Email": mgr.email},
        json={"to_status": "resolved", "version": 3},
    )
    assert self_appr.status_code == 403
    assert "cannot approve or reject their own work item" in self_appr.json()["detail"].lower()

    # 2. Independent team leader CAN approve the item -> 200 OK
    valid_appr = client.post(
        f"/work-items/{item.id}/transition",
        headers={"X-Dev-User-Email": independent_lead.email},
        json={"to_status": "resolved", "version": 3},
    )
    assert valid_appr.status_code == 200
    assert valid_appr.json()["status"] == "resolved"

    # 3. Role Request self-decision restriction
    with get_db_connection() as conn:
        with conn.transaction():
            owner = create_user(conn, email=f"owner-{uuid.uuid4().hex[:8]}@example.com", display_name="Org Owner")
            grant_or_update_role(conn, user_id=owner.id, role="owner", scope_type="organization", scope_id=org.id)

    # Owner submits a team role request
    req_res = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": owner.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team.id)},
    )
    assert req_res.status_code == 201
    req_id = req_res.json()["id"]

    # Owner attempts to approve their own request -> 403 Forbidden
    self_decide = client.post(
        f"/role-requests/{req_id}/approve",
        headers={"X-Dev-User-Email": owner.email},
    )
    assert self_decide.status_code == 403
    assert "nobody can decide their own request" in self_decide.json()["detail"].lower()


# ============================================================================
# 5. Constitution Rule #9: Information Hiding for Out-of-Scope (404, never 403)
# ============================================================================

def test_information_hiding_constitution_rule_9(client):
    """Verify resources outside caller's scope are strictly invisible with HTTP 404."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_a, _, team_a = _setup_org_and_team(conn, "alpha")
            org_b, _, team_b = _setup_org_and_team(conn, "beta")

            user_a = create_user(conn, email=f"ua-{uuid.uuid4().hex[:8]}@example.com", display_name="User A")
            user_b = create_user(conn, email=f"ub-{uuid.uuid4().hex[:8]}@example.com", display_name="User B")

            grant_or_update_role(conn, user_id=user_a.id, role="member", scope_type="team", scope_id=team_a.id)
            grant_or_update_role(conn, user_id=user_b.id, role="member", scope_type="team", scope_id=team_b.id)

            item_b = create_work_item(
                conn,
                creator_id=user_b.id,
                creator_name=user_b.display_name,
                dto=WorkItemCreate(team_id=team_b.id, kind="task", title="Secret Team B Work"),
            )

    user_a_headers = {"X-Dev-User-Email": user_a.email}

    # 1. Detail lookup of Item B from User A returns 404 (Information Hiding)
    res_detail = client.get(f"/work-items/{item_b.id}", headers=user_a_headers)
    assert res_detail.status_code == 404
    assert res_detail.json()["code"] == "NOT_FOUND"

    # 2. Claim attempt on Item B from User A returns 404
    res_claim = client.post(f"/work-items/{item_b.id}/claim", headers=user_a_headers)
    assert res_claim.status_code == 404

    # 3. Transition attempt on Item B from User A returns 404
    res_trans = client.post(
        f"/work-items/{item_b.id}/transition",
        headers=user_a_headers,
        json={"to_status": "in_progress", "version": 1},
    )
    assert res_trans.status_code == 404

    # 4. Comments attempt on Item B from User A returns 404
    res_comment = client.post(
        f"/work-items/{item_b.id}/comments",
        headers=user_a_headers,
        json={"comment": "Cross-org snooping"},
    )
    assert res_comment.status_code == 404

    # 5. List items filtered to foreign team returns 404
    res_list = client.get(f"/work-items?team_id={team_b.id}", headers=user_a_headers)
    assert res_list.status_code == 404

    # 6. Create item targeting foreign team returns 404
    res_create = client.post(
        "/work-items",
        headers=user_a_headers,
        json={"team_id": str(team_b.id), "kind": "task", "title": "Sneak Item"},
    )
    assert res_create.status_code == 404


# ============================================================================
# 6. Role Request Escalation and Cross-Organization Boundaries
# ============================================================================

def test_role_request_escalation_and_cross_organization_boundaries(client):
    """Verify unassigned users cannot request elevated roles and assigned users cannot request cross-org."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_a, _, team_a = _setup_org_and_team(conn, "org1")
            org_b, _, team_b = _setup_org_and_team(conn, "org2")

            unassigned = create_user(conn, email=f"unass-{uuid.uuid4().hex[:8]}@example.com", display_name="Unassigned")
            assigned_a = create_user(conn, email=f"assa-{uuid.uuid4().hex[:8]}@example.com", display_name="Assigned A")

            grant_or_update_role(conn, user_id=assigned_a.id, role="member", scope_type="team", scope_id=team_a.id)

    # 1. Unassigned user requesting elevated role 'owner' -> 403 Forbidden
    res_elevated = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": unassigned.email},
        json={"requested_role": "owner", "scope_type": "organization", "scope_id": str(org_a.id)},
    )
    assert res_elevated.status_code == 403
    assert "only request entry-level roles" in res_elevated.json()["detail"].lower()

    # 2. Unassigned user requesting 'team_leader' -> 403 Forbidden
    res_tl = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": unassigned.email},
        json={"requested_role": "team_leader", "scope_type": "team", "scope_id": str(team_a.id)},
    )
    assert res_tl.status_code == 403

    # 3. Assigned user in Org A requesting role in Org B -> 403 Forbidden
    res_cross = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": assigned_a.email},
        json={"requested_role": "member", "scope_type": "team", "scope_id": str(team_b.id)},
    )
    assert res_cross.status_code == 403
    assert "outside your assigned organization" in res_cross.json()["detail"].lower()


# ============================================================================
# 7. Suspended Account and Dynamic Role Revocation
# ============================================================================

def test_suspended_account_and_dynamic_position_revocation(client):
    """Verify suspended accounts cannot log in and role changes take effect immediately."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_and_team(conn)
            pwd = "ValidPassword123!"
            user = create_user(
                conn,
                email=f"dyn-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Dynamic User",
                password_hash=hash_password(pwd),
                status="active",
            )
            assignment = grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    token = create_access_token(subject=str(user.id), email=user.email)
    auth_header = {"Authorization": f"Bearer {token}"}

    # 1. User can access work items initially
    res1 = client.get("/work-items", headers=auth_header)
    assert res1.status_code == 200

    # 2. Suspend assignment in database
    with get_db_connection() as conn:
        with conn.transaction():
            suspend_role(conn, assignment.id, actor_id=user.id, reason="Security review")

    # 3. User immediately loses access to team work items on next request (without token reissuance)
    res2 = client.post(
        "/work-items",
        headers=auth_header,
        json={"team_id": str(team.id), "kind": "task", "title": "After Suspension"},
    )
    assert res2.status_code in (403, 404)

    # 4. Suspend user account entirely
    with get_db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET status = 'suspended' WHERE id = %s;", (user.id,))

    # 5. Suspended user cannot log in
    login_res = client.post("/auth/login", json={"email": user.email, "password": pwd})
    assert login_res.status_code == 401


# ============================================================================
# 8. Cross-Organization Data Leakage Prevention
# ============================================================================

def test_cross_organization_data_leakage_prevention(client):
    """Verify dashboard, todo list, summary counts, and searches never leak cross-org data."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_a, _, team_a = _setup_org_and_team(conn, "leak_a")
            org_b, _, team_b = _setup_org_and_team(conn, "leak_b")

            user_a = create_user(conn, email=f"leak-a-{uuid.uuid4().hex[:8]}@example.com", display_name="Org A User")
            user_b = create_user(conn, email=f"leak-b-{uuid.uuid4().hex[:8]}@example.com", display_name="Org B User")

            grant_or_update_role(conn, user_id=user_a.id, role="team_leader", scope_type="team", scope_id=team_a.id)
            grant_or_update_role(conn, user_id=user_b.id, role="team_leader", scope_type="team", scope_id=team_b.id)

            create_work_item(
                conn,
                creator_id=user_a.id,
                creator_name=user_a.display_name,
                dto=WorkItemCreate(team_id=team_a.id, kind="task", title="Alpha Public Task"),
            )
            create_work_item(
                conn,
                creator_id=user_b.id,
                creator_name=user_b.display_name,
                dto=WorkItemCreate(team_id=team_b.id, kind="task", title="Beta Confidential Task"),
            )

    headers_a = {"X-Dev-User-Email": user_a.email}

    # 1. /work-items list returns ONLY Alpha Task
    res_list = client.get("/work-items", headers=headers_a)
    assert res_list.status_code == 200
    items = res_list.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Alpha Public Task"
    assert all("Beta" not in i["title"] for i in items)

    # 2. Search query for "Confidential" returns empty results for User A
    res_search = client.get("/work-items?q=Confidential", headers=headers_a)
    assert res_search.status_code == 200
    assert len(res_search.json()["items"]) == 0

    # 3. /work-items/summary totals exactly 1
    res_summary = client.get("/work-items/summary", headers=headers_a)
    assert res_summary.status_code == 200
    assert res_summary.json()["total"] == 1

    # 4. /me/dashboard overview reflects exactly 1 open item
    res_dash = client.get("/me/dashboard", headers=headers_a)
    assert res_dash.status_code == 200
    assert res_dash.json()["work_summary"]["team_open_count"] == 1


# ============================================================================
# 9. Forbidden Requests Do Not Mutate State or Create Activity Records
# ============================================================================

def test_forbidden_requests_do_not_mutate_state_or_create_records(client):
    """Verify rejected/forbidden operations leave database records completely unmutated."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_and_team(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            viewer = create_user(conn, email=f"vwr-{uuid.uuid4().hex[:8]}@example.com", display_name="Viewer")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=viewer.id, role="viewer", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Original Title", priority="normal"),
            )

    # Record initial counts
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM activity WHERE work_item_id = %s;", (item.id,))
            initial_activity_count = cur.fetchone()[0]

    # Attempt forbidden mutations by viewer
    client.patch(
        f"/work-items/{item.id}",
        headers={"X-Dev-User-Email": viewer.email},
        json={"title": "Hacked Title", "version": 1},
    )
    client.post(
        f"/work-items/{item.id}/transition",
        headers={"X-Dev-User-Email": viewer.email},
        json={"to_status": "closed", "version": 1, "reason": "Hacked"},
    )
    client.post(
        f"/work-items/{item.id}/claim",
        headers={"X-Dev-User-Email": viewer.email},
    )

    # Verify state in database is pristine
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, status, version, assignee_id FROM work_items WHERE id = %s;",
                (item.id,),
            )
            title, status, version, assignee_id = cur.fetchone()
            assert title == "Original Title"
            assert status == "open"
            assert version == 1
            assert assignee_id is None

            cur.execute("SELECT count(*) FROM activity WHERE work_item_id = %s;", (item.id,))
            current_activity_count = cur.fetchone()[0]
            assert current_activity_count == initial_activity_count

import uuid
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import grant_or_update_role, suspend_role


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_team_and_users(conn):
    org = create_organization(conn, name="Test Org", slug=f"org-{uuid.uuid4().hex[:8]}")
    dept = create_department(conn, organization_id=org.id, name="Test Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
    team = create_team(conn, department_id=dept.id, name="Test Team", slug=f"team-{uuid.uuid4().hex[:8]}")

    manager = create_user(conn, email=f"mgr-{uuid.uuid4().hex[:8]}@example.com", display_name="Manager")
    member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
    viewer = create_user(conn, email=f"vwr-{uuid.uuid4().hex[:8]}@example.com", display_name="Viewer")
    outsider = create_user(conn, email=f"out-{uuid.uuid4().hex[:8]}@example.com", display_name="Outsider")

    grant_or_update_role(conn, user_id=manager.id, role="team_leader", scope_type="team", scope_id=team.id)
    grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)
    grant_or_update_role(conn, user_id=viewer.id, role="viewer", scope_type="team", scope_id=team.id)

    return org, dept, team, manager, member, viewer, outsider


def test_create_work_item_and_information_hiding(client):
    """Verify member can create work item, viewer cannot, and outsider receives 404."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team, manager, member, viewer, outsider = _setup_team_and_users(conn)

    # 1. Member creates work item
    res = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={
            "team_id": str(team.id),
            "kind": "task",
            "title": "Database connection optimization",
            "description": "Analyze connection pool parameters",
            "priority": "high",
        },
    )
    assert res.status_code == 201
    item = res.json()
    assert item["title"] == "Database connection optimization"
    assert item["status"] == "open"
    assert item["version"] == 1
    item_id = item["id"]

    # 2. Viewer cannot create work item (403 Forbidden)
    res_v = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": viewer.email},
        json={"team_id": str(team.id), "kind": "task", "title": "Viewer Task"},
    )
    assert res_v.status_code == 403

    # 3. Outsider looking up the item gets 404 (Rule #9: Information Hiding)
    res_out = client.get(
        f"/work-items/{item_id}",
        headers={"X-Dev-User-Email": outsider.email},
    )
    assert res_out.status_code == 404


def test_suspending_assignment_removes_access_on_next_request(client):
    """Verify role changes are read from PostgreSQL for every protected request."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, member, _, _ = _setup_team_and_users(conn)
            assignment = grant_or_update_role(
                conn,
                user_id=member.id,
                role="member",
                scope_type="team",
                scope_id=team.id,
            )

    created = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={"team_id": str(team.id), "kind": "task", "title": "Access revocation check"},
    )
    assert created.status_code == 201

    with get_db_connection() as conn:
        with conn.transaction():
            suspend_role(conn, assignment.id, reason="Access review")

    response = client.get(
        f"/work-items/{created.json()['id']}",
        headers={"X-Dev-User-Email": member.email},
    )
    assert response.status_code == 404


def test_keyset_cursor_pagination_and_search(client):
    """Verify keyset pagination produces deterministic results and full-text search works."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team, manager, member, _, _ = _setup_team_and_users(conn)

    # Create 5 items
    created_ids = []
    for i in range(5):
        res = client.post(
            "/work-items",
            headers={"X-Dev-User-Email": member.email},
            json={
                "team_id": str(team.id),
                "kind": "incident" if i == 2 else "task",
                "title": f"Item {i} - {'CRITICAL_BUG' if i == 2 else 'routine task'}",
                "description": f"Details for item {i}",
            },
        )
        assert res.status_code == 201
        created_ids.append(res.json()["id"])

    # 1. Page 1 with limit=2
    p1 = client.get(
        f"/work-items?team_id={team.id}&limit=2",
        headers={"X-Dev-User-Email": member.email},
    )
    assert p1.status_code == 200
    data1 = p1.json()
    assert len(data1["items"]) == 2
    assert data1["has_more"] is True
    assert data1["next_cursor"] is not None

    # 2. Page 2 using cursor
    p2 = client.get(
        f"/work-items?team_id={team.id}&limit=2&cursor={data1['next_cursor']}",
        headers={"X-Dev-User-Email": member.email},
    )
    assert p2.status_code == 200
    data2 = p2.json()
    assert len(data2["items"]) == 2
    # Ensure no duplicate items between page 1 and page 2
    p1_ids = {item["id"] for item in data1["items"]}
    p2_ids = {item["id"] for item in data2["items"]}
    assert p1_ids.isdisjoint(p2_ids)

    # 3. Full text search
    s_res = client.get(
        f"/work-items?team_id={team.id}&q=CRITICAL_BUG",
        headers={"X-Dev-User-Email": member.email},
    )
    assert s_res.status_code == 200
    search_data = s_res.json()
    assert len(search_data["items"]) == 1
    assert "CRITICAL_BUG" in search_data["items"][0]["title"]


def test_optimistic_concurrency_version_rejection(client):
    """Verify modifying a work item with an outdated version returns HTTP 409 Conflict."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, member, _, _ = _setup_team_and_users(conn)

    create_res = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={"team_id": str(team.id), "kind": "task", "title": "Concurrent Task"},
    )
    item = create_res.json()
    item_id = item["id"]
    assert item["version"] == 1

    # First edit with version 1 succeeds
    edit1 = client.patch(
        f"/work-items/{item_id}",
        headers={"X-Dev-User-Email": member.email},
        json={"title": "Updated Title Once", "version": 1},
    )
    assert edit1.status_code == 200
    assert edit1.json()["version"] == 2

    # Second edit still using stale version 1 must get 409
    edit2 = client.patch(
        f"/work-items/{item_id}",
        headers={"X-Dev-User-Email": member.email},
        json={"title": "Stale Edit Attempt", "version": 1},
    )
    assert edit2.status_code == 409
    assert "version mismatch" in edit2.json()["detail"].lower()


def test_atomic_claim_and_competing_conflict(client):
    """Verify atomic claim moves item to in_progress and competing claim returns 409 naming winner."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, manager, member, _, _ = _setup_team_and_users(conn)

    create_res = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={"team_id": str(team.id), "kind": "incident", "title": "Outage Incident"},
    )
    item_id = create_res.json()["id"]

    # Member claims item
    claim1 = client.post(
        f"/work-items/{item_id}/claim",
        headers={"X-Dev-User-Email": member.email},
    )
    assert claim1.status_code == 200
    claimed_item = claim1.json()
    assert claimed_item["status"] == "in_progress"
    assert claimed_item["assignee_id"] == str(member.id)

    # Manager attempts competing claim -> 409 Conflict
    claim2 = client.post(
        f"/work-items/{item_id}/claim",
        headers={"X-Dev-User-Email": manager.email},
    )
    assert claim2.status_code == 409
    assert "already claimed" in claim2.json()["detail"].lower()
    assert member.display_name in claim2.json()["detail"]


def test_work_item_workflow_and_self_approval_restriction(client):
    """Verify workflow rules and strict owner-cannot-approve rule on awaiting_approval items."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, manager, member, _, _ = _setup_team_and_users(conn)

    # Create item that requires approval
    res = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={
            "team_id": str(team.id),
            "kind": "compliance",
            "title": "Audit Review",
            "requires_approval": True,
        },
    )
    item = res.json()
    item_id = item["id"]

    # 1. Member claims item (moves to in_progress)
    claim_res = client.post(f"/work-items/{item_id}/claim", headers={"X-Dev-User-Email": member.email})
    assert claim_res.status_code == 200
    item = claim_res.json()

    # 2. Member transitions in_progress to awaiting_approval
    trans1 = client.post(
        f"/work-items/{item_id}/transition",
        headers={"X-Dev-User-Email": member.email},
        json={"to_status": "awaiting_approval", "version": item["version"]},
    )
    assert trans1.status_code == 200
    item = trans1.json()
    assert item["status"] == "awaiting_approval"

    # 3. Rule #6 check: The item assignee (member) CANNOT approve their own work item
    self_app = client.post(
        f"/work-items/{item_id}/transition",
        headers={"X-Dev-User-Email": member.email},
        json={"to_status": "resolved", "version": item["version"]},
    )
    assert self_app.status_code == 403
    assert "cannot approve or reject their own work" in self_app.json()["detail"].lower()

    # 4. Manager (who is NOT the assignee) approves the item (moves to resolved)
    mgr_app = client.post(
        f"/work-items/{item_id}/transition",
        headers={"X-Dev-User-Email": manager.email},
        json={"to_status": "resolved", "version": item["version"]},
    )
    assert mgr_app.status_code == 200
    assert mgr_app.json()["status"] == "resolved"


def test_activity_timeline_and_immutability(client):
    """Verify work item activity records are appended and immutable."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, _, member, _, _ = _setup_team_and_users(conn)

    create_res = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email},
        json={"team_id": str(team.id), "kind": "task", "title": "Timeline Test"},
    )
    item_id = create_res.json()["id"]

    update_res = client.patch(
        f"/work-items/{item_id}",
        headers={"X-Dev-User-Email": member.email},
        json={"title": "Timeline Test Updated", "version": create_res.json()["version"]},
    )
    assert update_res.status_code == 200

    # Add comment
    cmt_res = client.post(
        f"/work-items/{item_id}/comments",
        headers={"X-Dev-User-Email": member.email},
        json={"comment": "Investigating the issue now."},
    )
    assert cmt_res.status_code == 200

    # Retrieve activity
    act_res = client.get(
        f"/work-items/{item_id}/activity",
        headers={"X-Dev-User-Email": member.email},
    )
    assert act_res.status_code == 200
    activity = act_res.json()
    assert len(activity) == 3  # 'created', 'updated', and 'comment' events
    assert activity[0]["event_type"] == "created"
    assert activity[1]["event_type"] == "updated"
    assert activity[1]["metadata"]["title"] == {
        "from": "Timeline Test",
        "to": "Timeline Test Updated",
    }
    assert activity[2]["event_type"] == "comment"
    assert activity[2]["comment"] == "Investigating the issue now."

    # Direct database test of trigger immutability
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM activity WHERE work_item_id = %s LIMIT 1;", (item_id,))
            act_id = cur.fetchone()[0]

            with pytest.raises(errors.RaiseException) as exc_info:
                cur.execute("UPDATE activity SET comment = 'Tampered' WHERE id = %s;", (act_id,))
            conn.rollback()
            assert "append-only" in str(exc_info.value).lower()

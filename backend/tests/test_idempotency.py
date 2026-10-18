import uuid
from concurrent.futures import ThreadPoolExecutor
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


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_idempotent_work_item_creation(client):
    """Verify duplicate requests with same Idempotency-Key return cached response and create one item."""
    with get_db_connection() as conn:
        with conn.transaction():
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"key-{uuid.uuid4().hex}"
    payload = {
        "team_id": str(team.id),
        "kind": "task",
        "title": "Idempotent Task",
        "description": "Double-click safety test",
    }

    # 1. First request
    res1 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload,
    )
    assert res1.status_code == 201
    item1 = res1.json()

    # 2. Second request with identical key and payload (simulate double-click or network retry)
    res2 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload,
    )
    assert res2.status_code == 201
    item2 = res2.json()

    # Must return identical item ID
    assert item1["id"] == item2["id"]

    # Verify only ONE item was inserted in database
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items WHERE team_id = %s;", (team.id,))
            count = cur.fetchone()[0]
            assert count == 1


def test_idempotency_key_payload_mismatch_raises_422(client):
    """Verify reusing same key with different payload returns HTTP 422."""
    with get_db_connection() as conn:
        with conn.transaction():
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"mismatch-{uuid.uuid4().hex}"
    payload1 = {"team_id": str(team.id), "kind": "task", "title": "Original Title"}
    payload2 = {"team_id": str(team.id), "kind": "task", "title": "Different Title"}

    res1 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload1,
    )
    assert res1.status_code == 201

    # Reusing same key with different payload must fail with 422
    res2 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload2,
    )
    assert res2.status_code == 422
    assert "different request payload" in res2.json()["detail"].lower()


def test_failed_request_does_not_cache_idempotency_key(client):
    """Verify that a failed request leaves no key, allowing subsequent retry to succeed."""
    with get_db_connection() as conn:
        with conn.transaction():
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"fail-retry-{uuid.uuid4().hex}"
    nonexistent_team_id = str(uuid.uuid4())

    # 1. Request fails due to non-existent / unauthorized team (404)
    res1 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json={"team_id": nonexistent_team_id, "kind": "task", "title": "Failed Attempt"},
    )
    assert res1.status_code == 404

    # 2. Subsequent retry with valid team using the same key succeeds
    res2 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json={"team_id": str(team.id), "kind": "task", "title": "Successful Retry"},
    )
    assert res2.status_code == 201
    assert res2.json()["title"] == "Successful Retry"


def test_concurrent_idempotent_creation_commits_once(client):
    """Verify concurrent first attempts serialize on the idempotency key."""
    with get_db_connection() as conn:
        with conn.transaction():
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    key = f"concurrent-{uuid.uuid4()}"
    payload = {
        "team_id": str(team.id),
        "kind": "task",
        "title": "Concurrent Idempotent Task",
    }
    headers = {"X-Dev-User-Email": member.email, "Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.post, "/work-items", headers=headers, json=payload)
        second = executor.submit(client.post, "/work-items", headers=headers, json=payload)
        responses = [first.result(timeout=15), second.result(timeout=15)]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM work_items WHERE team_id = %s AND title = %s;",
                (team.id, payload["title"]),
            )
            assert cur.fetchone()[0] == 1


def test_idempotency_key_is_bound_to_target_resource(client):
    """Verify an identical payload cannot replay another item's mutation response."""
    with get_db_connection() as conn:
        with conn.transaction():
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    headers = {"X-Dev-User-Email": member.email}
    items = []
    for title in ("First comment target", "Second comment target"):
        response = client.post(
            "/work-items",
            headers=headers,
            json={"team_id": str(team.id), "kind": "task", "title": title},
        )
        assert response.status_code == 201
        items.append(response.json()["id"])

    key = f"cross-resource-{uuid.uuid4()}"
    first = client.post(
        f"/work-items/{items[0]}/comments",
        headers={**headers, "Idempotency-Key": key},
        json={"comment": "Same comment body"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/work-items/{items[1]}/comments",
        headers={**headers, "Idempotency-Key": key},
        json={"comment": "Same comment body"},
    )
    assert second.status_code == 422

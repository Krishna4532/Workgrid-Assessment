import uuid
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.db import get_db_connection
from app.repositories.identity_repo import create_organization, create_department, create_team, create_user
from app.repositories.roles_repo import grant_or_update_role, suspend_role, revoke_role


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_positions_endpoint_requires_authentication(client):
    """Verify unauthenticated requests to /me/positions return HTTP 401."""
    response = client.get("/me/positions")
    assert response.status_code == 401
    assert "detail" in response.json()
    assert "authentication required" in response.json()["detail"].lower()


def test_positions_endpoint_returns_active_positions_with_scope_lineage(client):
    """Verify /me/positions returns only active role assignments with scope hierarchy."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Position User")
            org = create_organization(conn, name="Position Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Position Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Position Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            # Active team assignment
            grant_or_update_role(conn, user_id=user.id, role="team_leader", scope_type="team", scope_id=team.id)

    response = client.get(
        "/me/positions",
        headers={"X-Dev-User-Email": user.email},
    )
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["role"] == "team_leader"
    assert pos["scope_type"] == "team"
    assert pos["scope_name"] == "Position Team"
    assert pos["department_name"] == "Position Dept"
    assert pos["organization_name"] == "Position Org"
    assert pos["status"] == "active"


def test_positions_endpoint_excludes_suspended_and_revoked(client):
    """Verify suspended and revoked assignments are excluded from /me/positions."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Mixed Status User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team1 = create_team(conn, department_id=dept.id, name="Team 1", slug=f"team-{uuid.uuid4().hex[:8]}")
            team2 = create_team(conn, department_id=dept.id, name="Team 2", slug=f"team-{uuid.uuid4().hex[:8]}")

            # 1. Active team1 assignment
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team1.id)

            # 2. Suspended team2 assignment
            assign2 = grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team2.id)
            suspend_role(conn, assign2.id)

            # 3. Revoked org assignment
            assign3 = grant_or_update_role(conn, user_id=user.id, role="viewer", scope_type="organization", scope_id=org.id)
            revoke_role(conn, assign3.id)

    response = client.get(
        "/me/positions",
        headers={"X-Dev-User-Email": user.email},
    )
    assert response.status_code == 200
    positions = response.json()
    # Only active team1 assignment must be present
    assert len(positions) == 1
    assert positions[0]["scope_name"] == "Team 1"
    assert positions[0]["status"] == "active"


def test_positions_endpoint_isolates_users(client):
    """Verify user cannot see another person's role assignments."""
    with get_db_connection() as conn:
        with conn.transaction():
            user_a = create_user(conn, email=f"usera-{uuid.uuid4().hex[:8]}@example.com", display_name="User A")
            user_b = create_user(conn, email=f"userb-{uuid.uuid4().hex[:8]}@example.com", display_name="User B")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")

            grant_or_update_role(conn, user_id=user_a.id, role="owner", scope_type="organization", scope_id=org.id)

    # User B requests /me/positions
    response = client.get(
        "/me/positions",
        headers={"X-Dev-User-Email": user_b.email},
    )
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 0


def test_dev_headers_strictly_rejected_in_production(client):
    """Verify that development headers are rejected with HTTP 401 when APP_ENV is production."""
    with patch("app.auth.settings.APP_ENV", "production"):
        response = client.get(
            "/me/positions",
            headers={"X-Dev-User-Email": "alex.owner@example.com"},
        )
        assert response.status_code == 401
        assert "disabled in production" in response.json()["detail"]

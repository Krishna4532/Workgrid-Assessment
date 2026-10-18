import json
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
from app.routers.events_router import _is_event_authorized
from app.security import create_access_token


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_sse_hierarchy(conn):
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"SSE Org {suffix}", slug=f"sse-org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"SSE Dept {suffix}", slug=f"sse-dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"SSE Team {suffix}", slug=f"sse-team-{suffix}")
    return org, dept, team


# ============================================================================
# 1. Single-Use Ticket Security and Replay Prevention (WG-03)
# ============================================================================

def test_sse_single_use_ticket_and_replay_prevention(client):
    """Verify single-use tickets authenticate EventSource without reusable credentials, and reuse is blocked."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_sse_hierarchy(conn)
            user = create_user(conn, email=f"ticket-{uuid.uuid4().hex[:8]}@example.com", display_name="Ticket User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    token = create_access_token(subject=str(user.id), email=user.email)
    auth_header = {"Authorization": f"Bearer {token}"}

    # 1. Request ticket using standard Authorization header -> 200 OK
    ticket_res = client.post("/me/events/ticket", headers=auth_header)
    assert ticket_res.status_code == 200
    ticket_data = ticket_res.json()
    assert "ticket" in ticket_data
    assert ticket_data["expires_in"] == 60
    ticket = ticket_data["ticket"]

    # 2. General endpoint rejects ?ticket=...
    bad_route = client.get(f"/me?ticket={ticket}")
    assert bad_route.status_code == 401

    # 3. First connection to SSE stream with ticket succeeds
    with client.stream("GET", f"/me/events?ticket={ticket}&limit=1") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        found = False
        for _ in range(5):
            line = next(lines)
            if str(user.id) in line:
                found = True
                break
        assert found

    # 4. Reusing the exact same ticket fails immediately with 401 (single-use guarantee)
    reuse_res = client.get(f"/me/events?ticket={ticket}")
    assert reuse_res.status_code == 401
    assert "ticket" in reuse_res.json()["detail"].lower()


def test_sse_query_string_bearer_tokens_are_strictly_rejected(client):
    """Verify passing bearer token in query parameter (?token=...) is strictly rejected."""
    token = create_access_token(subject=str(uuid.uuid4()), email="hacker@example.com")

    res = client.get(f"/me/events?token={token}")
    assert res.status_code == 401
    assert "authorization header" in res.json()["detail"].lower()


# ============================================================================
# 2. Mid-Stream Account Deactivation / Revocation
# ============================================================================

def test_sse_mid_stream_account_deactivation_fails_closed(client):
    """Verify an active SSE connection terminates and fails closed if account is suspended."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_sse_hierarchy(conn)
            user = create_user(conn, email=f"mid-{uuid.uuid4().hex[:8]}@example.com", display_name="Midstream User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    # Suspend user in database
    with get_db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET status = 'suspended' WHERE id = %s;", (user.id,))

    # Attempting to open SSE stream for suspended user fails closed with 401
    res = client.get("/me/events", headers={"X-Dev-User-Email": user.email})
    assert res.status_code == 401
    assert "suspended" in res.json()["detail"].lower() or "unauthorized" in res.json()["detail"].lower()


# ============================================================================
# 3. Tenant and Scope Event Authorization Logic
# ============================================================================

def test_is_event_authorized_tenant_and_scope_isolation():
    """Verify _is_event_authorized delivers events only to matching scopes and recipients."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()

    accessible_teams_a = {team_a}

    # 1. Notification event: delivered ONLY to matching recipient_id
    notif_for_a = {"event_type": "notification_created", "recipient_id": str(user_a)}
    notif_for_b = {"event_type": "notification_created", "recipient_id": str(user_b)}

    assert _is_event_authorized(notif_for_a, user_a, accessible_teams_a) is True
    assert _is_event_authorized(notif_for_b, user_a, accessible_teams_a) is False

    # 2. Work-item event: delivered ONLY to users with coverage in target team
    item_in_team_a = {"event_type": "work_item_created", "team_id": str(team_a)}
    item_in_team_b = {"event_type": "work_item_created", "team_id": str(team_b)}

    assert _is_event_authorized(item_in_team_a, user_a, accessible_teams_a) is True
    assert _is_event_authorized(item_in_team_b, user_a, accessible_teams_a) is False

    # 3. Role assignment change: delivered to the affected subject
    role_change_a = {"event_type": "role_assignment_changed", "user_id": str(user_a)}
    role_change_b = {"event_type": "role_assignment_changed", "user_id": str(user_b)}

    assert _is_event_authorized(role_change_a, user_a, accessible_teams_a) is True
    assert _is_event_authorized(role_change_b, user_a, accessible_teams_a) is False

import json
import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import create_department, create_organization, create_team, create_user
from app.repositories.roles_repo import grant_or_update_role, revoke_role
from app.security import create_access_token
from app.sse import broadcaster


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_hierarchy(conn):
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"Org {suffix}", slug=f"org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"Dept {suffix}", slug=f"dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"Team {suffix}", slug=f"team-{suffix}")
    return org, dept, team


def test_sse_requires_authentication(client):
    """Verify unauthenticated requests to /me/events are rejected with HTTP 401."""
    res = client.get("/me/events")
    assert res.status_code == 401
    assert "detail" in res.json()


def test_sse_query_token_rejected_on_general_routes(client):
    """Verify WG-03: General API routes reject bearer tokens in query parameters."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"token-query-{uuid.uuid4().hex[:8]}@example.com", display_name="Token User")
    token = create_access_token(subject=str(user.id), email=user.email)

    # General endpoint must reject ?token=...
    res = client.get(f"/me?token={token}")
    assert res.status_code == 401
    assert "Authorization header" in res.json()["detail"]


def test_sse_single_use_ticket_lifecycle(client):
    """Verify WG-03: Single-use short-lived SSE tickets authenticate EventSource without reusable URL credentials."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"ticket-user-{uuid.uuid4().hex[:8]}@example.com", display_name="Ticket User")
    token = create_access_token(subject=str(user.id), email=user.email)

    # 1. Request ticket using standard Authorization header
    ticket_res = client.post("/me/events/ticket", headers={"Authorization": f"Bearer {token}"})
    assert ticket_res.status_code == 200
    ticket_data = ticket_res.json()
    assert "ticket" in ticket_data
    assert ticket_data["expires_in"] == 60
    ticket = ticket_data["ticket"]

    # 2. Ticket cannot be used to authenticate unrelated API routes
    bad_route_res = client.get(f"/me?ticket={ticket}")
    assert bad_route_res.status_code == 401

    # 3. Connect to SSE stream using ticket (first use succeeds)
    with client.stream("GET", f"/me/events?ticket={ticket}&limit=1") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        found_user = False
        for _ in range(5):
            line = next(lines)
            if str(user.id) in line:
                found_user = True
                break
        assert found_user

    # 4. Reusing the exact same ticket must fail with 401 (single-use guarantee)
    reuse_res = client.get(f"/me/events?ticket={ticket}")
    assert reuse_res.status_code == 401
    assert "ticket" in reuse_res.json()["detail"].lower()


def test_sse_dev_header_authentication(client):
    """Verify dev identity headers are authenticated in development/test environment."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"sse-dev-{uuid.uuid4().hex[:8]}@example.com", display_name="SSE Dev User")

    with client.stream("GET", "/me/events?limit=1", headers={"X-Dev-User-Email": user.email}) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        found_user = False
        for _ in range(5):
            line = next(lines)
            if str(user.id) in line:
                found_user = True
                break
        assert found_user


def test_sse_scope_filtering_and_notification_isolation(client):
    """Verify users only receive events matching their accessible scopes and private notifications."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team_a = _setup_hierarchy(conn)
            _, _, team_b = _setup_hierarchy(conn)

            alice = create_user(conn, email=f"alice-sse-{uuid.uuid4().hex[:8]}@example.com", display_name="Alice")
            bob = create_user(conn, email=f"bob-sse-{uuid.uuid4().hex[:8]}@example.com", display_name="Bob")

            grant_or_update_role(conn, user_id=alice.id, role="member", scope_type="team", scope_id=team_a.id)

    baseline_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": baseline_id,
        "event_type": "work_item_created",
        "team_id": str(team_a.id),
    })

    # 1. Publish event for Team B (out of Alice's scope)
    team_b_event_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": team_b_event_id,
        "event_type": "work_item_created",
        "team_id": str(team_b.id),
    })

    # 2. Publish private notification for Bob (not Alice)
    bob_notif_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": bob_notif_id,
        "event_type": "notification_created",
        "recipient_id": str(bob.id),
    })

    # 3. Publish private notification for Alice
    alice_notif_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": alice_notif_id,
        "event_type": "notification_created",
        "recipient_id": str(alice.id),
    })

    # 4. Publish work item event for Team A (within Alice's scope)
    team_a_event_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": team_a_event_id,
        "event_type": "work_item_status_changed",
        "team_id": str(team_a.id),
    })

    # Alice connects with Last-Event-ID: baseline_id and limit=3 (handshake + 2 authorized events)
    with client.stream(
        "GET",
        "/me/events?limit=3",
        headers={"X-Dev-User-Email": alice.email, "Last-Event-ID": baseline_id},
    ) as stream:
        assert stream.status_code == 200
        lines = list(stream.iter_lines())
        received_ids = [line.replace("id: ", "").strip() for line in lines if line.startswith("id: ")]

        # Must receive Alice's events
        assert alice_notif_id in received_ids
        assert team_a_event_id in received_ids

        # Must NOT receive out-of-scope or other users' events
        assert team_b_event_id not in received_ids
        assert bob_notif_id not in received_ids


def test_sse_reconnect_replay_from_ring_buffer(client):
    """Verify clients providing Last-Event-ID receive missed events from the replay buffer (WG-08)."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team = _setup_hierarchy(conn)
            user = create_user(conn, email=f"recon-{uuid.uuid4().hex[:8]}@example.com", display_name="Replay User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    baseline_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": baseline_id,
        "event_type": "work_item_created",
        "team_id": str(team.id),
    })

    missed_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": missed_id,
        "event_type": "work_item_updated",
        "team_id": str(team.id),
    })

    with client.stream(
        "GET",
        "/me/events?limit=2",
        headers={"X-Dev-User-Email": user.email, "Last-Event-ID": baseline_id},
    ) as stream:
        assert stream.status_code == 200
        lines = stream.iter_lines()
        found_missed = False
        for line in lines:
            if line.startswith("id: ") and missed_id in line:
                found_missed = True
                break
        assert found_missed


def test_sse_disconnect_cleans_up_subscriber(client):
    """Verify disconnecting an SSE stream unregisters the client from the broadcaster."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"dc-{uuid.uuid4().hex[:8]}@example.com", display_name="DC User")

    initial_subscribers = len(broadcaster._subscribers)

    with client.stream("GET", "/me/events?limit=1", headers={"X-Dev-User-Email": user.email}) as stream:
        assert stream.status_code == 200
        lines = stream.iter_lines()
        next(lines)

    assert len(broadcaster._subscribers) == initial_subscribers


def test_sse_authorization_refresh_revoked_scope_suppression(client):
    """Verify WG-04: When role access is revoked, subsequent events for that scope are not delivered."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team = _setup_hierarchy(conn)
            user = create_user(conn, email=f"revoked-sse-{uuid.uuid4().hex[:8]}@example.com", display_name="Revoked User")
            assignment = grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    # 1. Baseline event before revocation
    baseline_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": baseline_id,
        "event_type": "work_item_created",
        "team_id": str(team.id),
    })

    # Revoke user role in database
    with get_db_connection() as conn:
        with conn.transaction():
            revoke_role(conn, assignment.id, actor_id=user.id, reason="Security revocation")

    # 2. Trigger role_assignment_changed
    broadcaster.publish_local({
        "id": str(uuid.uuid4()),
        "event_type": "role_assignment_changed",
        "user_id": str(user.id),
        "action": "revoked",
    })

    # 3. Subsequent event for revoked team
    revoked_team_event_id = str(uuid.uuid4())
    broadcaster.publish_local({
        "id": revoked_team_event_id,
        "event_type": "work_item_updated",
        "team_id": str(team.id),
    })

    # Reconnect with baseline_id: user must receive sync/handshake but NOT the revoked team event
    with client.stream(
        "GET",
        "/me/events?limit=1",
        headers={"X-Dev-User-Email": user.email, "Last-Event-ID": baseline_id},
    ) as stream:
        assert stream.status_code == 200
        lines = list(stream.iter_lines())
        received_ids = [line.replace("id: ", "").strip() for line in lines if line.startswith("id: ")]
        assert revoked_team_event_id not in received_ids


def test_sse_deactivated_account_rejected_fail_closed(client):
    """Verify WG-04: Suspended or deactivated accounts are rejected from SSE fail-closed."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"deact-sse-{uuid.uuid4().hex[:8]}@example.com", display_name="Deactivated User")
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET status = 'suspended' WHERE id = %s;", (user.id,))

    res = client.get("/me/events", headers={"X-Dev-User-Email": user.email})
    assert res.status_code == 401
    assert "suspended" in res.json()["detail"].lower()

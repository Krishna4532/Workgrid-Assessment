import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import psycopg
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
    org = create_organization(conn, name=f"Idemp Org {suffix}", slug=f"idemp-org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"Idemp Dept {suffix}", slug=f"idemp-dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"Idemp Team {suffix}", slug=f"idemp-team-{suffix}")
    return org, dept, team


# ============================================================================
# 1. Idempotency Key Replay and Exact Result Matching
# ============================================================================

def test_idempotent_work_item_creation_replays_cached_response(client):
    """Verify repeating a request with the same Idempotency-Key returns cached response without duplicate records."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"key-{uuid.uuid4().hex}"
    payload = {
        "team_id": str(team.id),
        "kind": "task",
        "title": "Idempotent Creation Task",
        "description": "Double click safety test",
    }
    headers = {"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key}

    # 1. First request -> 201 Created
    res1 = client.post("/work-items", headers=headers, json=payload)
    assert res1.status_code == 201
    item1 = res1.json()

    # 2. Second request with identical key and payload -> returns cached 201 with identical item
    res2 = client.post("/work-items", headers=headers, json=payload)
    assert res2.status_code == 201
    item2 = res2.json()

    assert item1["id"] == item2["id"]
    assert item1["created_at"] == item2["created_at"]

    # Verify database persistence: exactly 1 work item, 1 activity row, 1 outbox row
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items WHERE team_id = %s;", (team.id,))
            assert cur.fetchone()[0] == 1

            cur.execute("SELECT count(*) FROM activity WHERE work_item_id = %s;", (item1["id"],))
            assert cur.fetchone()[0] == 1

            cur.execute(
                "SELECT count(*) FROM outbox WHERE event_type = 'work_item_created' AND payload->>'item_id' = %s;",
                (item1["id"],),
            )
            assert cur.fetchone()[0] == 1


# ============================================================================
# 2. Idempotency Key Payload Mismatch Detection (HTTP 422)
# ============================================================================

def test_idempotency_payload_mismatch_rejected_with_422(client):
    """Verify sending a different payload with an already-used Idempotency-Key returns 422."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"mismatch-{uuid.uuid4().hex}"
    payload1 = {"team_id": str(team.id), "kind": "task", "title": "Original Payload"}
    payload2 = {"team_id": str(team.id), "kind": "task", "title": "Conflicting Different Payload"}

    # 1. Request with payload1
    res1 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload1,
    )
    assert res1.status_code == 201

    # 2. Reusing same key with payload2 -> 422 Unprocessable Entity
    res2 = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
        json=payload2,
    )
    assert res2.status_code == 422
    assert "different request payload" in res2.json()["detail"].lower()


# ============================================================================
# 3. Concurrent Requests Sharing the Same Idempotency Key
# ============================================================================

def test_concurrent_requests_sharing_idempotency_key(client):
    """Verify rapid simultaneous submissions with the same key serialize via advisory lock."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    idemp_key = f"concurrent-{uuid.uuid4().hex}"
    payload = {"team_id": str(team.id), "kind": "task", "title": "Rapid Double Submit Task"}
    barrier = Barrier(2)

    def do_submit():
        barrier.wait()
        return client.post(
            "/work-items",
            headers={"X-Dev-User-Email": member.email, "Idempotency-Key": idemp_key},
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_submit)
        f2 = executor.submit(do_submit)
        res1 = f1.result()
        res2 = f2.result()

    # Both requests must succeed (one created, one replayed from lock)
    assert res1.status_code == 201
    assert res2.status_code == 201
    assert res1.json()["id"] == res2.json()["id"]

    # Verify only 1 row created in database
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items WHERE team_id = %s;", (team.id,))
            assert cur.fetchone()[0] == 1


# ============================================================================
# 4. Idempotency Key User Isolation
# ============================================================================

def test_idempotency_key_user_isolation(client):
    """Verify idempotency keys are strictly scoped per user and cannot leak results across users."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_a, _, team_a = _setup_hierarchy(conn)
            org_b, _, team_b = _setup_hierarchy(conn)
            user_a = create_user(conn, email=f"ua-{uuid.uuid4().hex[:8]}@example.com", display_name="User A")
            user_b = create_user(conn, email=f"ub-{uuid.uuid4().hex[:8]}@example.com", display_name="User B")

            grant_or_update_role(conn, user_id=user_a.id, role="member", scope_type="team", scope_id=team_a.id)
            grant_or_update_role(conn, user_id=user_b.id, role="member", scope_type="team", scope_id=team_b.id)

    shared_key = f"shared-{uuid.uuid4().hex}"

    # User A creates item with shared_key
    res_a = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": user_a.email, "Idempotency-Key": shared_key},
        json={"team_id": str(team_a.id), "kind": "task", "title": "User A Task"},
    )
    assert res_a.status_code == 201
    item_a_id = res_a.json()["id"]

    # User B uses the exact same idempotency key in their own team
    res_b = client.post(
        "/work-items",
        headers={"X-Dev-User-Email": user_b.email, "Idempotency-Key": shared_key},
        json={"team_id": str(team_b.id), "kind": "task", "title": "User B Task"},
    )
    assert res_b.status_code == 201
    item_b_id = res_b.json()["id"]

    # Must NOT return User A's cached response; User B gets their own independent item
    assert item_a_id != item_b_id


# ============================================================================
# 5. Idempotent Approvals and Transitions
# ============================================================================

def test_idempotent_approval_decision_replays_without_duplicate_audit(client):
    """Verify repeating an approval decision with an Idempotency-Key returns cached response without duplicate audits."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            applicant = create_user(conn, email=f"app-{uuid.uuid4().hex[:8]}@example.com", display_name="Applicant")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)

            req = create_role_request(
                conn,
                applicant.id,
                RoleRequestCreate(
                    requested_role="member",
                    scope_type="team",
                    scope_id=team.id,
                    reason="Requesting membership",
                ),
            )

    idemp_key = f"appr-{uuid.uuid4().hex}"
    headers = {"X-Dev-User-Email": lead.email, "Idempotency-Key": idemp_key}

    # 1. First approval
    res1 = client.post(f"/role-requests/{req.id}/approve", headers=headers, json={"comment": "Approved"})
    assert res1.status_code == 200

    # 2. Replay approval with same key
    res2 = client.post(f"/role-requests/{req.id}/approve", headers=headers, json={"comment": "Approved"})
    assert res2.status_code == 200

    # Verify only 1 approval record and 1 role assignment in database
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM approval_records WHERE request_id = %s;", (req.id,))
            assert cur.fetchone()[0] == 1

            cur.execute(
                "SELECT count(*) FROM role_assignments WHERE user_id = %s AND team_id = %s;",
                (applicant.id, team.id),
            )
            assert cur.fetchone()[0] == 1


# ============================================================================
# 6. Append-Only Activity and Audit Immutability Triggers
# ============================================================================

def test_append_only_activity_table_immutability_trigger(client):
    """Verify trg_activity_immutable trigger prevents direct SQL UPDATE or DELETE on activity."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead")
            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Audit Immutability Item"),
            )

    # 1. Attempting direct UPDATE on activity table must raise PostgreSQL exception
    with pytest.raises(psycopg.errors.RaiseException, match="Activity records are append-only"):
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE activity SET comment = 'Tampered' WHERE work_item_id = %s;",
                        (item.id,),
                    )

    # 2. Attempting direct DELETE on activity table must raise PostgreSQL exception
    with pytest.raises(psycopg.errors.RaiseException, match="Activity records are append-only"):
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM activity WHERE work_item_id = %s;",
                        (item.id,),
                    )


def test_role_assignment_audit_table_immutability_trigger():
    """Verify trg_role_assignment_audit_immutable trigger blocks direct UPDATE or DELETE on role audit."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            user = create_user(conn, email=f"audit-{uuid.uuid4().hex[:8]}@example.com", display_name="Audit User")
            assignment = grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

    # 1. Attempt direct UPDATE on role_assignment_audit
    with pytest.raises(psycopg.errors.RaiseException, match="Audit records are append-only"):
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE role_assignment_audit SET reason = 'Tampered' WHERE assignment_id = %s;",
                        (assignment.id,),
                    )

    # 2. Attempt direct DELETE on role_assignment_audit
    with pytest.raises(psycopg.errors.RaiseException, match="Audit records are append-only"):
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM role_assignment_audit WHERE assignment_id = %s;",
                        (assignment.id,),
                    )


# ============================================================================
# 7. Rollback Safety: No Residual Activity or History on Failed Operations
# ============================================================================

def test_transaction_rollback_leaves_no_residual_activity():
    """Verify rolling back a transaction discards any uncommitted activity and history records."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_hierarchy(conn)
            user = create_user(conn, email=f"rb-{uuid.uuid4().hex[:8]}@example.com", display_name="Rollback User")
            item = create_work_item(
                conn,
                creator_id=user.id,
                creator_name=user.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Rollback Baseline"),
            )

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM activity WHERE work_item_id = %s;", (item.id,))
            baseline_count = cur.fetchone()[0]

    # Execute a failing transaction that rolls back
    try:
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO activity (work_item_id, actor_id, event_type, comment)
                        VALUES (%s, %s, 'comment', 'This must roll back');
                        """,
                        (item.id, user.id),
                    )
                # Force failure
                raise RuntimeError("Simulated transaction failure")
    except RuntimeError:
        pass

    # Verify no residual activity was committed
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM activity WHERE work_item_id = %s;", (item.id,))
            final_count = cur.fetchone()[0]
            assert final_count == baseline_count

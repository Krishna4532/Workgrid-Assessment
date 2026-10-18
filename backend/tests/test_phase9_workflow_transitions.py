import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.models import WorkItemCreate
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import grant_or_update_role
from app.repositories.work_items_repo import create_work_item, transition_work_item


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_workflow_team(conn):
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"WF Org {suffix}", slug=f"wf-org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"WF Dept {suffix}", slug=f"wf-dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"WF Team {suffix}", slug=f"wf-team-{suffix}")

    lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Team Lead")
    member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Member")
    other_member = create_user(conn, email=f"om-{uuid.uuid4().hex[:8]}@example.com", display_name="Other Member")

    grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
    grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)
    grant_or_update_role(conn, user_id=other_member.id, role="member", scope_type="team", scope_id=team.id)

    return org, dept, team, lead, member, other_member


# ============================================================================
# 1. State Transition Matrix: Standard Work Items (requires_approval = False)
# ============================================================================

def test_standard_work_item_state_transition_matrix(client):
    """Verify complete state transition matrix for standard items: open -> in_progress -> resolved -> closed."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, lead, member, _ = _setup_workflow_team(conn)
            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Standard Workflow Item",
                    requires_approval=False,
                ),
            )
            assert item.status == "open"

    member_headers = {"X-Dev-User-Email": member.email}
    lead_headers = {"X-Dev-User-Email": lead.email}

    # 1. open -> resolved directly is ILLEGAL (must go through in_progress) -> 422
    res_bad = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "resolved", "version": 1},
    )
    assert res_bad.status_code == 422
    assert "illegal transition" in res_bad.json()["detail"].lower()

    # 2. open -> in_progress (valid for member rank >= 1) -> 200 OK
    res_in_prog = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "in_progress", "version": 1},
    )
    assert res_in_prog.status_code == 200
    assert res_in_prog.json()["status"] == "in_progress"
    assert res_in_prog.json()["version"] == 2

    # 3. in_progress -> awaiting_approval is ILLEGAL when requires_approval=False -> 422
    res_bad_appr = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "awaiting_approval", "version": 2},
    )
    assert res_bad_appr.status_code == 422

    # 4. in_progress -> open (release) requires a reason; missing reason -> 422
    res_no_reason = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "open", "version": 2},
    )
    assert res_no_reason.status_code == 422
    assert "reason is required" in res_no_reason.json()["detail"].lower()

    # 5. in_progress -> open with reason succeeds -> 200 OK
    res_released = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "open", "version": 2, "reason": "Releasing to pool"},
    )
    assert res_released.status_code == 200
    assert res_released.json()["status"] == "open"
    assert res_released.json()["assignee_id"] is None
    assert res_released.json()["version"] == 3

    # 6. Re-claim to in_progress
    res_claim = client.post(f"/work-items/{item.id}/claim", headers=member_headers)
    assert res_claim.status_code == 200
    assert res_claim.json()["status"] == "in_progress"
    assert res_claim.json()["version"] == 4

    # 7. in_progress -> resolved succeeds for assignee -> 200 OK
    res_resolved = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "resolved", "version": 4},
    )
    assert res_resolved.status_code == 200
    assert res_resolved.json()["status"] == "resolved"
    assert res_resolved.json()["version"] == 5

    # 8. resolved -> in_progress (reopen) requires a reason; missing reason -> 422
    res_reopen_bad = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "in_progress", "version": 5},
    )
    assert res_reopen_bad.status_code == 422

    # 9. resolved -> closed succeeds for lead -> 200 OK
    res_closed = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "closed", "version": 5},
    )
    assert res_closed.status_code == 200
    assert res_closed.json()["status"] == "closed"
    assert res_closed.json()["version"] == 6

    # 10. closed is FINAL: any further transition is rejected -> 422
    res_post_closed = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "in_progress", "version": 6, "reason": "Reopen attempt"},
    )
    assert res_post_closed.status_code == 422
    assert "closed is final" in res_post_closed.json()["detail"].lower()


# ============================================================================
# 2. State Transition Matrix: Approval Items (requires_approval = True)
# ============================================================================

def test_approval_workflow_and_rejection_cycles(client):
    """Verify approval workflow: open -> in_prog -> awaiting_approval -> in_prog (rejected) -> awaiting_approval -> resolved."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, lead, member, _ = _setup_workflow_team(conn)
            item = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="compliance",
                    title="Audit Verification Item",
                    requires_approval=True,
                ),
            )

    member_headers = {"X-Dev-User-Email": member.email}
    lead_headers = {"X-Dev-User-Email": lead.email}

    # 1. Member claims item (status -> in_progress, version 2)
    c_res = client.post(f"/work-items/{item.id}/claim", headers=member_headers)
    assert c_res.status_code == 200

    # 2. Direct transition to resolved is forbidden when requires_approval=True -> 422
    res_skip = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "resolved", "version": 2},
    )
    assert res_skip.status_code == 422

    # 3. Transition to awaiting_approval succeeds -> 200 OK
    res_awaiting = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "awaiting_approval", "version": 2},
    )
    assert res_awaiting.status_code == 200
    assert res_awaiting.json()["status"] == "awaiting_approval"
    assert res_awaiting.json()["version"] == 3

    # 4. Lead rejects back to in_progress without reason -> 422
    res_rej_no_reason = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "in_progress", "version": 3},
    )
    assert res_rej_no_reason.status_code == 422
    assert "reason is required to reject" in res_rej_no_reason.json()["detail"].lower()

    # 5. Lead rejects back to in_progress with reason -> 200 OK
    res_rejected = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "in_progress", "version": 3, "reason": "Missing supporting document"},
    )
    assert res_rejected.status_code == 200
    assert res_rejected.json()["status"] == "in_progress"
    assert res_rejected.json()["version"] == 4

    # 6. Member resubmits to awaiting_approval -> 200 OK
    res_resubmit = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "awaiting_approval", "version": 4},
    )
    assert res_resubmit.status_code == 200
    assert res_resubmit.json()["status"] == "awaiting_approval"
    assert res_resubmit.json()["version"] == 5

    # 7. Lead approves to resolved -> 200 OK
    res_approved = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "resolved", "version": 5},
    )
    assert res_approved.status_code == 200
    assert res_approved.json()["status"] == "resolved"
    assert res_approved.json()["version"] == 6


# ============================================================================
# 3. Direct Closure of Open Items Requires Manager Role and Non-Empty Reason
# ============================================================================

def test_direct_closure_of_open_item_rules(client):
    """Verify direct closure of open items requires manager rank >= 2 and a non-empty reason."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, lead, member, _ = _setup_workflow_team(conn)
            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Cancelable Task"),
            )

    member_headers = {"X-Dev-User-Email": member.email}
    lead_headers = {"X-Dev-User-Email": lead.email}

    # 1. Member attempting to close open item -> 403 Forbidden
    res_m = client.post(
        f"/work-items/{item.id}/transition",
        headers=member_headers,
        json={"to_status": "closed", "version": 1, "reason": "Duplicate"},
    )
    assert res_m.status_code == 403

    # 2. Lead attempting to close open item without reason -> 422
    res_no_r = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "closed", "version": 1},
    )
    assert res_no_r.status_code == 422
    assert "reason is required to close" in res_no_r.json()["detail"].lower()

    # 3. Lead closing with reason -> 200 OK
    res_closed = client.post(
        f"/work-items/{item.id}/transition",
        headers=lead_headers,
        json={"to_status": "closed", "version": 1, "reason": "No longer needed"},
    )
    assert res_closed.status_code == 200
    assert res_closed.json()["status"] == "closed"


# ============================================================================
# 4. Assignee Eligibility Constraints
# ============================================================================

def test_assignee_eligibility_enforcement(client):
    """Verify items can only be assigned to active members or managers within the team scope."""
    with get_db_connection() as conn:
        with conn.transaction():
            _, _, team, lead, member, _ = _setup_workflow_team(conn)
            outsider = create_user(conn, email=f"out-{uuid.uuid4().hex[:8]}@example.com", display_name="Outsider")

            item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(team_id=team.id, kind="task", title="Assignee Scope Item"),
            )

    lead_headers = {"X-Dev-User-Email": lead.email}

    # 1. Assigning to an outsider (not a member of team) -> 422 Unprocessable Entity
    res_bad = client.post(
        f"/work-items/{item.id}/assign",
        headers=lead_headers,
        json={"assignee_id": str(outsider.id), "version": 1},
    )
    assert res_bad.status_code == 422
    assert "not an active member or manager" in res_bad.json()["detail"].lower()

    # 2. Assigning to active member -> 200 OK
    res_good = client.post(
        f"/work-items/{item.id}/assign",
        headers=lead_headers,
        json={"assignee_id": str(member.id), "version": 1, "reason": "Eligible assignment"},
    )
    assert res_good.status_code == 200
    assert res_good.json()["assignee_id"] == str(member.id)
    assert res_good.json()["status"] == "in_progress"  # Assigning an open item automatically moves it to in_progress

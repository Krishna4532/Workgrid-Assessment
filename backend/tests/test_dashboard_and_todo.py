from datetime import datetime, timedelta, timezone
import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.models import WorkItemCreate
from app.repositories.approvals_repo import create_role_request
from app.models import RoleRequestCreate
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.notifications_repo import create_notification
from app.repositories.outbox_repo import enqueue_outbox_event
from app.repositories.roles_repo import grant_or_update_role, suspend_role
from app.repositories.work_items_repo import create_work_item, transition_work_item


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_org_hierarchy(conn):
    """Helper to create an isolated organization, department, and team with unique slugs."""
    suffix = uuid.uuid4().hex[:8]
    org = create_organization(conn, name=f"Org {suffix}", slug=f"org-{suffix}")
    dept = create_department(conn, organization_id=org.id, name=f"Dept {suffix}", slug=f"dept-{suffix}")
    team = create_team(conn, department_id=dept.id, name=f"Team {suffix}", slug=f"team-{suffix}")
    return org, dept, team


# ============================================================================
# 1. Dashboard Overview Tests
# ============================================================================

def test_dashboard_requires_authentication(client):
    """Verify unauthenticated requests to /me/dashboard are rejected with HTTP 401."""
    res = client.get("/me/dashboard")
    assert res.status_code == 401
    assert "detail" in res.json()


def test_dashboard_overview_counts_and_isolation(client):
    """Verify /me/dashboard returns accurate counts for open, overdue, unread, and approvals."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)

            # Users
            lead = create_user(conn, email=f"lead-{uuid.uuid4().hex[:8]}@example.com", display_name="Team Lead")
            member = create_user(conn, email=f"mbr-{uuid.uuid4().hex[:8]}@example.com", display_name="Team Member")
            outsider = create_user(conn, email=f"out-{uuid.uuid4().hex[:8]}@example.com", display_name="Outsider")

            # Roles
            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            # 1. Open work item (assigned to member, not overdue)
            create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Active Member Task",
                    assignee_id=member.id,
                    due_date=now + timedelta(days=2),
                ),
            )

            # 2. Overdue work item (assigned to member)
            create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="incident",
                    title="Overdue Member Incident",
                    assignee_id=member.id,
                    due_date=now - timedelta(days=1),
                ),
            )

            # 3. Work item with missing due date (assigned to lead) -> must NOT be counted as overdue
            create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Task without due date",
                    assignee_id=lead.id,
                    due_date=None,
                ),
            )

            # 4. Closed work item (must NOT be counted as open or overdue)
            closed_item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Resolved and closed item",
                    assignee_id=member.id,
                    due_date=now - timedelta(days=3),
                ),
            )
            # Manager transitions to closed
            transition_work_item(conn, closed_item.id, lead.id, "closed", 1, "Completed early")

            # 5. Work item awaiting approval (requires approval, assigned to member)
            item_awaiting = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="compliance",
                    title="Compliance check awaiting approval",
                    requires_approval=True,
                    assignee_id=member.id,
                ),
            )
            transition_work_item(conn, item_awaiting.id, member.id, "awaiting_approval", 1)

            # 6. Notifications for member
            notif_event_id = enqueue_outbox_event(
                conn,
                "work_item_assigned",
                {"item_id": str(uuid.uuid4()), "assignee_id": str(member.id)},
            )
            create_notification(
                conn,
                user_id=member.id,
                event_id=notif_event_id,
                dedupe_key=f"notif-1-{uuid.uuid4().hex[:8]}",
                notification_type="work_item_assigned",
                title="New Assignment",
                message="You were assigned a task",
            )
            create_notification(
                conn,
                user_id=member.id,
                event_id=notif_event_id,
                dedupe_key=f"notif-2-{uuid.uuid4().hex[:8]}",
                notification_type="work_item_assigned",
                title="Second Assignment",
                message="You were assigned another task",
            )

            # 7. Unrelated out-of-scope org & work item
            other_org, other_dept, other_team = _setup_org_hierarchy(conn)
            other_user = create_user(conn, email=f"other-{uuid.uuid4().hex[:8]}@example.com", display_name="Other User")
            grant_or_update_role(conn, user_id=other_user.id, role="member", scope_type="team", scope_id=other_team.id)
            create_work_item(
                conn,
                creator_id=other_user.id,
                creator_name=other_user.display_name,
                dto=WorkItemCreate(
                    team_id=other_team.id,
                    kind="incident",
                    title="Out of scope item",
                    assignee_id=other_user.id,
                    due_date=now - timedelta(days=5),
                ),
            )

    # Member requests /me/dashboard
    res_m = client.get("/me/dashboard", headers={"X-Dev-User-Email": member.email})
    assert res_m.status_code == 200
    data_m = res_m.json()
    assert data_m["user"]["email"] == member.email
    assert "Welcome back" in data_m["greeting"]
    assert len(data_m["positions"]) == 1
    assert data_m["positions"][0]["role"] == "member"
    # Team open count: items 1, 2, 3, 5 = 4 items
    assert data_m["open_work_items_count"] == 4
    # Team overdue count: item 2 = 1 item (item 4 is closed, item 3 has no due date, other_team item is out-of-scope)
    assert data_m["overdue_work_items_count"] == 1
    # Member assigned open count: items 1, 2, 5 = 3 items
    assert data_m["my_open_work_items_count"] == 3
    # Member assigned overdue: item 2 = 1 item
    assert data_m["my_overdue_work_items_count"] == 1
    # Unread notifications: 2
    assert data_m["unread_notifications_count"] == 2
    # Member is rank 1, so 0 pending approvals requiring their attention
    assert data_m["pending_approvals_count"] == 0

    # Lead requests /me/dashboard
    res_l = client.get("/me/dashboard", headers={"X-Dev-User-Email": lead.email})
    assert res_l.status_code == 200
    data_l = res_l.json()
    # Team open count is 4
    assert data_l["open_work_items_count"] == 4
    # Lead has item 3 assigned (open, not overdue)
    assert data_l["my_open_work_items_count"] == 1
    assert data_l["my_overdue_work_items_count"] == 0
    # Lead is manager (rank 2), so item 5 awaiting approval requires lead's attention!
    assert data_l["pending_work_item_approvals_count"] == 1
    assert data_l["pending_approvals_count"] >= 1

    # Outsider requests /me/dashboard -> has zero access to team, counts are 0
    res_out = client.get("/me/dashboard", headers={"X-Dev-User-Email": outsider.email})
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["open_work_items_count"] == 0
    assert data_out["overdue_work_items_count"] == 0
    assert data_out["my_open_work_items_count"] == 0
    assert data_out["pending_approvals_count"] == 0


# ============================================================================
# 2. Unified To-Do Tests
# ============================================================================

def test_todo_requires_authentication(client):
    """Verify unauthenticated requests to /me/todo return HTTP 401."""
    res = client.get("/me/todo")
    assert res.status_code == 401


def test_todo_empty_queue(client):
    """Verify empty to-do queue returns clean structure with 0 counts."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"empty-{uuid.uuid4().hex[:8]}@example.com", display_name="Empty User")

    res = client.get("/me/todo", headers={"X-Dev-User-Email": user.email})
    assert res.status_code == 200
    data = res.json()
    assert data["items"] == []
    assert data["total_count"] == 0
    assert data["overdue_count"] == 0
    assert data["next_cursor"] is None
    assert data["has_more"] is False


def test_todo_items_aggregation_and_self_approval_exclusion(client):
    """Verify unified to-do aggregates assigned items, work approvals, and role requests while enforcing self-approval."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)

            lead = create_user(conn, email=f"lead2-{uuid.uuid4().hex[:8]}@example.com", display_name="Lead Two")
            member = create_user(conn, email=f"mbr2-{uuid.uuid4().hex[:8]}@example.com", display_name="Member Two")
            requester = create_user(conn, email=f"req-{uuid.uuid4().hex[:8]}@example.com", display_name="Applicant")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            # 1. Actionable work item assigned to member
            item_assigned = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Implement API endpoint",
                    assignee_id=member.id,
                    priority="high",
                    due_date=now + timedelta(days=1),
                ),
            )

            # 2. Work item awaiting approval (created by member, assigned to member; lead can approve, member CANNOT approve)
            item_approval = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Deploy security patch",
                    requires_approval=True,
                    assignee_id=member.id,
                    priority="urgent",
                ),
            )
            transition_work_item(conn, item_approval.id, member.id, "awaiting_approval", 1)

            # 3a. Work item created/owned by lead but assigned to member, awaiting approval (Rule #6: lead is owner/creator -> cannot approve)
            lead_owned_item = create_work_item(
                conn,
                creator_id=lead.id,
                creator_name=lead.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Lead owned task assigned to member",
                    requires_approval=True,
                    assignee_id=member.id,
                    priority="normal",
                ),
            )
            transition_work_item(conn, lead_owned_item.id, member.id, "awaiting_approval", 1)

            # 3b. Work item created by member but assigned to lead, awaiting approval (Rule #6: lead is assignee -> cannot approve)
            lead_assigned_item = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="compliance",
                    title="Member task assigned to lead",
                    requires_approval=True,
                    assignee_id=lead.id,
                    priority="normal",
                ),
            )
            transition_work_item(conn, lead_assigned_item.id, lead.id, "awaiting_approval", 1)

            # 4. Role request submitted by external user requesting member in team -> lead is eligible decider
            req_pending = create_role_request(
                conn,
                user_id=requester.id,
                dto=RoleRequestCreate(
                    requested_role="member",
                    scope_type="team",
                    scope_id=team.id,
                    reason="Joining development team",
                ),
            )

            # 5. Role request submitted by lead for higher role -> lead CANNOT approve own request
            create_role_request(
                conn,
                user_id=lead.id,
                dto=RoleRequestCreate(
                    requested_role="department_manager",
                    scope_type="department",
                    scope_id=dept.id,
                    reason="Promotion request",
                ),
            )

    # Check Member's To-Do list:
    res_m = client.get("/me/todo", headers={"X-Dev-User-Email": member.email})
    assert res_m.status_code == 200
    m_items = res_m.json()["items"]
    # Member has 1 assigned actionable item (item 1).
    # Awaiting approval items cannot be decided by member (rank 1 member, and self-approval prohibited).
    assert len(m_items) == 1
    assert m_items[0]["target_id"] == str(item_assigned.id)
    assert m_items[0]["item_type"] == "assigned_work_item"

    # Check Lead's To-Do list:
    res_l = client.get("/me/todo", headers={"X-Dev-User-Email": lead.email})
    assert res_l.status_code == 200
    l_data = res_l.json()
    l_items = l_data["items"]
    item_types = [i["item_type"] for i in l_items]

    # Lead should see:
    # - work_item_approval for item 2 (created by member AND assigned to member)
    # - role_request_approval for requester's request
    # Lead must NOT see:
    # - lead_owned_item (lead created it -> owner self-approval prohibited)
    # - lead_assigned_item (lead is assignee -> worker self-approval prohibited)
    # - their own role request (self-approval prohibited)
    assert "work_item_approval" in item_types
    assert "role_request_approval" in item_types
    target_ids = [i["target_id"] for i in l_items]
    assert str(item_approval.id) in target_ids
    assert str(req_pending.id) in target_ids
    assert str(lead_owned_item.id) not in target_ids
    assert str(lead_assigned_item.id) not in target_ids

    # Server-side enforcement check on transition endpoint:
    # 1. Lead attempting to approve lead_owned_item must be rejected with 403 Forbidden
    res_try_owner = client.post(
        f"/work-items/{lead_owned_item.id}/transition",
        headers={"X-Dev-User-Email": lead.email},
        json={"to_status": "resolved", "version": 2},
    )
    assert res_try_owner.status_code == 403
    assert "Creators and assignees cannot approve" in res_try_owner.json()["detail"]

    # 2. Lead attempting to approve lead_assigned_item must be rejected with 403 Forbidden
    res_try_assignee = client.post(
        f"/work-items/{lead_assigned_item.id}/transition",
        headers={"X-Dev-User-Email": lead.email},
        json={"to_status": "resolved", "version": 2},
    )
    assert res_try_assignee.status_code == 403
    assert "Creators and assignees cannot approve" in res_try_assignee.json()["detail"]

    # 3. Lead approving third-party item_approval succeeds
    res_approve = client.post(
        f"/work-items/{item_approval.id}/transition",
        headers={"X-Dev-User-Email": lead.email},
        json={"to_status": "resolved", "version": 2},
    )
    assert res_approve.status_code == 200
    assert res_approve.json()["status"] == "resolved"


def test_todo_deterministic_urgency_ordering(client):
    """Verify to-do items are deterministically sorted: overdue first, then priority, then due date."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)
            user = create_user(conn, email=f"order-{uuid.uuid4().hex[:8]}@example.com", display_name="Order User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

            # Item A: Normal priority, NOT overdue, due tomorrow
            item_a = create_work_item(
                conn, creator_id=user.id, creator_name=user.display_name,
                dto=WorkItemCreate(
                    team_id=team.id, kind="task", title="Item A - Normal future",
                    assignee_id=user.id, priority="normal", due_date=now + timedelta(days=1),
                ),
            )
            # Item B: Urgent priority, NOT overdue, due in 2 days
            item_b = create_work_item(
                conn, creator_id=user.id, creator_name=user.display_name,
                dto=WorkItemCreate(
                    team_id=team.id, kind="task", title="Item B - Urgent future",
                    assignee_id=user.id, priority="urgent", due_date=now + timedelta(days=2),
                ),
            )
            # Item C: Low priority, OVERDUE (yesterday)
            item_c = create_work_item(
                conn, creator_id=user.id, creator_name=user.display_name,
                dto=WorkItemCreate(
                    team_id=team.id, kind="task", title="Item C - Low overdue",
                    assignee_id=user.id, priority="low", due_date=now - timedelta(days=1),
                ),
            )
            # Item D: Urgent priority, OVERDUE (2 days ago)
            item_d = create_work_item(
                conn, creator_id=user.id, creator_name=user.display_name,
                dto=WorkItemCreate(
                    team_id=team.id, kind="task", title="Item D - Urgent overdue",
                    assignee_id=user.id, priority="urgent", due_date=now - timedelta(days=2),
                ),
            )

    res = client.get("/me/todo", headers={"X-Dev-User-Email": user.email})
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4

    # Ordering expectation under WG-07 Urgency Tiers:
    # 1. Tier 0 (Overdue): Item D (urgent, overdue 2 days)
    # 2. Tier 0 (Overdue): Item C (low, overdue 1 day)
    # 3. Tier 1 (Due Soon <= 24h): Item A (normal priority, due in 24h)
    # 4. Tier 2 (Standard > 24h): Item B (urgent priority, but due in 48h)
    assert items[0]["target_id"] == str(item_d.id)
    assert items[1]["target_id"] == str(item_c.id)
    assert items[2]["target_id"] == str(item_a.id)
    assert items[3]["target_id"] == str(item_b.id)


def test_todo_keyset_pagination_stability_under_mutation(client):
    """Verify WG-06: Keyset cursor pagination does not drift or duplicate items when queue mutates."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)
            user = create_user(conn, email=f"keyset-{uuid.uuid4().hex[:8]}@example.com", display_name="Keyset User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

            # Create 5 work items with spaced due dates
            items = []
            for i in range(5):
                item = create_work_item(
                    conn, creator_id=user.id, creator_name=user.display_name,
                    dto=WorkItemCreate(
                        team_id=team.id, kind="task", title=f"Task {i}",
                        assignee_id=user.id, priority="normal", due_date=now + timedelta(days=i + 5),
                    ),
                )
                items.append(item)

    # 1. Fetch Page 1 (limit 2)
    p1 = client.get("/me/todo?limit=2", headers={"X-Dev-User-Email": user.email}).json()
    assert len(p1["items"]) == 2
    assert p1["has_more"] is True
    cursor_p1 = p1["next_cursor"]
    assert cursor_p1 is not None

    # 2. Mutate the queue: Insert a NEW urgent overdue item at the HEAD of the queue
    with get_db_connection() as conn:
        with conn.transaction():
            create_work_item(
                conn, creator_id=user.id, creator_name=user.display_name,
                dto=WorkItemCreate(
                    team_id=team.id, kind="incident", title="Inserted Urgent Overdue Incident",
                    assignee_id=user.id, priority="urgent", due_date=now - timedelta(days=3),
                ),
            )

    # 3. Fetch Page 2 using cursor_p1
    # Under keyset pagination: Page 2 must resume strictly after the last item of Page 1.
    # It must NOT skip items or duplicate items from Page 1 despite the insertion at head!
    p2 = client.get(f"/me/todo?limit=2&cursor={cursor_p1}", headers={"X-Dev-User-Email": user.email}).json()
    assert len(p2["items"]) == 2

    p1_ids = {i["id"] for i in p1["items"]}
    p2_ids = {i["id"] for i in p2["items"]}
    assert p1_ids.isdisjoint(p2_ids), "Keyset pagination must never duplicate items across pages"

    # 4. Fetch Page 3 using cursor_p2
    cursor_p2 = p2["next_cursor"]
    p3 = client.get(f"/me/todo?limit=2&cursor={cursor_p2}", headers={"X-Dev-User-Email": user.email}).json()
    assert len(p3["items"]) == 1
    assert p3["has_more"] is False
    p3_ids = {i["id"] for i in p3["items"]}
    assert p3_ids.isdisjoint(p1_ids) and p3_ids.isdisjoint(p2_ids)

    # 5. Invalid / malformed cursor rejects with 422
    res_bad = client.get("/me/todo?cursor=invalid_base64_not_cursor", headers={"X-Dev-User-Email": user.email})
    assert res_bad.status_code == 422
    assert "cursor" in res_bad.json()["detail"].lower()


def test_todo_cursor_pagination_and_validation(client):
    """Verify cursor pagination validates cursors and stably traverses to-do items."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)
            user = create_user(conn, email=f"page-{uuid.uuid4().hex[:8]}@example.com", display_name="Page User")
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

            for i in range(5):
                create_work_item(
                    conn, creator_id=user.id, creator_name=user.display_name,
                    dto=WorkItemCreate(
                        team_id=team.id, kind="task", title=f"Paged Task {i}",
                        assignee_id=user.id, priority="normal", due_date=now + timedelta(days=i + 1),
                    ),
                )

    # Page 1: limit 2
    res_p1 = client.get("/me/todo?limit=2", headers={"X-Dev-User-Email": user.email})
    assert res_p1.status_code == 200
    p1_data = res_p1.json()
    assert len(p1_data["items"]) == 2
    assert p1_data["has_more"] is True
    assert p1_data["total_count"] == 5
    cursor1 = p1_data["next_cursor"]
    assert cursor1 is not None

    # Page 2: limit 2
    res_p2 = client.get(f"/me/todo?limit=2&cursor={cursor1}", headers={"X-Dev-User-Email": user.email})
    assert res_p2.status_code == 200
    p2_data = res_p2.json()
    assert len(p2_data["items"]) == 2
    assert p2_data["has_more"] is True
    cursor2 = p2_data["next_cursor"]

    # Verify no overlapping items between Page 1 and Page 2
    p1_ids = {i["id"] for i in p1_data["items"]}
    p2_ids = {i["id"] for i in p2_data["items"]}
    assert p1_ids.isdisjoint(p2_ids)

    # Page 3: limit 2
    res_p3 = client.get(f"/me/todo?limit=2&cursor={cursor2}", headers={"X-Dev-User-Email": user.email})
    assert res_p3.status_code == 200
    p3_data = res_p3.json()
    assert len(p3_data["items"]) == 1
    assert p3_data["has_more"] is False
    assert p3_data["next_cursor"] is None

    # Test invalid cursor returns HTTP 422
    res_bad = client.get("/me/todo?cursor=invalid-cursor-value", headers={"X-Dev-User-Email": user.email})
    assert res_bad.status_code == 422
    assert "detail" in res_bad.json()


# ============================================================================
# 3. Workspace Team Members Tests
# ============================================================================

def test_team_members_access_control(client):
    """Verify /teams/{team_id}/members enforces team membership and non-disclosure."""
    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_org_hierarchy(conn)
            lead = create_user(conn, email=f"lead3-{uuid.uuid4().hex[:8]}@example.com", display_name="Team Leader")
            member = create_user(conn, email=f"mbr3-{uuid.uuid4().hex[:8]}@example.com", display_name="Active Member")
            outsider = create_user(conn, email=f"out3-{uuid.uuid4().hex[:8]}@example.com", display_name="External User")

            grant_or_update_role(conn, user_id=lead.id, role="team_leader", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

    # 1. Active member can view team members
    res_m = client.get(f"/teams/{team.id}/members", headers={"X-Dev-User-Email": member.email})
    assert res_m.status_code == 200
    members = res_m.json()["members"]
    assert len(members) == 2
    # Ordered by role_rank DESC: team_leader (2) before member (1)
    assert members[0]["role"] == "team_leader"
    assert members[1]["role"] == "member"

    # 2. Outsider gets HTTP 404 (Rule #9: Non-disclosure)
    res_out = client.get(f"/teams/{team.id}/members", headers={"X-Dev-User-Email": outsider.email})
    assert res_out.status_code == 404

    # 3. Unauthenticated request gets 401
    res_unauth = client.get(f"/teams/{team.id}/members")
    assert res_unauth.status_code == 401

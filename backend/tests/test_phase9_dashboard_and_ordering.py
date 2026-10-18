from datetime import datetime, timedelta, timezone
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
from app.repositories.work_items_repo import create_work_item


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _setup_dash_org(conn, suffix=None):
    uid = uuid.uuid4().hex[:8]
    s = f"{suffix}-{uid}" if suffix else uid
    org = create_organization(conn, name=f"Dash Org {s}", slug=f"dash-org-{s}")
    dept = create_department(conn, organization_id=org.id, name=f"Dash Dept {s}", slug=f"dash-dept-{s}")
    team = create_team(conn, department_id=dept.id, name=f"Dash Team {s}", slug=f"dash-team-{s}")
    return org, dept, team


# ============================================================================
# 1. Deterministic Sorting Under Tie Conditions
# ============================================================================

def test_todo_list_deterministic_ordering_under_ties(client):
    """Verify items with identical priority, due dates, and status sort deterministically by (created_at, id)."""
    now = datetime.now(timezone.utc)
    due = now + timedelta(days=3)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_dash_org(conn)
            member = create_user(conn, email=f"order-{uuid.uuid4().hex[:8]}@example.com", display_name="Order Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            item_ids = []
            for i in range(5):
                item = create_work_item(
                    conn,
                    creator_id=member.id,
                    creator_name=member.display_name,
                    dto=WorkItemCreate(
                        team_id=team.id,
                        kind="task",
                        title=f"Tied Task {i}",
                        priority="normal",
                        due_date=due,
                        assignee_id=member.id,
                    ),
                )
                item_ids.append(str(item.id))

    headers = {"X-Dev-User-Email": member.email}

    # Query todo list twice; ordering must be 100% stable and identical
    res1 = client.get("/me/todo", headers=headers)
    res2 = client.get("/me/todo", headers=headers)
    assert res1.status_code == 200
    assert res2.status_code == 200

    ids1 = [item["id"] for item in res1.json()["items"]]
    ids2 = [item["id"] for item in res2.json()["items"]]
    assert ids1 == ids2, "Sorting must be completely deterministic across requests"
    assert len(ids1) == 5


# ============================================================================
# 2. Keyset / Cursor Pagination Consistency
# ============================================================================

def test_todo_keyset_pagination_zero_omissions_and_duplicates(client):
    """Verify keyset pagination returns all items across pages without duplicates or skips."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_dash_org(conn)
            member = create_user(conn, email=f"page-{uuid.uuid4().hex[:8]}@example.com", display_name="Page Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            created_ids = set()
            for i in range(6):
                item = create_work_item(
                    conn,
                    creator_id=member.id,
                    creator_name=member.display_name,
                    dto=WorkItemCreate(
                        team_id=team.id,
                        kind="task",
                        title=f"Pagination Item {i}",
                        priority="high",
                        assignee_id=member.id,
                        due_date=now + timedelta(hours=i + 1),
                    ),
                )
                created_ids.add(str(item.id))

    headers = {"X-Dev-User-Email": member.email}
    retrieved_ids = []
    cursor = None

    # Walk all pages with page limit = 2
    for _ in range(10):
        url = "/me/todo?limit=2"
        if cursor:
            url += f"&cursor={cursor}"

        res = client.get(url, headers=headers)
        assert res.status_code == 200
        data = res.json()
        items = data["items"]
        retrieved_ids.extend([it["id"] for it in items])

        if not data["has_more"]:
            break
        cursor = data["next_cursor"]
        assert cursor is not None

    # Assert all 6 items were retrieved with zero duplicates
    assert len(retrieved_ids) == 6
    assert len(set(retrieved_ids)) == 6
    assert set(retrieved_ids) == created_ids


# ============================================================================
# 3. Urgency Tiers & Overdue Handling
# ============================================================================

def test_urgency_tiers_and_null_due_dates(client):
    """Verify tier 0 (overdue) precedes tier 1 (due soon) precedes tier 2 (standard), and nulls are not overdue."""
    now = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.transaction():
            org, dept, team = _setup_dash_org(conn)
            member = create_user(conn, email=f"urg-{uuid.uuid4().hex[:8]}@example.com", display_name="Urgency Member")
            grant_or_update_role(conn, user_id=member.id, role="member", scope_type="team", scope_id=team.id)

            # Item 1: Overdue (due yesterday)
            it_overdue = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Overdue Task",
                    assignee_id=member.id,
                    due_date=now - timedelta(days=1),
                ),
            )
            # Item 2: Due soon (due in 4 hours)
            it_due_soon = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Due Soon Task",
                    assignee_id=member.id,
                    due_date=now + timedelta(hours=4),
                ),
            )
            # Item 3: Standard (due in 5 days)
            it_standard = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="Standard Task",
                    assignee_id=member.id,
                    due_date=now + timedelta(days=5),
                ),
            )
            # Item 4: No due date (must NEVER be marked overdue)
            it_no_due = create_work_item(
                conn,
                creator_id=member.id,
                creator_name=member.display_name,
                dto=WorkItemCreate(
                    team_id=team.id,
                    kind="task",
                    title="No Due Date Task",
                    assignee_id=member.id,
                    due_date=None,
                ),
            )

    headers = {"X-Dev-User-Email": member.email}
    res = client.get("/me/todo", headers=headers)
    assert res.status_code == 200
    data = res.json()

    # Overdue count must be exactly 1
    assert data["overdue_count"] == 1
    items = data["items"]
    assert len(items) == 4

    # Ordering: Overdue first, then Due Soon, then others
    assert items[0]["id"] == str(it_overdue.id)
    assert items[0]["is_overdue"] is True

    assert items[1]["id"] == str(it_due_soon.id)
    assert items[1]["is_overdue"] is False

    # The item with no due date must have is_overdue == False
    no_due_res = next(i for i in items if i["id"] == str(it_no_due.id))
    assert no_due_res["is_overdue"] is False


# ============================================================================
# 4. Owner Dashboard Organization Scoping and Authorization
# ============================================================================

def test_owner_dashboard_organization_scoping(client):
    """Verify /me/owner-dashboard requires owner/org_head role and is strictly scoped to owner's organization."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_a, _, team_a = _setup_dash_org(conn, "owner_a")
            org_b, _, team_b = _setup_dash_org(conn, "owner_b")

            owner_a = create_user(conn, email=f"owner-a-{uuid.uuid4().hex[:8]}@example.com", display_name="Owner A")
            member_a = create_user(conn, email=f"member-a-{uuid.uuid4().hex[:8]}@example.com", display_name="Member A")

            grant_or_update_role(conn, user_id=owner_a.id, role="owner", scope_type="organization", scope_id=org_a.id)
            grant_or_update_role(conn, user_id=member_a.id, role="member", scope_type="team", scope_id=team_a.id)

            create_work_item(
                conn,
                creator_id=owner_a.id,
                creator_name=owner_a.display_name,
                dto=WorkItemCreate(team_id=team_a.id, kind="task", title="Org A Item"),
            )
            create_work_item(
                conn,
                creator_id=owner_a.id,
                creator_name=owner_a.display_name,
                dto=WorkItemCreate(team_id=team_b.id, kind="task", title="Org B Item"),
            )

    # 1. Non-owner attempting to access owner dashboard -> 403 Forbidden
    res_mem = client.get("/me/owner-dashboard", headers={"X-Dev-User-Email": member_a.email})
    assert res_mem.status_code == 403

    # 2. Owner A retrieves owner dashboard -> 200 OK
    res_owner = client.get("/me/owner-dashboard", headers={"X-Dev-User-Email": owner_a.email})
    assert res_owner.status_code == 200
    dash_data = res_owner.json()

    # Must contain Org A data only
    assert dash_data["organization_id"] == str(org_a.id)
    assert dash_data["organization_name"] == org_a.name
    team_ids_in_dash = [t["team_id"] for t in dash_data["teams"]]
    assert str(team_a.id) in team_ids_in_dash
    assert str(team_b.id) not in team_ids_in_dash

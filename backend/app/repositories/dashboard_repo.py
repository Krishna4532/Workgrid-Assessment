import base64
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status
import psycopg
from psycopg.rows import dict_row

from app.authorization import require_team_access
from app.models import (
    DashboardApprovalsSummary,
    DashboardResponse,
    DashboardWorkSummary,
    OwnerAttentionItem,
    OwnerDashboardResponse,
    OwnerOrgSummary,
    OwnerTeamSummary,
    TeamMemberResponse,
    TodoItem,
    User,
)
from app.repositories.approvals_repo import get_approvals_inbox
from app.repositories.notifications_repo import get_unread_count
from app.repositories.roles_repo import get_active_positions

logger = logging.getLogger("workgrid.dashboard_repo")


def get_user_accessible_team_ids(conn: psycopg.Connection, user_id: UUID) -> List[UUID]:
    """Retrieve all team IDs where the user has active membership or inherited coverage."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT team_id
            FROM effective_roles
            WHERE user_id = %s;
            """,
            (user_id,),
        )
        return [row[0] for row in cur.fetchall()]


def get_dashboard_data(conn: psycopg.Connection, user: User) -> DashboardResponse:
    """Retrieve aggregated dashboard overview strictly scoped to the authenticated caller."""
    positions = get_active_positions(conn, user.id)
    team_ids = get_user_accessible_team_ids(conn, user.id)

    by_status: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    team_open_count = 0
    team_overdue_count = 0
    my_open_count = 0
    my_overdue_count = 0

    if team_ids:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT status, count(*) AS cnt
                FROM work_items
                WHERE team_id = ANY(%s)
                GROUP BY status;
                """,
                (team_ids,),
            )
            by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

            cur.execute(
                """
                SELECT priority, count(*) AS cnt
                FROM work_items
                WHERE team_id = ANY(%s)
                GROUP BY priority;
                """,
                (team_ids,),
            )
            by_priority = {r["priority"]: r["cnt"] for r in cur.fetchall()}

            cur.execute(
                """
                SELECT 
                    count(*) FILTER (WHERE status NOT IN ('resolved', 'closed')) AS team_open,
                    count(*) FILTER (WHERE due_date IS NOT NULL AND due_date < NOW() AND status NOT IN ('resolved', 'closed')) AS team_overdue,
                    count(*) FILTER (WHERE assignee_id = %(user_id)s AND status NOT IN ('resolved', 'closed')) AS my_open,
                    count(*) FILTER (WHERE assignee_id = %(user_id)s AND due_date IS NOT NULL AND due_date < NOW() AND status NOT IN ('resolved', 'closed')) AS my_overdue
                FROM work_items
                WHERE team_id = ANY(%(team_ids)s);
                """,
                {"user_id": user.id, "team_ids": team_ids},
            )
            counts_row = cur.fetchone()
            if counts_row:
                team_open_count = counts_row["team_open"] or 0
                team_overdue_count = counts_row["team_overdue"] or 0
                my_open_count = counts_row["my_open"] or 0
                my_overdue_count = counts_row["my_overdue"] or 0

    # Role requests requiring caller's decision
    role_requests_inbox = get_approvals_inbox(conn, user.id)
    pending_role_requests_count = len(role_requests_inbox)

    # Work items awaiting caller's approval (manager rank >= 2 in team, caller is not assignee)
    pending_work_item_approvals_count = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT team_id
            FROM effective_roles
            WHERE user_id = %s AND role_rank >= 2;
            """,
            (user.id,),
        )
        manager_team_ids = [r[0] for r in cur.fetchall()]
        if manager_team_ids:
            cur.execute(
                """
                SELECT count(*)
                FROM work_items
                WHERE team_id = ANY(%s)
                  AND status = 'awaiting_approval'
                  AND (assignee_id IS NULL OR assignee_id != %s)
                  AND creator_id != %s;
                """,
                (manager_team_ids, user.id, user.id),
            )
            pending_work_item_approvals_count = cur.fetchone()[0]

    total_pending_count = pending_role_requests_count + pending_work_item_approvals_count
    unread_notifications_count = get_unread_count(conn, user.id)

    work_summary = DashboardWorkSummary(
        assigned_open_count=my_open_count,
        assigned_overdue_count=my_overdue_count,
        team_open_count=team_open_count,
        team_overdue_count=team_overdue_count,
        by_status=by_status,
        by_priority=by_priority,
    )
    approvals_summary = DashboardApprovalsSummary(
        pending_role_requests_count=pending_role_requests_count,
        pending_work_item_approvals_count=pending_work_item_approvals_count,
        total_pending_count=total_pending_count,
    )

    greeting = f"Welcome back, {user.display_name}"

    return DashboardResponse(
        user=user,
        greeting=greeting,
        positions=positions,
        open_work_items_count=team_open_count,
        overdue_work_items_count=team_overdue_count,
        my_open_work_items_count=my_open_count,
        my_overdue_work_items_count=my_overdue_count,
        pending_approvals_count=total_pending_count,
        pending_role_requests_count=pending_role_requests_count,
        pending_work_item_approvals_count=pending_work_item_approvals_count,
        unread_notifications_count=unread_notifications_count,
        work_summary=work_summary,
        approvals_summary=approvals_summary,
    )


def get_unified_todo(
    conn: psycopg.Connection,
    user: User,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Tuple[List[TodoItem], int, int, Optional[str], bool]:
    """Retrieve actionable items for the caller: assigned work, eligible approvals, and role requests."""
    limit = min(max(1, limit), 100)
    now = datetime.now(timezone.utc)
    team_ids = get_user_accessible_team_ids(conn, user.id)

    todo_items: List[TodoItem] = []

    # 1. Work items directly assigned to caller requiring action (open or in_progress)
    if team_ids:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                       w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                       w.created_at, w.updated_at,
                       t.name AS team_name,
                       u_assignee.display_name AS assignee_name
                FROM work_items w
                JOIN teams t ON w.team_id = t.id
                JOIN users u_assignee ON w.assignee_id = u_assignee.id
                WHERE w.team_id = ANY(%s)
                  AND w.assignee_id = %s
                  AND w.status IN ('open', 'in_progress');
                """,
                (team_ids, user.id),
            )
            for row in cur.fetchall():
                is_overdue = bool(row["due_date"] and row["due_date"] < now)
                todo_items.append(
                    TodoItem(
                        id=row["id"],
                        item_type="assigned_work_item",
                        title=row["title"],
                        description=row["description"],
                        priority=row["priority"],
                        status=row["status"],
                        is_overdue=is_overdue,
                        due_date=row["due_date"],
                        created_at=row["created_at"],
                        scope_type="team",
                        scope_id=row["team_id"],
                        scope_name=row["team_name"],
                        target_id=row["id"],
                        assignee_id=row["assignee_id"],
                        assignee_name=row["assignee_name"],
                        action_url=f"/work?item={row['id']}",
                    )
                )

    # 2. Work item approvals requiring caller's decision (awaiting_approval, caller has rank >= 2, caller is not assignee)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT team_id
            FROM effective_roles
            WHERE user_id = %s AND role_rank >= 2;
            """,
            (user.id,),
        )
        manager_team_ids = [r["team_id"] for r in cur.fetchall()]
        if manager_team_ids:
            cur.execute(
                """
                SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                       w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                       w.created_at, w.updated_at,
                       t.name AS team_name,
                       u_assignee.display_name AS assignee_name
                FROM work_items w
                JOIN teams t ON w.team_id = t.id
                LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
                WHERE w.team_id = ANY(%s)
                  AND w.status = 'awaiting_approval'
                  AND (w.assignee_id IS NULL OR w.assignee_id != %s)
                  AND w.creator_id != %s;
                """,
                (manager_team_ids, user.id, user.id),
            )
            for row in cur.fetchall():
                is_overdue = bool(row["due_date"] and row["due_date"] < now)
                todo_items.append(
                    TodoItem(
                        id=row["id"],
                        item_type="work_item_approval",
                        title=f"Review Work Item: {row['title']}",
                        description=row["description"],
                        priority=row["priority"],
                        status=row["status"],
                        is_overdue=is_overdue,
                        due_date=row["due_date"],
                        created_at=row["created_at"],
                        scope_type="team",
                        scope_id=row["team_id"],
                        scope_name=row["team_name"],
                        target_id=row["id"],
                        assignee_id=row["assignee_id"],
                        assignee_name=row["assignee_name"],
                        action_url=f"/work?item={row['id']}",
                    )
                )

    # 3. Role requests requiring caller's decision
    role_requests_inbox = get_approvals_inbox(conn, user.id)
    for req in role_requests_inbox:
        # High priority for manager/head/owner requests, normal for others
        req_prio = "high" if req.requested_role in ("owner", "organization_head", "department_manager") else "normal"
        todo_items.append(
            TodoItem(
                id=req.id,
                item_type="role_request_approval",
                title=f"Role Request: {req.user_name or req.user_email} requests {req.requested_role}",
                description=req.reason,
                priority=req_prio,
                status=req.status,
                is_overdue=False,
                due_date=None,
                created_at=req.created_at,
                scope_type=req.scope_type,
                scope_id=req.scope_id,
                scope_name=req.scope_name,
                target_id=req.id,
                requester_id=req.user_id,
                requester_name=req.user_name,
                requester_email=req.user_email,
                requested_role=req.requested_role,
                action_url=f"/approvals?request={req.id}",
            )
        )

    # WG-07: Urgency Tiers implementation according to Blueprint §11:
    # Tier 0: Overdue (due_date < now)
    # Tier 1: Due Soon (now <= due_date <= now + 24 hours)
    # Tier 2: Standard (due_date > now + 24 hours or missing due_date)
    # Within tier: Priority (urgent > high > normal > low), Due date ASC (nulls last), Created at ASC, Item ID ASC.
    p_weights = {"urgent": 4, "high": 3, "normal": 2, "low": 1}
    one_day_later = now + timedelta(hours=24)

    def todo_sort_key(item: TodoItem) -> Tuple[int, int, float, float, str]:
        if item.due_date:
            if item.due_date < now:
                tier = 0
            elif item.due_date <= one_day_later:
                tier = 1
            else:
                tier = 2
        else:
            tier = 2

        priority_rank = -p_weights.get(item.priority, 2)
        due_rank = item.due_date.timestamp() if item.due_date else 1e15
        created_rank = item.created_at.timestamp()
        id_str = str(item.id)
        return (tier, priority_rank, due_rank, created_rank, id_str)

    todo_items.sort(key=todo_sort_key)

    total_count = len(todo_items)
    overdue_count = sum(1 for item in todo_items if item.is_overdue)

    # WG-06: Stable Keyset / Cursor Pagination
    # Prevents drifts, skips, and duplicate records across concurrent additions/removals.
    filtered_items = todo_items
    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
            if raw.startswith("wgk:"):
                # Keyset cursor format: wgk:tier:priority_rank:due_rank:created_rank:id
                parts = raw.split(":", 5)
                if len(parts) != 6:
                    raise ValueError("Malformed keyset cursor")
                cursor_key = (
                    int(parts[1]),
                    int(parts[2]),
                    float(parts[3]),
                    float(parts[4]),
                    parts[5],
                )
                filtered_items = [item for item in todo_items if todo_sort_key(item) > cursor_key]
            elif raw.startswith("todo:"):
                # Backwards-compatible legacy offset cursor
                offset = int(raw.split(":", 1)[1])
                if offset < 0:
                    raise ValueError("Negative offset")
                filtered_items = todo_items[offset:]
            else:
                raise ValueError("Unrecognized cursor prefix")
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid pagination cursor format",
            )

    page_items = filtered_items[:limit]
    has_more = len(filtered_items) > limit
    next_cursor = None
    if has_more and page_items:
        last_item = page_items[-1]
        last_key = todo_sort_key(last_item)
        payload = f"wgk:{last_key[0]}:{last_key[1]}:{last_key[2]}:{last_key[3]}:{last_key[4]}"
        next_cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")

    return page_items, total_count, overdue_count, next_cursor, has_more


def get_team_members(
    conn: psycopg.Connection,
    team_id: UUID,
    caller_id: UUID,
) -> List[TeamMemberResponse]:
    """Retrieve members with active effective roles in the specified team.
    
    Enforces Rule #9: If caller lacks access to the team, returns 404 (non-disclosure).
    """
    require_team_access(conn, caller_id, team_id, min_rank=0, resource_description="Team")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 
                u.id AS user_id,
                u.display_name,
                u.email,
                u.status,
                er.role,
                er.role_rank,
                er.scope_type,
                er.assignment_id
            FROM effective_roles er
            JOIN users u ON u.id = er.user_id
            WHERE er.team_id = %s AND u.status = 'active'
            ORDER BY er.role_rank DESC, u.display_name ASC;
            """,
            (team_id,),
        )
        rows = cur.fetchall()
        return [TeamMemberResponse(**r) for r in rows]


def get_owner_dashboard_data(
    conn: psycopg.Connection,
    user: User,
) -> OwnerDashboardResponse:
    """Retrieve organization-wide operational dashboard for an authorized Organization Owner or Head.
    
    Guarantees:
    - Scoped strictly to the caller's authorized organization.
    - Non-disclosure 403 if caller lacks active owner or organization_head assignment.
    - Zero N+1 queries: computes summary, team-level breakdowns, and attention items in consolidated queries.
    - Accurate status and priority aggregations without duplicate counting.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        # 1. Identify owner's authorized organization
        cur.execute(
            """
            SELECT ra.organization_id, o.name AS organization_name
            FROM role_assignments ra
            JOIN organizations o ON ra.organization_id = o.id
            WHERE ra.user_id = %s
              AND ra.scope_type = 'organization'
              AND ra.role IN ('owner', 'organization_head')
              AND ra.status = 'active'
            ORDER BY CASE WHEN ra.role = 'owner' THEN 1 ELSE 2 END, ra.created_at ASC
            LIMIT 1;
            """,
            (user.id,),
        )
        org_row = cur.fetchone()
        if not org_row:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization Owner or Head authorization required to view organization overview",
            )

        org_id = org_row["organization_id"]
        org_name = org_row["organization_name"]

        # 2. Organization-wide summary metrics
        cur.execute(
            """
            SELECT 
                COUNT(*) AS total_work_items,
                COUNT(*) FILTER (WHERE w.status = 'in_progress') AS in_progress_count,
                COUNT(*) FILTER (WHERE w.status IN ('resolved', 'closed')) AS completed_count,
                COUNT(*) FILTER (WHERE w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status NOT IN ('resolved', 'closed')) AS overdue_count,
                COUNT(*) FILTER (WHERE w.status = 'awaiting_approval') AS blocked_count,
                COUNT(*) FILTER (WHERE w.assignee_id IS NULL AND w.status NOT IN ('resolved', 'closed')) AS unassigned_count,
                COUNT(*) FILTER (WHERE w.status IN ('open', 'in_progress', 'awaiting_approval')) AS active_count
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN departments d ON t.department_id = d.id
            WHERE d.organization_id = %s;
            """,
            (org_id,),
        )
        summary_row = cur.fetchone() or {}

        # Status distribution
        cur.execute(
            """
            SELECT w.status, count(*) AS cnt
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN departments d ON t.department_id = d.id
            WHERE d.organization_id = %s
            GROUP BY w.status;
            """,
            (org_id,),
        )
        by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

        # Priority distribution
        cur.execute(
            """
            SELECT w.priority, count(*) AS cnt
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN departments d ON t.department_id = d.id
            WHERE d.organization_id = %s
            GROUP BY w.priority;
            """,
            (org_id,),
        )
        by_priority = {r["priority"]: r["cnt"] for r in cur.fetchall()}

        summary = OwnerOrgSummary(
            organization_id=org_id,
            organization_name=org_name,
            total_work_items=summary_row.get("total_work_items", 0),
            in_progress_count=summary_row.get("in_progress_count", 0),
            completed_count=summary_row.get("completed_count", 0),
            overdue_count=summary_row.get("overdue_count", 0),
            blocked_count=summary_row.get("blocked_count", 0),
            unassigned_count=summary_row.get("unassigned_count", 0),
            active_count=summary_row.get("active_count", 0),
            by_status=by_status,
            by_priority=by_priority,
        )

        # 3. Team-level breakdown
        cur.execute(
            """
            SELECT 
                t.id AS team_id,
                t.name AS team_name,
                d.name AS department_name,
                COUNT(w.id) AS total_work,
                COUNT(w.id) FILTER (WHERE w.status IN ('open', 'in_progress', 'awaiting_approval')) AS active_work,
                COUNT(w.id) FILTER (WHERE w.status IN ('resolved', 'closed')) AS completed_work,
                COUNT(w.id) FILTER (WHERE w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status NOT IN ('resolved', 'closed')) AS overdue_work,
                COUNT(w.id) FILTER (WHERE w.status = 'awaiting_approval') AS blocked_work,
                COUNT(w.id) FILTER (WHERE (w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status NOT IN ('resolved', 'closed')) OR w.status = 'awaiting_approval') AS attention_count,
                (SELECT COUNT(DISTINCT er.user_id) FROM effective_roles er WHERE er.team_id = t.id) AS members_count
            FROM teams t
            JOIN departments d ON t.department_id = d.id
            LEFT JOIN work_items w ON w.team_id = t.id
            WHERE d.organization_id = %s
            GROUP BY t.id, t.name, d.name
            ORDER BY d.name, t.name;
            """,
            (org_id,),
        )
        team_rows = cur.fetchall()
        teams = [OwnerTeamSummary(**tr) for tr in team_rows]

        # 4. Immediate attention items (Overdue or Awaiting Approval)
        cur.execute(
            """
            SELECT 
                w.id,
                w.title,
                t.name AS team_name,
                w.priority,
                w.status,
                w.due_date,
                u.display_name AS assignee_name,
                CASE 
                    WHEN w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status = 'awaiting_approval' THEN 'Overdue & Awaiting Approval'
                    WHEN w.due_date IS NOT NULL AND w.due_date < NOW() THEN 'Overdue'
                    ELSE 'Awaiting Approval'
                END AS reason
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN departments d ON t.department_id = d.id
            LEFT JOIN users u ON w.assignee_id = u.id
            WHERE d.organization_id = %s
              AND (
                  (w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status NOT IN ('resolved', 'closed'))
                  OR w.status = 'awaiting_approval'
              )
            ORDER BY 
                CASE WHEN w.priority = 'urgent' THEN 1 WHEN w.priority = 'high' THEN 2 ELSE 3 END,
                w.due_date ASC NULLS LAST
            LIMIT 25;
            """,
            (org_id,),
        )
        attention_rows = cur.fetchall()
        attention_items = [OwnerAttentionItem(**ar) for ar in attention_rows]

        # 5. Pending approvals within organization
        cur.execute(
            """
            SELECT COUNT(*)
            FROM role_requests rr
            WHERE rr.status = 'pending'
              AND (
                  rr.organization_id = %(org_id)s
                  OR rr.department_id IN (SELECT id FROM departments WHERE organization_id = %(org_id)s)
                  OR rr.team_id IN (
                      SELECT t.id FROM teams t JOIN departments d ON t.department_id = d.id WHERE d.organization_id = %(org_id)s
                  )
              );
            """,
            {"org_id": org_id},
        )
        pending_role_requests_count = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN departments d ON t.department_id = d.id
            WHERE d.organization_id = %s AND w.status = 'awaiting_approval';
            """,
            (org_id,),
        )
        pending_work_item_approvals_count = cur.fetchone()["count"]

        return OwnerDashboardResponse(
            organization_id=org_id,
            organization_name=org_name,
            summary=summary,
            teams=teams,
            attention_items=attention_items,
            pending_role_requests_count=pending_role_requests_count,
            pending_work_item_approvals_count=pending_work_item_approvals_count,
        )

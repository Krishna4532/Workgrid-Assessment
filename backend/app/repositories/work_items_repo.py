import base64
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4
from fastapi import HTTPException, status
import psycopg
from psycopg.rows import dict_row

from app.models import (
    ActivityItem,
    WorkItem,
    WorkItemCreate,
    WorkItemKind,
    WorkItemPriority,
    WorkItemStatus,
    WorkItemUpdate,
)
from app.repositories.outbox_repo import enqueue_outbox_event
from app.sse import emit_pg_notify

logger = logging.getLogger("workgrid.work_items")


def _encode_cursor(updated_at: datetime, item_id: UUID) -> str:
    """Encode (updated_at, id) tuple into a stable, opaque base64 cursor."""
    raw = f"{updated_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _decode_cursor(cursor: str) -> Tuple[datetime, UUID]:
    """Decode base64 cursor into (updated_at, id) tuple."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        ts_str, id_str = raw.split("|")
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid pagination cursor format",
        )


def create_work_item(
    conn: psycopg.Connection,
    creator_id: UUID,
    creator_name: str,
    dto: WorkItemCreate,
) -> WorkItem:
    """Create a new work item with atomic creation activity record."""
    initial_status: WorkItemStatus = "in_progress" if dto.assignee_id else "open"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO work_items (
                team_id, kind, title, description, status, priority,
                requires_approval, assignee_id, creator_id, due_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, team_id, kind, title, description, status, priority,
                      requires_approval, assignee_id, creator_id, due_date,
                      version, created_at, updated_at;
            """,
            (
                dto.team_id,
                dto.kind,
                dto.title,
                dto.description,
                initial_status,
                dto.priority,
                dto.requires_approval,
                dto.assignee_id,
                creator_id,
                dto.due_date,
            ),
        )
        row = cur.fetchone()
        item = WorkItem(**row, creator_name=creator_name)

        # Record activity event
        cur.execute(
            """
            INSERT INTO activity (
                work_item_id, actor_id, event_type, new_status, new_assignee_id
            ) VALUES (%s, %s, 'created', %s, %s);
            """,
            (item.id, creator_id, initial_status, dto.assignee_id),
        )

        # Enqueue transactional outbox event
        enqueue_outbox_event(
            conn,
            "work_item_created",
            {
                "item_id": str(item.id),
                "team_id": str(item.team_id),
                "title": item.title,
                "kind": item.kind,
                "status": item.status,
                "priority": item.priority,
                "creator_id": str(creator_id),
                "creator_name": creator_name,
                "actor_id": str(creator_id),
                "actor_name": creator_name,
                "assignee_id": str(dto.assignee_id) if dto.assignee_id else None,
            },
        )

        return item


def get_work_item_by_id(conn: psycopg.Connection, item_id: UUID) -> Optional[WorkItem]:
    """Retrieve a single work item by ID with creator and assignee display names."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   t.name AS team_name,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN teams t ON w.team_id = t.id
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.id = %s;
            """,
            (item_id,),
        )
        row = cur.fetchone()
        return WorkItem(**row) if row else None


def list_work_items(
    conn: psycopg.Connection,
    team_ids: List[UUID],
    view: str = "all",
    user_id: Optional[UUID] = None,
    status_filter: Optional[WorkItemStatus] = None,
    priority_filter: Optional[WorkItemPriority] = None,
    kind_filter: Optional[WorkItemKind] = None,
    assignee_id: Optional[UUID] = None,
    q: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 20,
) -> Tuple[List[WorkItem], Optional[str], bool]:
    """List work items scoped to accessible teams using keyset pagination."""
    if not team_ids:
        return [], None, False

    limit = min(max(1, limit), 100)
    clauses = ["w.team_id = ANY(%s)"]
    params: List[Any] = [team_ids]

    if view == "mine" and user_id:
        clauses.append("w.assignee_id = %s")
        params.append(user_id)
    elif view == "unassigned":
        clauses.append("w.assignee_id IS NULL AND w.status = 'open'")
    elif view == "approvals":
        clauses.append("w.status = 'awaiting_approval'")
    elif view == "overdue":
        clauses.append("w.due_date IS NOT NULL AND w.due_date < NOW() AND w.status NOT IN ('resolved', 'closed')")

    if status_filter:
        clauses.append("w.status = %s")
        params.append(status_filter)

    if priority_filter:
        clauses.append("w.priority = %s")
        params.append(priority_filter)

    if kind_filter:
        clauses.append("w.kind = %s")
        params.append(kind_filter)

    if assignee_id:
        clauses.append("w.assignee_id = %s")
        params.append(assignee_id)

    if q and q.strip():
        clauses.append("w.search_tsv @@ plainto_tsquery('english', %s)")
        params.append(q.strip())

    if cursor:
        c_ts, c_id = _decode_cursor(cursor)
        clauses.append("(w.updated_at, w.id) < (%s, %s)")
        params.extend([c_ts, c_id])

    sql = f"""
        SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
               w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
               w.version, w.created_at, w.updated_at,
               t.name AS team_name,
               u_creator.display_name AS creator_name,
               u_assignee.display_name AS assignee_name
        FROM work_items w
        JOIN teams t ON w.team_id = t.id
        JOIN users u_creator ON w.creator_id = u_creator.id
        LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
        WHERE {" AND ".join(clauses)}
        ORDER BY w.updated_at DESC, w.id DESC
        LIMIT %s;
    """
    params.append(limit + 1)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    has_more = len(rows) > limit
    results = [WorkItem(**r) for r in rows[:limit]]

    next_cursor = None
    if has_more and results:
        last_item = results[-1]
        next_cursor = _encode_cursor(last_item.updated_at, last_item.id)

    return results, next_cursor, has_more


def claim_work_item(
    conn: psycopg.Connection, item_id: UUID, caller_id: UUID, caller_name: str
) -> WorkItem:
    """Atomically claim an open and unowned item.
    
    If winner, sets status to 'in_progress' and assignee to caller.
    If competing update arrived first, returns HTTP 409 naming the owner.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE work_items
            SET status = 'in_progress',
                assignee_id = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND status = 'open' AND assignee_id IS NULL
            RETURNING id, team_id, kind, title, description, status, priority,
                      requires_approval, assignee_id, creator_id, due_date,
                      version, created_at, updated_at;
            """,
            (caller_id, item_id),
        )
        updated = cur.fetchone()

        if updated:
            item = WorkItem(**updated, assignee_name=caller_name)
            cur.execute(
                """
                INSERT INTO activity (
                    work_item_id, actor_id, event_type, previous_status,
                    new_status, new_assignee_id
                ) VALUES (%s, %s, 'claimed', 'open', 'in_progress', %s);
                """,
                (item.id, caller_id, caller_id),
            )
            enqueue_outbox_event(
                conn,
                "work_item_claimed",
                {
                    "item_id": str(item.id),
                    "team_id": str(item.team_id),
                    "title": item.title,
                    "kind": item.kind,
                    "status": item.status,
                    "actor_id": str(caller_id),
                    "actor_name": caller_name,
                    "new_assignee_id": str(caller_id),
                },
            )
            return item

        # Statement affected zero rows: check why
        current_item = get_work_item_by_id(conn, item_id)
        if not current_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Work item not found",
            )

        owner_desc = current_item.assignee_name or "another user"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Item is already claimed by {owner_desc}",
        )


def assign_work_item(
    conn: psycopg.Connection,
    item_id: UUID,
    actor_id: UUID,
    assignee_id: Optional[UUID],
    expected_version: int,
    reason: Optional[str] = None,
) -> WorkItem:
    """Assign or unassign an item using optimistic version verification."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM work_items WHERE id = %s FOR UPDATE;", (item_id,))
        current = cur.fetchone()
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

        if current["version"] != expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Item was modified concurrently (current version: {current['version']}, supplied: {expected_version})",
            )

        new_status = current["status"]
        if assignee_id and current["status"] == "open":
            new_status = "in_progress"
        elif not assignee_id and current["status"] == "in_progress":
            new_status = "open"

        cur.execute(
            """
            UPDATE work_items
            SET assignee_id = %s,
                status = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND version = %s
            RETURNING id, team_id, kind, title, description, status, priority,
                      requires_approval, assignee_id, creator_id, due_date,
                      version, created_at, updated_at;
            """,
            (assignee_id, new_status, item_id, expected_version),
        )
        updated = cur.fetchone()

        cur.execute(
            """
            INSERT INTO activity (
                work_item_id, actor_id, event_type, previous_status, new_status,
                previous_assignee_id, new_assignee_id, reason
            ) VALUES (%s, %s, 'assigned', %s, %s, %s, %s, %s);
            """,
            (
                item_id,
                actor_id,
                current["status"],
                new_status,
                current["assignee_id"],
                assignee_id,
                reason,
            ),
        )

        enqueue_outbox_event(
            conn,
            "work_item_assigned",
            {
                "item_id": str(updated["id"]),
                "team_id": str(updated["team_id"]),
                "title": updated["title"],
                "kind": updated["kind"],
                "status": updated["status"],
                "actor_id": str(actor_id),
                "previous_assignee_id": str(current["assignee_id"]) if current["assignee_id"] else None,
                "new_assignee_id": str(assignee_id) if assignee_id else None,
                "reason": reason,
            },
        )

        return WorkItem(**updated)


def transition_work_item(
    conn: psycopg.Connection,
    item_id: UUID,
    actor_id: UUID,
    to_status: WorkItemStatus,
    expected_version: int,
    reason: Optional[str] = None,
) -> WorkItem:
    """Transition work item status with optimistic version check."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM work_items WHERE id = %s FOR UPDATE;", (item_id,))
        current = cur.fetchone()
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

        if current["version"] != expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Item was modified concurrently (current version: {current['version']}, expected: {expected_version})",
            )

        new_assignee_id = current["assignee_id"]
        event_type = "status_changed"
        if to_status == "open":
            # Releasing an item clears assignee
            new_assignee_id = None
            event_type = "released"
        elif to_status == "in_progress" and not new_assignee_id:
            # Transitioning an unassigned item to in_progress assigns it to the acting user
            new_assignee_id = actor_id

        cur.execute(
            """
            UPDATE work_items
            SET status = %s,
                assignee_id = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND version = %s
            RETURNING id, team_id, kind, title, description, status, priority,
                      requires_approval, assignee_id, creator_id, due_date,
                      version, created_at, updated_at;
            """,
            (to_status, new_assignee_id, item_id, expected_version),
        )
        updated = cur.fetchone()

        cur.execute(
            """
            INSERT INTO activity (
                work_item_id, actor_id, event_type, previous_status, new_status,
                previous_assignee_id, new_assignee_id, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                item_id,
                actor_id,
                event_type,
                current["status"],
                to_status,
                current["assignee_id"],
                new_assignee_id,
                reason,
            ),
        )

        enqueue_outbox_event(
            conn,
            "work_item_status_changed",
            {
                "item_id": str(updated["id"]),
                "team_id": str(updated["team_id"]),
                "title": updated["title"],
                "kind": updated["kind"],
                "previous_status": current["status"],
                "new_status": to_status,
                "creator_id": str(updated["creator_id"]),
                "assignee_id": str(updated["assignee_id"]) if updated["assignee_id"] else None,
                "actor_id": str(actor_id),
                "reason": reason,
            },
        )

        return WorkItem(**updated)


def update_work_item_fields(
    conn: psycopg.Connection,
    item_id: UUID,
    actor_id: UUID,
    dto: WorkItemUpdate,
) -> WorkItem:
    """Update editable fields of a work item with optimistic version check."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM work_items WHERE id = %s FOR UPDATE;", (item_id,))
        current = cur.fetchone()
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

        if current["version"] != dto.version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Version mismatch. Server version is {current['version']}, supplied version was {dto.version}",
            )

        title = dto.title if dto.title is not None else current["title"]
        description = dto.description if dto.description is not None else current["description"]
        priority = dto.priority if dto.priority is not None else current["priority"]
        due_date = dto.due_date if dto.due_date is not None else current["due_date"]
        changes = {
            field: {"from": current[field], "to": value}
            for field, value in (
                ("title", title),
                ("description", description),
                ("priority", priority),
                ("due_date", due_date),
            )
            if current[field] != value
        }

        cur.execute(
            """
            UPDATE work_items
            SET title = %s,
                description = %s,
                priority = %s,
                due_date = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND version = %s
            RETURNING id, team_id, kind, title, description, status, priority,
                      requires_approval, assignee_id, creator_id, due_date,
                      version, created_at, updated_at;
            """,
            (title, description, priority, due_date, item_id, dto.version),
        )
        updated = cur.fetchone()
        cur.execute(
            """
            INSERT INTO activity (work_item_id, actor_id, event_type, metadata)
            VALUES (%s, %s, 'updated', %s::jsonb);
            """,
            (item_id, actor_id, json.dumps(changes, default=str)),
        )
        emit_pg_notify(
            conn,
            {
                "id": str(uuid4()),
                "event_type": "work_item_updated",
                "team_id": str(updated["team_id"]),
                "item_id": str(item_id),
            },
        )
        return WorkItem(**updated)


def add_comment_to_item(
    conn: psycopg.Connection,
    item_id: UUID,
    actor_id: UUID,
    actor_name: str,
    comment: str,
) -> ActivityItem:
    """Add a comment to the work item's append-only activity timeline."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO activity (work_item_id, actor_id, event_type, comment)
            VALUES (%s, %s, 'comment', %s)
            RETURNING id, work_item_id, actor_id, event_type, previous_status,
                      new_status, previous_assignee_id, new_assignee_id,
                      comment, reason, metadata, created_at;
            """,
            (item_id, actor_id, comment),
        )
        row = cur.fetchone()
        cur.execute("SELECT team_id FROM work_items WHERE id = %s;", (item_id,))
        wi_row = cur.fetchone()
        team_id = wi_row["team_id"] if wi_row else None
        emit_pg_notify(
            conn,
            {
                "id": str(row["id"]),
                "event_type": "comment_added",
                "team_id": str(team_id) if team_id else None,
                "item_id": str(item_id),
            },
        )
        return ActivityItem(**row, actor_name=actor_name)


def get_item_activity(
    conn: psycopg.Connection, item_id: UUID
) -> List[ActivityItem]:
    """Retrieve full append-only timeline for a work item ordered by creation time."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.id, a.work_item_id, a.actor_id, a.event_type, a.previous_status,
                   a.new_status, a.previous_assignee_id, a.new_assignee_id,
                   a.comment, a.reason, a.metadata, a.created_at,
                   u.display_name AS actor_name
            FROM activity a
            JOIN users u ON a.actor_id = u.id
            WHERE a.work_item_id = %s
            ORDER BY a.created_at ASC, a.id ASC;
            """,
            (item_id,),
        )
        rows = cur.fetchall()
        return [ActivityItem(**r) for r in rows]


def get_items_summary_counts(
    conn: psycopg.Connection, team_ids: List[UUID]
) -> Dict[str, Any]:
    """Retrieve summary counts by status and priority across accessible teams."""
    if not team_ids:
        return {"total": 0, "by_status": {}, "by_priority": {}, "overdue": 0}

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
        status_counts = {r["status"]: r["cnt"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT priority, count(*) AS cnt
            FROM work_items
            WHERE team_id = ANY(%s)
            GROUP BY priority;
            """,
            (team_ids,),
        )
        priority_counts = {r["priority"]: r["cnt"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT count(*) AS cnt
            FROM work_items
            WHERE team_id = ANY(%s)
              AND due_date IS NOT NULL
              AND due_date < NOW()
              AND status NOT IN ('resolved', 'closed');
            """,
            (team_ids,),
        )
        overdue_cnt = cur.fetchone()["cnt"]

    return {
        "total": sum(status_counts.values()),
        "by_status": status_counts,
        "by_priority": priority_counts,
        "overdue": overdue_cnt,
    }

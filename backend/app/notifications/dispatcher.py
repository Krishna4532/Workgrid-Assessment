import logging
from typing import Any, Dict, List, Optional, Set
from uuid import UUID
import psycopg
from psycopg.rows import dict_row

from app.models import NotificationItem, OutboxEvent
from app.repositories.approvals_repo import check_decider_authority
from app.repositories.notifications_repo import create_notification

logger = logging.getLogger("workgrid.dispatcher")

KNOWN_EVENT_TYPES = {
    "work_item_created",
    "work_item_assigned",
    "work_item_claimed",
    "work_item_status_changed",
    "role_request_submitted",
    "role_request_approved",
    "role_request_rejected",
    "role_request_cancelled",
    "role_assignment_changed",
}


class PermanentEventError(Exception):
    """An outbox event cannot succeed through retrying, for example an unknown type."""


def _get_team_managers(conn: psycopg.Connection, team_id: UUID) -> List[UUID]:
    """Retrieve all users holding active roles of rank >= 2 (team leader, department manager, org head, owner) covering a team."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT d.organization_id, t.department_id
            FROM teams t
            JOIN departments d ON t.department_id = d.id
            WHERE t.id = %s;
            """,
            (team_id,),
        )
        row = cur.fetchone()
        if not row:
            return []

        org_id = row["organization_id"]
        dept_id = row["department_id"]

        cur.execute(
            """
            SELECT DISTINCT user_id
            FROM role_assignments
            WHERE status = 'active'
              AND (
                  (scope_type = 'team' AND team_id = %s AND role = 'team_leader')
                  OR (scope_type = 'department' AND department_id = %s AND role = 'department_manager')
                  OR (scope_type = 'organization' AND organization_id = %s AND role IN ('organization_head', 'owner'))
              );
            """,
            (team_id, dept_id, org_id),
        )
        return [r["user_id"] for r in cur.fetchall()]


def _get_eligible_role_request_deciders(
    conn: psycopg.Connection, request_id: UUID, requester_id: UUID
) -> List[UUID]:
    """Find all users holding covering roles authorized to decide a role request."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM role_requests WHERE id = %s;", (request_id,))
        req_row = cur.fetchone()
        if not req_row:
            return []

        # Find candidate users holding active leadership/owner roles
        cur.execute(
            """
            SELECT DISTINCT user_id
            FROM role_assignments
            WHERE status = 'active'
              AND role IN ('team_leader', 'department_manager', 'organization_head', 'owner')
              AND user_id != %s;
            """,
            (requester_id,),
        )
        candidates = [r["user_id"] for r in cur.fetchall()]

    deciders = []
    for cand_id in candidates:
        if check_decider_authority(conn, cand_id, req_row):
            deciders.append(cand_id)
    return deciders


def dispatch_outbox_event(
    conn: psycopg.Connection,
    event: OutboxEvent,
) -> List[NotificationItem]:
    """Translate a committed outbox business event into target in-app notifications.
    
    Rules enforced:
    1. Deterministic deduplication key: `${event.id}:${recipient_id}:${action}`
    2. Actor exclusion: An actor is never notified about their own action.
    3. Scoped recipients: Only directly involved participants or authorized deciders are notified.
    4. Database idempotency: Safe across multiple worker attempts or retries.
    """
    event_type = event.event_type
    p = event.payload
    notifications: List[NotificationItem] = []

    if event_type not in KNOWN_EVENT_TYPES:
        raise PermanentEventError(f"Unsupported outbox event type: {event_type}")

    actor_id_str = p.get("actor_id")
    actor_id = UUID(actor_id_str) if actor_id_str else None
    actor_name = p.get("actor_name") or "A team member"

    if event_type == "work_item_created":
        assignee_id_str = p.get("assignee_id")
        if assignee_id_str:
            assignee_id = UUID(assignee_id_str)
            if assignee_id != actor_id:
                notif = create_notification(
                    conn=conn,
                    user_id=assignee_id,
                    event_id=event.id,
                    dedupe_key=f"{event.id}:{assignee_id}:assigned",
                    notification_type="work_item_assigned",
                    title="Work Item Assigned",
                    message=f"You were assigned to '{p.get('title')}' by {actor_name}.",
                    entity_type="work_item",
                    entity_id=UUID(p["item_id"]),
                )
                if notif:
                    notifications.append(notif)

    elif event_type in ("work_item_assigned", "work_item_claimed"):
        new_assignee_id_str = p.get("new_assignee_id")
        if new_assignee_id_str:
            new_assignee_id = UUID(new_assignee_id_str)
            if new_assignee_id != actor_id:
                action_text = "claimed" if event_type == "work_item_claimed" else "assigned"
                notif = create_notification(
                    conn=conn,
                    user_id=new_assignee_id,
                    event_id=event.id,
                    dedupe_key=f"{event.id}:{new_assignee_id}:assigned",
                    notification_type="work_item_assigned",
                    title="Work Item Assigned",
                    message=f"Work item '{p.get('title')}' was {action_text} to you by {actor_name}.",
                    entity_type="work_item",
                    entity_id=UUID(p["item_id"]),
                )
                if notif:
                    notifications.append(notif)

    elif event_type == "work_item_status_changed":
        new_status = p.get("new_status")
        item_title = p.get("title") or "work item"
        item_id = UUID(p["item_id"])
        team_id = UUID(p["team_id"]) if p.get("team_id") else None

        if new_status == "awaiting_approval" and team_id:
            # Notify covering team leaders/managers
            managers = _get_team_managers(conn, team_id)
            for mgr_id in managers:
                if mgr_id != actor_id:
                    notif = create_notification(
                        conn=conn,
                        user_id=mgr_id,
                        event_id=event.id,
                        dedupe_key=f"{event.id}:{mgr_id}:awaiting_approval",
                        notification_type="work_item_approval_required",
                        title=f"Approval Required: {item_title}",
                        message=f"Work item '{item_title}' is awaiting review from {actor_name}.",
                        entity_type="work_item",
                        entity_id=item_id,
                    )
                    if notif:
                        notifications.append(notif)

        elif new_status in ("resolved", "closed"):
            # Notify creator and assignee
            recipients: Set[UUID] = set()
            if p.get("creator_id"):
                recipients.add(UUID(p["creator_id"]))
            if p.get("assignee_id"):
                recipients.add(UUID(p["assignee_id"]))

            for recip_id in recipients:
                if recip_id != actor_id:
                    notif = create_notification(
                        conn=conn,
                        user_id=recip_id,
                        event_id=event.id,
                        dedupe_key=f"{event.id}:{recip_id}:{new_status}",
                        notification_type=f"work_item_{new_status}",
                        title=f"Work Item {new_status.title()}",
                        message=f"Work item '{item_title}' was marked as {new_status} by {actor_name}.",
                        entity_type="work_item",
                        entity_id=item_id,
                    )
                    if notif:
                        notifications.append(notif)

    elif event_type == "role_request_submitted":
        req_id = UUID(p["request_id"])
        requester_id = UUID(p["user_id"])
        role_name = p.get("requested_role")
        scope_name = p.get("scope_name") or p.get("scope_type")

        # Notify eligible deciders
        deciders = _get_eligible_role_request_deciders(conn, req_id, requester_id)
        for dec_id in deciders:
            if dec_id != actor_id:
                notif = create_notification(
                    conn=conn,
                    user_id=dec_id,
                    event_id=event.id,
                    dedupe_key=f"{event.id}:{dec_id}:role_requested",
                    notification_type="role_request_submitted",
                    title="Role Request Pending Approval",
                    message=f"{p.get('user_name', 'A user')} requested role '{role_name}' for {scope_name}.",
                    entity_type="role_request",
                    entity_id=req_id,
                )
                if notif:
                    notifications.append(notif)

    elif event_type == "role_request_approved":
        req_id = UUID(p["request_id"])
        requester_id = UUID(p["user_id"])
        role_name = p.get("requested_role")
        scope_name = p.get("scope_name") or p.get("scope_type")

        if requester_id != actor_id:
            notif = create_notification(
                conn=conn,
                user_id=requester_id,
                event_id=event.id,
                dedupe_key=f"{event.id}:{requester_id}:approved",
                notification_type="role_request_approved",
                title="Role Request Approved",
                message=f"Your request for role '{role_name}' at {scope_name} was approved by {actor_name}.",
                entity_type="role_request",
                entity_id=req_id,
            )
            if notif:
                notifications.append(notif)

    elif event_type == "role_request_rejected":
        req_id = UUID(p["request_id"])
        requester_id = UUID(p["user_id"])
        role_name = p.get("requested_role")
        scope_name = p.get("scope_name") or p.get("scope_type")
        reason = p.get("reason") or "Declined by reviewer"

        if requester_id != actor_id:
            notif = create_notification(
                conn=conn,
                user_id=requester_id,
                event_id=event.id,
                dedupe_key=f"{event.id}:{requester_id}:rejected",
                notification_type="role_request_rejected",
                title="Role Request Declined",
                message=f"Your request for role '{role_name}' at {scope_name} was declined: {reason}",
                entity_type="role_request",
                entity_id=req_id,
            )
            if notif:
                notifications.append(notif)

    elif event_type == "role_assignment_changed":
        user_id = UUID(p["user_id"])
        action = p.get("action", "updated")
        role_name = p.get("role")
        reason = p.get("reason") or "Administrative update"

        if user_id != actor_id and action in ("suspended", "revoked"):
            notif = create_notification(
                conn=conn,
                user_id=user_id,
                event_id=event.id,
                dedupe_key=f"{event.id}:{user_id}:{action}",
                notification_type=f"role_{action}",
                title=f"Role Assignment {action.title()}",
                message=f"Your role '{role_name}' was {action} by {actor_name}. Reason: {reason}.",
                entity_type="role_assignment",
                entity_id=UUID(p["assignment_id"]) if p.get("assignment_id") else None,
            )
            if notif:
                notifications.append(notif)

    return notifications

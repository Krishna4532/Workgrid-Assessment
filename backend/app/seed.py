import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import UUID
import psycopg
from psycopg.rows import dict_row

from app.config import settings
from app.repositories.identity_repo import (
    create_user,
    create_organization,
    create_department,
    create_team,
    get_user_by_email,
    get_organization_by_slug,
    get_department_by_slug,
    get_team_by_slug,
)
from app.repositories.roles_repo import grant_or_update_role, suspend_role, revoke_role
from app.security import hash_password

logger = logging.getLogger("workgrid.seed")


def _has_existing_assignment(cur: psycopg.Cursor, user_id: UUID, scope_type: str, scope_id: UUID) -> bool:
    """Check if any assignment record already exists for the user at this scope node."""
    if scope_type == "organization":
        cur.execute("SELECT 1 FROM role_assignments WHERE user_id = %s AND organization_id = %s;", (user_id, scope_id))
    elif scope_type == "department":
        cur.execute("SELECT 1 FROM role_assignments WHERE user_id = %s AND department_id = %s;", (user_id, scope_id))
    else:
        cur.execute("SELECT 1 FROM role_assignments WHERE user_id = %s AND team_id = %s;", (user_id, scope_id))
    return cur.fetchone() is not None


def seed_demo_data(conninfo: str | None = None) -> Dict[str, int]:
    """Seed canonical demo data for local development and testing.
    
    Safety & Security Guarantees (WG-01):
    1. Refuses execution in production unless WORKGRID_ALLOW_PRODUCTION_SEED=true.
    2. Safe to re-run: NEVER overwrites or resets existing users' password hashes.
    3. Safe to re-run: NEVER silently escalates, modifies, or re-grants existing role assignments.
    4. Creates missing scope nodes and demo users idempotently.
    5. Seeds concise, coherent demo work items showcasing statuses, priorities, blockers, and overdue items.
    """
    env_clean = settings.APP_ENV.strip().lower()
    if env_clean not in ("development", "testing", "test") and not os.environ.get("WORKGRID_ALLOW_PRODUCTION_SEED"):
        raise RuntimeError(
            f"Demo seed data cannot be applied in '{settings.APP_ENV}' environment without "
            "explicit WORKGRID_ALLOW_PRODUCTION_SEED=true."
        )

    db_url = conninfo or settings.DATABASE_URL
    counts = {
        "organizations": 0,
        "departments": 0,
        "teams": 0,
        "users": 0,
        "roles": 0,
        "work_items": 0,
        "role_requests": 0,
    }

    with psycopg.connect(db_url, autocommit=False) as conn:
        with conn.transaction():
            # 1. Organization: Acme Operations
            org = get_organization_by_slug(conn, "acme-ops")
            if not org:
                org = create_organization(conn, name="Acme Operations", slug="acme-ops")
                counts["organizations"] += 1

            # 2. Departments
            dept_specs = [
                ("Engineering", "eng"),
                ("Operations", "ops"),
                ("Design", "design"),
            ]
            departments = {}
            for name, slug in dept_specs:
                dept = get_department_by_slug(conn, org.id, slug)
                if not dept:
                    dept = create_department(conn, organization_id=org.id, name=name, slug=slug)
                    counts["departments"] += 1
                departments[slug] = dept

            # 3. Teams
            team_specs = [
                (departments["eng"].id, "Platform Infrastructure", "platform"),
                (departments["eng"].id, "Core Payments", "payments"),
                (departments["ops"].id, "Field Support", "field-ops"),
                (departments["design"].id, "Product Design", "product-design"),
            ]
            teams = {}
            for dept_id, name, slug in team_specs:
                team = get_team_by_slug(conn, dept_id, slug)
                if not team:
                    team = create_team(conn, department_id=dept_id, name=name, slug=slug)
                    counts["teams"] += 1
                teams[slug] = team

            # 4. Users (Personas matching blueprint)
            default_pw_hash = hash_password("workgrid")
            user_specs = [
                ("alex.owner@example.com", "Alex Vance (Owner)"),
                ("morgan.head@example.com", "Morgan Reed (Org Head)"),
                ("dana.eng@example.com", "Dana Scully (Dept Manager)"),
                ("rahul.leader@example.com", "Rahul Patel (Tech Lead)"),
                ("sam.member@example.com", "Sam Taylor (Member)"),
                ("jordan.viewer@example.com", "Jordan Lee (Viewer)"),
                ("taylor.suspended@example.com", "Taylor Morgan (Suspended)"),
                ("casey.revoked@example.com", "Casey Quinn (Revoked)"),
            ]
            users = {}
            for email, display_name in user_specs:
                user = get_user_by_email(conn, email)
                if not user:
                    user = create_user(conn, email=email, display_name=display_name, password_hash=default_pw_hash)
                    counts["users"] += 1
                # WG-01: Do not overwrite existing user's password on rerun!
                users[email] = user

            with conn.cursor() as cur:
                # 5. Role assignments representing all role/scope combinations
                # Alex Vance: Owner at Acme Operations
                if not _has_existing_assignment(cur, users["alex.owner@example.com"].id, "organization", org.id):
                    grant_or_update_role(
                        conn,
                        user_id=users["alex.owner@example.com"].id,
                        role="owner",
                        scope_type="organization",
                        scope_id=org.id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial seed: Organization Owner",
                    )
                    counts["roles"] += 1

                # Morgan Reed: Organization Head at Acme Operations
                if not _has_existing_assignment(cur, users["morgan.head@example.com"].id, "organization", org.id):
                    grant_or_update_role(
                        conn,
                        user_id=users["morgan.head@example.com"].id,
                        role="organization_head",
                        scope_type="organization",
                        scope_id=org.id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial seed: Organization Head",
                    )
                    counts["roles"] += 1

                # Dana Scully: Department Manager at Engineering
                if not _has_existing_assignment(cur, users["dana.eng@example.com"].id, "department", departments["eng"].id):
                    grant_or_update_role(
                        conn,
                        user_id=users["dana.eng@example.com"].id,
                        role="department_manager",
                        scope_type="department",
                        scope_id=departments["eng"].id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial seed: Engineering Department Manager",
                    )
                    counts["roles"] += 1

                # Rahul Patel: Team Leader in Platform Infrastructure, Member in Field Support
                if not _has_existing_assignment(cur, users["rahul.leader@example.com"].id, "team", teams["platform"].id):
                    grant_or_update_role(
                        conn,
                        user_id=users["rahul.leader@example.com"].id,
                        role="team_leader",
                        scope_type="team",
                        scope_id=teams["platform"].id,
                        actor_id=users["dana.eng@example.com"].id,
                        reason="Initial seed: Platform Team Leader",
                    )
                    counts["roles"] += 1

                if not _has_existing_assignment(cur, users["rahul.leader@example.com"].id, "team", teams["field-ops"].id):
                    grant_or_update_role(
                        conn,
                        user_id=users["rahul.leader@example.com"].id,
                        role="member",
                        scope_type="team",
                        scope_id=teams["field-ops"].id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial seed: Field Support Member",
                    )
                    counts["roles"] += 1

                # Sam Taylor: Member in Platform Infrastructure
                if not _has_existing_assignment(cur, users["sam.member@example.com"].id, "team", teams["platform"].id):
                    grant_or_update_role(
                        conn,
                        user_id=users["sam.member@example.com"].id,
                        role="member",
                        scope_type="team",
                        scope_id=teams["platform"].id,
                        actor_id=users["rahul.leader@example.com"].id,
                        reason="Initial seed: Platform Team Member",
                    )
                    counts["roles"] += 1

                # Jordan Lee: Viewer in Design Department
                if not _has_existing_assignment(cur, users["jordan.viewer@example.com"].id, "department", departments["design"].id):
                    grant_or_update_role(
                        conn,
                        user_id=users["jordan.viewer@example.com"].id,
                        role="viewer",
                        scope_type="department",
                        scope_id=departments["design"].id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial seed: Design Department Viewer",
                    )
                    counts["roles"] += 1

                # Taylor Morgan: Suspended assignment in Core Payments
                if not _has_existing_assignment(cur, users["taylor.suspended@example.com"].id, "team", teams["payments"].id):
                    taylor_assignment = grant_or_update_role(
                        conn,
                        user_id=users["taylor.suspended@example.com"].id,
                        role="member",
                        scope_type="team",
                        scope_id=teams["payments"].id,
                        actor_id=users["dana.eng@example.com"].id,
                        reason="Initial grant before suspension",
                    )
                    suspend_role(
                        conn,
                        assignment_id=taylor_assignment.id,
                        actor_id=users["dana.eng@example.com"].id,
                        reason="Administrative suspension test",
                    )
                    counts["roles"] += 1

                # Casey Quinn: Revoked assignment in Field Support
                if not _has_existing_assignment(cur, users["casey.revoked@example.com"].id, "team", teams["field-ops"].id):
                    casey_assignment = grant_or_update_role(
                        conn,
                        user_id=users["casey.revoked@example.com"].id,
                        role="member",
                        scope_type="team",
                        scope_id=teams["field-ops"].id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Initial grant before revocation",
                    )
                    revoke_role(
                        conn,
                        assignment_id=casey_assignment.id,
                        actor_id=users["alex.owner@example.com"].id,
                        reason="Departure revocation test",
                    )
                    counts["roles"] += 1

                # 6. Canonical Demo Work Items
                # A balanced set of tasks showcasing all statuses, priorities, overdue, blockers, and unassigned work
                now = datetime.now(timezone.utc)
                demo_work_items = [
                    # Platform Infrastructure (Engineering)
                    {
                        "team_id": teams["platform"].id,
                        "kind": "engineering",
                        "title": "Upgrade Kubernetes cluster control plane to v1.30",
                        "description": "Coordinate scheduled rolling upgrade of control plane nodes and worker daemonsets across staging and production.",
                        "status": "in_progress",
                        "priority": "high",
                        "requires_approval": False,
                        "assignee_id": users["sam.member@example.com"].id,
                        "creator_id": users["rahul.leader@example.com"].id,
                        "due_date": now + timedelta(days=2),
                    },
                    {
                        "team_id": teams["platform"].id,
                        "kind": "incident",
                        "title": "Investigate database connection pool exhaustion in worker tier",
                        "description": "Spike in pool wait-time alerts observed during peak traffic. Requires pgbouncer tuning and connection lifecycle analysis.",
                        "status": "open",
                        "priority": "urgent",
                        "requires_approval": False,
                        "assignee_id": users["rahul.leader@example.com"].id,
                        "creator_id": users["rahul.leader@example.com"].id,
                        "due_date": now - timedelta(days=1),  # Overdue
                    },
                    {
                        "team_id": teams["platform"].id,
                        "kind": "task",
                        "title": "Production rollout: Distributed Redis caching tier",
                        "description": "Validation passed in staging. Requires team leader architectural sign-off before canary routing to live traffic.",
                        "status": "awaiting_approval",
                        "priority": "high",
                        "requires_approval": True,
                        "assignee_id": users["sam.member@example.com"].id,
                        "creator_id": users["sam.member@example.com"].id,
                        "due_date": now + timedelta(days=4),  # Blocked awaiting approval
                    },
                    {
                        "team_id": teams["platform"].id,
                        "kind": "task",
                        "title": "Rotate internal cluster mTLS certificates",
                        "description": "Annual rotation of mutual TLS service mesh identities. Completed ahead of expiry schedule.",
                        "status": "resolved",
                        "priority": "normal",
                        "requires_approval": False,
                        "assignee_id": users["sam.member@example.com"].id,
                        "creator_id": users["rahul.leader@example.com"].id,
                        "due_date": now - timedelta(days=3),
                    },
                    # Core Payments (Engineering)
                    {
                        "team_id": teams["payments"].id,
                        "kind": "payment",
                        "title": "Migrate payment gateway webhook listener to modern idempotency contract",
                        "description": "Implement payload hash validation and 24-hour response caching for inbound processor notifications.",
                        "status": "open",
                        "priority": "high",
                        "requires_approval": False,
                        "assignee_id": None,  # Unassigned
                        "creator_id": users["dana.eng@example.com"].id,
                        "due_date": now + timedelta(days=3),
                    },
                    {
                        "team_id": teams["payments"].id,
                        "kind": "compliance",
                        "title": "Quarterly PCI-DSS cardholder data environment audit",
                        "description": "Verify access logs, key rotation procedures, and network perimeter isolation for PCI-DSS Level 1 compliance.",
                        "status": "in_progress",
                        "priority": "urgent",
                        "requires_approval": False,
                        "assignee_id": users["dana.eng@example.com"].id,
                        "creator_id": users["dana.eng@example.com"].id,
                        "due_date": now - timedelta(days=2),  # Overdue
                    },
                    {
                        "team_id": teams["payments"].id,
                        "kind": "incident",
                        "title": "Reconcile duplicate charge anomaly in checkout retry loop",
                        "description": "Root cause identified in transient network timeout handling; mitigation deployed and customer balances refunded.",
                        "status": "closed",
                        "priority": "urgent",
                        "requires_approval": False,
                        "assignee_id": users["dana.eng@example.com"].id,
                        "creator_id": users["dana.eng@example.com"].id,
                        "due_date": now - timedelta(days=7),
                    },
                    # Field Support (Operations)
                    {
                        "team_id": teams["field-ops"].id,
                        "kind": "customer_issue",
                        "title": "Resolve enterprise client single-sign-on integration delay",
                        "description": "Assist customer IT engineering team with SAML 2.0 metadata sync and assertion consumer service bindings.",
                        "status": "in_progress",
                        "priority": "normal",
                        "requires_approval": False,
                        "assignee_id": users["rahul.leader@example.com"].id,  # Cross-team role in Field Support
                        "creator_id": users["alex.owner@example.com"].id,
                        "due_date": now + timedelta(days=2),
                    },
                    {
                        "team_id": teams["field-ops"].id,
                        "kind": "task",
                        "title": "Hardware inventory dispatch for regional deployment hubs",
                        "description": "Catalog and provision secure mobile workstations for Q4 field operations expansion.",
                        "status": "open",
                        "priority": "low",
                        "requires_approval": False,
                        "assignee_id": None,  # Unassigned
                        "creator_id": users["alex.owner@example.com"].id,
                        "due_date": now + timedelta(days=6),
                    },
                    {
                        "team_id": teams["field-ops"].id,
                        "kind": "customer_issue",
                        "title": "Customer SLA escalation: Automated data export pipeline timeout",
                        "description": "Large tenancy export query timing out after 60s. Proposed batch window requires operational review.",
                        "status": "awaiting_approval",
                        "priority": "high",
                        "requires_approval": True,
                        "assignee_id": users["rahul.leader@example.com"].id,
                        "creator_id": users["rahul.leader@example.com"].id,
                        "due_date": now + timedelta(days=1),  # Blocked awaiting approval
                    },
                    # Product Design (Design)
                    {
                        "team_id": teams["product-design"].id,
                        "kind": "task",
                        "title": "Design audit: Dark mode accessibility and color contrast compliance",
                        "description": "Comprehensive review of WCAG 2.1 AA contrast ratios across dashboard metric cards, charts, and input controls.",
                        "status": "open",
                        "priority": "normal",
                        "requires_approval": False,
                        "assignee_id": None,
                        "creator_id": users["alex.owner@example.com"].id,
                        "due_date": now + timedelta(days=5),
                    },
                    {
                        "team_id": teams["product-design"].id,
                        "kind": "task",
                        "title": "Mobile navigation drawer and tablet layout specs",
                        "description": "Complete interactive Figma design tokens and component specs for compact viewport navigation.",
                        "status": "resolved",
                        "priority": "normal",
                        "requires_approval": False,
                        "assignee_id": None,
                        "creator_id": users["alex.owner@example.com"].id,
                        "due_date": now - timedelta(days=5),
                    },
                ]

                for item_spec in demo_work_items:
                    cur.execute(
                        "SELECT id FROM work_items WHERE title = %s AND team_id = %s;",
                        (item_spec["title"], item_spec["team_id"]),
                    )
                    existing_item = cur.fetchone()
                    if not existing_item:
                        cur.execute(
                            """
                            INSERT INTO work_items (
                                team_id, kind, title, description, status, priority,
                                requires_approval, assignee_id, creator_id, due_date
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id;
                            """,
                            (
                                item_spec["team_id"],
                                item_spec["kind"],
                                item_spec["title"],
                                item_spec["description"],
                                item_spec["status"],
                                item_spec["priority"],
                                item_spec["requires_approval"],
                                item_spec["assignee_id"],
                                item_spec["creator_id"],
                                item_spec["due_date"],
                            ),
                        )
                        new_item_id = cur.fetchone()[0]
                        # Record creation activity
                        cur.execute(
                            """
                            INSERT INTO activity (
                                work_item_id, actor_id, event_type, new_status, new_assignee_id
                            ) VALUES (%s, %s, 'created', %s, %s);
                            """,
                            (
                                new_item_id,
                                item_spec["creator_id"],
                                item_spec["status"],
                                item_spec["assignee_id"],
                            ),
                        )
                        counts["work_items"] += 1

                # 7. Canonical Pending Role Request
                # Jordan Lee requests member position in Product Design
                cur.execute(
                    """
                    SELECT id FROM role_requests
                    WHERE user_id = %s AND team_id = %s AND status = 'pending';
                    """,
                    (users["jordan.viewer@example.com"].id, teams["product-design"].id),
                )
                if not cur.fetchone():
                    cur.execute(
                        """
                        INSERT INTO role_requests (
                            user_id, requested_role, scope_type, team_id, reason, status
                        ) VALUES (%s, 'member', 'team', %s, %s, 'pending');
                        """,
                        (
                            users["jordan.viewer@example.com"].id,
                            teams["product-design"].id,
                            "Requesting member position in Product Design to contribute directly to component tokens",
                        ),
                    )
                    counts["role_requests"] += 1

    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("Seeding canonical demo data into database...")
    try:
        res = seed_demo_data()
        print(f"Seeding completed successfully: {res}")
    except Exception as e:
        print(f"Error seeding demo data: {e}", file=sys.stderr)
        sys.exit(1)

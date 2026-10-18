"""
WorkGrid — Scale Load Seed Script (Phase 7: Scale & Query Timing)

Generates a realistic scale dataset:
  - 5 organizations, 25 departments, 100 teams
  - ~5,000 users with role assignments across the scope tree
  - ~50,000 work items distributed across teams with varied statuses/priorities
  - Activity records for each work item
  - Outbox events for each work item
  - Notifications for a sample of users

Safety:
  - Only runs in development or testing environments
  - Preserves all existing canonical seed data
  - Uses batch INSERTs with ON CONFLICT DO NOTHING for idempotency
  - Can be re-run without duplicating data
"""

import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("workgrid.load_seed")

# --- Configuration ---
TARGET_USERS = 5_000
TARGET_WORK_ITEMS = 50_000
NUM_EXTRA_ORGS = 4       # Plus the existing "acme-ops"
DEPTS_PER_ORG = 5
TEAMS_PER_DEPT = 4
BATCH_SIZE = 500
NOTIFICATION_SAMPLE_FRACTION = 0.1  # 10% of users get notifications

# Deterministic random for reproducibility
RNG = random.Random(42)

# Work item enum values
KINDS = ["task", "incident", "customer_issue", "payment", "engineering", "compliance"]
STATUSES = ["open", "in_progress", "resolved", "closed"]
PRIORITIES = ["low", "normal", "high", "urgent"]
TITLE_PREFIXES = [
    "Fix", "Investigate", "Deploy", "Update", "Review", "Audit",
    "Repair", "Upgrade", "Configure", "Monitor", "Resolve", "Patch",
    "Migrate", "Document", "Test", "Validate", "Refactor", "Optimize",
]
TITLE_SUBJECTS = [
    "payment gateway", "database cluster", "auth service", "API endpoint",
    "load balancer", "SSL certificate", "DNS records", "CI/CD pipeline",
    "monitoring alerts", "user permissions", "cache layer", "backup system",
    "log aggregation", "rate limiter", "webhook handler", "search index",
    "queue consumer", "health check", "firewall rules", "disk usage",
    "memory leak", "CPU throttle", "network latency", "data sync",
    "compliance report", "incident response", "customer escalation",
    "SLA breach", "vendor integration", "license renewal",
]

FIRST_NAMES = [
    "Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Hank",
    "Iris", "Jack", "Kate", "Leo", "Mia", "Noah", "Olivia", "Paul",
    "Quinn", "Rose", "Steve", "Tina", "Uma", "Vic", "Wendy", "Xander",
    "Yara", "Zach", "Aiden", "Bella", "Caleb", "Diana", "Ethan", "Fiona",
    "George", "Holly", "Ivan", "Julia", "Kurt", "Luna", "Max", "Nora",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
]

ORG_NAMES = [
    ("GlobalTech Industries", "globaltech"),
    ("Meridian Healthcare", "meridian-health"),
    ("Apex Financial", "apex-fin"),
    ("Quantum Logistics", "quantum-log"),
]

DEPT_NAMES = [
    "Engineering", "Operations", "Customer Success", "Security", "Finance",
    "Infrastructure", "Product", "Analytics", "Compliance", "DevOps",
    "Support", "Research", "Marketing", "Sales", "Legal",
    "Quality Assurance", "Data Science", "Cloud Ops", "Platform", "Mobile",
]

TEAM_NAMES = [
    "Alpha", "Beta", "Gamma", "Delta", "Echo", "Foxtrot", "Golf",
    "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "November",
    "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango",
]


def get_db_url():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        # Try to load from .env
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    if not url:
        url = "postgresql://workgrid:workgrid_dev_password@localhost:5432/workgrid"
    return url


def generate_title():
    prefix = RNG.choice(TITLE_PREFIXES)
    subject = RNG.choice(TITLE_SUBJECTS)
    return f"{prefix} {subject}"


def generate_due_date(base_time: datetime):
    """Generate a due date: 40% past, 30% within 24h, 30% future, or None."""
    roll = RNG.random()
    if roll < 0.2:
        return None  # No due date
    elif roll < 0.5:
        # Past (overdue)
        return base_time - timedelta(days=RNG.randint(1, 30), hours=RNG.randint(0, 23))
    elif roll < 0.7:
        # Due soon (within 24h)
        return base_time + timedelta(hours=RNG.randint(1, 23))
    else:
        # Future
        return base_time + timedelta(days=RNG.randint(1, 60), hours=RNG.randint(0, 23))


def run_load_seed():
    """Execute the full scale load seed."""
    db_url = get_db_url()
    logger.info(f"Connecting to database...")

    # Safety check
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    if app_env not in ("development", "testing", "test"):
        logger.error(f"Refusing to run load seed in '{app_env}' environment.")
        sys.exit(1)

    conn = psycopg.connect(db_url, autocommit=False)
    now = datetime.now(timezone.utc)

    try:
        # ========================================
        # Phase 1: Create scope hierarchy
        # ========================================
        logger.info("=== Phase 1: Building scope hierarchy ===")

        # Get existing org (acme-ops)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, slug FROM organizations;")
            existing_orgs = {r["slug"]: r["id"] for r in cur.fetchall()}

        all_orgs = {}  # slug -> id
        all_depts = {}  # (org_slug, dept_index) -> id
        all_teams = []  # list of (team_id, dept_id, org_slug)

        # Include existing org
        for slug, oid in existing_orgs.items():
            all_orgs[slug] = oid

        # Create additional organizations
        orgs_created = 0
        for org_name, org_slug in ORG_NAMES:
            if org_slug not in all_orgs:
                org_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s) ON CONFLICT (slug) DO NOTHING RETURNING id;",
                        (org_id, org_name, org_slug),
                    )
                    row = cur.fetchone()
                    if row:
                        all_orgs[org_slug] = row[0]
                        orgs_created += 1
                    else:
                        with conn.cursor(row_factory=dict_row) as cur2:
                            cur2.execute("SELECT id FROM organizations WHERE slug = %s;", (org_slug,))
                            all_orgs[org_slug] = cur2.fetchone()["id"]

        conn.commit()
        logger.info(f"  Organizations: {len(all_orgs)} total ({orgs_created} new)")

        # Create departments for each org
        depts_created = 0
        for org_slug, org_id in all_orgs.items():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT id, slug FROM departments WHERE organization_id = %s;", (org_id,))
                existing_depts = {r["slug"]: r["id"] for r in cur.fetchall()}

            for i in range(DEPTS_PER_ORG):
                dept_name = DEPT_NAMES[i % len(DEPT_NAMES)]
                dept_slug = f"{dept_name.lower().replace(' ', '-')}-{org_slug[:4]}"

                if dept_slug in existing_depts:
                    all_depts[(org_slug, i)] = existing_depts[dept_slug]
                    continue

                dept_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO departments (id, organization_id, name, slug)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (organization_id, slug) DO NOTHING RETURNING id;""",
                        (dept_id, org_id, dept_name, dept_slug),
                    )
                    row = cur.fetchone()
                    if row:
                        all_depts[(org_slug, i)] = row[0]
                        depts_created += 1
                    else:
                        with conn.cursor(row_factory=dict_row) as cur2:
                            cur2.execute(
                                "SELECT id FROM departments WHERE organization_id = %s AND slug = %s;",
                                (org_id, dept_slug),
                            )
                            all_depts[(org_slug, i)] = cur2.fetchone()["id"]

        conn.commit()
        logger.info(f"  Departments: {len(all_depts)} total ({depts_created} new)")

        # Create teams for each department
        teams_created = 0
        # Also get existing teams
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, department_id, slug FROM teams;")
            existing_teams_map = {}
            for r in cur.fetchall():
                existing_teams_map[(r["department_id"], r["slug"])] = r["id"]

        for (org_slug, dept_idx), dept_id in all_depts.items():
            for j in range(TEAMS_PER_DEPT):
                team_name = TEAM_NAMES[j % len(TEAM_NAMES)]
                team_slug = f"{team_name.lower()}-{dept_idx}-{j}"

                if (dept_id, team_slug) in existing_teams_map:
                    all_teams.append((existing_teams_map[(dept_id, team_slug)], dept_id, org_slug))
                    continue

                team_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO teams (id, department_id, name, slug)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (department_id, slug) DO NOTHING RETURNING id;""",
                        (team_id, dept_id, team_name, team_slug),
                    )
                    row = cur.fetchone()
                    if row:
                        all_teams.append((row[0], dept_id, org_slug))
                        teams_created += 1
                    else:
                        with conn.cursor(row_factory=dict_row) as cur2:
                            cur2.execute(
                                "SELECT id FROM teams WHERE department_id = %s AND slug = %s;",
                                (dept_id, team_slug),
                            )
                            all_teams.append((cur2.fetchone()["id"], dept_id, org_slug))

        conn.commit()

        # Also include any existing teams not captured
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT t.id, t.department_id, d.organization_id, o.slug AS org_slug
                FROM teams t
                JOIN departments d ON t.department_id = d.id
                JOIN organizations o ON d.organization_id = o.id;
            """)
            existing_full_teams = cur.fetchall()
            existing_team_ids = {t[0] for t in all_teams}
            for r in existing_full_teams:
                if r["id"] not in existing_team_ids:
                    all_teams.append((r["id"], r["department_id"], r["org_slug"]))

        logger.info(f"  Teams: {len(all_teams)} total ({teams_created} new)")

        # ========================================
        # Phase 2: Create users
        # ========================================
        logger.info("=== Phase 2: Creating users ===")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users;")
            existing_user_count = cur.fetchone()[0]

        users_to_create = max(0, TARGET_USERS - existing_user_count)
        logger.info(f"  Existing users: {existing_user_count}, creating {users_to_create} new users")

        # Pre-generate a dummy password hash (bcrypt placeholder — not real bcrypt)
        # For load testing we use a fixed hash to avoid slow bcrypt computation
        dummy_hash = "$2b$12$LJ3m4ys2Kx8QfV5Dq5D9X.DUMMY_HASH_FOR_LOAD_TESTING_ONLY"

        new_user_ids = []
        batch = []
        for i in range(users_to_create):
            uid = uuid4()
            first = RNG.choice(FIRST_NAMES)
            last = RNG.choice(LAST_NAMES)
            email = f"load.{first.lower()}.{last.lower()}.{i}@example.com"
            display_name = f"{first} {last}"

            batch.append((uid, email.lower(), display_name, "active", dummy_hash))
            new_user_ids.append(uid)

            if len(batch) >= BATCH_SIZE:
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO users (id, email, display_name, status, password_hash)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING;""",
                        batch,
                    )
                conn.commit()
                batch = []
                if (i + 1) % 1000 == 0:
                    logger.info(f"    Created {i + 1}/{users_to_create} users...")

        if batch:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO users (id, email, display_name, status, password_hash)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING;""",
                    batch,
                )
            conn.commit()

        # Get all user IDs
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE status = 'active';")
            all_user_ids = [r[0] for r in cur.fetchall()]

        logger.info(f"  Total active users: {len(all_user_ids)}")

        # ========================================
        # Phase 3: Create role assignments
        # ========================================
        logger.info("=== Phase 3: Creating role assignments ===")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM role_assignments;")
            existing_assignments = cur.fetchone()[0]

        # Distribute users across teams with roles
        # Strategy: each user gets 1-3 team assignments
        team_ids_list = [t[0] for t in all_teams]
        org_slugs_for_teams = {t[0]: t[2] for t in all_teams}

        # Build org_id lookup
        org_id_by_slug = all_orgs

        # Build dept_id -> org_id lookup
        dept_to_org = {}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, organization_id FROM departments;")
            for r in cur.fetchall():
                dept_to_org[r["id"]] = r["organization_id"]

        # Build team_id -> dept_id lookup
        team_to_dept = {t[0]: t[1] for t in all_teams}

        assignments_created = 0
        assignment_batch = []

        # First assign some org-level and dept-level roles for important users
        # Pick some users as org owners/heads
        org_leaders = {}
        for org_slug, org_id in all_orgs.items():
            if assignments_created >= len(all_user_ids):
                break
            leader_idx = assignments_created % len(all_user_ids)

            # Owner
            uid_owner = all_user_ids[leader_idx % len(all_user_ids)]
            assignment_batch.append(
                (uuid4(), uid_owner, "owner", "organization", org_id, None, None, "active", uid_owner)
            )
            org_leaders[org_slug] = uid_owner
            assignments_created += 1

            # Org head
            uid_head = all_user_ids[(leader_idx + 1) % len(all_user_ids)]
            assignment_batch.append(
                (uuid4(), uid_head, "organization_head", "organization", org_id, None, None, "active", uid_owner)
            )
            assignments_created += 1

        # Dept managers
        for (org_slug, dept_idx), dept_id in all_depts.items():
            uid = all_user_ids[(assignments_created + 10) % len(all_user_ids)]
            org_id = org_id_by_slug.get(org_slug)
            granted_by = org_leaders.get(org_slug, uid)
            assignment_batch.append(
                (uuid4(), uid, "department_manager", "department", None, dept_id, None, "active", granted_by)
            )
            assignments_created += 1

        # Flush org/dept assignments
        if assignment_batch:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO role_assignments (id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING;""",
                    assignment_batch,
                )
            conn.commit()
            assignment_batch = []

        # Now distribute remaining users as team members/leaders
        team_roles = ["member", "member", "member", "member", "team_leader"]  # 80% members, 20% leaders

        # Each user gets 1-3 team assignments
        user_team_assignments = 0
        batch = []
        for uid in all_user_ids:
            num_teams = RNG.randint(1, 3)
            chosen_teams = RNG.sample(team_ids_list, min(num_teams, len(team_ids_list)))

            for team_id in chosen_teams:
                role = RNG.choice(team_roles)
                granted_by = uid  # Self for simplicity
                batch.append(
                    (uuid4(), uid, role, "team", None, None, team_id, "active", granted_by)
                )
                user_team_assignments += 1

                if len(batch) >= BATCH_SIZE:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """INSERT INTO role_assignments (id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT DO NOTHING;""",
                            batch,
                        )
                    conn.commit()
                    batch = []

            if user_team_assignments % 2000 == 0 and user_team_assignments > 0:
                logger.info(f"    Created {user_team_assignments} team assignments...")

        if batch:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO role_assignments (id, user_id, role, scope_type, organization_id, department_id, team_id, status, granted_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING;""",
                    batch,
                )
            conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM role_assignments;")
            total_assignments = cur.fetchone()[0]

        logger.info(f"  Total role assignments: {total_assignments} (was {existing_assignments})")

        # ========================================
        # Phase 4: Create work items
        # ========================================
        logger.info("=== Phase 4: Creating work items ===")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items;")
            existing_items = cur.fetchone()[0]

        items_to_create = max(0, TARGET_WORK_ITEMS - existing_items)
        logger.info(f"  Existing work items: {existing_items}, creating {items_to_create} new items")

        items_created = 0
        item_batch = []
        activity_batch = []
        outbox_batch = []

        base_time = now - timedelta(days=90)  # Spread items over 90 days

        for i in range(items_to_create):
            item_id = uuid4()
            team_id = RNG.choice(team_ids_list)
            creator_id = RNG.choice(all_user_ids)
            kind = RNG.choice(KINDS)
            title = generate_title()
            description = f"Automated load test item #{i + 1}. {title} for scale verification."
            status = RNG.choice(STATUSES)
            priority = RNG.choice(PRIORITIES)
            requires_approval = RNG.random() < 0.1  # 10% require approval

            # Assignee logic: open items have no assignee, others might
            assignee_id = None
            if status in ("in_progress", "resolved", "closed"):
                assignee_id = RNG.choice(all_user_ids)
            elif status == "open" and RNG.random() < 0.1:
                pass  # some open items have no assignee

            due_date = generate_due_date(now)
            version = 1

            # Spread creation times across 90 days
            created_at = base_time + timedelta(
                seconds=RNG.randint(0, 90 * 24 * 3600)
            )
            updated_at = created_at + timedelta(
                seconds=RNG.randint(0, max(1, int((now - created_at).total_seconds())))
            )

            item_batch.append((
                item_id, team_id, kind, title, description, status, priority,
                requires_approval, assignee_id, creator_id, due_date,
                version, created_at, updated_at,
            ))

            # Activity record for creation
            activity_batch.append((
                uuid4(), item_id, creator_id, "created", None, status, None, assignee_id, created_at,
            ))

            # Outbox event
            import json
            outbox_batch.append((
                uuid4(), "work_item_created",
                json.dumps({
                    "item_id": str(item_id),
                    "team_id": str(team_id),
                    "title": title,
                    "kind": kind,
                    "status": status,
                    "priority": priority,
                    "creator_id": str(creator_id),
                }),
                "completed", 1, created_at, created_at, created_at,
            ))

            items_created += 1

            if len(item_batch) >= BATCH_SIZE:
                _flush_items(conn, item_batch, activity_batch, outbox_batch)
                item_batch = []
                activity_batch = []
                outbox_batch = []
                if items_created % 5000 == 0:
                    logger.info(f"    Created {items_created}/{items_to_create} work items...")

        if item_batch:
            _flush_items(conn, item_batch, activity_batch, outbox_batch)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items;")
            total_items = cur.fetchone()[0]

        logger.info(f"  Total work items: {total_items}")

        # ========================================
        # Phase 5: Create notifications for sample users
        # ========================================
        logger.info("=== Phase 5: Creating notifications ===")

        sample_user_count = int(len(all_user_ids) * NOTIFICATION_SAMPLE_FRACTION)
        sample_users = RNG.sample(all_user_ids, min(sample_user_count, len(all_user_ids)))

        notif_batch = []
        notifs_created = 0

        for uid in sample_users:
            # Each sampled user gets 5-20 notifications
            num_notifs = RNG.randint(5, 20)
            for j in range(num_notifs):
                notif_id = uuid4()
                dedupe_key = f"load-{uid}-{j}"
                notif_type = RNG.choice([
                    "work_item_created", "work_item_claimed", "work_item_status_changed",
                    "role_request_submitted", "role_request_approved",
                ])
                title = f"Load test notification #{j + 1}"
                message = f"This is a load-test notification for scale verification."
                read_at = now - timedelta(hours=RNG.randint(0, 72)) if RNG.random() < 0.6 else None
                created_at = now - timedelta(hours=RNG.randint(0, 168))

                notif_batch.append((
                    notif_id, uid, dedupe_key, notif_type, title, message, read_at, created_at,
                ))

                if len(notif_batch) >= BATCH_SIZE:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """INSERT INTO notifications (id, user_id, dedupe_key, notification_type, title, message, read_at, created_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (dedupe_key) DO NOTHING;""",
                            notif_batch,
                        )
                    conn.commit()
                    notifs_created += len(notif_batch)
                    notif_batch = []

        if notif_batch:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO notifications (id, user_id, dedupe_key, notification_type, title, message, read_at, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (dedupe_key) DO NOTHING;""",
                    notif_batch,
                )
            conn.commit()
            notifs_created += len(notif_batch)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM notifications;")
            total_notifs = cur.fetchone()[0]

        logger.info(f"  Total notifications: {total_notifs}")

        # ========================================
        # Phase 6: Update table statistics
        # ========================================
        logger.info("=== Phase 6: Updating table statistics ===")
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in ["users", "organizations", "departments", "teams",
                          "role_assignments", "work_items", "activity", "outbox",
                          "notifications", "role_assignment_audit"]:
                cur.execute(f"ANALYZE {table};")
                logger.info(f"    ANALYZE {table} — done")
        conn.autocommit = False

        # ========================================
        # Final summary
        # ========================================
        logger.info("=== Load Seed Complete ===")
        with conn.cursor(row_factory=dict_row) as cur:
            summary = {}
            for table in ["users", "organizations", "departments", "teams",
                          "role_assignments", "work_items", "activity",
                          "outbox", "notifications"]:
                cur.execute(f"SELECT count(*) AS cnt FROM {table};")
                summary[table] = cur.fetchone()["cnt"]
            for table, count in summary.items():
                logger.info(f"    {table}: {count:,} rows")

    finally:
        conn.close()


def _flush_items(conn, item_batch, activity_batch, outbox_batch):
    """Flush batches of work items, activity, and outbox records."""
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO work_items (id, team_id, kind, title, description, status, priority,
                   requires_approval, assignee_id, creator_id, due_date, version, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING;""",
            item_batch,
        )
        cur.executemany(
            """INSERT INTO activity (id, work_item_id, actor_id, event_type, previous_status,
                   new_status, previous_assignee_id, new_assignee_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING;""",
            activity_batch,
        )
        cur.executemany(
            """INSERT INTO outbox (id, event_type, payload, status, attempts, created_at, updated_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING;""",
            outbox_batch,
        )
    conn.commit()


if __name__ == "__main__":
    t0 = time.time()
    run_load_seed()
    elapsed = time.time() - t0
    logger.info(f"Total elapsed: {elapsed:.1f}s")

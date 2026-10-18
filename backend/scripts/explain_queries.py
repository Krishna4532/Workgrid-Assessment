"""
WorkGrid — EXPLAIN ANALYZE Script (Phase 7: Scale & Query Timing)

Runs EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) on all critical application queries
against the loaded scale dataset. Records execution times and query plans.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


def get_db_url():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
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


def run_explain(cur, name, sql, params=None):
    """Run EXPLAIN (ANALYZE, BUFFERS) and return the plan + timing."""
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"
    t0 = time.perf_counter()
    cur.execute(explain_sql, params)
    elapsed = (time.perf_counter() - t0) * 1000  # ms
    plan_lines = [r[0] for r in cur.fetchall()]
    plan = "\n".join(plan_lines)

    # Extract execution time from the plan itself
    exec_time = None
    for line in plan_lines:
        if "Execution Time:" in line:
            parts = line.split("Execution Time:")[1].strip()
            exec_time = parts.split(" ")[0]
            break

    print(f"\n{'=' * 80}")
    print(f"QUERY: {name}")
    print(f"  Wall clock: {elapsed:.2f} ms")
    if exec_time:
        print(f"  PG Execution Time: {exec_time} ms")
    print(f"{'=' * 80}")
    print(plan)

    return {
        "name": name,
        "wall_ms": round(elapsed, 2),
        "pg_exec_ms": float(exec_time) if exec_time else None,
        "plan": plan,
        "sub_second": elapsed < 1000,
    }


def main():
    db_url = get_db_url()
    conn = psycopg.connect(db_url, autocommit=True)
    results = []

    with conn.cursor() as cur:
        # -------------------------------------------------------------------
        # Get representative data for parameterized queries
        # -------------------------------------------------------------------
        cur.execute("SELECT id FROM users WHERE status = 'active' LIMIT 1;")
        sample_user_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM teams LIMIT 1;")
        sample_team_id = cur.fetchone()[0]

        # Get a user with effective roles for realistic queries
        cur.execute("SELECT user_id FROM effective_roles LIMIT 1;")
        er_row = cur.fetchone()
        er_user_id = er_row[0] if er_row else sample_user_id

        # Get team_ids for the effective roles user
        cur.execute("SELECT team_id FROM effective_roles WHERE user_id = %s;", (er_user_id,))
        user_team_ids = [r[0] for r in cur.fetchall()]
        if not user_team_ids:
            user_team_ids = [sample_team_id]

        # Get a sample work item
        cur.execute("SELECT id FROM work_items LIMIT 1;")
        sample_item_id = cur.fetchone()[0]

        # -------------------------------------------------------------------
        # Q1: Work item list with keyset pagination (primary dashboard query)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q1: Work items list — keyset pagination (first page, 20 items)",
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.team_id = ANY(%s)
            ORDER BY w.updated_at DESC, w.id DESC
            LIMIT 21;
            """,
            (user_team_ids,),
        ))

        # -------------------------------------------------------------------
        # Q2: Work items with status filter
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q2: Work items — status filter (open items only)",
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.team_id = ANY(%s)
              AND w.status = %s
            ORDER BY w.updated_at DESC, w.id DESC
            LIMIT 21;
            """,
            (user_team_ids, "open"),
        ))

        # -------------------------------------------------------------------
        # Q3: Work items — full-text search
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q3: Work items — full-text search (tsvector)",
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.team_id = ANY(%s)
              AND w.search_tsv @@ plainto_tsquery('english', %s)
            ORDER BY w.updated_at DESC, w.id DESC
            LIMIT 21;
            """,
            (user_team_ids, "payment gateway"),
        ))

        # -------------------------------------------------------------------
        # Q4: My assigned items (assignee filter)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q4: Work items — my assigned items",
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.team_id = ANY(%s)
              AND w.assignee_id = %s
            ORDER BY w.updated_at DESC, w.id DESC
            LIMIT 21;
            """,
            (user_team_ids, er_user_id),
        ))

        # -------------------------------------------------------------------
        # Q5: Single work item by ID
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q5: Single work item by ID (primary key lookup)",
            """
            SELECT w.id, w.team_id, w.kind, w.title, w.description, w.status, w.priority,
                   w.requires_approval, w.assignee_id, w.creator_id, w.due_date,
                   w.version, w.created_at, w.updated_at,
                   u_creator.display_name AS creator_name,
                   u_assignee.display_name AS assignee_name
            FROM work_items w
            JOIN users u_creator ON w.creator_id = u_creator.id
            LEFT JOIN users u_assignee ON w.assignee_id = u_assignee.id
            WHERE w.id = %s;
            """,
            (sample_item_id,),
        ))

        # -------------------------------------------------------------------
        # Q6: Effective roles view (scope resolution)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q6: Effective roles — user's team coverage",
            """
            SELECT team_id
            FROM effective_roles
            WHERE user_id = %s;
            """,
            (er_user_id,),
        ))

        # -------------------------------------------------------------------
        # Q7: Dashboard summary counts (status aggregation)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q7: Dashboard — status/priority counts across teams",
            """
            SELECT
                count(*) FILTER (WHERE status NOT IN ('resolved', 'closed')) AS team_open,
                count(*) FILTER (WHERE due_date IS NOT NULL AND due_date < NOW() AND status NOT IN ('resolved', 'closed')) AS team_overdue,
                count(*) FILTER (WHERE assignee_id = %(user_id)s AND status NOT IN ('resolved', 'closed')) AS my_open,
                count(*) FILTER (WHERE assignee_id = %(user_id)s AND due_date IS NOT NULL AND due_date < NOW() AND status NOT IN ('resolved', 'closed')) AS my_overdue
            FROM work_items
            WHERE team_id = ANY(%(team_ids)s);
            """,
            {"user_id": er_user_id, "team_ids": user_team_ids},
        ))

        # -------------------------------------------------------------------
        # Q8: Todo — assigned work items query
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q8: Unified todo — assigned work items",
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
            (user_team_ids, er_user_id),
        ))

        # -------------------------------------------------------------------
        # Q9: Activity timeline for a work item
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q9: Activity timeline — single work item history",
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
            (sample_item_id,),
        ))

        # -------------------------------------------------------------------
        # Q10: Notifications — user feed with keyset pagination
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q10: Notifications — user feed (paginated)",
            """
            SELECT id, user_id, event_id, dedupe_key, notification_type, title, message,
                   entity_type, entity_id, read_at, created_at
            FROM notifications
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 21;
            """,
            (sample_user_id,),
        ))

        # -------------------------------------------------------------------
        # Q11: Notifications — unread count
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q11: Notifications — unread count",
            """
            SELECT count(*)
            FROM notifications
            WHERE user_id = %s AND read_at IS NULL;
            """,
            (sample_user_id,),
        ))

        # -------------------------------------------------------------------
        # Q12: Team members via effective_roles
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q12: Team members — effective_roles join users",
            """
            SELECT u.id AS user_id, u.display_name, u.email, u.status,
                   er.role, er.role_rank, er.scope_type, er.assignment_id
            FROM effective_roles er
            JOIN users u ON u.id = er.user_id
            WHERE er.team_id = %s AND u.status = 'active'
            ORDER BY er.role_rank DESC, u.display_name ASC;
            """,
            (sample_team_id,),
        ))

        # -------------------------------------------------------------------
        # Q13: User login — email lookup
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q13: Auth — email login lookup",
            """
            SELECT id, email, display_name, status, password_hash, created_at, updated_at
            FROM users
            WHERE LOWER(email) = LOWER(%s);
            """,
            ("alex.owner@example.com",),
        ))

        # -------------------------------------------------------------------
        # Q14: Active positions (user's role assignments)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q14: Active positions — user role assignments",
            """
            SELECT ra.id, ra.user_id, ra.role, ra.scope_type, ra.status,
                   ra.organization_id, ra.department_id, ra.team_id,
                   ra.created_at, ra.updated_at,
                   o.name AS organization_name,
                   d.name AS department_name,
                   t.name AS team_name
            FROM role_assignments ra
            LEFT JOIN organizations o ON ra.organization_id = o.id
            LEFT JOIN departments d ON ra.department_id = d.id
            LEFT JOIN teams t ON ra.team_id = t.id
            WHERE ra.user_id = %s AND ra.status = 'active'
            ORDER BY ra.created_at DESC;
            """,
            (er_user_id,),
        ))

        # -------------------------------------------------------------------
        # Q15: Outbox claiming (worker queue)
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q15: Outbox — worker claim query",
            """
            SELECT id, event_type, payload, status, attempts, max_attempts,
                   next_attempt_at, locked_at, locked_by, lease_token, last_error,
                   created_at, updated_at, completed_at
            FROM outbox
            WHERE status IN ('pending', 'processing')
              AND next_attempt_at <= NOW()
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT 10;
            """,
        ))

        # -------------------------------------------------------------------
        # Q16: Overdue items count
        # -------------------------------------------------------------------
        results.append(run_explain(cur, "Q16: Overdue items count across teams",
            """
            SELECT count(*) AS cnt
            FROM work_items
            WHERE team_id = ANY(%s)
              AND due_date IS NOT NULL
              AND due_date < NOW()
              AND status NOT IN ('resolved', 'closed');
            """,
            (user_team_ids,),
        ))

    conn.close()

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    print(f"\n\n{'=' * 80}")
    print("QUERY PERFORMANCE SUMMARY")
    print(f"{'=' * 80}")
    print(f"{'Query':<60} {'Wall (ms)':>10} {'PG (ms)':>10} {'Pass':>6}")
    print(f"{'-' * 60} {'-' * 10} {'-' * 10} {'-' * 6}")

    all_pass = True
    for r in results:
        pg_ms = f"{r['pg_exec_ms']:.2f}" if r['pg_exec_ms'] is not None else "N/A"
        status = "PASS" if r['sub_second'] else "FAIL"
        if not r['sub_second']:
            all_pass = False
        print(f"{r['name']:<60} {r['wall_ms']:>10.2f} {pg_ms:>10} {status:>6}")

    print(f"\n{'=' * 80}")
    if all_pass:
        print("RESULT: ALL QUERIES SUB-SECOND -- PASS")
    else:
        slow = [r for r in results if not r['sub_second']]
        print(f"RESULT: {len(slow)} QUERIES EXCEEDED 1 SECOND -- FAIL")
    print(f"{'=' * 80}")

    # Save results as JSON
    results_path = os.path.join(os.path.dirname(__file__), "..", "explain_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nDetailed results saved to: {os.path.abspath(results_path)}")

    return all_pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

# WorkGrid

> **Operational Work Coordination Platform**  
> WorkGrid replaces unstructured chat threads, spreadsheets, and emails for coordinating operational work across cross-functional teams. Teams manage incidents, investigations, compliance tasks, and operational requests with full audit history, strict role hierarchy, and atomic concurrency controls.

---

## 1. Project Status & Phase 3 Scope

This repository contains **Phase 0 (Project Foundation)**, **Phase 1 (Database and Identity)**, **Phase 2 (Authentication, Authorization, Work Items & Approvals)**, and **Phase 3 (Background Worker & Notifications)** of WorkGrid.

- **Current State**: Monorepo with PostgreSQL 16, bounded connection pooling via `psycopg 3` and `psycopg_pool`, FastAPI REST API with JWT authentication, React + TypeScript + Vite + TanStack Query dashboard, transactional outbox worker, and in-app notification system.
- **Phase 3 Deliverables Active**:
  - Transactional outbox table (`outbox`) committed in the same PostgreSQL transaction as business mutations (work items, approvals, role lifecycle).
  - Standalone background worker daemon (`python -m app.worker`) claiming events via `SELECT ... FOR UPDATE SKIP LOCKED` with lease fencing tokens, bounded connection pool, and exponential backoff retries.
  - Dead-letter state after 5 failed attempts, with administrative resurrection via `retry_dead_letter_event`.
  - In-app notifications table with database-enforced deduplication key (`ON CONFLICT (dedupe_key) DO NOTHING`).
  - Authenticated notifications REST API: `GET /notifications`, `GET /notifications/unread-count`, `POST /notifications/{id}/read`, `POST /notifications/read-all` with cursor pagination and strict user isolation.
  - Frontend notification bell icon with unread badge (polled every 15 seconds), dropdown drawer with mark-read and mark-all-read actions.
  - PostgreSQL-backed automated tests; run `pytest -q` from `backend/` for the current count.
- **Demo Disclosure**: In accordance with the WorkGrid Constitution, demo organizations, departments, teams, and users are invented for development and testing.

---

## 2. Technology Stack

- **Backend**: Python 3.12+ (Python 3.11+ supported), FastAPI, Pydantic v2, `pydantic-settings`.
- **Database**: PostgreSQL 16 (or local PostgreSQL 16/17 service).
- **Database Driver**: `psycopg 3` (`psycopg[binary]`) and `psycopg-pool` with hand-written parameterized SQL.
- **Frontend**: React 18, TypeScript (strict mode), Vite, TanStack Query (`@tanstack/react-query`).
- **Testing**: `pytest`, `httpx` / FastAPI `TestClient`, PostgreSQL test isolation.
- **Containerization**: Docker Compose for PostgreSQL.

---

## 3. Repository Structure

```text
workgrid/
├── README.md                      # Authoritative guide and setup instructions
├── .gitignore                     # Git ignore rules for Python, Node, Vite, env
├── docker-compose.yml             # PostgreSQL 16 container definition with healthcheck
├── ENGINEERING_DECISIONS.md       # Core architectural decisions from Constitution §15 + Phase additions
├── LIMITATIONS.md                 # Day-one boundaries, non-goals, and verified limitations
├── backend/                       # FastAPI application & test suite
│   ├── .env.example               # Template environment configuration
│   ├── requirements.txt           # Python dependencies with major version ranges
│   ├── pytest.ini                 # Pytest configuration
│   ├── migrations/                # Versioned SQL migrations
│   │   ├── 001_identity_and_scopes.sql  # Core schema, constraints, audit trigger, effective_roles view
│   │   ├── 002_auth_workitems_approvals.sql # Auth, work items, approvals, idempotency
│   │   ├── 003_outbox_and_notifications.sql # Transactional outbox and in-app notifications
│   │   └── 004_work_item_update_activity.sql # Append-only history for editable work-item fields
│   ├── app/
│   │   ├── __init__.py            # Application package & version
│   │   ├── config.py              # Centralized Pydantic settings & security warnings
│   │   ├── db.py                  # psycopg_pool connection pool lifecycle & health check
│   │   ├── migrations.py          # Versioned migration runner with schema_migrations tracking
│   │   ├── models.py              # Pydantic domain models & response schemas
│   │   ├── auth.py                # JWT authentication dependency
│   │   ├── seed.py                # Canonical demo seed data generator
│   │   ├── main.py                # FastAPI app, CORS, routers, lifecycle events
│   │   ├── worker.py              # Outbox background worker daemon (Phase 3)
│   │   ├── notifications/
│   │   │   └── dispatcher.py      # Event-to-notification mapping & recipient scoping
│   │   ├── routers/
│   │   │   ├── work_items_router.py  # Work item CRUD, claim, transition, activity, comments
│   │   │   ├── approvals_router.py   # Role requests, approval inbox, approve/reject
│   │   │   └── notifications_router.py # Notification list, unread count, mark read
│   │   └── repositories/
│   │       ├── identity_repo.py       # Users, organizations, departments, teams CRUD
│   │       ├── roles_repo.py          # Atomic role lifecycle, audit logging, effective roles
│   │       ├── work_items_repo.py     # Work item CRUD with outbox integration
│   │       ├── approvals_repo.py      # Role request lifecycle with outbox integration
│   │       ├── idempotency.py          # Idempotency key storage and request hashing
│   │       ├── outbox_repo.py         # Outbox claim, complete, fail, dead-letter (Phase 3)
│   │       └── notifications_repo.py  # Notification CRUD with dedupe_key (Phase 3)
│   └── tests/
│       ├── test_health.py             # Phase 0 process and database health tests
│       ├── test_schema_and_migrations.py # Schema constraints, slugs, scope FKs
│       ├── test_role_lifecycle_and_audit.py # Grant/suspend/revoke lifecycle & audit
│       ├── test_effective_roles.py    # Scope inheritance, rank precedence
│       ├── test_positions_api.py      # /me/positions authentication, isolation
│       ├── test_auth.py               # JWT login, token validation, suspension
│       ├── test_work_items.py         # Work item CRUD, claim, pagination, activity
│       ├── test_idempotency.py        # Idempotency key replay and mismatch
│       ├── test_approvals.py          # Role request lifecycle and approval routing
│       ├── test_outbox.py             # Transactional outbox co-commit and rollback (Phase 3)
│       ├── test_notifications.py      # Notification isolation, pagination, mark-read (Phase 3)
│       └── test_worker.py            # Worker claim, lease fencing, backoff, dead-letter (Phase 3)
└── frontend/                      # React TypeScript client
    ├── package.json               # Dependencies & scripts
    ├── tsconfig.json              # TypeScript strict configuration
    ├── tsconfig.node.json         # TypeScript bundler configuration
    ├── vite.config.ts             # Vite development & build configuration
    ├── index.html                 # Semantic HTML shell with SEO meta & fonts
    └── src/
        ├── vite-env.d.ts          # Vite client environment declarations
        ├── main.tsx               # TanStack Query client & React entrypoint
        ├── App.tsx                # Operations dashboard with notifications drawer
        └── styles.css             # Operations design system & styling
```

---

## 4. Prerequisites

Ensure the following tools are installed on your host machine:

- **Python**: 3.11 or 3.12+
- **Node.js**: v18+ (tested on v24) and `npm`
- **PostgreSQL**: Docker Desktop (or an existing local PostgreSQL 16/17 service)

---

## 5. Quick Start Guide

### Step 1: Start PostgreSQL

#### Option A: Using Docker Compose (Recommended)

```bash
# Start PostgreSQL container in background
docker compose up -d

# Check container status and health
docker compose ps

# Inspect logs
docker compose logs postgres

# Stop the database container
docker compose down

# Reset database (WARNING: destroys all persistent volume data)
docker compose down -v
```

#### Option B: Using an Existing Local PostgreSQL Service

```sql
CREATE ROLE workgrid WITH LOGIN PASSWORD 'workgrid_dev_password' SUPERUSER CREATEDB;
CREATE DATABASE workgrid OWNER workgrid;
```

---

### Step 2: Database Migrations & Demo Seed Data

With PostgreSQL running, apply database migrations and optionally seed canonical development data:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1

# Apply pending schema migrations
python -m app.migrations

# Seed canonical development personas and scope hierarchy
python -m app.seed
```

---

### Step 3: Backend Setup & Startup

#### Windows (PowerShell)

```powershell
cd backend

# Create Python virtual environment (if not already done)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Start FastAPI development server
uvicorn app.main:app --reload --port 8000
```

#### macOS / Linux (Bash)

```bash
cd backend

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

python3 -m app.migrations
python3 -m app.seed

uvicorn app.main:app --reload --port 8000
```

The interactive OpenAPI documentation will be accessible at:  
👉 `http://localhost:8000/docs`

---

### Step 4: Frontend Setup & Startup

Open a separate terminal window:

```bash
cd frontend

# Install npm dependencies
npm install

# Start Vite development server
npm run dev
```

The WorkGrid dashboard will be accessible at:  
👉 `http://localhost:5173`

---

### Step 5: Background Worker (Phase 3)

The outbox worker processes transactional outbox events and generates in-app notifications. Start it in a separate terminal:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1

# Start the background worker daemon
python -m app.worker
```

The worker will:
- Poll the `outbox` table for pending events
- Claim events via `SELECT ... FOR UPDATE SKIP LOCKED` with lease fencing
- Dispatch notifications with database-level deduplication
- Retry failed events with exponential backoff (default base: 2s, max: 60s)
- Transition events to `dead_letter` after 5 failed attempts
- Shut down gracefully on `SIGINT` / `SIGTERM`

---

## 6. Verification & Testing

### Development Database

Normal API and worker startup use `backend/.env` (or the equivalent environment variables). The checked-in example and current local configuration target the development database `workgrid` on `localhost:5432`, using the `workgrid` database role. Do not point test setup at this database.

### Dedicated PostgreSQL Test Database

The integration suite refuses to collect tests unless it is explicitly configured for the dedicated `workgrid_test` database and the separate non-superuser role `workgrid_test`. It verifies the connected database name, connected role, database owner, and superuser status before importing the test modules. The test role must not be the development role. The suite creates test records and its migration-concurrency test creates and removes only a uniquely named test table and migration record in this dedicated database.

If the dedicated database does not exist, create it manually using a PostgreSQL administrative account only after confirming the name is unused and that the database is intended to be disposable. Do not run `DROP DATABASE`, `TRUNCATE`, or reset commands against `workgrid`.

```sql
-- Run in psql connected to the PostgreSQL maintenance database.
SELECT datname FROM pg_database WHERE datname = 'workgrid_test';
SELECT rolname FROM pg_roles WHERE rolname = 'workgrid_test';
```

Only if both queries return no rows, create the dedicated role and database:

```sql
-- Choose a unique password; do not commit or share it.
CREATE ROLE workgrid_test LOGIN PASSWORD '<choose-a-unique-password>';
CREATE DATABASE workgrid_test OWNER workgrid_test;
```

After creating the database, connect to it as `workgrid_test` and add the explicit safety marker:

```sql
CREATE TABLE workgrid_test_safety (purpose TEXT PRIMARY KEY CHECK (purpose = 'disposable'));
INSERT INTO workgrid_test_safety (purpose) VALUES ('disposable');
```

Configure the test process in a PowerShell session. Replace the password placeholder locally and do not save it in source control:

```powershell
$env:WORKGRID_TEST_DATABASE_URL = "postgresql://workgrid_test:<choose-a-unique-password>@localhost:5432/workgrid_test"
$env:WORKGRID_TEST_DATABASE_NAME = "workgrid_test"
$env:WORKGRID_TEST_DATABASE_USER = "workgrid_test"
$env:WORKGRID_TEST_DATABASE_CONFIRM = "I_HAVE_VERIFIED_THIS_IS_DISPOSABLE"
$env:APP_ENV = "testing"
$env:DATABASE_URL = $env:WORKGRID_TEST_DATABASE_URL
Set-Location backend
.\.venv\Scripts\python.exe -m app.migrations
.\.venv\Scripts\python.exe -m pytest -q
```

The explicit confirmation variable is an operator attestation, not a replacement for checking the PostgreSQL target. The test guard additionally checks the actual connected identity. The test environment variables are process-local; start the normal API and worker in separate terminals with development configuration so they continue to use `workgrid`.

### Running Backend Automated Tests

Run the commands above only against the dedicated disposable test database. The PostgreSQL-backed test suite validates:
1. `GET /health` returns HTTP 200 with process health and metadata.
2. The health response strictly omits secrets, connection strings, or credentials.
3. The application modules can be imported and routes inspected without requiring an active PostgreSQL connection.
4. `GET /health/db` succeeds when PostgreSQL is reachable.
5. `GET /health/db` returns a controlled HTTP 503 error without credential leakage when the database is offline.
6. Schema migrations track applied scripts in `schema_migrations` and run idempotently.
7. Slugs enforce uniqueness on organizations, departments within org, and teams within dept.
8. Database check constraints reject invalid role/scope pairings directly in PostgreSQL.
9. Referential integrity guarantees role assignments cannot point to nonexistent scope nodes.
10. Role assignment grants, suspensions, and revocations write to `role_assignment_audit` atomically.
11. Mid-transaction failures roll back both assignment mutations and audit records completely.
12. Append-only triggers prevent any `UPDATE` or `DELETE` on `role_assignment_audit`.
13. Unique indexes enforce at most one assignment row per user per scope node across its entire lifecycle.
14. Effective roles resolve correctly for direct team assignments, department inheritance, and organization inheritance.
15. Higher-precedence roles win deterministically (`owner` > `organization_head` > `department_manager` > `team_leader` > `member` > `viewer`).
16. Suspended and revoked assignments contribute zero permissions to `effective_roles`.
17. `GET /me/positions` returns active assignments with scope lineage for the authenticated user.
18. `GET /me/positions` excludes suspended or revoked assignments.
19. Users cannot retrieve another person's role assignments.
20. Unauthenticated requests return HTTP 401.
21. Development authentication headers (`X-Dev-User-*`) are strictly rejected in production environments.
22. JWT login succeeds with correct credentials and returns a signed token.
23. Invalid password, unknown email, and suspended users receive generic 401 errors.
24. Expired and tampered tokens are rejected with 401.
25. Work item creation, keyset pagination, search, and optimistic version rejection.
26. Atomic claim with concurrent conflict reporting.
27. Work item state machine transitions and self-approval restriction.
28. Activity timeline immutability and comment posting.
29. Idempotency key replay returns cached response; payload mismatch returns 422.
30. Role request submission, duplicate prevention, and cancellation rules.
31. Approval routing, atomic assignment creation, and rejection with reason.
32. Transactional outbox co-commit: work item creation enqueues outbox event in the same transaction.
33. Transaction rollback leaves no orphaned outbox event.
34. Role request lifecycle produces correct sequence of outbox events.
35. Notification API requires authentication (401 without token).
36. Notifications enforce strict user isolation (404 for other users' items).
37. Unread count, mark-read, and mark-all-read operations work correctly.
38. Cursor-based keyset pagination on notifications.
39. Worker claims and processes pending events end-to-end (claim → dispatch → complete).
40. Two workers cannot claim the same event concurrently (`SKIP LOCKED`).
41. Lease fencing prevents stale workers from overwriting fresh claims.
42. Transient failures increment attempts and schedule retry with exponential backoff.
43. Terminal failure after max retries transitions event to `dead_letter`, with administrative resurrection.
44. Repeated event processing creates at most one notification row per deduplication key; event processing itself remains at-least-once.

### Building Frontend for Production

From the `frontend/` directory:

```bash
npm run build
npm run preview
```

---

## 7. Role and Scope Compatibility Rules

Enforced strictly at the database level via check constraint `chk_role_scope_compatibility`:

| Role | Permitted Scope Node | Rank | Inherits Downward? |
| :--- | :--- | :---: | :---: |
| `owner` | Organization only | 5 | Yes (all teams in organization) |
| `organization_head` | Organization only | 4 | Yes (all teams in organization) |
| `department_manager` | Department only | 3 | Yes (all teams in department) |
| `team_leader` | Team only | 2 | No (scoped to team) |
| `member` | Team only | 1 | No (scoped to team) |
| `viewer` | Organization, Department, or Team | 0 | Yes (if placed at org/dept) |

---

## 8. Implementation Roadmap & Phase Checklist

| Phase | Milestone Description | Status | Verification Target |
| :---: | :--- | :---: | :--- |
| **0** | **Project Foundation**: Scaffolding, Docker Compose, psycopg_pool, health endpoints, React shell | **Completed** | Clean startup, tests pass, real DB & API health |
| **1** | **Access Model**: Scope tree, `role_assignments`, `effective_roles` view, audit log, `/me/positions` | **Completed** | 27 green tests against real PostgreSQL |
| **2** | **Auth, Work Items & Approvals**: JWT login, work item CRUD, claim, transition, approval routing | **Completed** | 48 tests, self-approval refused, concurrent serialization |
| **3** | **Background Worker & Notifications**: Transactional outbox, `SKIP LOCKED` worker, in-app notifications | **Completed** | PostgreSQL tests for retry/backoff/dead-letter and worker isolation |
| 4 | **Dashboard API**: `/me/dashboard`, server-ordered `/me/todo` list, scope summaries | Next Milestone | Multi-position to-do ordering by derived urgency & priority |
| 5 | **Live Updates**: Server-Sent Events (SSE) fed by PostgreSQL `LISTEN`/`NOTIFY` | Planned | Commit-only notifications, client stale banner refetch |
| 6 | **Frontend Interface**: Interactive views for login, workspace, approvals inbox, and notifications | Planned | Five-minute interactive operator journey demo |
| 7 | **Scale & Query Timing**: 5,000 users, 50,000 items load seed, query plans (`EXPLAIN`) | Planned | Sub-second keyset queries and zero-error smoke run |
| 8 | **Final Packaging**: End-to-end smoke test, demo script verification, final documentation | Planned | Clean machine run from README |

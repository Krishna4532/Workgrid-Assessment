# WorkGrid

**Operational work management with scoped authorization, controlled approvals, concurrency safety and an auditable history.**

WorkGrid is an internal web application for coordinating operational work: incidents, payment investigations, customer issues and approvals. Teams create work items, claim and assign them, move them through a workflow with approvals, and keep a complete, append-only record of who did what and why. People hold different roles in different places (team, department, organization), and elevated access is granted through an approval workflow.

Built for the Newtonite Software Engineering Challenge, **"Operations Under Pressure."** Every organization, person, team and work item in the demo data is fictional.

**Stack:** Python, FastAPI, PostgreSQL 16, psycopg 3 (hand-written SQL), React, TypeScript, Vite, TanStack Query.

---

## Try it

| | |
|---|---|
| **Live API** | https://workgrid-assessment.onrender.com/docs (if idle, the first request can take about a minute to wake) |
| **Run it locally** | [Run it locally](#run-it-locally), about 5 minutes |
| **Guided walkthrough** | [`DEMO_SCRIPT.md`](./DEMO_SCRIPT.md): 12 steps, one per critical behaviour |

<!-- If the Vercel frontend is deployed and verified, add a "Live app" row above with its URL. -->

### Demo accounts

All demo users share the password `<<FILL: password from backend/app/seed.py>>`. They exist for development and assessment only.

| Sign in as | Role | Shows |
|---|---|---|
| `<<FILL: name or email>>` | owner (organization) | Everything; decides organization-head requests |
| `<<FILL>>` | organization_head | Organization-wide access |
| `<<FILL>>` | department_manager | Covers every team in one department, nothing outside it |
| `<<FILL>>` | team_leader in one team, member in another | One person, different powers in different teams |
| `<<FILL>>` | member | Claims and works items; cannot approve |
| `<<FILL>>` | viewer | Read-only; refused on every write |

---

## What it demonstrates

| Requirement | How it is handled | Where it is tested |
|---|---|---|
| Two people claim the same work | One atomic `UPDATE ... WHERE assignee_id IS NULL`; exactly one succeeds, the other gets `409` naming the winner | `test_work_items.py` |
| Stale information | Every item has a `version`; edits and transitions must send the version they saw, otherwise `409` with the current item | `test_work_items.py` |
| Repeated requests | `Idempotency-Key` stored in the same transaction as the action; a replay returns the first result, a changed payload is rejected | `test_idempotency.py` |
| Workflow and approvals | Valid transitions enforced on the server; nobody can approve something they own; simultaneous decisions are serialized | `test_approvals.py`, `test_work_items.py` |
| Resource-level authorization | Roles come from current assignments on every request; out-of-scope data returns `404`, not `403` | `test_effective_roles.py`, `test_auth.py` |
| Revoked access | Suspension or revocation applies on the next request, even with an old token | `test_role_lifecycle_and_audit.py`, `test_auth.py` |
| Reliable background work | Outbox row commits with the change; worker uses `SKIP LOCKED`, lease fencing, exponential backoff and a dead-letter state | `test_outbox.py`, `test_worker.py` |
| Duplicate delivery | A unique key makes replayed events create one notification | `test_notifications.py` |
| History | Work-item activity, approval records and the role audit are append-only, enforced by database triggers | `test_role_lifecycle_and_audit.py` |
| Scale | Keyset pagination, team-scoped indexes, full-text search; the browser never holds the whole dataset | `test_work_items.py` |

---

## Architecture

```text
React + TypeScript (Vite, TanStack Query)
        │  HTTP / SSE
        ▼
FastAPI  ── authentication, authorization, work items, approvals, dashboard, notifications
        │
        ▼
PostgreSQL ── identity and role assignments, workflow and audit history,
        │     transactional outbox, notifications, idempotency keys
        ▼
Background worker ── dispatch, retries, deduplication
```

- **PostgreSQL is the single store** for data, locking, search, the job queue (an `outbox` table read with `FOR UPDATE SKIP LOCKED`) and live-update events (`LISTEN`/`NOTIFY`). No extra services to run or keep consistent.
- **Requests are not permissions.** A role request is intent. Only an approval creates an assignment, in the same transaction as the decision.
- **Access is computed, not stored in the token.** A person's effective role is derived from active assignments on every request.
- **Live updates are hints.** Server-sent events say what changed; the client refetches what is on screen.

Rationale, alternatives and what changes at ten times the scale are in [`ENGINEERING_DECISIONS.md`](./ENGINEERING_DECISIONS.md).

### Roles

An organization holds departments, and a department holds teams. A role is held at one place and covers everything beneath it. A person has at most one role per place and can hold different roles in different places. The highest active assignment covering a team decides what the person can do there.

| Role | Held at | Rank | Covers |
|---|---|---|---|
| `owner` | organization | 5 | every team in the organization |
| `organization_head` | organization | 4 | every team in the organization |
| `department_manager` | department | 3 | every team in the department |
| `team_leader` | team | 2 | that team |
| `member` | team | 1 | that team |
| `viewer` | organization, department or team | 0 | everything beneath its place, read-only |

The database enforces which role may sit at which kind of place. Action-specific rules (who may assign, approve or edit) live in the server's authorization checks.

---

## Run it locally

**Prerequisites:** Python 3.11+, Node.js 18+, and Docker Desktop (or a local PostgreSQL 16/17 server).

### 1. Database

```bash
docker compose up -d
docker compose ps          # wait until the service is healthy
```

Using an existing PostgreSQL server instead? Create a normal, **non-superuser** role and database:

```sql
CREATE ROLE workgrid WITH LOGIN PASSWORD '<choose-a-password>';
CREATE DATABASE workgrid OWNER workgrid;
```

If a migration needs an extension, create it once as an administrator.

### 2. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                 # Windows PowerShell: Copy-Item .env.example .env
```

Edit `backend/.env`: set `<<FILL: variable name for the database URL>>` and `<<FILL: variable name for the JWT secret>>` (use a long random secret). Then:

```bash
python -m app.migrations             # apply schema migrations
python -m app.seed                   # demo data (development only)
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

### 3. Worker

In a second terminal, with the same virtual environment:

```bash
cd backend
python -m app.worker
```

The worker sends notifications. Without it, notifications will not appear.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. If it cannot reach the API, set `<<FILL: frontend API base URL variable name>>` and make sure the backend allows the frontend's origin (CORS).

---

## Tests

The integration tests run against a real, disposable PostgreSQL database and refuse to run against any other. From the repository root:

```powershell
# Windows PowerShell (needs psql on PATH and a PostgreSQL admin account)
$env:PGPASSWORD = "<admin-password>"; .\scripts\run_tests.ps1
```

```bash
# macOS / Linux
PGPASSWORD='<admin-password>' ./scripts/run_tests.sh
```

The script creates the disposable `workgrid_test` role and database if needed, applies the migrations and runs `pytest`. It never touches the development database.

**Last run:** `<<FILL: e.g. "N passed on PostgreSQL 16, <date>">>`

Frontend build check: `cd frontend && npm install && npm run build`.

---

## Deployment

- **Backend:** https://workgrid-assessment.onrender.com (API docs at `/docs`).
- **Worker on the hosted backend:** `<<FILL: running / not running, and what that means for notifications>>`.
- **Frontend:** runs locally with `npm run dev`.

This is an assessment build, not a hardened production service. Before real use: disable demo seeding and demo logins, run the API as a non-superuser database role, store secrets in the host's environment (never in Git), restrict CORS to the real frontend origin, and add login rate limiting, backups and monitoring.

---

## Repository layout

```text
.
├── README.md  DEMO_SCRIPT.md  ENGINEERING_DECISIONS.md  LIMITATIONS.md
├── docker-compose.yml
├── scripts/            one-command test runners
├── backend/
│   ├── app/            API, authorization, repositories, notifications, SSE, worker, seed
│   ├── migrations/     versioned SQL
│   └── tests/          PostgreSQL integration tests
└── frontend/src/       React application
```

## Status

Phases 0 to 6 are built: foundation, access model, authentication and work items, approvals, transactional outbox and worker, dashboard API, live updates, and the frontend. Phase 7 (scale seed and query timing): `<<FILL: done / not done>>`. Additional authorization, concurrency, idempotency and SSE security tests are in `backend/tests/`.

## Documentation

- [`DEMO_SCRIPT.md`](./DEMO_SCRIPT.md): a five-minute walkthrough of each critical behaviour.
- [`ENGINEERING_DECISIONS.md`](./ENGINEERING_DECISIONS.md): the main design decisions and their trade-offs.
- [`LIMITATIONS.md`](./LIMITATIONS.md): known issues with their status, deliberate non-goals, and questions for Newtonite.

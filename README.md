# WorkGrid — Operational Work Management Platform

WorkGrid is a full-stack operational work management application designed to help organizations coordinate tasks, manage responsibilities, track progress, and handle approvals through a structured workflow.

The project focuses on building a reliable internal platform with hierarchical role-based access control, organization-level data isolation, workflow management, concurrency handling, and a clean, maintainable application architecture.

**Repository:** https://github.com/Krishna4532/Workgrid-Assessment

## Features

### 1. Organization and User Management

- Hierarchical organization structure consisting of organizations, departments, teams, and users.
- Role-based access control with organizational scope.
- Server-side authorization for protected operations.
- Support for role assignments, suspension, and revocation.
- Access restrictions designed to prevent users from accessing data outside their permitted scope.

### 2. Work Item Management

- Create and manage operational work items.
- Assign work to team members and support claiming available work.
- Track priorities, status, assignments, and ownership.
- Manage work through defined workflow transitions.
- Add comments and maintain activity history.
- Filter and search work items.
- Support cursor-based pagination for retrieving larger result sets.

### 3. Workflow and Approval Management

- Controlled work item state transitions.
- Validation of user permissions before protected actions.
- Approval workflows with authorization checks.
- Version-aware updates designed to reject stale changes.
- Concurrency-aware handling of operations involving shared work items.

### 4. Dashboards and Data Isolation

- Role-aware dashboard functionality.
- Organization-scoped data retrieval.
- Access controls for organizational resources.
- Support for testing different user roles and their permitted actions.

### 5. Authentication and Security

- JWT-based authentication.
- Password hashing.
- Server-side role and permission validation.
- Organizational scope enforcement.
- Separation of application configuration from source code.

### 6. Background Processing

- A separate worker entry point for background tasks.
- Dedicated worker execution apart from the API process.
- Configuration designed to support independent API and worker processes.

### 7. Automated Testing

The project includes a pytest-based test suite intended to verify application behavior across important scenarios, including:

- Authentication and authorization.
- Organization and role-based access restrictions.
- Work item lifecycle and state transitions.
- Concurrent work item claiming.
- Concurrent approval decisions.
- Stale-version update rejection.
- Dashboard organization isolation.
- Demo-user workflows and access boundaries.

## Technology Stack

| Component | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| Server-state management | TanStack Query |
| Backend API | Python, FastAPI |
| Database | PostgreSQL |
| Database connection pooling | psycopg_pool |
| Authentication | JWT |
| Testing | pytest |
| Containerization | Docker, Docker Compose |

## Architecture

WorkGrid separates the frontend, API, database, and background worker into distinct components.

```text
User
 |
 v
React + TypeScript Frontend
 |
 v
FastAPI REST API
 |
 +--> Authentication
 |
 +--> Authorization and Scope Validation
 |
 +--> Workflow and Business Rules
 |
 v
PostgreSQL Database

Background Worker
 |
 +--> Background Processing
```

The frontend communicates with the backend API. The API validates requests, checks permissions, enforces workflow rules, and performs database operations. The background worker runs independently from the API process.

## Repository Structure

```text
Workgrid-Assessment/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── worker.py
│   │   └── ...
│   ├── tests/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── package.json
│   └── ...
├── docker-compose.yml
├── ENGINEERING_DECISIONS.md
├── LIMITATIONS.md
└── README.md
```

This is a high-level overview of the main components; refer to the repository for the complete implementation.

## Getting Started

### Prerequisites

- Git
- Python
- Node.js and npm
- Docker Desktop with Docker Compose
- PostgreSQL, available through the project's local Docker configuration

### 1. Clone the repository

```bash
git clone https://github.com/Krishna4532/Workgrid-Assessment.git
cd Workgrid-Assessment
```

### 2. Start the database

From the project root:

```bash
docker compose up -d
```

### 3. Configure the backend

```bash
cd backend
python -m venv .venv
```

Activate the virtual environment on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a local `.env` file using `.env.example` as a reference.

Configure the database and authentication settings:

```dotenv
DATABASE_URL=postgresql://workgrid:workgrid_dev_password@localhost:5432/workgrid
JWT_SECRET=replace_with_a_long_random_secret
```

Use a unique, sufficiently strong JWT secret for your environment. Keep secret values out of source control.

Initialize the database schema using the migration instructions included in the project. Run the project's seed process if you need the sample data.

### 4. Start the backend API

From the backend directory:

```bash
uvicorn app.main:app --reload
```

The API documentation is available at:

```text
http://127.0.0.1:8000/docs
```

### 5. Configure and start the frontend

Open a separate terminal:

```bash
cd frontend
npm install
```

Create a frontend `.env` file with the API base URL:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Start the development server:

```bash
npm run dev
```

Open the local URL shown in the terminal.

### 6. Start the background worker

In another terminal, activate the backend environment and run:

```bash
python -m app.worker
```

The worker runs independently of the API process.

## Configuration Reference

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `JWT_SECRET` | Secret used to sign JWTs |
| `VITE_API_BASE_URL` | Backend API URL used by the frontend |

Frontend variables prefixed with `VITE_` are exposed to browser code. Never store passwords, database credentials, or JWT secrets in frontend environment variables.

## Running the Tests

WorkGrid uses pytest for automated backend testing.

From the backend directory, activate the virtual environment, configure a suitable test database, and run:

```bash
pytest -v
```

To display shorter failure traces:

```bash
pytest -v --tb=short
```

The test suite covers core application behavior, including access control, workflow transitions, concurrent operations, and organization-level data isolation.

## Engineering Principles

The project follows these principles:

1. **Security by design:** Protected operations require backend authorization.
2. **Organizational isolation:** Users should access only resources permitted by their organizational scope.
3. **Explicit workflows:** State changes are governed by defined business rules.
4. **Concurrency awareness:** Shared resources require safeguards against conflicting operations.
5. **Separation of concerns:** The frontend, API, database, and worker have distinct responsibilities.
6. **Maintainability:** Application code and engineering decisions are organized for future extension.
7. **Configuration hygiene:** Environment-specific settings and sensitive credentials are kept separate from committed source code.

## Project Documentation

The repository includes additional documentation:

- `ENGINEERING_DECISIONS.md` — engineering choices and their rationale.
- `LIMITATIONS.md` — known constraints and areas for improvement.

## Summary

WorkGrid demonstrates the design and implementation of a full-stack operational work management system. It brings together frontend development, API design, authentication, hierarchical authorization, database-backed workflows, concurrency considerations, and automated testing.

The central engineering goal is to make operational work manageable, traceable, and secure while keeping the application modular and maintainable.

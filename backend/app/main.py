import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import List

# Fix for Windows: psycopg async LISTEN requires SelectorEventLoop instead of ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.auth import get_current_user
from app.config import settings
from app.db import get_db_connection, init_db_pool, close_db_pool, check_db_connectivity
from app.migrations import apply_migrations
from app.models import PositionResponse, User
from app.repositories.roles_repo import get_active_positions
from app.routers import (
    approvals_router,
    auth_router,
    dashboard_router,
    events_router,
    notifications_router,
    work_items_router,
)
from app.sse import broadcaster

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("workgrid.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage lifecycle of application services including connection pool and migrations."""
    logger.info("Starting WorkGrid API...")
    init_db_pool()
    try:
        try:
            apply_migrations()
        except Exception:
            logger.exception("Database migration failed; refusing to start the API.")
            raise

        # Start SSE Broadcaster background listener
        broadcaster.start()
        yield
    finally:
        logger.info("Shutting down WorkGrid API...")
        try:
            await broadcaster.stop()
        except Exception as exc:
            logger.warning("Error stopping SSE broadcaster: %s", exc)
        close_db_pool()


app = FastAPI(
    title="WorkGrid API",
    description="Operational work coordination platform API - Phase 5 Server-Sent Events (SSE) & Live Sync",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# Configure CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Standardized API Error Response Contract (WG-10)
# Preserves standard HTTP status codes, guarantees safe client-facing error envelopes
# with {code, message}, and retains {detail} for backward compatibility.
ERROR_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "UNPROCESSABLE_ENTITY",
    500: "INTERNAL_SERVER_ERROR",
}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code = ERROR_STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
    msg = str(exc.detail) if isinstance(exc.detail, str) else "An HTTP error occurred"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": code,
            "message": msg,
            "detail": exc.detail,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    msg = "Request validation failed"
    if errors:
        msg = f"{errors[0].get('msg', 'Validation error')} for {'.'.join(str(l) for l in errors[0].get('loc', []))}"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": "VALIDATION_ERROR",
            "message": msg,
            "detail": errors,
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected server error occurred",
            "detail": "Internal server error",
        },
    )

# Include Phase 2, 3, 4 & 5 Routers
app.include_router(auth_router.router)
app.include_router(work_items_router.router)
app.include_router(approvals_router.router)
app.include_router(notifications_router.router)
app.include_router(dashboard_router.router)
app.include_router(events_router.router)


@app.get(
    "/health",
    tags=["Health"],
    summary="Process Health Check",
    response_description="Process status and application metadata",
)
def get_process_health():
    """Verify that the FastAPI API process is running and responsive.
    
    Guarantees no internal secrets, tokens, or connection strings are leaked.
    """
    return {
        "status": "ok",
        "app": "WorkGrid API",
        "version": __version__,
        "environment": settings.APP_ENV,
    }


@app.get(
    "/health/db",
    tags=["Health"],
    summary="Database Connectivity Check",
    response_description="PostgreSQL reachability status",
)
def get_database_health():
    """Verify connectivity to PostgreSQL by executing a lightweight query.
    
    Returns HTTP 200 if reachable, or HTTP 503 if unreachable or pool timeout occurs.
    Guarantees sanitized error messages without credential disclosure.
    """
    is_healthy, message, db_name = check_db_connectivity()
    if is_healthy:
        return {
            "status": "ok",
            "database": "connected",
            "database_name": db_name,
        }
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "unavailable",
            "database": "disconnected",
            "error": message,
        },
    )


@app.get(
    "/me/positions",
    tags=["Identity & Positions"],
    summary="Get Current User Active Positions",
    response_model=List[PositionResponse],
    response_description="List of active role assignments with scope hierarchy",
)
def get_my_positions(current_user: User = Depends(get_current_user)):
    """Retrieve the current authenticated user's active role assignments only.
    
    Excludes suspended and revoked assignments.
    """
    with get_db_connection() as conn:
        return get_active_positions(conn, current_user.id)


@app.get(
    "/scopes",
    tags=["Identity & Positions"],
    summary="List Scope Nodes",
)
def get_scopes(current_user: User = Depends(get_current_user)):
    """List accessible scope nodes.
    
    - For users with active role assignments: returns only scopes within their assigned organization(s).
    - For unassigned users (0 active roles): returns entry scopes (organizations, departments, teams)
      allowing them to discover onboarding targets for entry-level role requests (WG-05).
    """
    from psycopg.rows import dict_row
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Check if user has any active role assignments
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM role_assignments WHERE user_id = %s AND status = 'active';",
                (current_user.id,),
            )
            has_assignments = cur.fetchone()["cnt"] > 0

            if not has_assignments:
                # First-time onboarding: expose root organizations, departments, and teams
                cur.execute("SELECT id, name, slug FROM organizations ORDER BY name;")
                orgs = cur.fetchall()
                cur.execute("SELECT id, organization_id, name, slug FROM departments ORDER BY name;")
                depts = cur.fetchall()
                cur.execute("SELECT id, department_id, name, slug FROM teams ORDER BY name;")
                teams = cur.fetchall()
                return {"organizations": orgs, "departments": depts, "teams": teams}

            # Assigned users: discover strictly within their authorized organization hierarchy
            cur.execute(
                """
                SELECT DISTINCT o.id, o.name, o.slug
                FROM organizations o
                WHERE EXISTS (
                    SELECT 1
                    FROM role_assignments ra
                    WHERE ra.user_id = %s AND ra.status = 'active'
                      AND (
                        (ra.scope_type = 'organization' AND ra.organization_id = o.id)
                        OR (
                            ra.scope_type = 'department'
                            AND ra.department_id IN (
                                SELECT d.id FROM departments d WHERE d.organization_id = o.id
                            )
                        )
                        OR (
                            ra.scope_type = 'team'
                            AND ra.team_id IN (
                                SELECT t.id
                                FROM teams t
                                JOIN departments d ON d.id = t.department_id
                                WHERE d.organization_id = o.id
                            )
                        )
                    )
                )
                ORDER BY o.name;
                """,
                (current_user.id,),
            )
            orgs = cur.fetchall()
            cur.execute(
                """
                SELECT DISTINCT d.id, d.organization_id, d.name, d.slug
                FROM departments d
                WHERE EXISTS (
                    SELECT 1
                    FROM role_assignments ra
                    WHERE ra.user_id = %s AND ra.status = 'active'
                      AND (
                        (ra.scope_type = 'organization' AND ra.organization_id = d.organization_id)
                        OR (ra.scope_type = 'department' AND ra.department_id = d.id)
                        OR (
                            ra.scope_type = 'team'
                            AND ra.team_id IN (
                                SELECT t.id FROM teams t WHERE t.department_id = d.id
                            )
                        )
                    )
                )
                ORDER BY d.name;
                """,
                (current_user.id,),
            )
            depts = cur.fetchall()
            cur.execute(
                """
                SELECT t.id, t.department_id, t.name, t.slug
                FROM effective_roles er
                JOIN teams t ON t.id = er.team_id
                WHERE er.user_id = %s
                ORDER BY t.name;
                """,
                (current_user.id,),
            )
            teams = cur.fetchall()
            return {"organizations": orgs, "departments": depts, "teams": teams}

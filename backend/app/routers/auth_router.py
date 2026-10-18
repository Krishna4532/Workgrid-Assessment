import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
import psycopg
from psycopg.rows import dict_row

from app.auth import get_current_user
from app.db import get_db_connection
from app.models import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RegisterRequest,
    RegisterResponse,
    RegistrationOptionsResponse,
    RegistrationTeamOption,
    RoleRequestCreate,
    User,
)
from app.repositories.approvals_repo import create_role_request
from app.repositories.identity_repo import (
    create_user,
    get_user_auth_by_email,
    get_user_by_email,
)
from app.repositories.roles_repo import get_active_positions
from app.security import create_access_token, hash_password, verify_password

logger = logging.getLogger("workgrid.auth_router")

router = APIRouter(tags=["Authentication & Session"])


@router.get(
    "/auth/registration-options",
    summary="Public Registration Onboarding Options",
    response_model=RegistrationOptionsResponse,
    response_description="Available teams and entry-level roles for new account registration",
)
def get_registration_options():
    """Retrieve public onboarding teams and permitted entry-level roles.
    
    Allows prospective users to inspect authorized teams before completing registration.
    """
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.id, t.name, d.name AS department_name, o.name AS organization_name
                FROM teams t
                JOIN departments d ON t.department_id = d.id
                JOIN organizations o ON d.organization_id = o.id
                WHERE o.slug = 'acme-ops'
                ORDER BY d.name, t.name
                LIMIT 50;
                """
            )
            rows = cur.fetchall()
            if not rows:
                cur.execute(
                    """
                    SELECT t.id, t.name, d.name AS department_name, o.name AS organization_name
                    FROM teams t
                    JOIN departments d ON t.department_id = d.id
                    JOIN organizations o ON d.organization_id = o.id
                    ORDER BY o.name, d.name, t.name
                    LIMIT 25;
                    """
                )
                rows = cur.fetchall()
            teams = [
                RegistrationTeamOption(
                    id=r["id"],
                    name=r["name"],
                    department_name=r["department_name"],
                    organization_name=r["organization_name"],
                )
                for r in rows
            ]
            return RegistrationOptionsResponse(
                teams=teams,
                roles=["member", "viewer"],
            )


@router.post(
    "/auth/register",
    summary="Create User Account & Request Role",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    response_description="Created user identity, signed JWT session, and pending role request",
)
def register(dto: RegisterRequest):
    """Register a real WorkGrid user account and submit an onboarding role request.
    
    Guarantees:
    - Email is strictly normalized to lowercase and validated for uniqueness.
    - Password is securely hashed using Argon2id.
    - Enforces principle of least privilege: unassigned users may ONLY request
      entry-level roles ('viewer' or 'member'). Elevated roles cannot be self-assigned.
    - A pending role request is created in 'role_requests'; user has 0 active positions
      until an authorized manager approves the request.
    - Protected operational endpoints strictly reject unassigned users with 403 Forbidden.
    """
    clean_email = dto.email.strip().lower()
    clean_display_name = dto.display_name.strip()

    if "@" not in clean_email or len(clean_email) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A valid email address is required.",
        )

    if len(dto.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters in length.",
        )

    if len(clean_display_name) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Display name must be at least 2 characters in length.",
        )

    if dto.requested_role and dto.requested_role not in ("viewer", "member"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unassigned onboarding users may only request entry-level roles ('viewer' or 'member').",
        )

    with get_db_connection() as conn:
        # 1. Verify email uniqueness
        existing_user = get_user_by_email(conn, clean_email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email address already exists. Please sign in instead.",
            )

        # 2. Resolve target team
        target_team_id = dto.target_team_id
        with conn.cursor(row_factory=dict_row) as cur:
            if target_team_id:
                cur.execute("SELECT id FROM teams WHERE id = %s;", (target_team_id,))
                if not cur.fetchone():
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="The specified target team does not exist.",
                    )
            else:
                cur.execute(
                    """
                    SELECT t.id
                    FROM teams t
                    JOIN departments d ON t.department_id = d.id
                    JOIN organizations o ON d.organization_id = o.id
                    WHERE o.slug = 'acme-ops'
                    ORDER BY d.name, t.name
                    LIMIT 1;
                    """
                )
                team_row = cur.fetchone()
                if not team_row:
                    cur.execute("SELECT id FROM teams LIMIT 1;")
                    team_row = cur.fetchone()
                if not team_row:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="No active teams available for onboarding.",
                    )
                target_team_id = team_row["id"]

        # 3. Create user and submit pending role request atomically
        pw_hash = hash_password(dto.password)
        with conn.transaction():
            user = create_user(
                conn,
                email=clean_email,
                display_name=clean_display_name,
                status="active",
                password_hash=pw_hash,
            )

            role_request_dto = RoleRequestCreate(
                requested_role=dto.requested_role or "member",
                scope_type="team",
                scope_id=target_team_id,
                reason=dto.reason.strip() if dto.reason and dto.reason.strip() else "Real account onboarding request",
            )
            role_req = create_role_request(conn, user.id, role_request_dto)

        token = create_access_token(subject=str(user.id), email=user.email)
        return RegisterResponse(
            user=user,
            access_token=token,
            token_type="bearer",
            role_request=role_req,
            requires_approval=True,
            message="Account created successfully. Your role assignment is pending approval by an authorized manager.",
        )


@router.post(
    "/auth/login",
    summary="User Authentication",
    response_model=LoginResponse,
    response_description="Signed JWT access token and user identity",
)
def login(dto: LoginRequest):
    """Authenticate with email and password to receive a signed JWT access token.
    
    Returns generic errors to prevent user account enumeration.
    """
    with get_db_connection() as conn:
        auth_info = get_user_auth_by_email(conn, dto.email.strip())
        if not auth_info:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        user, stored_hash = auth_info
        if not stored_hash or not verify_password(dto.password, stored_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        if user.status != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        token = create_access_token(subject=str(user.id), email=user.email)
        return LoginResponse(access_token=token, token_type="bearer", user=user)


@router.get(
    "/me",
    summary="Current User Profile & Active Positions",
    response_model=MeResponse,
    response_description="Authenticated identity and active scoped assignments",
)
def get_current_user_profile(current_user: User = Depends(get_current_user)):
    """Retrieve profile and active positions for the authenticated user."""
    with get_db_connection() as conn:
        positions = get_active_positions(conn, current_user.id)
        return MeResponse(user=current_user, positions=positions)


import logging
from uuid import UUID
import jwt
from fastapi import Header, HTTPException, Query, Request, status

from app.config import settings
from app.db import get_db_connection
from app.models import User
from app.repositories.identity_repo import get_user_by_email, get_user_by_id
from app.security import decode_access_token
from app.tickets import consume_sse_ticket

logger = logging.getLogger("workgrid.auth")


def get_current_user(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_dev_user_email: str | None = Header(None, alias="X-Dev-User-Email"),
    x_dev_user_id: str | None = Header(None, alias="X-Dev-User-Id"),
) -> User:
    """Resolve and authenticate the current user identity for general API routes.
    
    1. In production, bearer JWT token verification in Authorization header is mandatory.
    2. Query-string bearer tokens (?token=...) are strictly rejected on all general routes.
    3. In development and testing, Bearer tokens are validated if present; dev headers
       are accepted ONLY if explicitly enabled via ENABLE_DEMO_LOGIN=True.
    4. User must exist and have status 'active'. Suspended/deactivated accounts are rejected with 401.
    5. Roles are NEVER trusted from JWT claims; they are always queried from database assignments.
    """
    env_clean = settings.APP_ENV.strip().lower()
    is_dev_env = env_clean in ("development", "testing", "test")
    allow_dev_login = is_dev_env and settings.ENABLE_DEMO_LOGIN

    query_dev_email = request.query_params.get("dev_email")

    # In production or if demo login is not explicitly enabled, reject dev credentials
    if not allow_dev_login and (x_dev_user_email or x_dev_user_id or query_dev_email):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Development authentication credentials are disabled in production and not enabled in this environment",
        )

    # General routes MUST NOT accept query parameter tokens (WG-03)
    if request.query_params.get("token"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token must be transmitted via Authorization header, not query parameters",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # If Bearer token is supplied, validate it
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        try:
            payload = decode_access_token(token)
            user_id_str = payload.get("sub")
            if not user_id_str:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token payload",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user_id = UUID(user_id_str)
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except (jwt.InvalidTokenError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature or format",
                headers={"WWW-Authenticate": "Bearer"},
            )

        with get_db_connection() as conn:
            user = get_user_by_id(conn, user_id)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User account not found",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if user.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"User account is {user.status}",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return user

    # In development/test only, fallback to dev headers if explicitly enabled
    if allow_dev_login:
        with get_db_connection() as conn:
            target_email = x_dev_user_email or query_dev_email
            if target_email:
                user = get_user_by_email(conn, target_email.strip())
                if user:
                    if user.status != "active":
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"User account is {user.status}",
                        )
                    return user
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Development user with email '{target_email}' not found",
                )
            if x_dev_user_id:
                try:
                    uid = UUID(x_dev_user_id.strip())
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid X-Dev-User-Id UUID format",
                    )
                user = get_user_by_id(conn, uid)
                if user:
                    if user.status != "active":
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"User account is {user.status}",
                        )
                    return user
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Development user with ID '{x_dev_user_id}' not found",
                )

    # Missing authentication
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user_for_sse(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    ticket: str | None = Query(None, description="Single-use short-lived SSE connection ticket"),
    x_dev_user_email: str | None = Header(None, alias="X-Dev-User-Email"),
    x_dev_user_id: str | None = Header(None, alias="X-Dev-User-Id"),
) -> User:
    """Authenticate incoming Server-Sent Events stream connections.
    
    Priority:
    1. Standard 'Authorization: Bearer <token>' header if provided.
    2. Single-use short-lived SSE ticket (?ticket=...) issued by POST /me/events/ticket.
       The ticket is atomically consumed and cannot be reused or used on other routes.
    3. Dev headers if enabled via ENABLE_DEMO_LOGIN=True in development/test.
    """
    # 1. Bearer token in header
    if authorization and authorization.startswith("Bearer "):
        return get_current_user(request, authorization=authorization)

    # 2. Single-use short-lived SSE ticket
    if ticket:
        user_id = consume_sse_ticket(ticket.strip())
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, expired, or already used SSE ticket",
                headers={"WWW-Authenticate": "Bearer"},
            )
        with get_db_connection() as conn:
            user = get_user_by_id(conn, user_id)
            if not user or user.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User account not active",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return user

    # 3. Development fallback if allowed
    env_clean = settings.APP_ENV.strip().lower()
    is_dev_env = env_clean in ("development", "testing", "test")
    if is_dev_env and settings.ENABLE_DEMO_LOGIN:
        return get_current_user(
            request,
            authorization=authorization,
            x_dev_user_email=x_dev_user_email,
            x_dev_user_id=x_dev_user_id,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide valid Authorization header or single-use SSE ticket",
        headers={"WWW-Authenticate": "Bearer"},
    )

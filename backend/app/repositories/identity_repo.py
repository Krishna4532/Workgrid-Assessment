from typing import Optional
from uuid import UUID
import psycopg
from psycopg.rows import dict_row

from app.models import User, Organization, Department, Team


def create_user(
    conn: psycopg.Connection,
    email: str,
    display_name: str,
    status: str = "active",
    user_id: Optional[UUID] = None,
    password_hash: Optional[str] = None,
) -> User:
    """Create a user identity with canonical lowercase email and optional password hash."""
    with conn.cursor(row_factory=dict_row) as cur:
        if user_id:
            cur.execute(
                """
                INSERT INTO users (id, email, display_name, status, password_hash)
                VALUES (%s, LOWER(%s), %s, %s, %s)
                RETURNING id, email, display_name, status, created_at, updated_at;
                """,
                (user_id, email, display_name, status, password_hash),
            )
        else:
            cur.execute(
                """
                INSERT INTO users (email, display_name, status, password_hash)
                VALUES (LOWER(%s), %s, %s, %s)
                RETURNING id, email, display_name, status, created_at, updated_at;
                """,
                (email, display_name, status, password_hash),
            )
        row = cur.fetchone()
        return User(**row)


def set_user_password(conn: psycopg.Connection, user_id: UUID, password_hash: str) -> None:
    """Update a user's password hash."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s;",
            (password_hash, user_id),
        )


def get_user_auth_by_email(conn: psycopg.Connection, email: str) -> Optional[tuple[User, Optional[str]]]:
    """Retrieve user object and their password hash for authentication verification."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, email, display_name, status, password_hash, created_at, updated_at
            FROM users
            WHERE LOWER(email) = LOWER(%s);
            """,
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        pw_hash = row.pop("password_hash", None)
        return User(**row), pw_hash


def get_user_by_id(conn: psycopg.Connection, user_id: UUID) -> Optional[User]:
    """Retrieve a user by their unique UUID."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, email, display_name, status, created_at, updated_at FROM users WHERE id = %s;",
            (user_id,),
        )
        row = cur.fetchone()
        return User(**row) if row else None


def get_user_by_email(conn: psycopg.Connection, email: str) -> Optional[User]:
    """Retrieve a user by their case-insensitive email address."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, email, display_name, status, created_at, updated_at FROM users WHERE LOWER(email) = LOWER(%s);",
            (email,),
        )
        row = cur.fetchone()
        return User(**row) if row else None


def create_organization(
    conn: psycopg.Connection,
    name: str,
    slug: str,
    org_id: Optional[UUID] = None,
) -> Organization:
    """Create an organization node."""
    with conn.cursor(row_factory=dict_row) as cur:
        if org_id:
            cur.execute(
                """
                INSERT INTO organizations (id, name, slug)
                VALUES (%s, %s, %s)
                RETURNING id, name, slug, created_at, updated_at;
                """,
                (org_id, name, slug),
            )
        else:
            cur.execute(
                """
                INSERT INTO organizations (name, slug)
                VALUES (%s, %s)
                RETURNING id, name, slug, created_at, updated_at;
                """,
                (name, slug),
            )
        row = cur.fetchone()
        return Organization(**row)


def get_organization_by_slug(conn: psycopg.Connection, slug: str) -> Optional[Organization]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, name, slug, created_at, updated_at FROM organizations WHERE slug = %s;",
            (slug,),
        )
        row = cur.fetchone()
        return Organization(**row) if row else None


def create_department(
    conn: psycopg.Connection,
    organization_id: UUID,
    name: str,
    slug: str,
    dept_id: Optional[UUID] = None,
) -> Department:
    """Create a department belonging to an organization."""
    with conn.cursor(row_factory=dict_row) as cur:
        if dept_id:
            cur.execute(
                """
                INSERT INTO departments (id, organization_id, name, slug)
                VALUES (%s, %s, %s, %s)
                RETURNING id, organization_id, name, slug, created_at, updated_at;
                """,
                (dept_id, organization_id, name, slug),
            )
        else:
            cur.execute(
                """
                INSERT INTO departments (organization_id, name, slug)
                VALUES (%s, %s, %s)
                RETURNING id, organization_id, name, slug, created_at, updated_at;
                """,
                (organization_id, name, slug),
            )
        row = cur.fetchone()
        return Department(**row)


def get_department_by_slug(
    conn: psycopg.Connection, organization_id: UUID, slug: str
) -> Optional[Department]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, organization_id, name, slug, created_at, updated_at
            FROM departments
            WHERE organization_id = %s AND slug = %s;
            """,
            (organization_id, slug),
        )
        row = cur.fetchone()
        return Department(**row) if row else None


def create_team(
    conn: psycopg.Connection,
    department_id: UUID,
    name: str,
    slug: str,
    team_id: Optional[UUID] = None,
) -> Team:
    """Create a team belonging to a department."""
    with conn.cursor(row_factory=dict_row) as cur:
        if team_id:
            cur.execute(
                """
                INSERT INTO teams (id, department_id, name, slug)
                VALUES (%s, %s, %s, %s)
                RETURNING id, department_id, name, slug, created_at, updated_at;
                """,
                (team_id, department_id, name, slug),
            )
        else:
            cur.execute(
                """
                INSERT INTO teams (department_id, name, slug)
                VALUES (%s, %s, %s)
                RETURNING id, department_id, name, slug, created_at, updated_at;
                """,
                (department_id, name, slug),
            )
        row = cur.fetchone()
        return Team(**row)


def get_team_by_slug(
    conn: psycopg.Connection, department_id: UUID, slug: str
) -> Optional[Team]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, department_id, name, slug, created_at, updated_at
            FROM teams
            WHERE department_id = %s AND slug = %s;
            """,
            (department_id, slug),
        )
        row = cur.fetchone()
        return Team(**row) if row else None

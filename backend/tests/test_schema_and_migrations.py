import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import psycopg
from psycopg import errors

from app.db import get_db_connection
from app.migrations import apply_migrations
from app.repositories.identity_repo import create_organization, create_department, create_team, create_user


def test_migrations_tracking_and_idempotency():
    """Verify that migrations can run against the database and track applied state cleanly."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations WHERE version = '001_identity_and_scopes.sql';")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "001_identity_and_scopes.sql"

    # Re-running migrations returns no new migrations
    applied = apply_migrations()
    assert len(applied) == 0


def test_concurrent_migration_attempts_apply_each_migration_once(tmp_path, monkeypatch):
    """Exercise the advisory lock with two startup migration attempts in parallel."""
    migration_name = f"test_concurrent_{uuid.uuid4().hex}.sql"
    table_name = f"test_migration_{uuid.uuid4().hex}"
    migration_path = tmp_path / migration_name
    migration_path.write_text(
        f"CREATE TABLE {table_name} AS "
        "SELECT 1::integer AS value FROM (SELECT pg_sleep(0.75)) AS delay;",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.migrations.get_migration_files", lambda: [migration_path])
    start_together = Barrier(2)

    def run_migrations():
        start_together.wait(timeout=10)
        return apply_migrations()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run_migrations(), range(2)))

        assert sorted(map(len, results)) == [0, 1]
        assert sum(result.count(migration_name) for result in results) == 1

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = %s;",
                    (migration_name,),
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = %s;",
                    (table_name,),
                )
                assert cur.fetchone()[0] == 1
    finally:
        with get_db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {table_name};")
                    cur.execute(
                        "DELETE FROM schema_migrations WHERE version = %s;",
                        (migration_name,),
                    )


def test_organization_slug_uniqueness():
    """Verify organization slug must be unique."""
    unique_slug = f"org-{uuid.uuid4().hex[:8]}"
    with get_db_connection() as conn:
        create_organization(conn, name="Test Org 1", slug=unique_slug)
        conn.commit()

        # Duplicate slug must fail
        with pytest.raises(errors.UniqueViolation):
            create_organization(conn, name="Test Org 2", slug=unique_slug)
            conn.commit()
        conn.rollback()


def test_department_slug_unique_per_organization():
    """Verify department slug is unique within an organization, but allowed across different orgs."""
    slug = f"dept-{uuid.uuid4().hex[:8]}"
    with get_db_connection() as conn:
        org1 = create_organization(conn, name="Org 1", slug=f"org-{uuid.uuid4().hex[:8]}")
        org2 = create_organization(conn, name="Org 2", slug=f"org-{uuid.uuid4().hex[:8]}")
        create_department(conn, organization_id=org1.id, name="Dept 1", slug=slug)
        # Same slug in different org is allowed
        dept2 = create_department(conn, organization_id=org2.id, name="Dept 2", slug=slug)
        assert dept2.id is not None
        conn.commit()

        # Duplicate slug in same org must fail
        with pytest.raises(errors.UniqueViolation):
            create_department(conn, organization_id=org1.id, name="Dept 1 Duplicate", slug=slug)
            conn.commit()
        conn.rollback()


def test_team_slug_unique_per_department():
    """Verify team slug is unique within a department."""
    slug = f"team-{uuid.uuid4().hex[:8]}"
    with get_db_connection() as conn:
        org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
        create_team(conn, department_id=dept.id, name="Team 1", slug=slug)
        conn.commit()

        with pytest.raises(errors.UniqueViolation):
            create_team(conn, department_id=dept.id, name="Team Duplicate", slug=slug)
            conn.commit()
        conn.rollback()


def test_database_enforces_role_scope_compatibility():
    """Verify that PostgreSQL check constraints strictly reject invalid role/scope pairings."""
    with get_db_connection() as conn:
        user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Test User")
        org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
        team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
        conn.commit()

        with conn.cursor() as cur:
            # 1. 'member' role is only allowed at team scope (reject at org)
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, organization_id, status)
                    VALUES (%s, 'member', 'organization', %s, 'active');
                    """,
                    (user.id, org.id),
                )
            conn.rollback()
            assert "chk_role_scope_compatibility" in str(exc_info.value)

            # 2. 'team_leader' role is only allowed at team scope (reject at department)
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, department_id, status)
                    VALUES (%s, 'team_leader', 'department', %s, 'active');
                    """,
                    (user.id, dept.id),
                )
            conn.rollback()
            assert "chk_role_scope_compatibility" in str(exc_info.value)

            # 3. 'department_manager' role is only allowed at department scope (reject at team)
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, team_id, status)
                    VALUES (%s, 'department_manager', 'team', %s, 'active');
                    """,
                    (user.id, team.id),
                )
            conn.rollback()
            assert "chk_role_scope_compatibility" in str(exc_info.value)

            # 4. 'owner' role is only allowed at organization scope (reject at team)
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, team_id, status)
                    VALUES (%s, 'owner', 'team', %s, 'active');
                    """,
                    (user.id, team.id),
                )
            conn.rollback()
            assert "chk_role_scope_compatibility" in str(exc_info.value)

            # 5. 'organization_head' is only allowed at organization scope (reject at department)
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, department_id, status)
                    VALUES (%s, 'organization_head', 'department', %s, 'active');
                    """,
                    (user.id, dept.id),
                )
            conn.rollback()
            assert "chk_role_scope_compatibility" in str(exc_info.value)


def test_database_enforces_referential_integrity_and_scope_fks():
    """Verify that role_assignments cannot point to nonexistent scopes or mix scope FKs."""
    with get_db_connection() as conn:
        user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Test User")
        org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        nonexistent_id = uuid.uuid4()
        conn.commit()

        with conn.cursor() as cur:
            # Foreign key violation for nonexistent organization
            with pytest.raises(errors.ForeignKeyViolation):
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, organization_id, status)
                    VALUES (%s, 'owner', 'organization', %s, 'active');
                    """,
                    (user.id, nonexistent_id),
                )
            conn.rollback()

            # Scope FK constraint violation when scope_type does not match populated FK
            with pytest.raises(errors.CheckViolation) as exc_info:
                cur.execute(
                    """
                    INSERT INTO role_assignments (user_id, role, scope_type, organization_id, status)
                    VALUES (%s, 'viewer', 'team', %s, 'active');
                    """,
                    (user.id, org.id),
                )
            conn.rollback()
            assert "chk_role_assignments_scope_fk" in str(exc_info.value)

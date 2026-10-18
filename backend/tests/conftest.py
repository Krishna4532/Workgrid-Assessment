import asyncio
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict


CONFIRMATION_VALUE = "I_HAVE_VERIFIED_THIS_IS_DISPOSABLE"
TEST_DATABASE_NAME = "workgrid_test"
TEST_DATABASE_USER = "workgrid_test"


def _development_connection_info() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    development_dsn = None
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                development_dsn = line.split("=", 1)[1].strip().strip("\"'")
                break
    if not development_dsn:
        development_dsn = "dbname=workgrid user=workgrid host=localhost port=5432"
    return conninfo_to_dict(development_dsn)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse integration tests unless a separately owned disposable DB is verified."""
    # Psycopg async connections require SelectorEventLoop (add_reader/add_writer).
    # Windows defaults to ProactorEventLoop which lacks these methods.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    test_dsn = os.environ.get("WORKGRID_TEST_DATABASE_URL")
    if not test_dsn:
        raise pytest.UsageError(
            "Integration tests require WORKGRID_TEST_DATABASE_URL for a dedicated test database. "
            "See README.md; no tests were run."
        )
    if os.environ.get("WORKGRID_TEST_DATABASE_CONFIRM") != CONFIRMATION_VALUE:
        raise pytest.UsageError(
            "Set WORKGRID_TEST_DATABASE_CONFIRM only after verifying the target is disposable. "
            "No tests were run."
        )
    if os.environ.get("WORKGRID_TEST_DATABASE_NAME") != TEST_DATABASE_NAME:
        raise pytest.UsageError(
            f"WORKGRID_TEST_DATABASE_NAME must be exactly {TEST_DATABASE_NAME!r}. No tests were run."
        )
    if os.environ.get("WORKGRID_TEST_DATABASE_USER") != TEST_DATABASE_USER:
        raise pytest.UsageError(
            f"WORKGRID_TEST_DATABASE_USER must be exactly {TEST_DATABASE_USER!r}. No tests were run."
        )

    try:
        test_info = conninfo_to_dict(test_dsn)
        development_info = _development_connection_info()
    except Exception:
        raise pytest.UsageError(
            "Could not parse test/development database configuration; no tests were run."
        ) from None

    if test_info.get("dbname") != TEST_DATABASE_NAME or test_info.get("user") != TEST_DATABASE_USER:
        raise pytest.UsageError(
            "The test URL must explicitly target database workgrid_test as role workgrid_test. "
            "No tests were run."
        )
    if (
        test_info.get("dbname") == development_info.get("dbname")
        or test_info.get("user") == development_info.get("user")
    ):
        raise pytest.UsageError(
            "The test database and role must both differ from the development database/role. "
            "No tests were run."
        )

    os.environ["APP_ENV"] = "testing"
    os.environ["DATABASE_URL"] = test_dsn

    try:
        with psycopg.connect(test_dsn, application_name="workgrid-tests") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT current_database(), current_user,
                           current_setting('application_name'),
                           pg_get_userbyid(datdba)
                    FROM pg_database
                    WHERE datname = current_database();
                    """
                )
                actual_name, actual_user, application_name, database_owner = cur.fetchone()
                cur.execute(
                    "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;"
                )
                role_is_superuser = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM workgrid_test_safety
                        WHERE purpose = 'disposable'
                    );
                    """
                )
                has_safety_marker = cur.fetchone()[0]
    except Exception:
        raise pytest.UsageError(
            "Could not verify the dedicated test database. Check its configuration and create it "
            "manually as documented; no tests were run."
        ) from None

    if (
        actual_name != TEST_DATABASE_NAME
        or actual_user != TEST_DATABASE_USER
        or database_owner != TEST_DATABASE_USER
        or application_name != "workgrid-tests"
        or role_is_superuser
        or not has_safety_marker
    ):
        raise pytest.UsageError(
            "PostgreSQL did not confirm the expected marked, non-superuser-owned test database and role. "
            "No tests were run."
        )

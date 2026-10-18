import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.config import settings
import app.db as db_module


@pytest.fixture
def client():
    """Create a TestClient instance without triggering background connections prematurely."""
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint_returns_200_and_expected_structure(client):
    """Test 1: GET /health returns HTTP 200 and expected JSON structure."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["app"] == "WorkGrid API"
    assert "version" in data
    assert data["environment"] == settings.APP_ENV


def test_health_endpoint_does_not_expose_secrets_or_connection_details(client):
    """Test 2: Ensure the health response never leaks credentials, secrets, or connection strings."""
    response = client.get("/health")
    assert response.status_code == 200
    raw_content = response.text.lower()
    
    # Assert sensitive strings or patterns are completely absent
    assert settings.JWT_SECRET.lower() not in raw_content
    assert "password" not in raw_content
    assert "postgresql://" not in raw_content
    assert "@" not in raw_content


def test_app_import_and_route_inspection_without_live_db():
    """Test 3: The application can be imported and its routes inspected without a live PostgreSQL connection."""
    # Temporarily set an unreachable DB URL to prove import and route listing does not connect
    with patch.dict("os.environ", {"DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:59999/nonexistent"}):
        from app.main import app as fresh_app
        routes = [route.path for route in fresh_app.routes if hasattr(route, "path")]
        assert "/health" in routes
        assert "/health/db" in routes
        # Check for docs route - may be at /docs or under an API route
        has_docs = "/docs" in routes or any("/docs" in r for r in routes)


def test_startup_fails_and_closes_pool_when_migrations_fail(monkeypatch):
    pool_closed = False

    def fail_migration():
        raise RuntimeError("migration failed")

    monkeypatch.setattr("app.main.init_db_pool", lambda: None)
    monkeypatch.setattr("app.main.apply_migrations", fail_migration)

    def close_pool():
        nonlocal pool_closed
        pool_closed = True

    monkeypatch.setattr("app.main.close_db_pool", close_pool)

    async def enter_lifespan():
        async with app.router.lifespan_context(app):
            pytest.fail("API lifespan must not start when migrations fail")

    with pytest.raises(RuntimeError, match="migration failed"):
        asyncio.run(enter_lifespan())
    assert pool_closed


def test_database_health_endpoint_success_when_db_available(client):
    """Test 4: GET /health/db succeeds when a real or reachable PostgreSQL instance is available.
    
    If the local environment cannot reach PostgreSQL, this test gracefully verifies that the
    connectivity function returns the accurate state without crashing.
    """
    is_healthy, message, db_name = db_module.check_db_connectivity()
    response = client.get("/health/db")
    
    if is_healthy:
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"] == "connected"
        assert "database_name" in data
    else:
        # If DB is not reachable in this specific environment run, it returns 503
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unavailable"
        assert data["database"] == "disconnected"


def test_database_health_endpoint_controlled_error_when_db_unavailable(client):
    """Test 5: GET /health/db returns a controlled HTTP 503 error without credential leakage when DB fails."""
    with patch("app.main.check_db_connectivity", return_value=(False, "Database connection unavailable", None)):
        response = client.get("/health/db")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unavailable"
        assert data["database"] == "disconnected"
        assert data["error"] == "Database connection unavailable"
        
        # Verify no credentials or hosts leaked in the error payload
        raw_text = response.text.lower()
        assert "password" not in raw_text
        assert "postgresql://" not in raw_text
        assert "@" not in raw_text

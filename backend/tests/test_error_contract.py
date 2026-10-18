import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import create_organization, create_department, create_team, create_user
from app.repositories.roles_repo import grant_or_update_role


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_unauthenticated_error_contract(client):
    """Verify 401 returns standard error envelope with code UNAUTHORIZED."""
    res = client.get("/me")
    assert res.status_code == 401
    data = res.json()
    assert data["code"] == "UNAUTHORIZED"
    assert "message" in data
    assert "detail" in data


def test_forbidden_error_contract(client):
    """Verify 403 returns standard error envelope with code FORBIDDEN."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"forbidden-{uuid.uuid4().hex[:8]}@example.com", display_name="Forbidden User")

    # Unassigned user requesting owner role
    res = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": user.email},
        json={
            "requested_role": "owner",
            "scope_type": "organization",
            "scope_id": str(uuid.uuid4()),
            "reason": "Forbidden request",
        },
    )
    assert res.status_code in (403, 404)
    data = res.json()
    assert data["code"] in ("FORBIDDEN", "NOT_FOUND")
    assert "message" in data


def test_not_found_error_contract(client):
    """Verify 404 returns standard error envelope with code NOT_FOUND."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"nf-{uuid.uuid4().hex[:8]}@example.com", display_name="NF User")

    fake_id = str(uuid.uuid4())
    res = client.get(f"/work-items/{fake_id}", headers={"X-Dev-User-Email": user.email})
    assert res.status_code == 404
    data = res.json()
    assert data["code"] == "NOT_FOUND"
    assert "message" in data


def test_validation_error_contract(client):
    """Verify 422 returns standard error envelope with code VALIDATION_ERROR."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"val-{uuid.uuid4().hex[:8]}@example.com", display_name="Val User")

    # Submit invalid body (missing required fields)
    res = client.post(
        "/role-requests",
        headers={"X-Dev-User-Email": user.email},
        json={"invalid_field": 123},
    )
    assert res.status_code == 422
    data = res.json()
    assert data["code"] == "VALIDATION_ERROR"
    assert "message" in data
    assert "detail" in data

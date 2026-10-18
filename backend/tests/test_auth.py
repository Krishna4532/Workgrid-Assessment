from datetime import timedelta
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.config import DEFAULT_DEV_JWT_SECRET, Settings
from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import create_user, set_user_password
from app.security import create_access_token, hash_password


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_valid_login_returns_token_and_user_info(client):
    """Verify valid login returns a signed JWT token and user metadata."""
    email = f"auth-{uuid.uuid4().hex[:8]}@example.com"
    password = "CorrectHorseBatteryStaple!"

    with get_db_connection() as conn:
        with conn.transaction():
            create_user(
                conn,
                email=email,
                display_name="Auth Tester",
                password_hash=hash_password(password),
            )

    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == email.lower()
    assert data["user"]["display_name"] == "Auth Tester"


def test_invalid_password_returns_generic_401(client):
    """Verify invalid password returns generic 401 without credential details."""
    email = f"auth-{uuid.uuid4().hex[:8]}@example.com"
    with get_db_connection() as conn:
        with conn.transaction():
            create_user(
                conn,
                email=email,
                display_name="Auth Tester",
                password_hash=hash_password("correct-password"),
            )

    response = client.post("/auth/login", json={"email": email, "password": "wrong-password"})
    assert response.status_code == 401
    assert "invalid email or password" in response.json()["detail"].lower()


def test_unknown_email_returns_generic_401(client):
    """Verify unknown account returns generic 401 without revealing non-existence."""
    response = client.post(
        "/auth/login",
        json={"email": "nonexistent@example.com", "password": "some-password"},
    )
    assert response.status_code == 401
    assert "invalid email or password" in response.json()["detail"].lower()


def test_suspended_user_cannot_login(client):
    """Verify suspended accounts are rejected without disclosing account status."""
    email = f"suspended-{uuid.uuid4().hex[:8]}@example.com"
    password = "valid-password"
    with get_db_connection() as conn:
        with conn.transaction():
            create_user(
                conn,
                email=email,
                display_name="Suspended Person",
                status="suspended",
                password_hash=hash_password(password),
            )

    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_development_auth_headers_are_rejected_in_production(client):
    """Verify development identity headers cannot bypass production authentication."""
    with patch("app.auth.settings.APP_ENV", "production"):
        response = client.get(
            "/me/positions",
            headers={"X-Dev-User-Email": "nobody@example.com"},
        )
    assert response.status_code == 401
    assert "disabled in production" in response.json()["detail"].lower()


def test_production_configuration_fails_closed_without_a_strong_jwt_secret(monkeypatch):
    """Verify the implicit configuration defaults to production and rejects the dev key."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    default_settings = Settings(_env_file=None)
    assert default_settings.APP_ENV == "production"
    with pytest.raises(ValueError, match="JWT_SECRET must be explicitly configured"):
        default_settings.check_security()

    weak_production_settings = Settings(
        _env_file=None,
        APP_ENV="production",
        JWT_SECRET=DEFAULT_DEV_JWT_SECRET,
    )
    with pytest.raises(ValueError, match="JWT_SECRET must be explicitly configured"):
        weak_production_settings.check_security()


def test_expired_token_rejected_with_401(client):
    """Verify expired token returns HTTP 401."""
    user_id = uuid.uuid4()
    expired_token = create_access_token(
        subject=str(user_id),
        email="test@example.com",
        expires_delta=timedelta(seconds=-10),
    )

    response = client.get(
        "/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_invalid_signature_token_rejected_with_401(client):
    """Verify token signed with a foreign key is rejected."""
    import jwt

    fake_token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "hacker@example.com", "exp": 9999999999, "iat": 1000},
        "wrong-secret-key",
        algorithm="HS256",
    )

    response = client.get(
        "/me",
        headers={"Authorization": f"Bearer {fake_token}"},
    )
    assert response.status_code == 401
    assert "invalid token" in response.json()["detail"].lower()


def test_authenticated_get_me_returns_profile(client):
    """Verify GET /me returns authenticated user details and positions."""
    email = f"me-{uuid.uuid4().hex[:8]}@example.com"
    password = "secret-password"
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(
                conn,
                email=email,
                display_name="Profile User",
                password_hash=hash_password(password),
            )

    token = create_access_token(subject=str(user.id), email=user.email)

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["user"]["id"] == str(user.id)
    assert data["user"]["email"] == email.lower()
    assert "positions" in data

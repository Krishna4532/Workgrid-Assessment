from pathlib import Path
from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_JWT_SECRET = "dev_insecure_jwt_secret_change_in_production"
DEFAULT_DEV_DATABASE_URL = "postgresql://workgrid:workgrid_dev_password@localhost:5432/workgrid"

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or .env file."""

    APP_ENV: str = Field(default="production", description="Environment: development, staging, production, or testing")
    DATABASE_URL: str = Field(
        default=DEFAULT_DEV_DATABASE_URL,
        description="PostgreSQL connection string (psycopg 3 compatible)",
    )
    JWT_SECRET: str = Field(
        default=DEFAULT_DEV_JWT_SECRET,
        description="Secret key for signing JWT tokens",
    )
    JWT_EXPIRE_MINUTES: int = Field(
        default=60,
        description="Token expiration duration in minutes",
    )
    CORS_ORIGINS: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
        description="Comma-separated list of allowed CORS origins",
    )

    # Security & Demo Flags
    ENABLE_DEMO_LOGIN: bool = Field(
        default=False,
        description="Explicit flag to enable development persona headers and demo auth. Strictly forbidden in production.",
    )

    # Phase 3: Background Worker Configuration
    WORKER_POLL_INTERVAL_SECONDS: float = Field(
        default=1.0,
        description="Idle sleep interval between queue polling cycles",
        ge=0.1,
        le=60.0,
    )
    WORKER_BATCH_SIZE: int = Field(
        default=20,
        description="Maximum number of outbox events claimed in one transaction batch",
        ge=1,
        le=500,
    )
    WORKER_MAX_RETRIES: int = Field(
        default=5,
        description="Maximum retry attempts before terminal dead_letter state",
        ge=1,
        le=20,
    )
    WORKER_BACKOFF_BASE_SECONDS: float = Field(
        default=2.0,
        description="Base multiplier for exponential backoff calculations",
        ge=0.5,
        le=10.0,
    )
    WORKER_MAX_BACKOFF_SECONDS: float = Field(
        default=60.0,
        description="Maximum ceiling for exponential retry delay in seconds",
        ge=5.0,
        le=3600.0,
    )
    WORKER_LEASE_TIMEOUT_SECONDS: int = Field(
        default=60,
        description="Processing lease duration before considering worker crashed and reclaiming event",
        ge=10,
        le=600,
    )

    model_config = SettingsConfigDict(
        env_file=(_ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @property
    def cors_origins_list(self) -> List[str]:
        """Return parsed CORS origins as a trimmed list without trailing slashes."""
        return [origin.strip().rstrip("/") for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    def check_security(self) -> None:
        """Reject development JWT secrets, dev db credentials, and demo configurations outside dev/test environments."""
        env_clean = self.APP_ENV.strip().lower()
        if env_clean not in ("development", "testing", "test"):
            if self.JWT_SECRET == DEFAULT_DEV_JWT_SECRET:
                raise ValueError(
                    f"JWT_SECRET must be explicitly configured in '{self.APP_ENV}' environment."
                )
            elif len(self.JWT_SECRET) < 32:
                raise ValueError(
                    f"JWT_SECRET must be at least 32 characters in '{self.APP_ENV}' environment."
                )
            if self.DATABASE_URL == DEFAULT_DEV_DATABASE_URL:
                raise ValueError(
                    f"DATABASE_URL must not use default development credentials in '{self.APP_ENV}' environment."
                )
            if self.ENABLE_DEMO_LOGIN:
                raise ValueError(
                    f"ENABLE_DEMO_LOGIN cannot be enabled in '{self.APP_ENV}' environment."
                )


# Global settings singleton
settings = Settings()
settings.check_security()

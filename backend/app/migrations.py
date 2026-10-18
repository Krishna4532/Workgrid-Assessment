import os
import sys
import logging
from pathlib import Path
from typing import List, Optional
import psycopg

from app.config import settings

logger = logging.getLogger("workgrid.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def get_migration_files() -> List[Path]:
    """Retrieve all SQL migration files sorted lexicographically."""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def apply_migrations(conninfo: Optional[str] = None) -> List[str]:
    """Apply any pending database migrations in sequential order within transactions.
    
    Returns the list of newly applied migration names.
    """
    db_url = conninfo or settings.DATABASE_URL
    applied: List[str] = []

    with psycopg.connect(db_url, autocommit=True) as conn:
        # Serialize application startup across processes so migrations cannot race.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended('workgrid:migrations', 0));")

        with conn.cursor() as cur:
            # 1. Create tracking table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # 2. Query already applied migrations
            cur.execute("SELECT version FROM schema_migrations;")
            existing_versions = {row[0] for row in cur.fetchall()}

        # 3. Apply pending migrations
        migration_files = get_migration_files()
        for migration_path in migration_files:
            version_name = migration_path.name
            if version_name in existing_versions:
                continue

            logger.info("Applying migration: %s", version_name)
            with open(migration_path, "r", encoding="utf-8") as f:
                sql_content = f.read()

            # Execute migration in an explicit transaction
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql_content)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s);",
                        (version_name,),
                    )

            logger.info("Applied migration successfully: %s", version_name)
            applied.append(version_name)

    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(f"Connecting to database and running migrations from {MIGRATIONS_DIR}...")
    try:
        newly_applied = apply_migrations()
        if newly_applied:
            print(f"Successfully applied {len(newly_applied)} migration(s): {', '.join(newly_applied)}")
        else:
            print("Database schema is already up to date. No pending migrations.")
    except Exception as e:
        print(f"Migration error: {e}", file=sys.stderr)
        sys.exit(1)

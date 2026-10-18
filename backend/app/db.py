import logging
from contextlib import contextmanager
from typing import Generator, Optional, Tuple
import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from app.config import settings

logger = logging.getLogger("workgrid.db")

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    """Return the global connection pool singleton, initializing if needed with open=False.
    
    Database connectivity is never forced during import. If a previously opened pool was
    closed, a fresh pool instance is created.
    """
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            conninfo=settings.DATABASE_URL,
            min_size=1,
            max_size=10,
            timeout=5.0,
            open=False,
        )
    return _pool


def init_db_pool() -> ConnectionPool:
    """Explicitly open the connection pool during application startup."""
    pool = get_pool()
    try:
        if pool.closed:
            pool = get_pool()
        pool.open()
        logger.info("Database connection pool opened successfully.")
    except Exception as exc:
        # Non-fatal during startup so app can start and report status via health endpoints
        logger.warning("Database connection pool could not open connections at startup: %s", type(exc).__name__)
    return pool


def close_db_pool() -> None:
    """Close the global connection pool cleanly during application shutdown."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
            logger.info("Database connection pool closed.")
        except Exception as exc:
            logger.error("Error closing database connection pool: %s", type(exc).__name__)
        finally:
            _pool = None


@contextmanager
def get_db_connection(timeout: float = 5.0) -> Generator[psycopg.Connection, None, None]:
    """Context manager for acquiring a pooled connection with bounded timeout.
    
    Guarantees connection returns to the pool even if exceptions occur.
    """
    pool = get_pool()
    try:
        pool.open()
    except PoolClosed:
        pool = get_pool()
        try:
            pool.open()
        except Exception:
            pass
    except Exception:
        pass

    with pool.connection(timeout=timeout) as conn:
        yield conn


def check_db_connectivity(timeout: float = 3.0) -> Tuple[bool, str, Optional[str]]:
    """Execute a lightweight parameterized query to verify database health.
    
    Returns:
        (is_healthy, status_message, sanitized_db_name)
    Guarantees no credentials, hosts, or sensitive connection parameters are exposed.
    """
    try:
        pool = get_pool()
        if pool.closed:
            pool.open()

        with pool.connection(timeout=timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
            
            # Retrieve safe database name without host/credentials
            db_name = getattr(conn.info, "dbname", "postgresql")
            return True, "connected", db_name
    except (PoolTimeout, PoolClosed) as e:
        logger.warning("Database pool error during health check: %s", type(e).__name__)
        return False, "Database connection pool timeout or closed", None
    except psycopg.OperationalError as e:
        logger.warning("PostgreSQL operational error during health check: %s", type(e).__name__)
        return False, "Database connection unavailable", None
    except Exception as e:
        logger.warning("Database connectivity check failed: %s", type(e).__name__)
        return False, "Database health check failed", None

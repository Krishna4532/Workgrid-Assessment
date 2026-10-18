-- WorkGrid: Least-Privilege Database Role Separation (WG-02)
--
-- Run this script as a PostgreSQL superuser (e.g. postgres) during database provisioning:
-- 1. workgrid_migrator: Used during deployment to apply migrations, create/alter tables and triggers.
-- 2. workgrid_app: Used by the runtime FastAPI application and background worker. Has DML-only privileges,
--    no SUPERUSER, no CREATEDB, and cannot drop or alter tables.

-- Create application runtime user (if not exists)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workgrid_app') THEN
        CREATE ROLE workgrid_app WITH LOGIN PASSWORD 'change_this_in_production' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

-- Connect permissions
GRANT CONNECT ON DATABASE workgrid TO workgrid_app;
GRANT USAGE ON SCHEMA public TO workgrid_app;

-- Grant DML privileges on existing tables
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO workgrid_app;

-- Grant sequence access for auto-generated identifiers
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO workgrid_app;

-- Ensure future tables and sequences created by migrator automatically grant DML to runtime role
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO workgrid_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO workgrid_app;

-- Explicitly revoke destructive privileges from application runtime role
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM workgrid_app;

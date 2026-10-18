-- Migration: 001_identity_and_scopes.sql
-- Description: Core schema for users, organization hierarchy, scoped role assignments,
--              append-only audit log, and effective roles view.

-- 1. Users table (identity)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deactivated')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (LOWER(email));

-- 2. Organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Departments table (belongs to organization)
CREATE TABLE IF NOT EXISTS departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_departments_org_slug UNIQUE (organization_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_departments_organization_id ON departments(organization_id);

-- 4. Teams table (belongs to department)
CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    department_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_teams_dept_slug UNIQUE (department_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_teams_department_id ON teams(department_id);

-- 5. Role assignments table
-- Scopes use explicit foreign keys with a check constraint guaranteeing exactly one valid parent.
CREATE TABLE IF NOT EXISTS role_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL CHECK (role IN ('viewer', 'member', 'team_leader', 'department_manager', 'organization_head', 'owner')),
    scope_type VARCHAR(50) NOT NULL CHECK (scope_type IN ('organization', 'department', 'team')),
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    department_id UUID REFERENCES departments(id) ON DELETE CASCADE,
    team_id UUID REFERENCES teams(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
    granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Scope foreign key constraint: exactly one scope FK must match scope_type
    CONSTRAINT chk_role_assignments_scope_fk CHECK (
        (scope_type = 'organization' AND organization_id IS NOT NULL AND department_id IS NULL AND team_id IS NULL) OR
        (scope_type = 'department' AND department_id IS NOT NULL AND organization_id IS NULL AND team_id IS NULL) OR
        (scope_type = 'team' AND team_id IS NOT NULL AND organization_id IS NULL AND department_id IS NULL)
    ),

    -- Role and Scope Compatibility constraint:
    -- viewer: organization, department, or team
    -- member, team_leader: team only
    -- department_manager: department only
    -- organization_head, owner: organization only
    CONSTRAINT chk_role_scope_compatibility CHECK (
        (role = 'viewer' AND scope_type IN ('organization', 'department', 'team')) OR
        (role IN ('member', 'team_leader') AND scope_type = 'team') OR
        (role = 'department_manager' AND scope_type = 'department') OR
        (role IN ('organization_head', 'owner') AND scope_type = 'organization')
    )
);

-- Invariant: At most one assignment record per user per scope node across its entire lifecycle
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_assignments_user_org ON role_assignments (user_id, organization_id) WHERE organization_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_assignments_user_dept ON role_assignments (user_id, department_id) WHERE department_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_assignments_user_team ON role_assignments (user_id, team_id) WHERE team_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_role_assignments_user_id ON role_assignments (user_id);
CREATE INDEX IF NOT EXISTS idx_role_assignments_status ON role_assignments (status);

-- 6. Role assignment audit log (append-only)
CREATE TABLE IF NOT EXISTS role_assignment_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id UUID NOT NULL REFERENCES role_assignments(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('grant', 'role_change', 'suspend', 'restore', 'revoke')),
    previous_status VARCHAR(50),
    new_status VARCHAR(50) NOT NULL,
    previous_role VARCHAR(50),
    new_role VARCHAR(50) NOT NULL,
    scope_type VARCHAR(50) NOT NULL CHECK (scope_type IN ('organization', 'department', 'team')),
    scope_id UUID NOT NULL,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_role_assignment_audit_assignment_id ON role_assignment_audit (assignment_id);
CREATE INDEX IF NOT EXISTS idx_role_assignment_audit_user_id ON role_assignment_audit (user_id);
CREATE INDEX IF NOT EXISTS idx_role_assignment_audit_created_at ON role_assignment_audit (created_at);

-- Trigger to enforce append-only behavior on role_assignment_audit
CREATE OR REPLACE FUNCTION trg_prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Audit records are append-only and cannot be modified or deleted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_role_assignment_audit_immutable ON role_assignment_audit;
CREATE TRIGGER trg_role_assignment_audit_immutable
BEFORE UPDATE OR DELETE ON role_assignment_audit
FOR EACH ROW
EXECUTE FUNCTION trg_prevent_audit_modification();

-- 7. Effective roles view
-- Resolves a user's highest active role for every team, inheriting through the scope tree
-- Precedence: owner (5) > organization_head (4) > department_manager (3) > team_leader (2) > member (1) > viewer (0)
CREATE OR REPLACE VIEW effective_roles AS
WITH team_hierarchy AS (
    SELECT 
        t.id AS team_id,
        t.name AS team_name,
        d.id AS department_id,
        d.name AS department_name,
        o.id AS organization_id,
        o.name AS organization_name
    FROM teams t
    JOIN departments d ON t.department_id = d.id
    JOIN organizations o ON d.organization_id = o.id
),
applicable_assignments AS (
    SELECT 
        ra.user_id,
        th.team_id,
        ra.id AS assignment_id,
        ra.role,
        ra.scope_type,
        CASE ra.role
            WHEN 'owner' THEN 5
            WHEN 'organization_head' THEN 4
            WHEN 'department_manager' THEN 3
            WHEN 'team_leader' THEN 2
            WHEN 'member' THEN 1
            WHEN 'viewer' THEN 0
            ELSE -1
        END AS role_rank,
        ra.created_at
    FROM role_assignments ra
    CROSS JOIN team_hierarchy th
    WHERE ra.status = 'active'
      AND (
          (ra.scope_type = 'team' AND ra.team_id = th.team_id) OR
          (ra.scope_type = 'department' AND ra.department_id = th.department_id) OR
          (ra.scope_type = 'organization' AND ra.organization_id = th.organization_id)
      )
)
SELECT DISTINCT ON (user_id, team_id)
    user_id,
    team_id,
    assignment_id,
    role,
    scope_type,
    role_rank
FROM applicable_assignments
ORDER BY user_id, team_id, role_rank DESC, created_at ASC;

-- Migration: 002_auth_work_items_approvals.sql
-- Description: Phase 2 schema additions: password authentication, role requests and approval policies,
--              work items, activity history, and idempotency keys.

-- 1. Password storage for users
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);

-- 2. Approval Policies Table (Data-driven routing for role requests)
CREATE TABLE IF NOT EXISTS approval_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_role VARCHAR(50) NOT NULL,
    scope_type VARCHAR(50) NOT NULL,
    decider_role VARCHAR(50) NOT NULL,
    decider_scope_type VARCHAR(50) NOT NULL,
    CONSTRAINT uq_approval_policies_role_scope UNIQUE (requested_role, scope_type)
);

-- Seed initial policy rows as specified in the Constitution §3
INSERT INTO approval_policies (requested_role, scope_type, decider_role, decider_scope_type)
VALUES
    ('member', 'team', 'team_leader', 'team'),
    ('team_leader', 'team', 'department_manager', 'department'),
    ('department_manager', 'department', 'organization_head', 'organization'),
    ('organization_head', 'organization', 'owner', 'organization'),
    ('owner', 'organization', 'owner', 'organization'),
    ('viewer', 'team', 'team_leader', 'team'),
    ('viewer', 'department', 'department_manager', 'department'),
    ('viewer', 'organization', 'organization_head', 'organization')
ON CONFLICT (requested_role, scope_type) DO NOTHING;

-- 3. Role Requests Table (Intent for role assignment)
CREATE TABLE IF NOT EXISTS role_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_role VARCHAR(50) NOT NULL CHECK (requested_role IN ('viewer', 'member', 'team_leader', 'department_manager', 'organization_head', 'owner')),
    scope_type VARCHAR(50) NOT NULL CHECK (scope_type IN ('organization', 'department', 'team')),
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    department_id UUID REFERENCES departments(id) ON DELETE CASCADE,
    team_id UUID REFERENCES teams(id) ON DELETE CASCADE,
    reason TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_role_requests_scope_fk CHECK (
        (scope_type = 'organization' AND organization_id IS NOT NULL AND department_id IS NULL AND team_id IS NULL) OR
        (scope_type = 'department' AND department_id IS NOT NULL AND organization_id IS NULL AND team_id IS NULL) OR
        (scope_type = 'team' AND team_id IS NOT NULL AND organization_id IS NULL AND department_id IS NULL)
    ),

    CONSTRAINT chk_role_requests_role_scope CHECK (
        (requested_role = 'viewer' AND scope_type IN ('organization', 'department', 'team')) OR
        (requested_role IN ('member', 'team_leader') AND scope_type = 'team') OR
        (requested_role = 'department_manager' AND scope_type = 'department') OR
        (requested_role IN ('organization_head', 'owner') AND scope_type = 'organization')
    )
);

-- Invariant: At most one pending request per user, scope, and requested role
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_requests_pending_org
    ON role_requests (user_id, organization_id, requested_role)
    WHERE status = 'pending' AND organization_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_role_requests_pending_dept
    ON role_requests (user_id, department_id, requested_role)
    WHERE status = 'pending' AND department_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_role_requests_pending_team
    ON role_requests (user_id, team_id, requested_role)
    WHERE status = 'pending' AND team_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_role_requests_user_id ON role_requests (user_id);
CREATE INDEX IF NOT EXISTS idx_role_requests_status ON role_requests (status);

-- 4. Approval Records Table (Decision history)
CREATE TABLE IF NOT EXISTS approval_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES role_requests(id) ON DELETE CASCADE,
    decider_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    outcome VARCHAR(50) NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    comment TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_approval_records_request_id ON approval_records (request_id);
CREATE INDEX IF NOT EXISTS idx_approval_records_decider_id ON approval_records (decider_id);

-- Append-only trigger for approval_records
CREATE OR REPLACE FUNCTION trg_prevent_approval_record_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Approval records are append-only and cannot be modified or deleted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_approval_records_immutable ON approval_records;
CREATE TRIGGER trg_approval_records_immutable
BEFORE UPDATE OR DELETE ON approval_records
FOR EACH ROW
EXECUTE FUNCTION trg_prevent_approval_record_modification();

-- 5. Work Items Table
CREATE TABLE IF NOT EXISTS work_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    kind VARCHAR(50) NOT NULL CHECK (kind IN ('task', 'incident', 'customer_issue', 'payment', 'engineering', 'compliance')),
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(50) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'awaiting_approval', 'resolved', 'closed')),
    priority VARCHAR(50) NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    assignee_id UUID REFERENCES users(id) ON DELETE SET NULL,
    creator_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    due_date TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))) STORED,

    CONSTRAINT chk_in_progress_has_assignee CHECK (
        (status NOT IN ('in_progress', 'awaiting_approval')) OR (assignee_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_work_items_team_updated_id ON work_items (team_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_team_status ON work_items (team_id, status);
CREATE INDEX IF NOT EXISTS idx_work_items_assignee ON work_items (assignee_id) WHERE assignee_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_items_creator ON work_items (creator_id);
CREATE INDEX IF NOT EXISTS idx_work_items_search_tsv ON work_items USING gin(search_tsv);

-- 6. Activity Timeline Table (Work item history & comments)
CREATE TABLE IF NOT EXISTS activity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id UUID NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    actor_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('created', 'comment', 'status_changed', 'assigned', 'claimed', 'released')),
    previous_status VARCHAR(50),
    new_status VARCHAR(50),
    previous_assignee_id UUID REFERENCES users(id) ON DELETE SET NULL,
    new_assignee_id UUID REFERENCES users(id) ON DELETE SET NULL,
    comment TEXT,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_work_item_created ON activity (work_item_id, created_at ASC);

-- Append-only trigger for activity
CREATE OR REPLACE FUNCTION trg_prevent_activity_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Activity records are append-only and cannot be modified or deleted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_activity_immutable ON activity;
CREATE TRIGGER trg_activity_immutable
BEFORE UPDATE OR DELETE ON activity
FOR EACH ROW
EXECUTE FUNCTION trg_prevent_activity_modification();

-- 7. Idempotency Keys Table
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys (created_at);

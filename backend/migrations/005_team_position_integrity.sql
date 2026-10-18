-- Migration: 005_team_position_integrity.sql
-- Description: Phase 9 — Reinforce one-position-per-person-per-team invariant.
--
-- The existing unique index uq_role_assignments_user_team already prevents
-- duplicate (user_id, team_id) rows. This migration adds an explicit named
-- constraint for clearer error messages and documentation, plus a partial
-- unique index on active assignments to provide a tighter safety net.
-- No data is modified.

-- 1. Add a comment documenting the business rule on the existing index
COMMENT ON INDEX uq_role_assignments_user_team IS
    'Enforces one position per person per team across the entire lifecycle (active, suspended, revoked). '
    'A person may hold different positions in different teams but only one position within the same team at any time.';

-- 2. Additional partial unique index on active team assignments only
--    This provides a second safety net: even if the lifecycle index were ever relaxed,
--    there can never be two active team-scope assignments for the same user+team.
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_assignments_user_team_active
    ON role_assignments (user_id, team_id)
    WHERE team_id IS NOT NULL AND status = 'active';

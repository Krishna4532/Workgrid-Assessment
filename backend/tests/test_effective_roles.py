import uuid
from app.db import get_db_connection
from app.repositories.identity_repo import create_organization, create_department, create_team, create_user
from app.repositories.roles_repo import grant_or_update_role, suspend_role, revoke_role, get_effective_role


def test_effective_role_direct_team_assignment():
    """Verify team-level role resolution."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Direct Team Member")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)

        effective = get_effective_role(conn, user.id, team.id)
        assert effective is not None
        assert effective["role"] == "member"
        assert effective["scope_type"] == "team"
        assert effective["role_rank"] == 1


def test_effective_role_department_inheritance():
    """Verify department manager power inherits to all teams in that department."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Dept Manager")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team1 = create_team(conn, department_id=dept.id, name="Team 1", slug=f"team-{uuid.uuid4().hex[:8]}")
            team2 = create_team(conn, department_id=dept.id, name="Team 2", slug=f"team-{uuid.uuid4().hex[:8]}")

            grant_or_update_role(conn, user_id=user.id, role="department_manager", scope_type="department", scope_id=dept.id)

        eff1 = get_effective_role(conn, user.id, team1.id)
        eff2 = get_effective_role(conn, user.id, team2.id)

        assert eff1 is not None and eff1["role"] == "department_manager" and eff1["role_rank"] == 3
        assert eff2 is not None and eff2["role"] == "department_manager" and eff2["role_rank"] == 3


def test_effective_role_organization_inheritance():
    """Verify organization head / owner role inherits across all departments and teams."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Org Owner")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            grant_or_update_role(conn, user_id=user.id, role="owner", scope_type="organization", scope_id=org.id)

        eff = get_effective_role(conn, user.id, team.id)
        assert eff is not None
        assert eff["role"] == "owner"
        assert eff["role_rank"] == 5


def test_effective_role_precedence():
    """Verify higher precedence role wins when user has multiple active covering assignments."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Multi Role User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            # Assign member at team (rank 1) and owner at org (rank 5)
            grant_or_update_role(conn, user_id=user.id, role="member", scope_type="team", scope_id=team.id)
            grant_or_update_role(conn, user_id=user.id, role="owner", scope_type="organization", scope_id=org.id)

        # Owner (5) must win over member (1)
        eff = get_effective_role(conn, user.id, team.id)
        assert eff is not None
        assert eff["role"] == "owner"
        assert eff["role_rank"] == 5


def test_effective_role_ignores_suspended_and_revoked_assignments():
    """Verify that suspended and revoked assignments contribute zero permissions."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="Inactive User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

            assignment = grant_or_update_role(conn, user_id=user.id, role="team_leader", scope_type="team", scope_id=team.id)
            suspend_role(conn, assignment.id)

        # Suspended assignment yields no effective role
        assert get_effective_role(conn, user.id, team.id) is None

        # Revoke it
        with conn.transaction():
            revoke_role(conn, assignment.id)

        # Revoked assignment yields no effective role
        assert get_effective_role(conn, user.id, team.id) is None


def test_effective_role_no_assignment_yields_none():
    """Verify that a user without any covering assignment returns no effective role."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"user-{uuid.uuid4().hex[:8]}@example.com", display_name="External User")
            org = create_organization(conn, name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            dept = create_department(conn, organization_id=org.id, name="Dept", slug=f"dept-{uuid.uuid4().hex[:8]}")
            team = create_team(conn, department_id=dept.id, name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")

        assert get_effective_role(conn, user.id, team.id) is None

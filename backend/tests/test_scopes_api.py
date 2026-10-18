import uuid

from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import (
    create_department,
    create_organization,
    create_team,
    create_user,
)
from app.repositories.roles_repo import grant_or_update_role


def test_scopes_are_limited_to_active_role_hierarchy():
    with get_db_connection() as conn:
        with conn.transaction():
            org_a = create_organization(
                conn, name="Scope Org A", slug=f"scope-org-a-{uuid.uuid4().hex}"
            )
            org_b = create_organization(
                conn, name="Scope Org B", slug=f"scope-org-b-{uuid.uuid4().hex}"
            )
            dept_a1 = create_department(
                conn, org_a.id, name="Scope Dept A1", slug=f"scope-dept-a1-{uuid.uuid4().hex}"
            )
            dept_a2 = create_department(
                conn, org_a.id, name="Scope Dept A2", slug=f"scope-dept-a2-{uuid.uuid4().hex}"
            )
            dept_b = create_department(
                conn, org_b.id, name="Scope Dept B", slug=f"scope-dept-b-{uuid.uuid4().hex}"
            )
            team_a11 = create_team(
                conn, dept_a1.id, name="Scope Team A11", slug=f"scope-team-a11-{uuid.uuid4().hex}"
            )
            team_a12 = create_team(
                conn, dept_a1.id, name="Scope Team A12", slug=f"scope-team-a12-{uuid.uuid4().hex}"
            )
            team_a2 = create_team(
                conn, dept_a2.id, name="Scope Team A2", slug=f"scope-team-a2-{uuid.uuid4().hex}"
            )
            team_b = create_team(
                conn, dept_b.id, name="Scope Team B", slug=f"scope-team-b-{uuid.uuid4().hex}"
            )

            team_member = create_user(
                conn, email=f"scope-member-{uuid.uuid4().hex}@example.com", display_name="Scope Member"
            )
            department_manager = create_user(
                conn, email=f"scope-manager-{uuid.uuid4().hex}@example.com", display_name="Scope Manager"
            )
            organization_owner = create_user(
                conn, email=f"scope-owner-{uuid.uuid4().hex}@example.com", display_name="Scope Owner"
            )
            other_org_leader = create_user(
                conn, email=f"scope-other-{uuid.uuid4().hex}@example.com", display_name="Other Org Leader"
            )
            no_scope_user = create_user(
                conn, email=f"scope-none-{uuid.uuid4().hex}@example.com", display_name="No Scope User"
            )

            grant_or_update_role(
                conn, team_member.id, "member", "team", team_a11.id
            )
            grant_or_update_role(
                conn, department_manager.id, "department_manager", "department", dept_a1.id
            )
            grant_or_update_role(
                conn, organization_owner.id, "owner", "organization", org_a.id
            )
            grant_or_update_role(
                conn, other_org_leader.id, "team_leader", "team", team_b.id
            )

    with TestClient(app) as client:
        member_result = client.get(
            "/scopes", headers={"X-Dev-User-Email": team_member.email}
        )
        manager_result = client.get(
            "/scopes", headers={"X-Dev-User-Email": department_manager.email}
        )
        owner_result = client.get(
            "/scopes", headers={"X-Dev-User-Email": organization_owner.email}
        )
        other_org_result = client.get(
            "/scopes", headers={"X-Dev-User-Email": other_org_leader.email}
        )
        unassigned_result = client.get(
            "/scopes", headers={"X-Dev-User-Email": no_scope_user.email}
        )

    assert member_result.status_code == 200
    member_scopes = member_result.json()
    assert {o["id"] for o in member_scopes["organizations"]} == {str(org_a.id)}
    assert {d["id"] for d in member_scopes["departments"]} == {str(dept_a1.id)}
    assert {t["id"] for t in member_scopes["teams"]} == {str(team_a11.id)}

    assert manager_result.status_code == 200
    manager_scopes = manager_result.json()
    assert {o["id"] for o in manager_scopes["organizations"]} == {str(org_a.id)}
    assert {d["id"] for d in manager_scopes["departments"]} == {str(dept_a1.id)}
    assert {t["id"] for t in manager_scopes["teams"]} == {
        str(team_a11.id),
        str(team_a12.id),
    }

    assert owner_result.status_code == 200
    owner_scopes = owner_result.json()
    assert {o["id"] for o in owner_scopes["organizations"]} == {str(org_a.id)}
    assert {d["id"] for d in owner_scopes["departments"]} == {
        str(dept_a1.id),
        str(dept_a2.id),
    }
    assert {t["id"] for t in owner_scopes["teams"]} == {
        str(team_a11.id),
        str(team_a12.id),
        str(team_a2.id),
    }

    assert other_org_result.status_code == 200
    other_org_scopes = other_org_result.json()
    assert {o["id"] for o in other_org_scopes["organizations"]} == {str(org_b.id)}
    assert {d["id"] for d in other_org_scopes["departments"]} == {str(dept_b.id)}
    assert {t["id"] for t in other_org_scopes["teams"]} == {str(team_b.id)}

    assert unassigned_result.status_code == 200
    unassigned_scopes = unassigned_result.json()
    # WG-05: Unassigned onboarding users discover root organizations, departments, and teams to submit first role request
    assert len(unassigned_scopes["organizations"]) >= 2
    assert {o["id"] for o in unassigned_scopes["organizations"]}.issuperset({str(org_a.id), str(org_b.id)})
    assert {t["id"] for t in unassigned_scopes["teams"]}.issuperset({str(team_a11.id), str(team_b.id)})


def test_onboarding_and_cross_organization_boundary_rules():
    """Verify WG-05: First-access rules and cross-organization request boundary enforcement."""
    with get_db_connection() as conn:
        with conn.transaction():
            org_x = create_organization(conn, name="Org X", slug=f"org-x-{uuid.uuid4().hex[:8]}")
            dept_x = create_department(conn, org_x.id, name="Dept X", slug=f"dept-x-{uuid.uuid4().hex[:8]}")
            team_x = create_team(conn, dept_x.id, name="Team X", slug=f"team-x-{uuid.uuid4().hex[:8]}")

            org_y = create_organization(conn, name="Org Y", slug=f"org-y-{uuid.uuid4().hex[:8]}")
            dept_y = create_department(conn, org_y.id, name="Dept Y", slug=f"dept-y-{uuid.uuid4().hex[:8]}")
            team_y = create_team(conn, dept_y.id, name="Team Y", slug=f"team-y-{uuid.uuid4().hex[:8]}")

            new_user = create_user(conn, email=f"new-onboard-{uuid.uuid4().hex[:8]}@example.com", display_name="New Onboard")
            assigned_user = create_user(conn, email=f"assigned-user-{uuid.uuid4().hex[:8]}@example.com", display_name="Assigned User")
            grant_or_update_role(conn, assigned_user.id, "member", "team", team_x.id)

    with TestClient(app) as client:
        # 1. Unassigned user successfully requests entry-level role ('member') on entry team
        res1 = client.post(
            "/role-requests",
            headers={"X-Dev-User-Email": new_user.email},
            json={
                "requested_role": "member",
                "scope_type": "team",
                "scope_id": str(team_x.id),
                "reason": "Joining project",
            },
        )
        assert res1.status_code == 201
        assert res1.json()["status"] == "pending"

        # 2. Unassigned user attempts to request privileged executive role ('owner') directly via API -> 403 Forbidden
        privileged_user = f"priv-onboard-{uuid.uuid4().hex[:8]}@example.com"
        with get_db_connection() as conn:
            with conn.transaction():
                create_user(conn, email=privileged_user, display_name="Privilege Escalator")

        res2 = client.post(
            "/role-requests",
            headers={"X-Dev-User-Email": privileged_user},
            json={
                "requested_role": "owner",
                "scope_type": "organization",
                "scope_id": str(org_x.id),
                "reason": "Attempting privilege escalation",
            },
        )
        assert res2.status_code == 403
        assert "entry-level roles" in res2.json()["detail"]

        # 3. Requesting a non-existent scope returns 404
        fake_team_id = str(uuid.uuid4())
        res3 = client.post(
            "/role-requests",
            headers={"X-Dev-User-Email": new_user.email},
            json={
                "requested_role": "viewer",
                "scope_type": "team",
                "scope_id": fake_team_id,
                "reason": "Testing nonexistent scope",
            },
        )
        assert res3.status_code == 404
        assert "not found" in res3.json()["detail"].lower()

        # 4. Assigned user attempts cross-organization request (Org X member requests access in Org Y) -> 403 Forbidden
        res4 = client.post(
            "/role-requests",
            headers={"X-Dev-User-Email": assigned_user.email},
            json={
                "requested_role": "member",
                "scope_type": "team",
                "scope_id": str(team_y.id),
                "reason": "Unauthorized cross-org attempt",
            },
        )
        assert res4.status_code == 403
        assert "outside your assigned organization" in res4.json()["detail"]

        # 5. Assigned user requests legitimate role within their own organization (Dept X) -> 201 Created
        res5 = client.post(
            "/role-requests",
            headers={"X-Dev-User-Email": assigned_user.email},
            json={
                "requested_role": "department_manager",
                "scope_type": "department",
                "scope_id": str(dept_x.id),
                "reason": "Promotion to department manager",
            },
        )
        assert res5.status_code == 201
        assert res5.json()["status"] == "pending"

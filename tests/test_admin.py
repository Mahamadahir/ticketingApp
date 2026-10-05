from sqlmodel import select

from app.models import AuditLog, Team, User, UserRole
from tests.conftest import create_team, login_as, set_role


def test_non_super_admin_cannot_access_admin_console(client, db_session):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)

    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/teams").status_code == 403
    assert client.get("/admin/audit-log").status_code == 403


def test_super_admin_can_create_team(client, db_session):
    login_as(client, "admin@example.com")

    response = client.post("/admin/teams", data={"name": "Infrastructure"}, follow_redirects=False)
    assert response.status_code == 303

    team = db_session.exec(select(Team).where(Team.name == "Infrastructure")).first()
    assert team is not None

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "created", AuditLog.entity_type == "team")).all()
    assert len(audit_entries) == 1


def test_super_admin_can_create_local_account(client, db_session):
    login_as(client, "admin@example.com")
    team = create_team(db_session, "Service Desk")

    response = client.post(
        "/admin/users",
        data={
            "email": "contractor@example.com",
            "display_name": "Contractor",
            "password": "hunter2hunter2",
            "role": "agent",
            "team_id": team.id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    user = db_session.exec(select(User).where(User.email == "contractor@example.com")).first()
    assert user is not None
    assert user.role == UserRole.AGENT
    assert user.password_hash is not None


def test_role_change_writes_audit_entry(client, db_session):
    login_as(client, "admin@example.com")
    target_id = login_as(client, "someone@example.com")
    client.get("/auth/logout")
    login_as(client, "admin@example.com")

    response = client.post(
        f"/admin/users/{target_id}", data={"role": "agent", "team_id": ""}, follow_redirects=False
    )
    assert response.status_code == 303

    target = db_session.get(User, target_id)
    assert target.role == UserRole.AGENT

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "role_changed")).all()
    assert len(audit_entries) == 1


def test_audit_log_filters_by_entity_type(client, db_session):
    login_as(client, "admin@example.com")
    client.post("/admin/teams", data={"name": "Network"})
    db_session.add(AuditLog(entity_type="ticket", entity_id=1, actor_email="agent@example.com", action="commented"))
    db_session.commit()

    response = client.get("/admin/audit-log?entity_type=team")
    assert "team #" in response.text
    assert "ticket #1" not in response.text

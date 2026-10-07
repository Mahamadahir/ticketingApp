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


def test_audit_log_filters_by_actor(client, db_session):
    login_as(client, "admin@example.com")
    db_session.add(AuditLog(entity_type="ticket", entity_id=1, actor_email="alice@example.com", action="commented"))
    db_session.add(AuditLog(entity_type="ticket", entity_id=2, actor_email="bob@example.com", action="assigned"))
    db_session.commit()

    response = client.get("/admin/audit-log?actor=alice@example.com")
    assert "ticket #1" in response.text
    assert "ticket #2" not in response.text


def test_audit_log_filters_by_user_acted_on(client, db_session):
    login_as(client, "admin@example.com")
    db_session.add(AuditLog(entity_type="user", entity_id=7, actor_email="admin@example.com", action="role_changed"))
    db_session.add(AuditLog(entity_type="user", entity_id=8, actor_email="admin@example.com", action="team_changed"))
    db_session.add(AuditLog(entity_type="ticket", entity_id=7, actor_email="admin@example.com", action="commented"))
    db_session.commit()

    response = client.get("/admin/audit-log?subject_user_id=7")
    assert "user #7" in response.text
    assert "user #8" not in response.text
    assert "ticket #7" not in response.text


def test_audit_log_paginates_and_keeps_filters_in_page_links(client, db_session):
    login_as(client, "admin@example.com")
    for entity_id in range(1, 61):
        db_session.add(AuditLog(entity_type="ticket", entity_id=entity_id, actor_email="alice@example.com", action="commented"))
    db_session.commit()

    first_page = client.get("/admin/audit-log?actor=alice@example.com")
    assert first_page.text.count("ticket #") == 50
    assert "Page 1 of 2" in first_page.text
    assert "actor=alice%40example.com&amp;page=2" in first_page.text

    second_page = client.get("/admin/audit-log?actor=alice@example.com&page=2")
    assert second_page.text.count("ticket #") == 10

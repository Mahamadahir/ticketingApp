from sqlmodel import select

from app.models import AuditLog, User, UserPermission, UserRole
from app.permissions import MANAGE_REQUEST_TEMPLATES, PERMISSIONS
from app.visibility import has_permission
from tests.conftest import login_as, set_role


def as_admin(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def make_user(client, db_session, email, role):
    client.post("/auth/logout")
    user = login_as(client, email)
    set_role(db_session, user, role)
    client.post("/auth/logout")
    return db_session.exec(select(User).where(User.email == email)).one()


def test_the_registry_has_the_first_permission():
    assert MANAGE_REQUEST_TEMPLATES in PERMISSIONS


def test_super_admin_holds_everything_agent_needs_a_grant_end_user_never(client, db_session):
    as_admin(client, db_session)
    admin = db_session.exec(select(User).where(User.email == "admin@example.com")).one()
    agent = make_user(client, db_session, "a@example.com", UserRole.AGENT)
    end = make_user(client, db_session, "e@example.com", UserRole.END_USER)
    assert has_permission(db_session, admin, MANAGE_REQUEST_TEMPLATES)
    assert not has_permission(db_session, agent, MANAGE_REQUEST_TEMPLATES)
    db_session.add(UserPermission(user_id=end.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    assert has_permission(db_session, agent, MANAGE_REQUEST_TEMPLATES)
    assert not has_permission(db_session, end, MANAGE_REQUEST_TEMPLATES)  # a stray row counts for nothing
    assert not has_permission(db_session, agent, "nonsense")


def test_grant_and_revoke_are_super_admin_only_and_audited(client, db_session):
    as_admin(client, db_session)
    agent = make_user(client, db_session, "a@example.com", UserRole.AGENT)
    login_as(client, "admin@example.com")
    url = f"/admin/users/{agent.id}/permissions"
    assert client.post(url, data={"permissions": MANAGE_REQUEST_TEMPLATES}).status_code in (200, 303)
    assert len(db_session.exec(select(UserPermission)).all()) == 1
    assert client.get("/admin/users").status_code == 200
    assert client.post(url, data={}).status_code in (200, 303)
    assert db_session.exec(select(UserPermission)).all() == []
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "user")).all()]
    assert "permission_granted" in actions and "permission_revoked" in actions
    assert client.post(url, data={"permissions": "bogus"}).status_code == 400
    # an agent with the permission cannot grant
    client.post(url, data={"permissions": MANAGE_REQUEST_TEMPLATES})
    client.post("/auth/logout")
    login_as(client, "a@example.com")
    assert client.post(url, data={"permissions": MANAGE_REQUEST_TEMPLATES}).status_code == 403
    assert client.get("/admin/users").status_code == 403


def test_end_users_cannot_be_granted(client, db_session):
    as_admin(client, db_session)
    end = make_user(client, db_session, "e@example.com", UserRole.END_USER)
    login_as(client, "admin@example.com")
    resp = client.post(f"/admin/users/{end.id}/permissions", data={"permissions": MANAGE_REQUEST_TEMPLATES})
    assert resp.status_code == 400


def test_a_permitted_agent_reaches_request_types_and_nothing_else_in_admin(client, db_session):
    as_admin(client, db_session)
    agent = make_user(client, db_session, "a@example.com", UserRole.AGENT)
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    login_as(client, "a@example.com")
    page = client.get("/admin/request-types")
    assert page.status_code == 200
    assert 'href="/admin/request-types"' in page.text
    assert "/admin/users" not in page.text and "/admin/settings" not in page.text
    for url in ("/admin/users", "/admin/approval-groups", "/admin/settings", "/admin/teams"):
        assert client.get(url).status_code == 403
    assert client.post("/admin/request-types", data={"name": "x"}).status_code != 403


def test_without_the_permission_no_admin_link_and_request_types_refused(client, db_session):
    make_user(client, db_session, "a@example.com", UserRole.AGENT)
    login_as(client, "a@example.com")
    assert client.get("/admin/request-types").status_code == 403
    assert 'href="/admin/' not in client.get("/tickets").text


def test_changing_the_role_to_end_user_drops_the_grants(client, db_session):
    as_admin(client, db_session)
    agent = make_user(client, db_session, "a@example.com", UserRole.AGENT)
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    login_as(client, "admin@example.com")
    client.post(f"/admin/users/{agent.id}", data={"role": "end_user"})
    assert db_session.exec(select(UserPermission)).all() == []

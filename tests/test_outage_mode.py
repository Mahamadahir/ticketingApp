"""Outage mode (#164): emergency passwords, switching it on and off, the sign-in, the sessions it makes, and what
switching off undoes."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import emergency, mail, outage
from app.auth import MAX_FAILED_LOGINS, hash_password
from app.breakglass import create_account
from app.config import settings
from app.main import app
from app.models import ActivityLog, AuditLog, Notification, OutboundEmail, Setting, User, UserRole, UserSession
from app.settings_store import EMERGENCY_PASSWORD_MIN_LENGTH, set_setting
from fastapi.testclient import TestClient
from tests.conftest import login_as, set_role
from tests.test_break_glass import sign_in as break_glass_sign_in
from tests.test_sessions import sso_setup, sso_sign_in

PASSWORD = "correct horse battery"


def new_client() -> TestClient:
    return TestClient(app)


def make_agent(client, db_session, email="agent@example.com", role=UserRole.AGENT, password=PASSWORD) -> int:
    """A signed-in agent (dev sign-in) who has set an emergency password through the page."""
    user_id = login_as(client, email)
    set_role(db_session, user_id, role)
    if password:
        response = client.post("/settings/emergency-password", data={"password": password, "confirm": password}, follow_redirects=False)
        assert response.status_code == 303, response.text
    return user_id


def reload(db_session, user_id) -> User:
    db_session.expire_all()
    return db_session.get(User, user_id)


def audits(db_session, action):
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def emergency_sign_in(client, email="agent@example.com", password=PASSWORD):
    return client.post("/auth/emergency-login", data={"upn": email, "password": password}, follow_redirects=False)


def signed_in(response) -> bool:
    return response.headers["location"] == "/"


@pytest.fixture
def break_glass(db_session):
    return create_account(db_session, "breakglass1")


@pytest.fixture
def on(client, db_session, break_glass):
    """Outage mode switched on by the break-glass account; returns the break-glass client."""
    glass = new_client()
    assert signed_in(break_glass_sign_in(glass, break_glass))
    assert glass.post("/admin/outage/on", follow_redirects=False).status_code == 303
    assert outage.is_on(db_session)
    return glass


# --- setting and changing a password ---------------------------------------------------------------------

def test_an_agent_sets_an_emergency_password_stored_hashed_and_audited(client, db_session):
    user_id = make_agent(client, db_session)
    user = reload(db_session, user_id)
    assert user.emergency_password_hash.startswith("$2") and PASSWORD not in user.emergency_password_hash
    assert user.emergency_password_set_at is not None and user.emergency_password_used_at is None
    first_hash = user.emergency_password_hash
    [entry] = audits(db_session, "emergency_password_set")
    assert PASSWORD not in (entry.detail or "") and entry.actor_email == "agent@example.com"

    client.post("/settings/emergency-password", data={"password": "another long password", "confirm": "another long password"})
    assert len(audits(db_session, "emergency_password_changed")) == 1
    assert reload(db_session, user_id).emergency_password_hash != first_hash


def test_the_password_rules(client, db_session, break_glass):
    make_agent(client, db_session, password=None)
    def post(password, confirm=None):
        return client.post("/settings/emergency-password", data={"password": password, "confirm": confirm or password}, follow_redirects=False)

    assert "at least 14" in post("short").text  # the placeholder minimum
    assert "not the same" in post("a long enough password", "a different long one").text
    assert "at most 72" in post("x" * 73).text
    assert post("x" * 14).status_code == 303
    set_setting(db_session, EMERGENCY_PASSWORD_MIN_LENGTH, "20")
    db_session.commit()
    assert "at least 20" in post("x" * 19).text
    # Never a break-glass password.
    set_setting(db_session, EMERGENCY_PASSWORD_MIN_LENGTH, "14")
    db_session.commit()
    refused = post(break_glass.password)
    assert refused.status_code == 400 and "cannot be used" in refused.text
    assert not db_session.exec(select(AuditLog).where(AuditLog.action == "emergency_password_set", AuditLog.detail.contains(break_glass.password))).all()


def test_end_users_get_no_emergency_password(client, db_session):
    login_as(client, "enduser@example.com")
    assert client.get("/settings/emergency-password").status_code == 403
    assert client.post("/settings/emergency-password", data={"password": PASSWORD, "confirm": PASSWORD}).status_code == 403


def test_changing_a_role_to_end_user_clears_the_password(client, db_session):
    user_id = make_agent(client, db_session)
    admin = new_client()
    admin_id = login_as(admin, "admin@example.com")
    assert admin.post(f"/admin/users/{user_id}", data={"role": "end_user"}, follow_redirects=False).status_code == 303
    assert reload(db_session, user_id).emergency_password_hash is None
    assert len(audits(db_session, "emergency_password_invalidated")) == 1


# --- refused while outage mode is off ----------------------------------------------------------------------

def test_the_emergency_password_is_refused_while_outage_mode_is_off(client, db_session):
    user_id = make_agent(client, db_session)
    fresh = new_client()
    response = emergency_sign_in(fresh)
    assert response.headers["location"].startswith("/auth/login?error=")
    assert db_session.exec(select(UserSession).where(UserSession.emergency)).all() == []
    assert reload(db_session, user_id).failed_login_count == 0  # an attempt while off is not a strike
    assert emergency.attempt_emergency_login(db_session, "agent@example.com", PASSWORD).failure == "outage_off"  # refused by the check itself
    emergency_sign_in(fresh, password="not the password at all")
    assert reload(db_session, user_id).failed_login_count == 0 and db_session.exec(select(ActivityLog).where(ActivityLog.event_type == "login_failed")).all() == []
    assert "Emergency sign-in" not in fresh.get("/auth/login").text
    assert "Outage mode is on" not in fresh.get("/auth/login").text


# --- switching on -------------------------------------------------------------------------------------------

def test_an_ordinary_super_admin_cannot_switch_it_on(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    page = client.get("/admin/outage")
    assert "cannot switch it on" in page.text and "Switch outage mode on" not in page.text
    response = client.post("/admin/outage/on", follow_redirects=False)
    assert "Only+a+break-glass" in response.headers["location"]
    assert not outage.is_on(db_session)


def test_agents_and_end_users_cannot_reach_the_switch(client, db_session):
    make_agent(client, db_session)
    assert client.get("/admin/outage").status_code == 403
    assert client.post("/admin/outage/on").status_code == 403
    assert client.post("/admin/outage/off").status_code == 403


def test_a_break_glass_account_switches_it_on_audited_and_belled(client, db_session, break_glass):
    admin_id = login_as(client, "admin@example.com")  # a super admin, via SUPER_ADMIN_EMAILS
    other_id = login_as(new_client(), "second@example.com")
    set_role(db_session, other_id, UserRole.SUPER_ADMIN)
    glass = new_client()
    assert signed_in(break_glass_sign_in(glass, break_glass))
    response = glass.post("/admin/outage/on", follow_redirects=False)
    assert response.status_code == 303 and "Outage+mode+is+on" in response.headers["location"]

    [entry] = audits(db_session, "outage_mode_on")
    assert entry.actor_email == break_glass.email
    db_session.expire_all()
    belled = {n.user_id for n in db_session.exec(select(Notification).where(Notification.kind == "outage_mode")).all()}
    assert {admin_id, other_id} <= belled  # every active super admin; the break-glass account is not told of itself
    glass_id = db_session.exec(select(User).where(User.email == break_glass.email)).one().id
    assert glass_id not in belled
    assert "Outage mode is on" in client.get("/notifications").text or "Outage mode was switched on" in client.get("/notifications").text

    again = glass.post("/admin/outage/on", follow_redirects=False)
    assert "already+on" in again.headers["location"]
    assert len(audits(db_session, "outage_mode_on")) == 1


def test_the_banner_is_on_every_page_and_the_login_form_appears(client, db_session, on):
    anonymous = new_client()
    login = anonymous.get("/auth/login")
    assert "Outage mode is on" in login.text and 'action="/auth/emergency-login"' in login.text
    assert "Outage mode is on" in anonymous.get("/auth/break-glass").text
    login_as(client, "agent@example.com")
    for path in ("/portal", "/settings/notifications"):
        assert "Outage mode is on" in client.get(path).text, path


# --- signing in ---------------------------------------------------------------------------------------------

def test_an_agent_signs_in_with_the_emergency_password_during_an_outage(client, db_session, break_glass):
    user_id = make_agent(client, db_session)
    glass = new_client()
    assert signed_in(break_glass_sign_in(glass, break_glass))
    glass.post("/admin/outage/on")

    fresh = new_client()
    response = emergency_sign_in(fresh)
    assert signed_in(response)
    assert fresh.get("/portal").status_code == 200
    db_session.expire_all()
    [row] = db_session.exec(select(UserSession).where(UserSession.emergency)).all()
    assert row.user_id == user_id and not row.break_glass
    assert reload(db_session, user_id).emergency_password_used_at is not None
    [activity] = db_session.exec(select(ActivityLog).where(ActivityLog.event_type == "login_success", ActivityLog.detail == "emergency")).all()
    assert activity.actor_email == "agent@example.com"
    assert len(audits(db_session, "emergency_sign_in")) == 1


def test_a_super_admin_can_use_it_too_and_the_upn_is_not_case_sensitive(client, db_session, on):
    make_agent(client, db_session, email="boss@example.com", role=UserRole.SUPER_ADMIN)
    assert signed_in(emergency_sign_in(new_client(), email="  Boss@Example.com "))


def test_wrong_passwords_lock_the_account_like_a_local_account(client, db_session, on):
    user_id = make_agent(client, db_session)
    fresh = new_client()
    for _ in range(MAX_FAILED_LOGINS):
        assert not signed_in(emergency_sign_in(fresh, password="wrong wrong wrong"))
    assert reload(db_session, user_id).locked_until is not None
    assert not signed_in(emergency_sign_in(fresh))  # the right one is refused while locked
    db_session.expire_all()
    kinds = {row.event_type for row in db_session.exec(select(ActivityLog)).all()}
    assert {"login_failed", "account_locked"} <= kinds
    failed = db_session.exec(select(ActivityLog).where(ActivityLog.event_type == "login_failed")).all()
    assert all((row.detail or "").startswith("emergency") for row in failed if row.actor_email == "agent@example.com")
    assert db_session.exec(select(UserSession).where(UserSession.emergency)).all() == []


def test_the_form_is_rate_limited(client, db_session, on, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_emergency_per_minute", 3)
    fresh = new_client()
    for _ in range(3):
        assert emergency_sign_in(fresh, email="nobody@example.com", password="x").status_code == 303
    assert emergency_sign_in(fresh, email="nobody@example.com", password="x").status_code == 429


def test_end_users_unknown_names_and_agents_with_no_password_are_refused(client, db_session, on):
    end_user_id = login_as(new_client(), "enduser@example.com")
    # Even if a hash were somehow there, an end user is refused.
    user = db_session.get(User, end_user_id)
    user.emergency_password_hash = hash_password(PASSWORD)
    db_session.add(user)
    db_session.commit()
    no_password_id = login_as(new_client(), "nopassword@example.com")
    set_role(db_session, no_password_id, UserRole.AGENT)
    fresh = new_client()
    for email in ("enduser@example.com", "nobody@example.com", "nopassword@example.com"):
        assert not signed_in(emergency_sign_in(fresh, email=email)), email
    assert db_session.exec(select(UserSession).where(UserSession.emergency)).all() == []


def test_a_break_glass_account_cannot_use_the_emergency_form(client, db_session, on, break_glass):
    assert not signed_in(emergency_sign_in(new_client(), email=break_glass.email, password=break_glass.password))


def test_an_inactive_agent_is_refused_and_the_password_is_not_marked_used(client, db_session, on):
    user_id = make_agent(client, db_session)
    user = db_session.get(User, user_id)
    user.is_active = False
    db_session.add(user)
    db_session.commit()
    response = emergency_sign_in(new_client())
    assert not signed_in(response)
    assert db_session.exec(select(UserSession).where(UserSession.emergency)).all() == []
    assert reload(db_session, user_id).emergency_password_used_at is None
    assert len(audits(db_session, "sign_in_refused")) == 1


def test_an_emergency_password_is_not_a_local_password(client, db_session, on):
    make_agent(client, db_session)
    response = new_client().post("/auth/local-login", data={"email": "agent@example.com", "password": PASSWORD}, follow_redirects=False)
    assert "error" in response.headers["location"]


def test_a_sign_in_that_loses_the_race_with_switching_off_is_refused(client, db_session, break_glass):
    user_id = make_agent(client, db_session)
    glass = new_client()
    break_glass_sign_in(glass, break_glass)
    glass.post("/admin/outage/on")
    # The password is checked while the mode is on...
    result = emergency.attempt_emergency_login(db_session, "agent@example.com", PASSWORD)
    assert result.user is not None
    # ...then it is switched off before the session starts.
    glass.post("/admin/outage/off")
    db_session.expire_all()

    class FakeRequest:
        session: dict = {}
        client = None
        headers: dict = {}

    assert emergency.start_emergency_session(db_session, FakeRequest(), db_session.get(User, user_id)) is None
    assert db_session.exec(select(UserSession).where(UserSession.emergency)).all() == []


# --- switching off ------------------------------------------------------------------------------------------

def test_switching_off_ends_emergency_sessions_and_clears_used_passwords(client, db_session, break_glass, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")
    used_id = make_agent(client, db_session, email="used@example.com")
    unused_id = make_agent(new_client(), db_session, email="unused@example.com")
    inactive_id = make_agent(new_client(), db_session, email="inactive@example.com")
    glass = new_client()
    break_glass_sign_in(glass, break_glass)
    glass.post("/admin/outage/on")

    emergency_client = new_client()
    assert signed_in(emergency_sign_in(emergency_client, email="used@example.com"))
    inactive_client = new_client()
    assert signed_in(emergency_sign_in(inactive_client, email="inactive@example.com"))
    inactive = db_session.get(User, inactive_id)
    inactive.is_active = False
    db_session.add(inactive)
    db_session.commit()
    ordinary = new_client()
    login_as(ordinary, "colleague@example.com")  # a normal session, which must survive
    assert emergency_client.get("/portal").status_code == 200

    response = glass.post("/admin/outage/off", follow_redirects=False)
    assert "2+emergency+session" in response.headers["location"] and "2+emergency+password" in response.headers["location"]

    assert not outage.is_on(db_session)
    assert emergency_client.get("/portal", follow_redirects=False).status_code in (302, 303, 307, 401)  # signed out
    assert ordinary.get("/portal").status_code == 200
    assert glass.get("/admin/outage").status_code == 200  # the break-glass session is not an emergency session
    used = reload(db_session, used_id)
    assert used.emergency_password_hash is None and used.emergency_password_used_at is None
    assert reload(db_session, unused_id).emergency_password_hash is not None  # never used: kept
    assert len(audits(db_session, "emergency_password_invalidated")) == 2
    [off] = audits(db_session, "outage_mode_off")
    assert "2 emergency session(s) ended; 2 emergency password(s)" in off.detail

    # The password no longer works, even if outage mode is switched on again.
    glass.post("/admin/outage/on")
    assert not signed_in(emergency_sign_in(new_client(), email="used@example.com"))

    # An email goes to the active agent only, from the admin template, about no ticket.
    glass.post("/admin/outage/off")
    queued = db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "emergency_reset")).all()
    addresses = sorted(row.to_address for row in queued)
    assert addresses == ["used@example.com"]
    assert all(row.ticket_id is None for row in queued)
    sent, failed = mail.deliver_mail(db_session)
    assert (sent, failed) == (1, 0)
    db_session.expire_all()
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "emergency_reset")).all()
    assert row.subject == "Outage mode has ended: set a new emergency password"
    assert "/settings/emergency-password" in row.body and "Hello" in row.body


def test_the_reset_email_is_dropped_once_a_new_password_is_set(client, db_session, break_glass, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    user_id = make_agent(client, db_session)
    glass = new_client()
    break_glass_sign_in(glass, break_glass)
    glass.post("/admin/outage/on")
    agent = new_client()
    emergency_sign_in(agent)
    glass.post("/admin/outage/off")
    login_as(client, "agent@example.com")
    client.post("/settings/emergency-password", data={"password": "brand new password", "confirm": "brand new password"})
    assert mail.deliver_mail(db_session) == (0, 1)
    db_session.expire_all()
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "emergency_reset")).all()
    assert "already set" in row.last_error


def test_any_super_admin_can_switch_it_off(client, db_session, on):
    login_as(client, "admin@example.com")
    page = client.get("/admin/outage")
    assert "Switch outage mode off" in page.text
    response = client.post("/admin/outage/off", follow_redirects=False)
    assert response.status_code == 303 and not outage.is_on(db_session)
    assert "already+off" in client.post("/admin/outage/off", follow_redirects=False).headers["location"]
    assert "Outage mode is on" not in new_client().get("/auth/login").text


def test_the_super_admins_are_belled_when_it_is_switched_off(client, db_session, on):
    admin_id = login_as(client, "admin@example.com")
    other = new_client()
    other_id = login_as(other, "second@example.com")
    set_role(db_session, other_id, UserRole.SUPER_ADMIN)
    other.post("/admin/outage/off")
    db_session.expire_all()
    texts = [n.text for n in db_session.exec(select(Notification).where(Notification.user_id == admin_id, Notification.kind == "outage_mode")).all()]
    assert any("switched off" in text for text in texts)


def test_an_emergency_session_does_not_outlive_the_mode_even_if_not_revoked(client, db_session, on):
    make_agent(client, db_session)
    session_client = new_client()
    assert signed_in(emergency_sign_in(session_client))
    db_session.exec(Setting.__table__.delete().where(Setting.key == outage.OUTAGE_KEY))  # off by some path that revoked nothing
    db_session.commit()
    assert db_session.exec(select(UserSession).where(UserSession.emergency, UserSession.revoked_at.is_(None))).all()
    assert session_client.get("/portal", follow_redirects=False).status_code in (302, 303, 307, 401)


# --- the prompt at Entra sign-in ---------------------------------------------------------------------------

def test_an_agent_without_an_emergency_password_is_prompted_at_sso_sign_in(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch)
    agent_id = login_as(new_client(), "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    user = db_session.get(User, agent_id)
    user.entra_object_id = "oid-agent"
    db_session.add(user)
    db_session.commit()
    response = sso_sign_in(client, seen, preferred_username="agent@example.com", name="Agent", oid="oid-agent")
    assert response.headers["location"] == "/settings/emergency-password?first=1"
    page = client.get(response.headers["location"])
    assert "do not have an emergency password yet" in page.text and "Skip for now" in page.text

    client.post("/settings/emergency-password", data={"password": PASSWORD, "confirm": PASSWORD})
    again = sso_sign_in(new_client(), seen, preferred_username="agent@example.com", name="Agent", oid="oid-agent")
    assert again.headers["location"] == "/"


def test_end_users_are_not_prompted_and_a_cleared_password_prompts_again(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch)
    end_user = sso_sign_in(client, seen, preferred_username="enduser@example.com", name="End User", oid="oid-end")
    assert end_user.headers["location"] == "/"

    agent_id = login_as(new_client(), "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    user = db_session.get(User, agent_id)
    user.entra_object_id = "oid-agent"
    user.emergency_password_hash = hash_password(PASSWORD)
    user.emergency_password_used_at = datetime.now(timezone.utc)
    db_session.add(user)
    db_session.commit()
    assert sso_sign_in(new_client(), seen, preferred_username="agent@example.com", name="Agent", oid="oid-agent").headers["location"] == "/"
    # An outage ends: the used password is cleared, so the next SSO sign-in asks again.
    admin = new_client()
    login_as(admin, "admin@example.com")
    outage_row = Setting(key=outage.OUTAGE_KEY, value="{}")
    db_session.add(outage_row)
    db_session.commit()
    admin.post("/admin/outage/off")
    assert sso_sign_in(new_client(), seen, preferred_username="agent@example.com", name="Agent", oid="oid-agent").headers["location"] == "/settings/emergency-password?first=1"


# --- the admin page -----------------------------------------------------------------------------------------

def test_the_admin_page_says_who_has_no_password_yet(client, db_session):
    make_agent(new_client(), db_session, email="ready@example.com")
    make_agent(new_client(), db_session, email="notready@example.com", password=None)
    login_as(client, "admin@example.com")
    text = client.get("/admin/outage").text
    assert "notready@example.com" in text and "ready@example.com)" not in text.replace("notready@example.com)", "")
    assert "Outage mode is off" in text


def test_a_setting_row_that_cannot_be_read_still_counts_as_on(db_session):
    db_session.add(Setting(key=outage.OUTAGE_KEY, value="not json"))
    db_session.commit()
    assert outage.is_on(db_session) and outage.status(db_session).since is None

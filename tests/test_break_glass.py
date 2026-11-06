"""Break-glass accounts (#163): the command, the TOTP rules, the sign-in form, its audit and bell entries."""

import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import breakglass
from app.auth import MAX_FAILED_LOGINS
from app.breakglass import BreakGlassError, create_account, matching_step, reset_account, totp_code, unlock_account
from app.main import app
from app.models import ActivityLog, AuditLog, Notification, User, UserRole, UserSession
from app.settings_store import BREAK_GLASS_IDLE_MINUTES, SESSION_IDLE_MINUTES, set_setting
from tests.conftest import login_as, set_role

NAME = "breakglass1"
EMAIL = "breakglass1@local.invalid"


def secret_of(issued) -> bytes:
    text = issued.totp_secret
    return base64.b32decode(text + "=" * (-len(text) % 8))


def sign_in(client, issued, code=None, password=None, username=None, when=None):
    code = code if code is not None else totp_code(secret_of(issued), when or datetime.now(timezone.utc))
    return client.post(
        "/auth/break-glass",
        data={"username": username or issued.email, "password": password or issued.password, "code": code},
        follow_redirects=False,
    )


def ok(response) -> bool:
    return response.headers["location"] == "/"


def audits(db_session, action):
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).where(AuditLog.action == action)).all())


@pytest.fixture
def issued(db_session):
    return create_account(db_session, NAME)


def reload(db_session) -> User:
    db_session.expire_all()
    return db_session.exec(select(User).where(User.email == EMAIL)).one()


# --- TOTP ---------------------------------------------------------------------------------------------

def test_totp_matches_the_rfc_6238_vectors():
    secret = b"12345678901234567890"  # RFC 6238 appendix B, SHA-1; the RFC prints 8 digits, we use the last 6
    for seconds, expected in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
        assert totp_code(secret, datetime.fromtimestamp(seconds, timezone.utc)) == expected


def test_one_step_either_side_is_accepted_and_two_are_not():
    secret = b"12345678901234567890"
    now = datetime.fromtimestamp(1111111109, timezone.utc)
    for offset, accepted in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
        code = totp_code(secret, now + timedelta(seconds=30 * offset))
        assert (matching_step(secret, code, now) is not None) is accepted


def test_a_code_with_spaces_or_too_short_is_handled():
    secret = b"12345678901234567890"
    now = datetime.now(timezone.utc)
    code = totp_code(secret, now)
    assert matching_step(secret, code[:3] + " " + code[3:], now) is not None
    assert matching_step(secret, code[:5], now) is None
    assert matching_step(secret, "", now) is None


# --- The command --------------------------------------------------------------------------------------

def test_create_makes_a_local_super_admin_with_a_long_password_and_totp(db_session, issued):
    user = reload(db_session)
    assert (user.role, user.is_break_glass, user.is_active, user.entra_object_id) == (UserRole.SUPER_ADMIN, True, True, None)
    assert issued.email == EMAIL and len(issued.password) >= 24
    assert len(secret_of(issued)) == 20
    uri = issued.uri
    assert uri.startswith("otpauth://totp/") and f"secret={issued.totp_secret}" in uri
    assert "algorithm=SHA1" in uri and "digits=6" in uri and "period=30" in uri


def test_nothing_is_stored_in_plain_text(db_session, issued):
    user = reload(db_session)
    stored = " ".join([user.password_hash, user.totp_secret])
    assert issued.password not in stored and issued.totp_secret not in stored
    assert user.password_hash.startswith("$2")
    assert secret_of(issued) not in user.totp_secret.encode()
    assert breakglass.decrypt_secret(user.totp_secret) == secret_of(issued)
    # nor in the audit log
    text = " ".join(f"{a.action} {a.detail}" for a in db_session.exec(select(AuditLog)).all())
    assert issued.password not in text and issued.totp_secret not in text


def test_a_third_account_is_refused(db_session, issued):
    create_account(db_session, "breakglass2")
    with pytest.raises(BreakGlassError, match="already 2"):
        create_account(db_session, "breakglass3")


def test_create_refuses_a_taken_address_and_a_bad_name(db_session, issued):
    with pytest.raises(BreakGlassError, match="already exists"):
        create_account(db_session, NAME)
    db_session.add(User(email="taken@local.invalid", display_name="x"))
    db_session.commit()
    with pytest.raises(BreakGlassError, match="already exists"):
        create_account(db_session, "taken")
    for bad in ("", "a", "Has Space", "x@evil.example", "../x"):
        with pytest.raises(BreakGlassError):
            create_account(db_session, bad)


def test_reset_replaces_both_secrets_ends_sessions_and_clears_the_lock(client, db_session, issued):
    assert ok(sign_in(client, issued))
    user = reload(db_session)
    user.failed_login_count, user.locked_until = 3, datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.add(user)
    db_session.commit()
    again = reset_account(db_session, NAME)
    assert again.password != issued.password and again.totp_secret != issued.totp_secret
    user = reload(db_session)
    assert (user.failed_login_count, user.locked_until, user.totp_last_step) == (0, None, None)
    assert client.get("/portal", follow_redirects=False).status_code in (302, 303, 307)  # signed out
    other = TestClient(app)
    assert not ok(sign_in(other, issued))  # the old password and secret no longer work
    assert ok(sign_in(other, again))
    assert audits(db_session, "break_glass_reset")


def test_reset_and_unlock_of_an_unknown_account_are_refused(db_session):
    for command in (reset_account, unlock_account):
        with pytest.raises(BreakGlassError, match="No break-glass account"):
            command(db_session, "nobody")


def test_list_and_the_cli(db_session, capsys):
    assert breakglass.main(["list"]) == 0
    assert "No break-glass accounts" in capsys.readouterr().out
    assert breakglass.main(["create", NAME]) == 0
    out = capsys.readouterr().out
    assert "Password:" in out and "TOTP secret:" in out and "otpauth://" in out
    assert breakglass.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert EMAIL in listing and "active" in listing and "Password" not in listing and "otpauth" not in listing
    assert breakglass.main(["create", NAME]) == 1
    assert "already exists" in capsys.readouterr().err
    assert breakglass.main(["unlock", NAME]) == 0


def test_the_changed_session_key_makes_the_secret_unreadable_not_a_lockout(client, db_session, issued, monkeypatch):
    monkeypatch.setattr(breakglass.settings, "session_secret_key", "some-other-key")
    assert not ok(sign_in(client, issued))
    assert reload(db_session).failed_login_count == 0


# --- Sign in ------------------------------------------------------------------------------------------

def test_sign_in_with_password_and_code_starts_a_super_admin_session(client, db_session, issued):
    assert ok(sign_in(client, issued))
    assert client.get("/admin/users").status_code == 200
    [row] = db_session.exec(select(UserSession)).all()
    assert row.break_glass is True


def test_a_wrong_password_or_wrong_code_is_refused_and_counted(client, db_session, issued):
    assert not ok(sign_in(client, issued, password="wrong-password"))
    assert not ok(sign_in(client, issued, code="000000"))
    assert reload(db_session).failed_login_count == 2
    assert client.get("/admin/users", follow_redirects=False).status_code in (302, 303, 307)


def test_a_code_cannot_be_used_twice(client, db_session, issued):
    now = datetime.now(timezone.utc)
    code = totp_code(secret_of(issued), now)
    assert ok(sign_in(client, issued, code=code))
    other = TestClient(app)
    assert not ok(sign_in(other, issued, code=code))
    failed = [a for a in audits(db_session, "break_glass_sign_in") if "code_reused" in a.detail]
    assert failed


def test_an_older_step_is_refused_once_a_newer_one_was_used(client, db_session, issued):
    now = datetime.now(timezone.utc)
    assert ok(sign_in(client, issued, code=totp_code(secret_of(issued), now + timedelta(seconds=30))))
    other = TestClient(app)
    assert not ok(sign_in(other, issued, code=totp_code(secret_of(issued), now)))


def test_a_code_from_two_steps_ago_is_refused(client, issued):
    assert not ok(sign_in(client, issued, when=datetime.now(timezone.utc) - timedelta(seconds=75)))


def test_the_account_locks_like_a_local_account(client, db_session, issued):
    for _ in range(MAX_FAILED_LOGINS):
        assert not ok(sign_in(client, issued, password="wrong"))
    assert reload(db_session).locked_until is not None
    assert not ok(sign_in(client, issued))  # right credentials, still locked
    unlock_account(db_session, NAME)
    assert ok(sign_in(client, issued))


def test_the_ordinary_local_form_refuses_it(client, db_session, issued):
    response = client.post("/auth/local-login", data={"email": EMAIL, "password": issued.password}, follow_redirects=False)
    assert response.headers["location"] != "/"
    assert client.get("/admin/users", follow_redirects=False).status_code in (302, 303, 307)
    assert reload(db_session).failed_login_count == 0


def test_an_inactive_account_gets_no_session(client, db_session, issued):
    user = reload(db_session)
    user.is_active = False
    db_session.add(user)
    db_session.commit()
    assert not ok(sign_in(client, issued))
    assert db_session.exec(select(UserSession)).all() == []


def test_it_works_with_entra_unconfigured_and_never_calls_graph(client, issued, monkeypatch):
    from app import auth

    def boom(*args, **kwargs):
        raise AssertionError("Entra was contacted")

    monkeypatch.setattr(auth, "_msal_app", boom)
    assert ok(sign_in(client, issued))


def test_the_form_is_rate_limited(client, issued):
    from app.config import settings

    codes = [client.post("/auth/break-glass", data={"username": "x", "password": "y", "code": "1"}, follow_redirects=False).status_code
             for _ in range(settings.rate_limit_break_glass_per_minute + 2)]
    assert codes[-1] == 429 and codes[0] != 429


def test_the_page_and_the_link_exist(client):
    assert "Authenticator code" in client.get("/auth/break-glass").text
    assert "/auth/break-glass" in client.get("/auth/login").text


# --- Audit and bell -----------------------------------------------------------------------------------

def test_success_and_failure_are_audited_and_leak_no_secret(client, db_session, issued):
    sign_in(client, issued, password="wrong-password")
    sign_in(client, issued)
    entries = audits(db_session, "break_glass_sign_in")
    assert sorted(e.detail.split(" ")[0] for e in entries) == ["failed", "succeeded"]
    assert all(e.actor_email == EMAIL for e in entries)
    everything = " ".join(f"{a.detail}" for a in db_session.exec(select(AuditLog)).all())
    everything += " ".join(f"{a.detail}" for a in db_session.exec(select(ActivityLog)).all())
    for secret in (issued.password, issued.totp_secret, "wrong-password"):
        assert secret not in everything
    kinds = {a.event_type for a in db_session.exec(select(ActivityLog)).all()}
    assert {"login_failed", "login_success"} <= kinds


def test_an_unknown_name_is_not_stored(client, db_session, issued):
    client.post("/auth/break-glass", data={"username": "ghost@local.invalid", "password": "x", "code": "123456"})
    assert audits(db_session, "break_glass_sign_in") == []


def test_active_super_admins_get_a_bell_entry_for_a_success_only(client, db_session, issued):
    admin = TestClient(app)
    admin_id = login_as(admin, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    agent_id = login_as(TestClient(app), "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)

    sign_in(client, issued, password="wrong-password")
    db_session.expire_all()
    assert db_session.exec(select(Notification).where(Notification.kind == "break_glass_sign_in")).all() == []

    assert ok(sign_in(client, issued))
    db_session.expire_all()
    rows = db_session.exec(select(Notification).where(Notification.kind == "break_glass_sign_in")).all()
    assert {r.user_id for r in rows} >= {admin_id}
    assert agent_id not in {r.user_id for r in rows}
    assert EMAIL in rows[0].text
    assert "Break-glass account" in admin.get("/notifications").text


# --- Sessions -----------------------------------------------------------------------------------------

def test_break_glass_sessions_use_the_shorter_idle_timeout(client, db_session, issued):
    set_setting(db_session, BREAK_GLASS_IDLE_MINUTES, "30")
    set_setting(db_session, SESSION_IDLE_MINUTES, "480")
    db_session.commit()
    assert ok(sign_in(client, issued))
    [row] = db_session.exec(select(UserSession)).all()
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.add(row)
    db_session.commit()
    assert client.get("/admin/users", follow_redirects=False).status_code in (302, 303, 307)

    # an ordinary session idle for the same time is still valid
    ordinary = TestClient(app)
    user_id = login_as(ordinary, "agent@example.com")
    row = db_session.exec(select(UserSession).where(UserSession.user_id == user_id)).one()
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.add(row)
    db_session.commit()
    assert ordinary.get("/portal", follow_redirects=False).status_code == 200


def test_admin_users_marks_it_and_points_to_the_command(client, db_session, issued):
    admin = TestClient(app)
    set_role(db_session, login_as(admin, "admin@example.com"), UserRole.SUPER_ADMIN)
    page = admin.get("/admin/users").text
    assert "(break-glass)" in page and "python -m app.breakglass reset breakglass1" in page
    user_id = reload(db_session).id
    assert admin.post(f"/admin/users/{user_id}", data={"role": "agent"}).status_code == 400
    assert reload(db_session).role == UserRole.SUPER_ADMIN
    assert issued.password not in page and issued.totp_secret not in page

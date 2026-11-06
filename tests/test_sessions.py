"""Server-side sessions and matching users on the Entra object id (#157)."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import select

from app import scheduled_jobs
from app.main import app
from app.models import ActivityLog, AuditLog, User, UserRole, UserSession
from app.sessions import purge_ended_sessions
from app.settings_store import SESSION_IDLE_MINUTES, SESSION_KEEP_DAYS, SESSION_MAX_DAYS, set_setting
from tests.conftest import login_as, set_role

OID = "11111111-1111-1111-1111-111111111111"
OID2 = "22222222-2222-2222-2222-222222222222"


def session_rows(db_session):
    db_session.expire_all()
    return db_session.exec(select(UserSession).order_by(UserSession.created_at)).all()


def signed_in(client: TestClient) -> bool:
    return client.get("/portal", follow_redirects=False).status_code == 200


def audits(db_session, action):
    db_session.expire_all()
    return db_session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def other_client() -> TestClient:
    return TestClient(app)


def sso_setup(monkeypatch):
    from app.config import settings
    from app.routers import auth as auth_router

    seen = {"claims": {}}
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(auth_router, "build_auth_url", lambda state: seen.update(state=state) or "https://login.example/authorize")
    monkeypatch.setattr(auth_router, "acquire_token_by_auth_code", lambda code: {"id_token_claims": seen["claims"]})
    return seen


def sso_sign_in(client, seen, **claims):
    seen["claims"] = claims
    client.get("/auth/sso/start", follow_redirects=False)
    return client.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)


# ---- sessions -------------------------------------------------------------------------------------------


def test_sign_in_creates_a_row_and_the_cookie_holds_only_a_token(client, db_session):
    user_id = login_as(client, "a@example.com")
    [row] = session_rows(db_session)
    assert row.user_id == user_id and row.revoked_at is None
    assert row.expires_at - row.created_at == timedelta(days=14)
    assert client.cookies.get("session")
    assert len(row.id) == 64  # a SHA-256 of the token, never the token itself


def test_the_cookie_alone_is_not_enough_once_the_row_is_gone(client, db_session):
    login_as(client, "a@example.com")
    assert signed_in(client)
    db_session.exec(delete(UserSession))
    db_session.commit()
    assert not signed_in(client)


def test_logout_revokes_the_server_row_so_a_saved_cookie_stops_working(client, db_session):
    login_as(client, "a@example.com")
    saved = dict(client.cookies)
    client.post("/auth/logout")
    [row] = session_rows(db_session)
    assert row.revoked_at is not None
    replay = other_client()
    for name, value in saved.items():
        replay.cookies.set(name, value)
    assert not signed_in(replay)


def test_an_idle_session_ends_and_activity_keeps_it_alive(client, db_session):
    login_as(client, "a@example.com")
    [row] = session_rows(db_session)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=479)
    db_session.add(row)
    db_session.commit()
    assert signed_in(client)  # still inside 8 hours, and last seen moves on
    assert session_rows(db_session)[0].last_seen_at > datetime.now(timezone.utc) - timedelta(minutes=1)

    row = session_rows(db_session)[0]
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=481)
    db_session.add(row)
    db_session.commit()
    response = client.get("/portal", follow_redirects=False)
    assert response.status_code in (302, 307) and response.headers["location"].startswith("/auth/login")
    assert "signed+out" in response.headers["location"]


def test_the_idle_timeout_is_an_admin_setting(client, db_session):
    login_as(client, "a@example.com")
    set_setting(db_session, SESSION_IDLE_MINUTES, "10")
    db_session.commit()
    [row] = session_rows(db_session)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db_session.add(row)
    db_session.commit()
    assert not signed_in(client)


def test_the_absolute_lifetime_is_fixed_at_sign_in_and_is_a_setting(client, db_session):
    set_setting(db_session, SESSION_MAX_DAYS, "3")
    db_session.commit()
    login_as(client, "a@example.com")
    [row] = session_rows(db_session)
    assert row.expires_at - row.created_at == timedelta(days=3)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.add(row)
    db_session.commit()
    assert not signed_in(client)


def test_last_seen_is_written_at_most_once_a_minute(client, db_session):
    login_as(client, "a@example.com")
    [row] = session_rows(db_session)
    stamp = row.last_seen_at
    assert signed_in(client) and signed_in(client)
    assert session_rows(db_session)[0].last_seen_at == stamp  # inside the minute: untouched


def test_signing_in_again_replaces_the_session_and_ends_the_old_one(client, db_session):
    login_as(client, "a@example.com")
    login_as(client, "b@example.com")
    first, second = session_rows(db_session)
    assert first.revoked_at is not None and second.revoked_at is None


# ---- sign out everywhere -----------------------------------------------------------------------------------


def test_sign_out_everywhere_from_my_settings_ends_every_browser_and_is_audited(client, db_session):
    user_id = login_as(client, "a@example.com")
    second = other_client()
    login_as(second, "a@example.com")
    third = other_client()
    login_as(third, "someone-else@example.com")

    response = client.post("/settings/sign-out-everywhere", follow_redirects=False)
    assert response.status_code == 303
    assert not signed_in(client) and not signed_in(second)
    assert signed_in(third)  # nobody else is affected
    [entry] = audits(db_session, "signed_out_everywhere")
    assert entry.entity_id == user_id and entry.actor_email == "a@example.com"


def test_a_super_admin_can_sign_a_user_out_everywhere_and_it_is_audited(client, db_session):
    login_as(client, "admin@example.com")
    victim = other_client()
    victim_id = login_as(victim, "victim@example.com")
    response = client.post(f"/admin/users/{victim_id}/sign-out", follow_redirects=False)
    assert response.status_code == 303
    assert not signed_in(victim)
    assert signed_in(client)
    [entry] = audits(db_session, "signed_out_everywhere")
    assert (entry.entity_id, entry.actor_email) == (victim_id, "admin@example.com")


def test_only_a_super_admin_can_sign_someone_else_out(client, db_session):
    login_as(client, "plain@example.com")
    victim = other_client()
    victim_id = login_as(victim, "victim@example.com")
    assert client.post(f"/admin/users/{victim_id}/sign-out", follow_redirects=False).status_code == 403
    assert signed_in(victim)


# ---- inactive users and role changes -----------------------------------------------------------------------


def test_deactivating_a_user_ends_their_sessions_at_once_and_refuses_new_sign_ins(client, db_session):
    login_as(client, "admin@example.com")
    victim = other_client()
    victim_id = login_as(victim, "victim@example.com")
    client.post(f"/admin/users/{victim_id}/active", data={"is_active": "false"})
    # revoked by the deactivation itself, before the victim makes another request
    victim_rows = [row for row in session_rows(db_session) if row.user_id == victim_id]
    assert victim_rows and all(row.revoked_at is not None for row in victim_rows)
    assert not signed_in(victim)
    assert audits(db_session, "sessions_revoked")

    again = other_client()
    response = again.post(
        "/auth/dev-login", data={"email": "victim@example.com", "display_name": "V"}, follow_redirects=False
    )
    assert response.status_code == 303 and "deactivated" in response.headers["location"]
    assert not signed_in(again)
    assert audits(db_session, "sign_in_refused")
    assert db_session.exec(select(ActivityLog).where(ActivityLog.detail == "inactive_account")).first()

    client.post(f"/admin/users/{victim_id}/active", data={"is_active": "true"})
    assert login_as(other_client(), "victim@example.com") == victim_id


def test_an_inactive_user_with_a_live_session_is_refused_even_if_nothing_revoked_it(client, db_session):
    user_id = login_as(client, "a@example.com")
    user = db_session.get(User, user_id)
    user.is_active = False  # set behind the admin page's back, as a future nightly job would
    db_session.add(user)
    db_session.commit()
    response = client.get("/portal", follow_redirects=False)
    assert response.status_code in (302, 307) and "deactivated" in response.headers["location"]
    assert all(row.revoked_at is not None for row in session_rows(db_session))
    assert audits(db_session, "session_refused")


def test_a_local_account_that_is_inactive_cannot_sign_in(client, db_session):
    from app.auth import hash_password

    db_session.add(User(email="local@example.com", display_name="L", password_hash=hash_password("pw-long-enough-1"), is_active=False))
    db_session.commit()
    response = client.post("/auth/local-login", data={"email": "local@example.com", "password": "pw-long-enough-1"}, follow_redirects=False)
    assert "deactivated" in response.headers["location"]
    assert session_rows(db_session) == []
    # a wrong password still gets the usual message, not the deactivation one
    wrong = client.post("/auth/local-login", data={"email": "local@example.com", "password": "nope"}, follow_redirects=False)
    assert "Invalid" in wrong.headers["location"]


def test_changing_a_role_ends_that_users_sessions(client, db_session):
    login_as(client, "admin@example.com")
    agent = other_client()
    agent_id = login_as(agent, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    assert signed_in(agent)
    client.post(f"/admin/users/{agent_id}", data={"role": "end_user"})
    assert not signed_in(agent)
    assert audits(db_session, "sessions_revoked")


def test_saving_the_same_role_does_not_end_sessions(client, db_session):
    login_as(client, "admin@example.com")
    agent = other_client()
    agent_id = login_as(agent, "agent@example.com")
    client.post(f"/admin/users/{agent_id}", data={"role": "end_user"})
    assert signed_in(agent)


# ---- housekeeping ---------------------------------------------------------------------------------------


def test_the_cleanup_job_deletes_ended_rows_after_the_keep_period_only(client, db_session):
    now = datetime.now(timezone.utc)
    user_id = login_as(client, "a@example.com")
    for name, expires, revoked in [
        ("live", now + timedelta(days=1), None),
        ("expired-recently", now - timedelta(days=1), None),
        ("expired-long-ago", now - timedelta(days=40), None),
        ("revoked-recently", now + timedelta(days=5), now - timedelta(days=2)),
        ("revoked-long-ago", now + timedelta(days=5), now - timedelta(days=40)),
    ]:
        db_session.add(UserSession(id=name, user_id=user_id, expires_at=expires, revoked_at=revoked))
    db_session.commit()
    purge_ended_sessions(db_session)
    db_session.commit()
    kept = {row.id for row in session_rows(db_session)}
    assert {"live", "expired-recently", "revoked-recently"} <= kept
    assert not {"expired-long-ago", "revoked-long-ago"} & kept
    assert len(kept) == 4  # and the real sign-in


def test_the_keep_period_is_a_setting(db_session):
    user = User(email="x@example.com", display_name="X")
    db_session.add(user)
    db_session.commit()
    db_session.add(UserSession(id="old", user_id=user.id, expires_at=datetime.now(timezone.utc) - timedelta(days=3)))
    set_setting(db_session, SESSION_KEEP_DAYS, "2")
    db_session.commit()
    purge_ended_sessions(db_session)
    db_session.commit()
    assert session_rows(db_session) == []


def test_the_cleanup_job_is_registered_daily():
    [job] = [job for job in scheduled_jobs.SCHEDULED_JOBS if job.name == "session-cleanup"]
    assert job.every_seconds == 86400 and job.run is purge_ended_sessions


# ---- matching on the Entra object id ----------------------------------------------------------------------


def test_a_first_entra_sign_in_creates_the_user_with_the_oid(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="New@Example.com", name="New Person", oid=OID.upper())
    user = db_session.exec(select(User).where(User.email == "new@example.com")).one()
    assert user.entra_object_id == OID and user.role == UserRole.END_USER
    assert signed_in(client)


def test_an_email_match_binds_the_oid_and_is_audited(client, db_session, monkeypatch):
    db_session.add(User(email="known@example.com", display_name="Known"))
    db_session.commit()
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="known@example.com", name="Known", oid=OID)
    user = db_session.exec(select(User).where(User.email == "known@example.com")).one()
    assert user.entra_object_id == OID
    assert audits(db_session, "oid_bound")


def test_the_oid_wins_over_the_email_and_a_changed_email_and_name_are_refreshed(client, db_session, monkeypatch):
    user = User(email="old@example.com", display_name="Old Name", entra_object_id=OID, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="renamed@example.com", name="New Name", oid=OID)
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert (user.email, user.display_name, user.role) == ("renamed@example.com", "New Name", UserRole.AGENT)
    assert len(db_session.exec(select(User)).all()) == 1  # no second account
    [entry] = audits(db_session, "email_changed")
    assert "old@example.com -> renamed@example.com" in entry.detail
    assert signed_in(client)


def test_an_email_already_bound_to_another_oid_is_refused_and_audited(client, db_session, monkeypatch):
    db_session.add(User(email="taken@example.com", display_name="Original", entra_object_id=OID))
    db_session.commit()
    seen = sso_setup(monkeypatch)
    response = sso_sign_in(client, seen, preferred_username="taken@example.com", name="Impostor", oid=OID2)
    assert response.status_code == 303 and response.headers["location"].startswith("/auth/login?error=")
    assert not signed_in(client)
    assert session_rows(db_session) == []
    assert audits(db_session, "sso_oid_mismatch")
    assert db_session.exec(select(ActivityLog).where(ActivityLog.detail == "oid_mismatch")).first()
    user = db_session.exec(select(User)).one()
    assert (user.entra_object_id, user.display_name) == (OID, "Original")


def test_a_super_admin_can_clear_the_oid_so_a_recreated_account_can_bind(client, db_session, monkeypatch):
    user = User(email="rehired@example.com", display_name="R", entra_object_id=OID)
    db_session.add(user)
    db_session.commit()
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="rehired@example.com", name="R", oid=OID2)
    assert not signed_in(client)

    admin = other_client()
    login_as(admin, "admin@example.com")
    assert admin.post(f"/admin/users/{user.id}/clear-oid", follow_redirects=False).status_code == 303
    assert audits(db_session, "oid_cleared")
    sso_sign_in(client, seen, preferred_username="rehired@example.com", name="R", oid=OID2)
    db_session.expire_all()
    assert db_session.get(User, user.id).entra_object_id == OID2
    assert signed_in(client)


def test_only_a_super_admin_can_clear_an_oid(client, db_session):
    user = User(email="x@example.com", display_name="X", entra_object_id=OID)
    db_session.add(user)
    db_session.commit()
    login_as(client, "agent@example.com")
    assert client.post(f"/admin/users/{user.id}/clear-oid", follow_redirects=False).status_code == 403


def test_a_token_without_an_oid_is_refused(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch)
    response = sso_sign_in(client, seen, preferred_username="a@example.com", name="A")
    assert response.headers["location"].startswith("/auth/login?error=")
    assert db_session.exec(select(User)).all() == []


def test_an_email_change_onto_another_accounts_address_is_refused(client, db_session, monkeypatch):
    db_session.add(User(email="mine@example.com", display_name="Mine", entra_object_id=OID))
    db_session.add(User(email="theirs@example.com", display_name="Theirs"))
    db_session.commit()
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="theirs@example.com", name="Mine", oid=OID)
    assert not signed_in(client)
    assert audits(db_session, "sso_email_conflict")


def test_a_deactivated_user_cannot_sign_in_through_entra(client, db_session, monkeypatch):
    db_session.add(User(email="gone@example.com", display_name="Gone", entra_object_id=OID, is_active=False))
    db_session.commit()
    seen = sso_setup(monkeypatch)
    response = sso_sign_in(client, seen, preferred_username="gone@example.com", name="Gone", oid=OID)
    assert "deactivated" in response.headers["location"]
    assert session_rows(db_session) == []
    assert audits(db_session, "sign_in_refused")


def test_the_admin_users_page_shows_the_oid_and_session_count(client, db_session):
    db_session.add(User(email="x@example.com", display_name="X", entra_object_id=OID))
    db_session.commit()
    login_as(client, "admin@example.com")
    text = client.get("/admin/users").text
    assert OID in text and "Sign out everywhere" in text and "1 signed-in session" in text


def test_a_very_short_idle_timeout_still_keeps_an_active_session_alive(client, db_session):
    login_as(client, "a@example.com")
    set_setting(db_session, SESSION_IDLE_MINUTES, "1")
    db_session.commit()
    [row] = session_rows(db_session)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=40)
    db_session.add(row)
    db_session.commit()
    assert signed_in(client)
    assert session_rows(db_session)[0].last_seen_at > datetime.now(timezone.utc) - timedelta(seconds=10)


def test_a_refused_entra_sign_in_changes_nothing_on_the_account(client, db_session, monkeypatch):
    db_session.add(User(email="mine@example.com", display_name="Mine", entra_object_id=OID))
    db_session.add(User(email="theirs@example.com", display_name="Theirs"))
    db_session.commit()
    seen = sso_setup(monkeypatch)
    sso_sign_in(client, seen, preferred_username="theirs@example.com", name="Renamed", oid=OID)
    mine = db_session.exec(select(User).where(User.entra_object_id == OID)).one()
    assert (mine.email, mine.display_name) == ("mine@example.com", "Mine")

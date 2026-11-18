"""A cancelled or refused Microsoft sign-in gets a page, not FastAPI's JSON (#251)."""

import pytest
from sqlmodel import select

from app.config import settings
from app.models import ActivityLog, AuditLog, LinkedIdentity, User, UserSession
from app.routers import auth as auth_router
from app.routers import link_account
from tests.conftest import login_as

CANCELLED = "Sign-in was cancelled."
NOT_FINISHED = "Microsoft sign-in did not finish. Try again, or contact IT if it keeps happening."
SECRET_TEXT = "AADSTS65004 for jane.doe@secret.example"


@pytest.fixture
def seen(monkeypatch):
    state = {}
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(auth_router, "build_auth_url", lambda s: state.setdefault("state", s) and "https://login.example/authorize")
    monkeypatch.setattr(
        link_account, "build_auth_url", lambda s, prompt=None: state.update(link_state=s) or "https://login.example/authorize"
    )

    def no_exchange(code):
        raise AssertionError("no token exchange for a cancelled sign-in")

    monkeypatch.setattr(auth_router, "acquire_token_by_auth_code", no_exchange)
    monkeypatch.setattr(link_account, "acquire_token_by_auth_code", no_exchange)
    return state


def failures(db_session):
    db_session.expire_all()
    return list(db_session.exec(select(ActivityLog).where(ActivityLog.event_type == "sso_failed")).all())


def test_cancel_after_a_started_sign_in_shows_the_message_and_is_recorded(client, db_session, seen):
    client.get("/auth/sso/start", follow_redirects=False)
    response = client.get(
        f"/auth/callback?error=access_denied&error_subcode=cancel&error_description={SECRET_TEXT}&state={seen['state']}",
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/auth/login?error=")
    page = client.get(response.headers["location"])
    assert CANCELLED in page.text and 'role="alert"' in page.text
    assert "AADSTS" not in page.text and "secret.example" not in page.text
    [row] = failures(db_session)
    assert row.detail == "idp_error: access_denied"
    assert "secret.example" not in str(row.detail) + str(db_session.exec(select(AuditLog)).all())
    assert db_session.exec(select(UserSession)).first() is None


def test_another_error_says_it_did_not_finish(client, db_session, seen):
    client.get("/auth/sso/start", follow_redirects=False)
    response = client.get(f"/auth/callback?error=consent_required&state={seen['state']}", follow_redirects=False)
    page = client.get(response.headers["location"])
    assert NOT_FINISHED in page.text
    [row] = failures(db_session)
    assert row.detail == "idp_error: consent_required"


def test_no_code_and_no_error_is_not_a_422(client, db_session, seen):
    response = client.get("/auth/callback", follow_redirects=False)
    assert response.status_code == 303
    assert NOT_FINISHED in client.get(response.headers["location"]).text
    assert failures(db_session) == []  # nothing was started, so no row


def test_a_normal_reply_with_a_wrong_state_is_still_refused(client, seen):
    client.get("/auth/sso/start", follow_redirects=False)
    assert client.get("/auth/callback?code=c&state=forged", follow_redirects=False).status_code == 400


def test_a_cancelled_link_returns_to_the_link_page_and_links_nothing(client, db_session, seen):
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/sign-in", follow_redirects=False)
    response = client.get(
        f"/auth/callback?error=access_denied&error_subcode=cancel&error_description={SECRET_TEXT}&state={seen['link_state']}",
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/settings/link-account?")
    page = client.get(response.headers["location"])
    assert "Linking was cancelled." in page.text and "secret.example" not in page.text
    assert db_session.exec(select(LinkedIdentity)).first() is None
    assert db_session.exec(select(User)).all().__len__() == 1  # still only Ann, no second account was made
    assert client.get("/tickets", follow_redirects=False).status_code != 401  # still signed in as Ann, not someone else


def test_a_forged_error_does_not_end_a_pending_sign_in(client, db_session, seen):
    client.get("/auth/sso/start", follow_redirects=False)
    forged = client.get("/auth/callback?error=access_denied", follow_redirects=False)
    assert forged.status_code == 303 and failures(db_session) == []
    # The real sign-in's state still works (it gets as far as the token exchange, which the fixture forbids).
    with pytest.raises(AssertionError, match="no token exchange"):
        client.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)

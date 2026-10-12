import pytest
from sqlmodel import select

from app import activity
from app.auth import hash_password
from app.models import ActivityLog, Ticket, TicketStatus, User, UserRole
from tests.conftest import create_team, login_as, set_role


def rows(db_session, event_type=None):
    db_session.expire_all()
    query = select(ActivityLog).order_by(ActivityLog.id)
    if event_type:
        query = query.where(ActivityLog.event_type == event_type)
    return db_session.exec(query).all()


def every_text_stored(db_session):
    """All text in every row, to prove something was never written anywhere."""
    columns = ("event_type", "actor_email", "method", "path", "ip", "user_agent", "detail")
    return " | ".join(str(getattr(row, c)) for row in rows(db_session) for c in columns)


def local_user(db_session, email="contractor@example.com", password="hunter2-correct", role=UserRole.AGENT):
    user = User(email=email, display_name="Contractor", role=role, password_hash=hash_password(password))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def sign_in(client, email, password):
    return client.post(
        "/auth/local-login", data={"email": email, "password": password}, follow_redirects=False,
        headers={"User-Agent": "TestBrowser/1.0"},
    )


# ---- Sign-in events -------------------------------------------------------------


def test_a_successful_sign_in_is_recorded_with_where_it_came_from_but_not_the_password(client, db_session):
    user = local_user(db_session)

    sign_in(client, user.email, "hunter2-correct")

    [row] = rows(db_session)
    assert (row.event_type, row.actor_email, row.actor_user_id, row.detail) == ("login_success", user.email, user.id, "local")
    assert row.ip == "testclient" and row.user_agent == "TestBrowser/1.0"
    assert row.path == "/auth/local-login" and row.method == "POST"
    assert "hunter2" not in every_text_stored(db_session)


def test_failed_sign_ins_record_the_real_reason_while_the_person_sees_one_message(client, db_session):
    user = local_user(db_session)
    sso_only = User(email="sso-only@example.com", display_name="SSO", role=UserRole.AGENT)
    db_session.add(sso_only)
    db_session.commit()

    attempts = [
        ("nobody@example.com", "whatever-1", "unknown_account"),
        (sso_only.email, "whatever-2", "not_a_local_account"),
        (user.email, "wrong-password-3", "wrong_password"),
    ]
    locations = set()
    for email, password, _ in attempts:
        locations.add(sign_in(client, email, password).headers["location"])

    assert len(locations) == 1  # identical for every reason, so the form leaks nothing
    failed = rows(db_session, "login_failed")
    assert [(r.actor_email, r.detail) for r in failed] == [(email, reason) for email, _, reason in attempts]
    stored = every_text_stored(db_session)
    assert not any(secret in stored for secret in ("whatever-1", "whatever-2", "wrong-password-3", "hunter2"))


def test_the_lockout_is_recorded_once_when_it_happens_and_later_attempts_say_locked(client, db_session):
    user = local_user(db_session)

    for number in range(6):  # 5 wrong guesses lock the account; the 6th arrives while locked
        sign_in(client, user.email, f"wrong-{number}")

    locked = rows(db_session, "account_locked")
    assert len(locked) == 1 and locked[0].actor_email == user.email and locked[0].detail.startswith("locked until ")
    reasons = [r.detail for r in rows(db_session, "login_failed")]
    assert reasons == ["wrong_password"] * 5 + ["account_locked"]


def test_the_correct_password_on_a_locked_account_is_recorded_as_a_failure(client, db_session):
    user = local_user(db_session)
    for number in range(5):
        sign_in(client, user.email, f"wrong-{number}")

    response = sign_in(client, user.email, "hunter2-correct")

    assert "error" in response.headers["location"]
    assert rows(db_session, "login_failed")[-1].detail == "account_locked"
    assert rows(db_session, "login_success") == []


def test_dev_sign_in_is_recorded(client, db_session):
    login_as(client, "dev-user@example.com")

    [row] = rows(db_session, "login_success")
    assert (row.actor_email, row.detail) == ("dev-user@example.com", "dev")


def test_sign_out_is_recorded_for_the_signed_in_user_and_not_when_nobody_is(client, db_session):
    client.post("/auth/logout")
    assert rows(db_session, "logout") == []

    user_id = login_as(client, "someone@example.com")
    client.post("/auth/logout")

    [row] = rows(db_session, "logout")
    assert (row.actor_email, row.actor_user_id) == ("someone@example.com", user_id)


def sso_setup(monkeypatch, claims):
    from app.config import settings
    from app.routers import auth as auth_router

    seen = {}
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(auth_router, "build_auth_url", lambda state: seen.setdefault("state", state) and "https://login.example/authorize")
    monkeypatch.setattr(auth_router, "acquire_token_by_auth_code", lambda code: claims)
    return seen


def test_single_sign_on_success_and_each_failure_are_recorded_without_the_one_time_code(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch, {"id_token_claims": {"preferred_username": "sso-user@example.com", "name": "SSO User"}})
    client.get("/auth/sso/start", follow_redirects=False)
    client.get(f"/auth/callback?code=ONE-TIME-CODE-1&state={seen['state']}", follow_redirects=False)
    [ok] = rows(db_session, "login_success")
    assert (ok.actor_email, ok.detail) == ("sso-user@example.com", "sso")

    client.get("/auth/callback?code=ONE-TIME-CODE-2&state=forged", follow_redirects=False)  # no state in the session
    assert [r.detail for r in rows(db_session, "sso_failed")] == ["invalid_state"]
    assert "ONE-TIME-CODE" not in every_text_stored(db_session)


def test_a_sign_in_that_returns_no_email_is_recorded_with_microsofts_error_code_only(client, db_session, monkeypatch):
    seen = sso_setup(monkeypatch, {"error": "invalid_grant", "error_description": "AADSTS70008: secret detail for jane@x"})
    client.get("/auth/sso/start", follow_redirects=False)

    client.get(f"/auth/callback?code=abc&state={seen['state']}", follow_redirects=False)

    assert [r.detail for r in rows(db_session, "sso_failed")] == ["token_error: invalid_grant"]
    assert "AADSTS70008" not in every_text_stored(db_session) and "jane@x" not in every_text_stored(db_session)


def test_record_activity_stores_the_route_template_not_the_address_it_was_called_on(db_session):
    """The sign-in routes have no ids in them, so the dedicated events cannot show
    this on their own. Anything that records activity from a route with a token or
    id in its path must still store only the template."""
    from types import SimpleNamespace

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/feedback/SECRET-TOKEN-123",
        "query_string": b"code=ALSO-SECRET",
        "headers": [(b"user-agent", b"Probe/1.0")],
        "client": ("203.0.113.9", 4242),
        "route": SimpleNamespace(path="/feedback/{token}"),
    }

    activity.record_activity(db_session, Request(scope), "request", status_code=200)
    db_session.commit()

    [row] = rows(db_session)
    assert (row.path, row.ip, row.user_agent) == ("/feedback/{token}", "203.0.113.9", "Probe/1.0")
    assert "SECRET" not in every_text_stored(db_session)

# ---- Request activity ---------------------------------------------------------------


def make_ticket(db_session, team_id):
    ticket = Ticket(subject="Printer", requester_email="me@example.com", team_id=team_id, status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_in(client, db_session, team, email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


def test_state_changing_requests_are_recorded_by_route_template_with_the_actor_and_result(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_id = agent_in(client, db_session, team)

    client.post(f"/tickets/{ticket.id}/note?token=QUERYSECRET", data={"body": "BODYSECRET internal"})

    [row] = rows(db_session, "request")
    assert row.method == "POST" and row.path == "/tickets/{ticket_id}/note"  # the template, never the real address
    assert (row.status_code, row.actor_email, row.actor_user_id) == (200, "agent@example.com", agent_id)
    stored = every_text_stored(db_session)
    assert "QUERYSECRET" not in stored and "BODYSECRET" not in stored and str(ticket.id) not in row.path


def test_a_request_that_crashes_is_still_recorded_as_a_500_and_the_error_still_propagates(client, db_session, monkeypatch):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_in(client, db_session, team)

    def boom(*args, **kwargs):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr("app.routers.tickets.add_note", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/tickets/{ticket.id}/note", data={"body": "x"})

    [row] = rows(db_session, "request")
    assert (row.path, row.status_code) == ("/tickets/{ticket_id}/note", 500)


def test_reading_pages_is_not_recorded(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_in(client, db_session, team)
    before = len(rows(db_session))

    for path in ("/tickets", f"/tickets/{ticket.id}", "/portal", "/health"):
        client.get(path)

    assert len(rows(db_session)) == before


def test_refused_access_is_recorded_for_reads_and_for_writes(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    ticket = make_ticket(db_session, other.id)
    agent_in(client, db_session, mine)

    client.get(f"/tickets/{ticket.id}")
    client.post(f"/tickets/{ticket.id}/status", data={"status": "resolved"})

    denied = rows(db_session, "access_denied")
    assert [(r.method, r.path, r.status_code) for r in denied] == [
        ("GET", "/tickets/{ticket_id}", 403),
        ("POST", "/tickets/{ticket_id}/status", 403),
    ]
    assert all(r.actor_email == "agent@example.com" for r in denied)
    assert rows(db_session, "request") == []  # a refused write is one row, not two


def test_unknown_paths_cannot_flood_the_log(client, db_session):
    login_as(client, "someone@example.com")
    before = len(rows(db_session))

    for path in ("/wp-login.php", "/admin.php", "/tickets/not/a/route"):
        client.post(path, data={"x": "y"})

    assert len(rows(db_session)) == before


def test_sign_in_posts_are_not_also_recorded_as_generic_requests(client, db_session):
    user = local_user(db_session)

    sign_in(client, user.email, "hunter2-correct")
    sign_in(client, user.email, "wrong")
    client.post("/auth/logout")

    assert rows(db_session, "request") == []
    assert [r.event_type for r in rows(db_session)] == ["login_success", "login_failed", "logout"]


def test_an_unauthenticated_request_has_no_actor(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)

    client.post(f"/tickets/{ticket.id}/note", data={"body": "x"}, follow_redirects=False)

    [row] = rows(db_session, "request")
    assert row.actor_email is None and row.actor_user_id is None and row.status_code == 307


def test_a_failure_to_write_the_log_never_breaks_the_request(client, db_session, monkeypatch):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_in(client, db_session, team)

    def explode(*args, **kwargs):
        raise RuntimeError("the log table is on fire")

    monkeypatch.setattr(activity, "_write_request_row", explode)

    response = client.post(f"/tickets/{ticket.id}/note", data={"body": "Still works"})

    assert response.status_code == 200 and "Still works" in response.text


def test_nothing_secret_is_ever_stored_across_a_whole_session(client, db_session):
    user = local_user(db_session)
    sign_in(client, user.email, "hunter2-correct")
    client.post("/webhooks/dev/simulate-inbound", data={"requester_email": "a@x.org", "subject": "SUBJECTSECRET", "body": "BODYSECRET"})
    client.post("/auth/logout")

    stored = every_text_stored(db_session)
    assert rows(db_session)  # something was recorded
    for secret in ("hunter2", "SUBJECTSECRET", "BODYSECRET", "password", "cookie", "session"):
        assert secret not in stored.lower().replace("contractor@example.com", ""), secret


# ---- The page ------------------------------------------------------------------------


def super_admin(client):
    login_as(client, "admin@example.com")


def test_only_super_admins_can_read_the_activity_log(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_in(client, db_session, team)

    assert client.get("/admin/activity-log").status_code == 403


def test_the_activity_log_page_filters_and_shows_problems_only(client, db_session):
    # Judged on table cells: the event names also appear as options in the filter menu.
    def has(page, event):
        return f"<td>{event}</td>" in page

    user = local_user(db_session)
    sign_in(client, user.email, "hunter2-correct")
    sign_in(client, user.email, "wrong")
    client.post("/auth/logout")
    super_admin(client)

    everything = client.get("/admin/activity-log").text
    assert has(everything, "login success") and has(everything, "login failed") and has(everything, "logout")

    only_failed = client.get("/admin/activity-log?event_type=login_failed").text
    assert "wrong_password" in only_failed and not has(only_failed, "login success")

    problems = client.get("/admin/activity-log?problems=true").text
    assert "wrong_password" in problems and not has(problems, "login success") and not has(problems, "logout")

    by_person = client.get(f"/admin/activity-log?actor={user.email.upper()}").text
    assert f"<td>{user.email}</td>" in by_person and "<td>admin@example.com</td>" not in by_person

    today = rows(db_session)[0].created_at.strftime("%Y-%m-%d")
    assert has(client.get(f"/admin/activity-log?date_from={today}&date_to={today}").text, "login failed")
    assert "Nothing matches" in client.get("/admin/activity-log?date_to=2000-01-01").text
    assert client.get("/admin/activity-log?date_from=31/10/2026").status_code == 400

def test_the_page_escapes_what_a_visitor_controls_and_clips_the_browser_string(client, db_session):
    client.post(
        "/auth/local-login",
        data={"email": "<script>alert(1)</script>@evil.example", "password": "x"},
        headers={"User-Agent": "<img src=x onerror=alert(2)>" + "A" * 400},
        follow_redirects=False,
    )
    super_admin(client)

    page = client.get("/admin/activity-log").text

    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page
    stored = rows(db_session, "login_failed")[0].user_agent
    assert len(stored) == 300


def test_the_activity_log_is_paginated(client, db_session):
    for number in range(55):
        db_session.add(ActivityLog(event_type="request", method="POST", path="/x", status_code=200, actor_email=f"u{number}@x.org"))
    db_session.commit()
    super_admin(client)

    first, second = client.get("/admin/activity-log").text, client.get("/admin/activity-log?page=2").text

    assert "page=2" in first and first.count("<tr>") != second.count("<tr>")
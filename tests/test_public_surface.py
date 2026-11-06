"""3A: the public surface. APP_ROLE decides which routes a process serves; the Graph webhook stores
what it receives and answers 202; the public routes are rate limited; approval tokens stay out of the
access log."""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from starlette.middleware.sessions import SessionMiddleware

from app import rate_limit
from app.config import Settings, settings, unsafe_setting_errors
from app.log_redaction import ApprovalTokenFilter, redact_approval_tokens
from app.main import build_app
from app.models import GraphNotification
from app.rate_limit import RateLimiter

WEBHOOK = "/webhooks/graph/notifications"
STATE = "the-client-state"


def paths(app):
    return set(app.openapi()["paths"])


@pytest.fixture
def client_state(monkeypatch):
    monkeypatch.setattr(settings, "graph_webhook_client_state", STATE)


def notification(**fields):
    base = {
        "subscriptionId": "sub-1",
        "changeType": "created",
        "resource": "users/support@example.com/messages/AAA",
        "resourceData": {"id": "AAA"},
        "clientState": STATE,
    }
    return {**base, **fields}


# ---- Which routes each role serves ----------------------------------------------------------------------


def test_the_public_app_serves_only_the_webhook_the_approval_link_and_health():
    public = paths(build_app("public"))

    assert {"/health", WEBHOOK, "/approvals/{token}"} <= public
    for agent_route in ("/", "/tickets", "/portal", "/admin/users", "/auth/dev-login", "/auth/break-glass", "/webhooks/dev/simulate-inbound"):
        assert agent_route not in public, agent_route
    assert not {p for p in public if p.startswith(("/tickets", "/admin", "/portal", "/auth", "/notifications", "/settings"))}


def test_the_web_app_serves_the_console_but_not_the_internet_facing_routes():
    web = paths(build_app("web"))

    assert {"/", "/health", "/tickets", "/portal", "/auth/dev-login", "/auth/break-glass"} <= web
    assert WEBHOOK not in web and "/approvals/{token}" not in web


def test_all_serves_everything_for_development_and_the_worker_serves_only_health():
    everything = paths(build_app("all"))
    assert {"/tickets", WEBHOOK, "/approvals/{token}", "/webhooks/dev/simulate-inbound"} <= everything
    assert paths(build_app("worker")) == {"/health"}


def test_the_public_app_has_no_session_cookie_and_the_web_app_does():
    def classes(app):
        return [m.cls for m in app.user_middleware]

    assert SessionMiddleware not in classes(build_app("public"))
    assert SessionMiddleware in classes(build_app("web"))


def test_the_public_app_answers_without_signing_anyone_in(db_session):
    with TestClient(build_app("public")) as public:
        assert public.get("/health").status_code == 200
        invalid = public.get("/approvals/not-a-real-token")
        assert invalid.status_code == 404 and "set-cookie" not in invalid.headers
        assert public.get("/tickets").status_code == 404
        assert public.get("/").status_code == 404


# ---- Per-role settings checks ---------------------------------------------------------------------------


def production(**fields):
    return Settings(env="production", session_secret_key="a-real-secret", graph_webhook_client_state="a-real-state", **fields)


def test_an_unknown_role_is_refused_even_in_dev():
    assert any("APP_ROLE" in e for e in unsafe_setting_errors(Settings(env="dev", app_role="webb")))


def test_production_refuses_to_serve_everything_in_one_process():
    assert any("APP_ROLE" in e for e in unsafe_setting_errors(production(app_role="all")))
    assert unsafe_setting_errors(Settings(env="dev", app_role="all")) == []


@pytest.mark.parametrize("role", ["web", "public", "worker"])
def test_each_role_starts_with_real_secrets(role):
    assert unsafe_setting_errors(production(app_role=role)) == []


def test_the_public_app_needs_a_real_client_state_even_before_graph_is_configured():
    config = Settings(env="production", app_role="public", session_secret_key="a-real-secret", graph_webhook_client_state="change-me")
    assert any("GRAPH_WEBHOOK_CLIENT_STATE" in e for e in unsafe_setting_errors(config))
    web = Settings(env="production", app_role="web", session_secret_key="a-real-secret", graph_webhook_client_state="change-me")
    assert unsafe_setting_errors(web) == []  # web does not receive the webhook


def test_the_public_app_must_not_hold_the_sso_secret():
    assert any("SSO" in e for e in unsafe_setting_errors(production(app_role="public", sso_client_secret="s")))
    assert unsafe_setting_errors(production(app_role="web", sso_client_secret="s")) == []


# ---- The webhook stores what it receives and answers 202 -------------------------------------------------


def test_a_valid_notification_is_stored_and_acknowledged_with_202(client, db_session, client_state):
    response = client.post(WEBHOOK, json={"value": [notification(), notification(resourceData={"id": "BBB"})]})

    assert response.status_code == 202 and response.json() == {"status": "accepted"}
    rows = db_session.exec(select(GraphNotification).order_by(GraphNotification.id)).all()
    assert [(r.resource_id, r.change_type, r.subscription_id) for r in rows] == [("AAA", "created", "sub-1"), ("BBB", "created", "sub-1")]
    assert all(r.processed_at is None and r.lifecycle_event is None for r in rows)


def test_a_notification_with_the_wrong_client_state_is_not_stored_but_looks_the_same(client, db_session, client_state):
    response = client.post(WEBHOOK, json={"value": [notification(clientState="forged"), notification(clientState=5), "junk", {}]})

    assert response.status_code == 202
    assert db_session.exec(select(GraphNotification)).all() == []


def test_only_the_notifications_that_carry_our_state_are_kept_from_a_mixed_batch(client, db_session, client_state):
    client.post(WEBHOOK, json={"value": [notification(clientState="forged"), notification(resourceData={"id": "KEEP"})]})

    assert [r.resource_id for r in db_session.exec(select(GraphNotification)).all()] == ["KEEP"]


def test_a_lifecycle_notification_is_stored_with_its_event(client, db_session, client_state):
    client.post(WEBHOOK, json={"value": [{"subscriptionId": "sub-1", "lifecycleEvent": "reauthorizationRequired", "clientState": STATE}]})

    [row] = db_session.exec(select(GraphNotification)).all()
    assert (row.lifecycle_event, row.resource_id, row.change_type) == ("reauthorizationRequired", None, None)


def test_stored_values_are_clipped_and_odd_types_are_ignored(client, db_session, client_state):
    client.post(WEBHOOK, json={"value": [notification(resource="x" * 5000, subscriptionId=7, resourceData="nope")]})

    [row] = db_session.exec(select(GraphNotification)).all()
    assert len(row.resource) == 500 and row.subscription_id is None and row.resource_id is None


def test_a_flood_in_one_request_stores_at_most_a_thousand(client, db_session, client_state):
    client.post(WEBHOOK, json={"value": [notification() for _ in range(1010)]})

    assert len(db_session.exec(select(GraphNotification)).all()) == 1000


def test_a_huge_body_is_refused_before_it_is_parsed(client, db_session, client_state):
    response = client.post(WEBHOOK, content=b"[" + b"1," * 600_000 + b"1]", headers={"content-type": "application/json"})

    assert response.status_code == 413
    assert db_session.exec(select(GraphNotification)).all() == []


def test_a_huge_body_sent_in_chunks_with_no_declared_length_is_refused_too(client, db_session, client_state):
    def chunks():
        for _ in range(600):
            yield b"1," * 1000

    response = client.post(WEBHOOK, content=chunks(), headers={"content-type": "application/json"})

    assert response.status_code == 413


def test_the_value_must_be_a_list(client, client_state):
    assert client.post(WEBHOOK, json={"value": "no"}).status_code == 400
    assert client.post(WEBHOOK, json={}).status_code == 202  # nothing to store is fine


def test_the_validation_token_is_echoed_with_a_200_and_stores_nothing(client, db_session):
    response = client.post(WEBHOOK + "?validationToken=abc123")

    assert (response.status_code, response.text) == (200, "abc123")
    assert response.headers["content-type"].startswith("text/plain")
    assert db_session.exec(select(GraphNotification)).all() == []


def test_the_webhook_works_on_the_public_app(db_session, client_state):
    with TestClient(build_app("public")) as public:
        assert public.post(WEBHOOK, json={"value": [notification()]}).status_code == 202
    assert len(db_session.exec(select(GraphNotification)).all()) == 1


# ---- Rate limiting ---------------------------------------------------------------------------------------


def test_a_caller_over_the_limit_is_told_to_wait_until_the_window_has_passed():
    limiter = RateLimiter(lambda: 3, window_seconds=60)

    assert [limiter.hit("a", now=t) for t in (0, 1, 2)] == [None, None, None]
    wait = limiter.hit("a", now=3)
    assert wait is not None and 55 <= wait <= 60
    assert limiter.hit("a", now=59) is not None  # the first hit is still inside the window
    assert limiter.hit("a", now=61) is None  # the first has aged out


def test_callers_are_counted_separately_and_a_limit_of_zero_means_none():
    limiter = RateLimiter(lambda: 1)
    assert limiter.hit("a", now=0) is None and limiter.hit("b", now=0) is None
    assert limiter.hit("a", now=1) is not None
    unlimited = RateLimiter(lambda: 0)
    assert all(unlimited.hit("a", now=n) is None for n in range(100))


def test_the_limiter_forgets_idle_callers_so_it_cannot_grow_without_bound(monkeypatch):
    monkeypatch.setattr(rate_limit, "MAX_TRACKED_CALLERS", 5)
    limiter = RateLimiter(lambda: 10)
    for n in range(20):
        limiter.hit(f"caller-{n}", now=0)
    limiter.hit("late", now=1000)
    assert len(limiter._hits) <= 6


def test_the_approval_link_is_rate_limited_per_caller(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_approvals_per_minute", 3)

    codes = [client.get("/approvals/guess").status_code for _ in range(5)]

    assert codes == [404, 404, 404, 429, 429]
    refused = client.get("/approvals/guess")
    assert int(refused.headers["retry-after"]) >= 1


def test_posting_a_decision_counts_against_the_same_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_approvals_per_minute", 2)

    codes = [client.post("/approvals/guess", data={"decision": "approve"}).status_code for _ in range(3)]

    assert codes[-1] == 429


def test_the_webhook_has_its_own_limit_and_health_has_none(client, monkeypatch, client_state):
    monkeypatch.setattr(settings, "rate_limit_webhook_per_minute", 2)

    assert [client.post(WEBHOOK, json={}).status_code for _ in range(3)] == [202, 202, 429]
    assert client.get("/approvals/guess").status_code == 404  # a different bucket
    assert all(client.get("/health").status_code == 200 for _ in range(50))


# ---- Approval tokens stay out of the access log ----------------------------------------------------------


def record(*args):
    return logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d', args, None)


def test_the_token_in_an_approval_path_is_replaced_in_the_access_log_line():
    entry = record("10.0.0.1:5555", "GET", "/approvals/abc.DEF-ghi_123?utm=1", "1.1", 200)

    assert ApprovalTokenFilter().filter(entry) is True

    assert entry.getMessage() == '10.0.0.1:5555 - "GET /approvals/{token}?utm=1 HTTP/1.1" 200'
    assert "abc.DEF" not in entry.getMessage()


def test_other_paths_are_logged_as_they_are():
    entry = record("10.0.0.1:5555", "GET", "/tickets/INC-1042", "1.1", 200)
    ApprovalTokenFilter().filter(entry)
    assert "/tickets/INC-1042" in entry.getMessage()


@pytest.mark.parametrize("name", ["uvicorn.access", "uvicorn.error"])
def test_the_filter_is_on_uvicorns_logs_exactly_once(name):
    redact_approval_tokens()
    redact_approval_tokens()
    assert sum(isinstance(f, ApprovalTokenFilter) for f in logging.getLogger(name).filters) == 1


def test_the_public_app_does_not_write_an_activity_row_for_every_post(db_session, client_state):
    from app.models import ActivityLog

    with TestClient(build_app("public")) as public:
        for _ in range(3):
            public.post(WEBHOOK, json={"value": [notification(clientState="forged")]})
            public.post("/approvals/not-a-real-token", data={"decision": "approve"})

    assert db_session.exec(select(ActivityLog)).all() == []


def test_the_dev_helper_is_not_mounted_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")

    assert "/webhooks/dev/simulate-inbound" not in paths(build_app("web"))
    assert "/webhooks/dev/simulate-inbound" not in paths(build_app("all"))


def test_with_no_client_state_configured_nothing_is_accepted(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_webhook_client_state", "")

    assert client.post(WEBHOOK, json={"value": [notification(clientState="")]}).status_code == 202
    assert db_session.exec(select(GraphNotification)).all() == []

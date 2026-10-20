import pytest

from app import check_settings
from app.config import Settings, settings, unsafe_setting_errors
from tests.conftest import login_as


@pytest.fixture
def production_mode(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")


def test_dev_login_is_unavailable_outside_dev(client, production_mode):
    assert client.get("/auth/dev-login").status_code == 404
    response = client.post(
        "/auth/dev-login",
        data={"email": "attacker@example.com", "display_name": "Attacker"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_login_page_hides_dev_login_outside_dev(client, production_mode):
    assert "/auth/dev-login" not in client.get("/auth/login").text


def test_simulate_inbound_is_unavailable_outside_dev(client, production_mode):
    response = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "a@example.com", "subject": "s", "body": "b"},
    )
    assert response.status_code == 404


def test_simulate_inbound_works_in_dev(client):
    response = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "a@example.com", "subject": "s", "body": "b"},
    )
    assert response.status_code == 200


def test_dev_allows_default_secrets():
    assert unsafe_setting_errors(Settings(env="dev", session_secret_key="change-me")) == []


def test_production_rejects_default_session_secret():
    errors = unsafe_setting_errors(Settings(env="production", session_secret_key="dev-secret-change-me"))
    assert any("SESSION_SECRET_KEY" in error for error in errors)


def test_production_rejects_default_client_state_once_graph_is_configured():
    config = Settings(
        env="production",
        session_secret_key="a-real-secret",
        graph_tenant_id="tenant",
        graph_client_id="client",
        graph_client_secret="secret",
        graph_webhook_client_state="change-me",
    )
    assert any("GRAPH_WEBHOOK_CLIENT_STATE" in error for error in unsafe_setting_errors(config))


def test_production_accepts_real_secrets():
    assert unsafe_setting_errors(Settings(env="production", app_role="web", session_secret_key="a-real-secret")) == []


def test_dev_login_still_works_in_dev(client):
    assert login_as(client, "dev@example.com") is not None


def test_settings_check_exits_non_zero_on_unsafe_config(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "session_secret_key", "change-me")
    assert check_settings.main() == 1


def test_settings_check_passes_in_dev():
    assert check_settings.main() == 0

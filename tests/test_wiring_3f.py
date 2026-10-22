"""The app side of the 3F wiring: two base URLs, and Application Insights that never sees an approval token."""

import pytest

from app import telemetry
from app.config import Settings, settings
from app.models import Ticket
from app.teams import new_ticket_card
from app.telemetry import EXCLUDED_URLS, start_telemetry

EXCLUSION_VARIABLES = ("OTEL_PYTHON_EXCLUDED_URLS", "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in EXCLUSION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


# ---- Two addresses -----------------------------------------------------------------


def test_a_ticket_link_uses_the_web_address_when_there_is_one():
    both = Settings(public_base_url="https://respond.example.org", web_base_url="https://tickets.example.org")
    assert both.ticket_link_base == "https://tickets.example.org"


def test_one_address_still_serves_both_in_development():
    assert Settings(public_base_url="http://localhost:8000").ticket_link_base == "http://localhost:8000"
    assert Settings().ticket_link_base == ""


def test_a_teams_card_opens_the_ticket_on_the_web_app_not_the_public_one(db_session, monkeypatch):
    ticket = Ticket(subject="Printer", requester_email="me@example.com")
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    monkeypatch.setattr(settings, "public_base_url", "https://respond.example.org")
    monkeypatch.setattr(settings, "web_base_url", "https://tickets.example.org/")

    [action] = new_ticket_card(ticket)["attachments"][0]["content"]["actions"]

    assert action["url"] == f"https://tickets.example.org/tickets/{ticket.number}"


# ---- Application Insights ------------------------------------------------------------


def test_nothing_starts_without_a_connection_string(monkeypatch):
    monkeypatch.setattr(settings, "applicationinsights_connection_string", "")
    calls = []

    assert start_telemetry(lambda **kwargs: calls.append(kwargs)) is False

    assert calls == []


def test_it_starts_with_the_connection_string_and_skips_approval_links(monkeypatch):
    monkeypatch.setattr(settings, "applicationinsights_connection_string", "InstrumentationKey=abc")
    calls = []

    assert start_telemetry(lambda **kwargs: calls.append(kwargs)) is True

    assert calls == [{"connection_string": "InstrumentationKey=abc"}]
    import os

    for name in EXCLUSION_VARIABLES:
        assert "/approvals/" in os.environ[name].split(",")


def test_the_exclusion_is_set_before_the_instrumentation_starts(monkeypatch):
    monkeypatch.setattr(settings, "applicationinsights_connection_string", "InstrumentationKey=abc")
    import os

    seen = {}

    def configure(**kwargs):
        seen.update({name: os.environ.get(name) for name in EXCLUSION_VARIABLES})

    start_telemetry(configure)

    assert all("/approvals/" in (value or "") for value in seen.values())


def test_existing_exclusions_are_kept_and_not_duplicated(monkeypatch):
    monkeypatch.setattr(settings, "applicationinsights_connection_string", "InstrumentationKey=abc")
    monkeypatch.setenv("OTEL_PYTHON_EXCLUDED_URLS", "/health")
    import os

    start_telemetry(lambda **kwargs: None)
    start_telemetry(lambda **kwargs: None)

    assert os.environ["OTEL_PYTHON_EXCLUDED_URLS"].split(",") == ["/health", *EXCLUDED_URLS]


def test_a_telemetry_failure_does_not_stop_the_app(monkeypatch, caplog):
    monkeypatch.setattr(settings, "applicationinsights_connection_string", "InstrumentationKey=abc")

    def broken(**kwargs):
        raise RuntimeError("no network")

    assert start_telemetry(broken) is False
    assert "carrying on without telemetry" in caplog.text


def test_the_real_package_is_importable():
    # The default `configure` imports it; a typo in the name would only show on staging otherwise.
    from azure.monitor.opentelemetry import configure_azure_monitor

    assert callable(configure_azure_monitor)
    assert telemetry.EXCLUDED_URLS == ("/approvals/", "/feedback/", "/reopen/")

"""The sign-in pages on 9A (#251): the card, the one primary action, the disclosure, outage mode and the error."""

import re
from pathlib import Path

import pytest
from sqlmodel import select

from app import outage
from app.breakglass import create_account
from app.config import settings
from app.models import User

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def open_tags(html: str, name: str) -> list[str]:
    return re.findall(rf"<{name}\b[^>]*>", html)


def tag_with(html: str, name: str, needle: str) -> str:
    [found] = [t for t in open_tags(html, name) if needle in t]
    return found


def has_class(tag: str, cls: str) -> bool:
    match = re.search(r'class="([^"]*)"', tag)
    return bool(match) and cls in match.group(1).split()


def form_of(html: str, action: str) -> str:
    match = re.search(rf'<form\b[^>]*action="{re.escape(action)}".*?</form>', html, re.S)
    assert match, action
    return match.group(0)


def names(form: str) -> set[str]:
    return set(re.findall(r'<input\b[^>]*\bname="([^"]+)"', form))


def page(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200
    return response.text


@pytest.fixture
def sso(monkeypatch):
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    assert settings.sso_configured


@pytest.fixture
def no_sso(monkeypatch):
    monkeypatch.setattr(settings, "sso_tenant_id", "")
    monkeypatch.setattr(settings, "sso_client_id", "")
    monkeypatch.setattr(settings, "sso_client_secret", "")
    assert not settings.sso_configured


def switch_outage_on(db_session):
    issued = create_account(db_session, "breakglass1")
    admin = db_session.exec(select(User).where(User.email == issued.email)).one()
    outage.turn_on(db_session, admin)
    db_session.commit()


def test_login_with_sso_has_a_primary_microsoft_link_and_a_closed_disclosure(client, sso):
    html = page(client, "/auth/login")
    link = tag_with(html, "a", 'href="/auth/sso/start"')
    assert has_class(link, "btn-primary") and has_class(link, "btn")
    details = tag_with(html, "details", "signin-more")
    assert " open" not in details
    block = re.search(r"<details\b.*?</details>", html, re.S).group(0)
    assert 'action="/auth/local-login"' in block and 'href="/auth/break-glass"' in block
    assert "card" in tag_with(html, "section", "signin-card") and "<h1" in html


def test_login_without_sso_shows_the_local_form_without_a_disclosure(client, no_sso):
    html = page(client, "/auth/login")
    assert "/auth/sso/start" not in html
    assert "signin-more" not in html
    assert names(form_of(html, "/auth/local-login")) == {"email", "password"}
    assert 'href="/auth/break-glass"' in html


def test_login_in_outage_mode_leads_with_the_emergency_form(client, db_session, sso):
    switch_outage_on(db_session)
    html = page(client, "/auth/login")
    assert html.index("/auth/emergency-login") < html.index("/auth/sso/start")
    assert re.search(r'class="signin-notice signin-warn"[^>]*>.*?Outage mode is on', html, re.S)
    assert not has_class(tag_with(html, "a", 'href="/auth/sso/start"'), "btn-primary")
    emergency = form_of(html, "/auth/emergency-login")
    assert names(emergency) == {"upn", "password"}
    assert has_class(tag_with(emergency, "button", "submit"), "btn-primary")


def test_no_emergency_form_when_outage_mode_is_off(client, sso):
    assert "/auth/emergency-login" not in page(client, "/auth/login")


def test_an_error_is_an_alert(client, sso):
    html = page(client, "/auth/login", error="Wrong password")
    match = re.search(r'<p class="signin-notice signin-error" role="alert">(.*?)</p>', html, re.S)
    assert match and "Wrong password" in match.group(1)


def test_break_glass_page_keeps_its_fields_in_the_card(client):
    html = page(client, "/auth/break-glass")
    assert "signin-card" in html
    form = form_of(html, "/auth/break-glass")
    assert names(form) == {"username", "password", "code"}
    code = tag_with(form, "input", 'name="code"')
    assert 'autocomplete="one-time-code"' in code and 'maxlength="10"' in code and 'inputmode="numeric"' in code
    assert re.search(r"signin-warn[^>]*>.*?recorded", html, re.S)
    assert 'href="/auth/login"' in html
    assert "portal-bar" in html  # the Bare frame


def test_dev_login_page_keeps_its_fields_in_the_card(client):
    html = page(client, "/auth/dev-login")
    assert "signin-card" in html and "portal-bar" in html
    assert names(form_of(html, "/auth/dev-login")) == {"email", "display_name"}


@pytest.mark.guard
@pytest.mark.parametrize("name", ["login.html", "break_glass.html", "dev_login.html"])
def test_no_raw_colour_in_the_sign_in_templates(name):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    if name == "login.html":  # the Microsoft four-square mark is the one documented exception
        text = re.sub(r"<svg class=\"signin-logo\".*?</svg>", "", text, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text)
    assert "rgb(" not in text and "hsl(" not in text

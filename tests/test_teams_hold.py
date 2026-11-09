"""8F (#193): the hold on a Teams copy while the person is active in the app, the fallback when Teams refuses the bot's
message because the app was removed, and the privacy and terms pages the Teams manifest points at."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import teams
from app.config import settings
from app.email_templates import save_template
from app.jobs import teams_copies
from app.main import build_app
from app.models import TeamsConversation, UserSession
from app.notifications import APPROVAL_WAITING, due_for_teams
from app.settings_store import REGISTRY, TEAMS_HOLD_WHILE_ACTIVE_MINUTES, set_setting
from tests.test_teams_copy import NOW, an_agent, row
from tests.test_teams_copy_sending import due_notification, posts
from tests.test_teams_dm import CONVERSATION, SERVICE, Sent, bot, installed, queued  # noqa: F401  (fixtures)


def due_ids(db_session, now=NOW):
    return [n.id for n in due_for_teams(db_session, now)]


def be_seen(db_session, person, minutes_ago, now=NOW, revoked=False):
    """The person's one live session was last seen this long before `now`."""
    for existing in db_session.exec(select(UserSession).where(UserSession.user_id == person.id)).all():
        db_session.delete(existing)
    db_session.add(UserSession(
        id=f"s-{person.id}-{minutes_ago}", user_id=person.id, created_at=now - timedelta(days=1), expires_at=now + timedelta(days=1),
        last_seen_at=now - timedelta(minutes=minutes_ago), revoked_at=now if revoked else None,
    ))
    db_session.commit()


# ---- The hold while the person is active -----------------------------------------------------------------


def test_the_setting_is_an_off_able_number_of_minutes_defaulting_to_five():
    definition = REGISTRY[TEAMS_HOLD_WHILE_ACTIVE_MINUTES]
    assert definition.kind == "off_minutes" and definition.default == "5"


def test_a_copy_is_held_while_the_person_has_been_active_in_the_app_within_the_window(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=2)
    assert due_ids(db_session) == []  # they will see the bell

    be_seen(db_session, bob, minutes_ago=4)
    assert due_ids(db_session) == []  # still inside five minutes


def test_when_they_go_idle_the_unread_copy_is_sent(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=2)
    assert due_ids(db_session) == []

    assert due_ids(db_session, NOW + timedelta(minutes=2, seconds=59)) == []  # seen 4 minutes 59 seconds before that moment
    assert due_ids(db_session, NOW + timedelta(minutes=3, seconds=1)) == [unread]  # idle for just over five minutes


def test_a_copy_for_someone_not_seen_lately_is_due(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=6)
    assert due_ids(db_session) == [unread]


def test_a_signed_out_session_does_not_count_as_active(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=1, revoked=True)
    assert due_ids(db_session) == [unread]


def test_a_person_with_no_session_at_all_is_not_held(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    for existing in db_session.exec(select(UserSession).where(UserSession.user_id == bob.id)).all():
        db_session.delete(existing)
    db_session.commit()
    assert due_ids(db_session) == [unread]


def test_zero_turns_the_hold_off(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=0)
    assert due_ids(db_session) == []
    set_setting(db_session, TEAMS_HOLD_WHILE_ACTIVE_MINUTES, "0")
    db_session.commit()
    assert due_ids(db_session) == [unread]


def test_the_window_is_the_setting(client, db_session):
    bob, _ = an_agent(client, db_session)
    unread = row(db_session, bob, age_minutes=60)
    be_seen(db_session, bob, minutes_ago=8)
    assert due_ids(db_session) == [unread]
    set_setting(db_session, TEAMS_HOLD_WHILE_ACTIVE_MINUTES, "10")
    db_session.commit()
    assert due_ids(db_session) == []


def test_a_read_notification_is_never_sent_whether_or_not_they_are_active(client, db_session):
    bob, _ = an_agent(client, db_session)
    row(db_session, bob, age_minutes=60, read=True)
    be_seen(db_session, bob, minutes_ago=60)
    assert due_ids(db_session) == []


def test_a_copy_with_no_bell_is_not_held_because_there_is_nothing_to_see(client, db_session):
    bob, _ = an_agent(client, db_session)
    teams_only = row(db_session, bob, age_minutes=0, bell=False)
    be_seen(db_session, bob, minutes_ago=0)
    assert due_ids(db_session) == [teams_only]


def test_the_persons_own_delay_still_applies_first(client, db_session):
    bob, _ = an_agent(client, db_session)
    bob.teams_delay_minutes = 30
    db_session.add(bob)
    db_session.commit()
    too_soon = row(db_session, bob, age_minutes=20)
    be_seen(db_session, bob, minutes_ago=60)
    assert due_ids(db_session) == []
    assert due_ids(db_session, NOW + timedelta(minutes=11)) == [too_soon]


def test_approval_cards_follow_the_same_rule(client, db_session):
    bob, _ = an_agent(client, db_session)
    waiting = row(db_session, bob, age_minutes=60, kind=APPROVAL_WAITING)
    be_seen(db_session, bob, minutes_ago=1)
    assert due_ids(db_session) == []
    be_seen(db_session, bob, minutes_ago=9)
    assert due_ids(db_session) == [waiting]


def test_the_worker_job_hands_over_nothing_while_active_and_hands_it_over_once_idle(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    n = due_notification(db_session, bob)
    teams_copies(db_session)
    db_session.commit()
    assert posts(db_session) == []  # the login made them active a moment ago
    db_session.refresh(n)
    assert n.teams_handled_at is None  # held, not dropped

    for existing in db_session.exec(select(UserSession).where(UserSession.user_id == bob.id)).all():
        existing.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        db_session.add(existing)
    db_session.commit()
    teams_copies(db_session)
    db_session.commit()
    assert len(posts(db_session)) == 1


# ---- Teams refuses the bot's message because the app was removed -----------------------------------------


def refused(status):
    request = httpx.Request("POST", SERVICE + "v3/conversations/x/activities")

    def send(delivery):
        raise httpx.HTTPStatusError("refused", request=request, response=httpx.Response(status, request=request))

    return send


@pytest.mark.parametrize("status", [403, 404])
def test_a_refused_bot_message_falls_back_to_the_channel_mention_when_it_is_on(client, db_session, no_hold_while_active, bot, monkeypatch, status):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    channel = []

    result = teams.deliver_pending(db_session, send=channel.append, bot_send=refused(status))
    db_session.commit()

    assert result == (1, 0) and len(channel) == 1  # the same notification, now as a mention in the channel
    assert "<at>" in str(channel[0])
    assert posts(db_session)[0].sent_at is not None
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1  # kept: a 403 or 404 can have other causes than a removal


@pytest.mark.parametrize("status", [403, 404])
def test_with_the_channel_off_a_refused_bot_message_is_given_up(client, db_session, no_hold_while_active, bot, status):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)

    assert teams.deliver_pending(db_session, send=lambda p: pytest.fail("channel"), bot_send=refused(status)) == (0, 1)
    db_session.commit()

    [post] = posts(db_session)
    assert post.sent_at is None and post.attempts == teams.MAX_ATTEMPTS and "app was removed" in post.last_error
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1


def test_an_approval_card_never_falls_back_to_the_channel(client, db_session, no_hold_while_active, bot, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    assert teams.route_for(db_session, bob, direct_only=True)[0] == teams.DIRECT

    # Refused by Teams: it names the request and carries the buttons, so the channel is not an option even though it is on.
    assert teams.route_for(db_session, bob, direct_only=True, bot_refused=True) == (None, None)
    assert teams.route_for(db_session, bob, bot_refused=True) == (teams.CHANNEL, None)
    assert teams.route_for(db_session, bob)[0] == teams.DIRECT  # the next copy tries the bot again


def test_other_errors_are_still_retried_and_the_chat_is_kept(client, db_session, no_hold_while_active, bot, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)

    assert teams.deliver_pending(db_session, send=lambda p: pytest.fail("channel"), bot_send=refused(503)) == (0, 1)
    db_session.commit()

    [post] = posts(db_session)
    assert post.attempts == 1 and post.next_attempt_at is not None
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1


def test_the_channel_fallback_with_no_webhook_in_this_process_gives_up_rather_than_loops(client, db_session, no_hold_while_active, bot, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)

    assert teams.deliver_pending(db_session, send=lambda p: pytest.fail("channel"), bot_send=refused(404)) == (0, 1)
    db_session.commit()
    assert "TEAMS_NEW_TICKET_WEBHOOK_URL" in posts(db_session)[0].last_error


# ---- The privacy and terms pages -------------------------------------------------------------------------


def serving(role):
    return TestClient(build_app(role))


@pytest.mark.parametrize("role", ["public", "all"])
@pytest.mark.parametrize("page", ["privacy", "terms"])
def test_the_pages_are_served_by_the_public_app_and_all_without_signing_in(db_session, role, page):
    with serving(role) as c:
        response = c.get(f"/legal/{page}")
    assert response.status_code == 200
    assert "not been written yet" in response.text  # the placeholder says so


@pytest.mark.parametrize("role", ["web", "worker"])
def test_the_web_and_worker_apps_do_not_serve_them(db_session, role):
    with serving(role) as c:
        assert c.get("/legal/privacy").status_code == 404


def test_an_unknown_page_is_not_found(db_session):
    with serving("public") as c:
        assert c.get("/legal/cookies").status_code == 404


def test_the_text_is_the_admin_edited_template_shown_as_escaped_paragraphs(db_session):
    save_template(db_session, "legal_privacy", None, "We keep your messages.\n\nAsk <b>IT</b> to erase them.", "admin@example.com")
    db_session.commit()
    with serving("public") as c:
        text = c.get("/legal/privacy").text
        terms = c.get("/legal/terms").text
    assert "<p>We keep your messages.</p>" in text and "<p>Ask &lt;b&gt;IT&lt;/b&gt; to erase them.</p>" in text
    assert "not been written yet" in terms  # the other page is still the placeholder


def test_the_texts_are_edited_with_the_email_templates(client, db_session):
    from tests.conftest import login_as, set_role
    from app.models import UserRole

    admin_id = login_as(client, "admin@example.com", "Admin")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    page = client.get("/admin/email-templates").text
    assert "Privacy page (Teams app)" in page and "Terms of use page (Teams app)" in page
    saved = client.post("/admin/email-templates/legal_terms", data={"body": "Use it sensibly."}, follow_redirects=False)
    assert saved.status_code in (200, 303)
    with serving("public") as c:
        assert "<p>Use it sensibly.</p>" in c.get("/legal/terms").text


def test_the_pages_are_rate_limited_per_caller(db_session, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_legal_per_minute", 2)
    from app import rate_limit

    rate_limit.LEGAL.reset()
    with serving("public") as c:
        codes = [c.get("/legal/privacy").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    rate_limit.LEGAL.reset()


# ---- The manifest defaults -------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_package_8f", ROOT / "teams-app" / "build_package.py")
build_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_package)
APP_ID = "11111111-2222-3333-4444-555555555555"


def manifest_for(tmp_path, *args):
    import json
    import zipfile

    assert build_package.main(["--environment", "staging", "--bot-app-id", APP_ID, "--out", str(tmp_path), *args]) == 0
    [package] = tmp_path.glob("*.zip")
    return json.loads(zipfile.ZipFile(package).read("manifest.json"))


def test_the_urls_default_to_pages_of_the_public_base_url(tmp_path):
    developer = manifest_for(tmp_path, "--public-base-url", "https://tickets.example.org/", "--developer-name", "Vardags IT")["developer"]
    assert developer == {
        "name": "Vardags IT", "websiteUrl": "https://tickets.example.org/portal",
        "privacyUrl": "https://tickets.example.org/legal/privacy", "termsOfUseUrl": "https://tickets.example.org/legal/terms",
    }


def test_each_url_can_still_be_overridden(tmp_path):
    developer = manifest_for(
        tmp_path, "--public-base-url", "https://tickets.example.org", "--privacy-url", "https://legal.example.org/privacy"
    )["developer"]
    assert developer["privacyUrl"] == "https://legal.example.org/privacy"
    assert developer["termsOfUseUrl"] == "https://tickets.example.org/legal/terms"


def test_without_a_base_url_every_address_must_be_given(tmp_path, capsys):
    assert build_package.main(["--environment", "staging", "--bot-app-id", APP_ID, "--out", str(tmp_path)]) == 2
    assert "--public-base-url" in capsys.readouterr().err
    explicit = ["--website-url", "https://a.example", "--privacy-url", "https://a.example/p", "--terms-url", "https://a.example/t"]
    assert manifest_for(tmp_path, *explicit)["developer"]["websiteUrl"] == "https://a.example"


def test_a_base_url_that_is_not_https_is_refused(tmp_path, capsys):
    assert build_package.main(["--environment", "staging", "--bot-app-id", APP_ID, "--out", str(tmp_path), "--public-base-url", "http://x.example"]) == 2


# ---- The bell's poll is not activity ---------------------------------------------------------------------


def test_the_bells_poll_does_not_move_last_seen_but_a_page_view_does(client, db_session):
    from tests.conftest import login_as

    person_id = login_as(client, "ann@example.com", "Ann Able")
    row_ = db_session.exec(select(UserSession).where(UserSession.user_id == person_id)).one()
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    row_.last_seen_at = old
    db_session.add(row_)
    db_session.commit()

    assert client.get("/notifications/count").status_code == 200
    db_session.expire_all()
    assert db_session.exec(select(UserSession).where(UserSession.user_id == person_id)).one().last_seen_at == old

    assert client.get("/notifications").status_code == 200  # a page the person opened
    db_session.expire_all()
    assert db_session.exec(select(UserSession).where(UserSession.user_id == person_id)).one().last_seen_at > old


def test_the_poll_still_needs_a_valid_session(client):
    assert client.get("/notifications/count", follow_redirects=False).status_code in (303, 307, 401)

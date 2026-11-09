"""8B: a person's Teams copy of a notification goes to them as a direct message from the bot, through the same outbox.
Selection (what is due, the unread delay, the person's settings) is in test_teams_copy.py; the channel route is in
test_teams_copy_sending.py. Nothing here touches the network."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import bot_cards, bot_connector, teams
from app.bot_connector import BotAuthFailed
from app.config import settings
from app.jobs import teams_copies
from app.models import MembershipLevel, TeamsConversation, TeamsPost, Ticket, User, UserRole
from app.notification_settings import save_choices
from app.notifications import hand_to_teams
from tests.conftest import add_membership, create_team
from tests.test_teams_copy import an_agent
from tests.test_teams_copy_sending import NOW, due_notification, posts

SERVICE = "https://smba.example.net/emea/"
CONVERSATION = "a:1conversation"

pytestmark = pytest.mark.usefixtures("no_hold_while_active")  # the hold while the person is active has its own tests (test_teams_hold.py)


@pytest.fixture(autouse=True)
def restore(monkeypatch):
    for name in ("teams_notifications_enabled", "teams_bot_enabled", "teams_bot_app_id", "teams_bot_tenant_id",
                 "teams_new_ticket_webhook_url", "web_base_url", "public_base_url"):
        monkeypatch.setattr(settings, name, getattr(settings, name))


@pytest.fixture
def bot(monkeypatch):
    """The bot is on; the channel switch and the webhook are off, so only a direct message can be sent."""
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(settings, "teams_bot_tenant_id", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")


def installed(db_session, person, oid="aaaaaaaa-0000-0000-0000-000000000001"):
    row = TeamsConversation(entra_object_id=oid, user_id=person.id, service_url=SERVICE, conversation_id=CONVERSATION)
    db_session.add(row)
    db_session.commit()
    return row


class Sent:
    """Records what each route was asked to send."""

    def __init__(self):
        self.direct, self.channel = [], []

    def deliver(self, db_session, **kwargs):
        result = teams.deliver_pending(db_session, send=self.channel.append, bot_send=self.direct.append, **kwargs)
        db_session.commit()
        return result


def queued(db_session, person, **fields):
    n = due_notification(db_session, person, **fields)
    teams_copies(db_session)
    db_session.commit()
    return n


# ---- Delivery -----------------------------------------------------------------------------------------


def test_a_person_with_a_conversation_gets_a_direct_message_not_a_channel_post(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    n = queued(db_session, bob)
    n.text, n.actor_email = "#42 Printer jam was assigned to you", "ann@example.com"
    db_session.add(n)
    db_session.commit()
    sent = Sent()

    assert sent.deliver(db_session) == (1, 0)

    assert sent.channel == []  # no webhook is configured and none is needed
    [delivery] = sent.direct
    assert (delivery.service_url, delivery.conversation_id) == (SERVICE, CONVERSATION)
    card = delivery.card
    assert card["type"] == "AdaptiveCard" and "msteams" not in card  # a bare card, and no @mention in your own chat
    assert "Printer jam" in card["body"][0]["text"]
    assert {"title": "By", "value": teams.plain("ann@example.com")} in card["body"][1]["facts"]
    assert posts(db_session)[0].sent_at is not None


def test_the_card_has_an_open_ticket_button_when_the_address_is_known(client, db_session, bot, monkeypatch):
    from tests.test_notifications import agent_in_desk, back_to
    from tests.test_raising import desk
    from tests.test_team_activity import classified, ticket_named

    monkeypatch.setattr(settings, "web_base_url", "https://tickets.example.com/")
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Printer jam")
    ticket = ticket_named(db_session, "Printer jam")
    installed(db_session, bob)
    queued(db_session, bob, ticket_id=ticket.id)
    sent = Sent()

    sent.deliver(db_session)

    [action] = sent.direct[0].card["actions"]
    assert action == {"type": "Action.OpenUrl", "title": "Open ticket", "url": f"https://tickets.example.com/tickets/{ticket.number}"}


def test_words_from_a_requester_cannot_make_markup_or_links_in_the_direct_card(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    n = due_notification(db_session, bob, actor_email="[x](https://evil.example)")
    n.text = "[Reset your password](https://evil.example) **now**"
    db_session.add(n)
    db_session.commit()
    teams_copies(db_session)
    db_session.commit()
    sent = Sent()

    sent.deliver(db_session)

    card = sent.direct[0].card
    assert card["body"][0]["text"] == teams.plain(n.text)  # every markdown character escaped, so it shows as typed
    assert "\\[Reset your password\\]\\(" in card["body"][0]["text"] and "\\*\\*now\\*\\*" in card["body"][0]["text"]
    assert card["body"][1]["facts"][1]["value"] == teams.plain("[x](https://evil.example)")


def test_the_direct_card_is_built_in_one_module_with_a_slot_per_kind(client, db_session):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)
    assert bot_cards.notification_card(db_session, n, None) == bot_cards.what_happened_card(n, None)
    bot_cards.CARD_FOR_KIND["mentioned"] = lambda session, notification, ticket: {"custom": True}
    try:
        assert bot_cards.notification_card(db_session, n, None) == {"custom": True}
    finally:
        del bot_cards.CARD_FOR_KIND["mentioned"]


# ---- No conversation ----------------------------------------------------------------------------------


def test_with_no_conversation_the_channel_mention_is_the_fallback_when_it_is_on(client, db_session, bot, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")
    bob, _ = an_agent(client, db_session)
    queued(db_session, bob)
    sent = Sent()

    assert sent.deliver(db_session) == (1, 0)

    assert sent.direct == []
    [payload] = sent.channel
    assert payload["attachments"][0]["content"]["msteams"]["entities"][0]["mentioned"]["id"] == "bob@example.com"


def test_with_no_conversation_and_the_channel_off_the_copy_is_dropped_when_handed_over(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)

    assert hand_to_teams(db_session, teams.queue_notification, can_deliver=teams.can_deliver) == (0, 1)
    db_session.commit()

    assert posts(db_session) == []  # no failed row left in the outbox for a person who was never reachable
    assert db_session.get(type(n), n.id).teams_handled_at is not None


def test_the_job_drops_copies_for_people_who_cannot_be_reached(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)

    teams_copies(db_session)
    db_session.commit()

    assert posts(db_session) == [] and db_session.get(type(n), n.id).teams_handled_at is not None


def test_a_queued_copy_whose_route_has_gone_is_given_up_with_its_reason(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    row = installed(db_session, bob)
    queued(db_session, bob)
    db_session.delete(row)  # uninstalled since, and there is no channel to fall back to
    db_session.commit()
    sent = Sent()

    assert sent.deliver(db_session) == (0, 1)

    [post] = posts(db_session)
    assert sent.direct == [] and sent.channel == []
    assert post.sent_at is None and post.attempts == teams.MAX_ATTEMPTS and "No Teams conversation" in post.last_error


def test_a_person_who_uninstalled_after_the_copy_was_queued_gets_the_channel_mention_when_it_is_on(client, db_session, bot, monkeypatch):
    bob, _ = an_agent(client, db_session)
    row = installed(db_session, bob)
    queued(db_session, bob)
    db_session.delete(row)
    db_session.commit()
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")
    sent = Sent()

    assert sent.deliver(db_session) == (1, 0)
    assert sent.direct == [] and len(sent.channel) == 1


def test_the_route_is_decided_in_one_place(client, db_session, bot, monkeypatch):
    bob, _ = an_agent(client, db_session)
    assert teams.route_for(db_session, bob) == (None, None)
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    assert teams.route_for(db_session, bob) == (teams.CHANNEL, None)
    row = installed(db_session, bob)
    assert teams.route_for(db_session, bob)[0] == teams.DIRECT and teams.route_for(db_session, bob)[1].id == row.id
    monkeypatch.setattr(settings, "teams_bot_enabled", False)  # bot off: back to the channel
    assert teams.route_for(db_session, bob) == (teams.CHANNEL, None)


# ---- Rules that stay as they are ----------------------------------------------------------------------


def test_the_worker_queues_copies_when_only_the_bot_is_on(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    n = queued(db_session, bob)
    assert len(posts(db_session)) == 1 and db_session.get(type(n), n.id).teams_handled_at is not None


def test_with_neither_the_bot_nor_the_channel_on_copies_are_dropped(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_enabled", False)
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    assert posts(db_session) == []


def test_the_access_check_still_applies_when_the_message_is_sent(client, db_session, bot):
    from tests.test_notifications import agent_in_desk, back_to
    from tests.test_raising import desk
    from tests.test_team_activity import classified, ticket_named

    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Secret subject")
    ticket = ticket_named(db_session, "Secret subject")
    installed(db_session, bob)
    queued(db_session, bob, ticket_id=ticket.id)
    moved = db_session.get(Ticket, ticket.id)
    moved.team_id = create_team(db_session, "Network").id
    db_session.add(moved)
    db_session.commit()
    sent = Sent()

    assert sent.deliver(db_session) == (0, 1)

    assert sent.direct == []
    assert "can no longer open" in posts(db_session)[0].last_error


def test_a_person_who_turned_teams_off_is_not_messaged(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    n = due_notification(db_session, bob)
    save_choices(db_session, bob, {"mentioned:in_app"})  # Teams ticked off since it was written
    db_session.commit()

    assert hand_to_teams(db_session, teams.queue_notification) == (0, 1)
    db_session.commit()

    assert posts(db_session) == [] and Sent().deliver(db_session) == (0, 0)
    assert db_session.get(type(n), n.id).teams_handled_at is not None


def test_an_inactive_person_is_never_messaged(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    due_notification(db_session, bob)
    bob.is_active = False
    db_session.add(bob)
    db_session.commit()

    teams_copies(db_session)
    db_session.commit()

    assert posts(db_session) == []


def test_a_person_who_became_inactive_after_the_copy_was_queued_is_not_messaged(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    bob.is_active = False
    db_session.add(bob)
    db_session.commit()
    sent = Sent()

    assert sent.deliver(db_session) == (0, 1)

    assert sent.direct == [] and "no longer active" in posts(db_session)[0].last_error


def test_an_anonymised_person_is_never_messaged(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    bob.email, bob.display_name, bob.is_active = "anonymised-1-9@invalid", "Anonymised requester #1", False
    db_session.add(bob)
    db_session.commit()
    sent = Sent()

    assert sent.deliver(db_session) == (0, 1)
    assert sent.direct == []


def test_a_copy_queued_before_the_bot_was_switched_off_is_not_sent(client, db_session, bot, monkeypatch):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    monkeypatch.setattr(settings, "teams_bot_enabled", False)
    sent = Sent()

    assert sent.deliver(db_session) == (0, 0)  # without the bot or a webhook this process cannot send anything
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")
    assert sent.deliver(db_session) == (0, 1)
    assert sent.direct == [] and sent.channel == []


# ---- The outbox ---------------------------------------------------------------------------------------


def test_a_failed_direct_message_is_retried_with_the_shared_backoff_then_sent(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)
    request = httpx.Request("POST", SERVICE + "v3/conversations/x/activities")

    def unavailable(delivery):
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(503, request=request))

    assert teams.deliver_pending(db_session, bot_send=unavailable, now=NOW) == (0, 1)
    db_session.commit()
    [post] = posts(db_session)
    assert post.attempts == 1 and post.sent_at is None
    assert post.next_attempt_at == NOW + timedelta(minutes=teams.backoff_minutes(1))
    assert post.last_error == "Teams answered 503"

    sent = Sent()
    assert sent.deliver(db_session, now=NOW) == (0, 0)  # not due yet
    assert sent.deliver(db_session, now=NOW + timedelta(minutes=3)) == (1, 0)
    assert len(sent.direct) == 1 and posts(db_session)[0].last_error is None


def test_a_refused_credential_keeps_retrying_and_a_refused_message_gives_up(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)

    def bad_credential(delivery):
        raise BotAuthFailed("The Bot Connector refused the bot's credential")

    teams.deliver_pending(db_session, bot_send=bad_credential, now=NOW)
    db_session.commit()
    [post] = posts(db_session)
    assert post.attempts == 1 and post.last_error == "BotAuthFailed"

    request = httpx.Request("POST", SERVICE)

    def forbidden(delivery):
        raise httpx.HTTPStatusError("no", request=request, response=httpx.Response(403, request=request))

    teams.deliver_pending(db_session, bot_send=forbidden, now=NOW + timedelta(hours=2))
    db_session.commit()
    assert posts(db_session)[0].attempts == teams.MAX_ATTEMPTS  # the request itself is refused: not tried again


def test_one_notification_is_one_message_however_often_the_jobs_run(client, db_session, bot):
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    due_notification(db_session, bob)
    sent = Sent()
    for _ in range(3):
        teams_copies(db_session)
        db_session.commit()
        sent.deliver(db_session)

    assert len(sent.direct) == 1 and len(posts(db_session)) == 1


def test_a_direct_message_does_not_wait_for_the_webhook_but_a_channel_post_does(client, db_session, bot, monkeypatch, caplog):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    bob, _ = an_agent(client, db_session)
    ann_row = installed(db_session, bob)
    queued(db_session, bob)
    sent = Sent()
    assert sent.deliver(db_session) == (1, 0)  # direct: sent although this process has no webhook URL

    db_session.delete(ann_row)
    db_session.commit()
    due_notification(db_session, bob)
    teams_copies(db_session)
    db_session.commit()
    assert sent.deliver(db_session, now=NOW) == (0, 0)  # channel post, no URL here: left waiting, untried
    [first, second] = posts(db_session)
    assert second.sent_at is None and second.attempts == 0
    assert second.next_attempt_at == NOW + timedelta(minutes=teams.WEBHOOK_MISSING_RETRY_MINUTES)  # not first in every batch


def test_posts_that_cannot_be_sent_here_do_not_hold_up_the_direct_messages_behind_them(client, db_session, bot, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    bob, team = an_agent(client, db_session)
    ann = User(email="ann@example.com", display_name="Ann", role=UserRole.AGENT)
    db_session.add(ann)
    db_session.commit()
    add_membership(db_session, ann.id, team.id, MembershipLevel.MEMBER)
    for _ in range(2):
        queued(db_session, ann)  # Ann has not installed the app: channel posts
    installed(db_session, bob)
    queued(db_session, bob)  # a direct message, queued behind them
    sent = Sent()

    assert sent.deliver(db_session, limit=2, now=NOW) == (0, 0)  # the batch is the two channel posts: deferred
    assert sent.deliver(db_session, limit=2, now=NOW) == (1, 0)  # now the direct message is reached
    assert len(sent.direct) == 1


def test_new_ticket_cards_are_left_alone_by_a_process_with_only_the_bot_which_says_so(client, db_session, bot, caplog):
    import logging

    db_session.add(TeamsPost(kind=teams.NEW_TICKET, ticket_id=None))
    db_session.commit()
    sent = Sent()
    with caplog.at_level(logging.WARNING, logger="app.teams"):
        assert sent.deliver(db_session) == (0, 0)
    assert posts(db_session)[0].attempts == 0
    assert "TEAMS_NEW_TICKET_WEBHOOK_URL is not set" in caplog.text


# ---- Through the real Bot Connector client ------------------------------------------------------------


def test_the_real_sender_posts_the_bare_card_into_the_stored_conversation(client, db_session, bot, monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in request.url.host:
            return httpx.Response(200, json={"access_token": "bot-token", "expires_in": 3600})
        requests.append(request)
        return httpx.Response(200, json={"id": "sent-1"})

    monkeypatch.setattr(settings, "teams_bot_client_secret", "secret")
    monkeypatch.setattr(bot_connector, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    bot_connector.TOKENS.reset()
    bob, _ = an_agent(client, db_session)
    installed(db_session, bob)
    queued(db_session, bob)

    assert teams.deliver_pending(db_session) == (1, 0)
    db_session.commit()
    bot_connector.TOKENS.reset()

    [request] = requests
    assert str(request.url) == f"{SERVICE}v3/conversations/a%3A1conversation/activities"
    body = json.loads(request.content)
    assert body["type"] == "message" and body["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert request.headers["Authorization"] == "Bearer bot-token"


# ---- My settings --------------------------------------------------------------------------------------


def test_the_teams_option_reads_direct_message_when_the_bot_is_on(client, db_session, monkeypatch):
    an_agent(client, db_session)
    monkeypatch.setattr(settings, "teams_bot_enabled", False)
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    assert "Teams (direct message)" not in client.get("/settings/notifications").text
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(settings, "teams_bot_tenant_id", "22222222-2222-2222-2222-222222222222")
    page = client.get("/settings/notifications").text
    assert "Teams (direct message)" in page and "not connected yet" not in page


def test_a_person_without_the_app_is_told_to_install_it(client, db_session, bot, monkeypatch):
    bob, _ = an_agent(client, db_session)
    assert "install the Teams app to get these" in client.get("/settings/notifications").text
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    assert "until then they go to the support channel" in client.get("/settings/notifications").text
    installed(db_session, bob)
    assert "install the Teams app" not in client.get("/settings/notifications").text

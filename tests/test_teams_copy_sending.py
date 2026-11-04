"""2C-ii (sending): a due notification becomes a Teams post that @mentions the person, sent by the
shared outbox. The selection of what is due is in test_teams_copy.py."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlmodel import Session, select

from app import teams
from app.config import settings
from app.database import engine
from app.jobs import JOBS, teams_copies
from app.models import Notification, TeamsPost, Ticket, User, UserRole
from app.notifications import drop_due_for_teams
from tests.conftest import create_team, login_as, set_role
from tests.test_teams_copy import an_agent

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def restore_switch(monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", settings.teams_notifications_enabled)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://example.invalid/hook-secret")


def due_notification(db_session, person, age_minutes=60, **fields):
    """An unread notification old enough that its Teams copy is due now, by the real clock (the worker
    job uses the real clock; the selection tests in test_teams_copy.py pass their own)."""
    n = Notification(
        user_id=person.id, kind="mentioned", text="x", bell=True, teams_wanted=True,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes), **fields,
    )
    db_session.add(n)
    db_session.commit()
    db_session.refresh(n)
    return n


def posts(db_session):
    db_session.expire_all()
    return db_session.exec(select(TeamsPost).order_by(TeamsPost.id)).all()


# ---- Queueing --------------------------------------------------------------------------------------


def test_queueing_a_notification_writes_one_post_that_points_at_it(client, db_session):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)

    post = teams.queue_notification(db_session, n)
    db_session.commit()

    assert (post.kind, post.notification_id, post.sent_at) == ("notification", n.id, None)
    assert "notification" in teams.CARD_BUILDERS


def test_a_notification_can_never_be_posted_twice(client, db_session):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)
    teams.queue_notification(db_session, n)
    db_session.commit()

    teams.queue_notification(db_session, n)
    with pytest.raises(Exception):  # the database refuses the second post
        db_session.commit()
    db_session.rollback()
    assert len(posts(db_session)) == 1


# ---- The worker job ----------------------------------------------------------------------------------


def test_the_job_is_registered_and_runs_before_the_one_that_sends():
    names = [job.name for job in JOBS]
    assert "teams-copies" in names and names.index("teams-copies") < names.index("teams-posts")
    assert len(names) == len(set(names))


def test_with_sending_on_a_due_notification_is_queued_once_and_marked_dealt_with(client, db_session, on):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)

    teams_copies(db_session)
    db_session.commit()
    teams_copies(db_session)  # a second run finds nothing left to do
    db_session.commit()

    [post] = posts(db_session)
    assert (post.kind, post.notification_id) == ("notification", n.id)
    assert db_session.get(Notification, n.id).teams_handled_at is not None


def test_with_sending_off_due_notifications_are_dropped_not_left_to_pile_up(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)

    teams_copies(db_session)
    db_session.commit()

    assert posts(db_session) == []  # nothing queued
    assert db_session.get(Notification, n.id).teams_handled_at is not None  # and no flood later
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    teams_copies(db_session)
    assert posts(db_session) == []


def test_dropping_leaves_alone_what_is_not_due(client, db_session):
    bob, _ = an_agent(client, db_session)
    too_soon = due_notification(db_session, bob, age_minutes=0)
    assert drop_due_for_teams(db_session) == 0
    db_session.expire_all()
    assert db_session.get(Notification, too_soon.id).teams_handled_at is None


# ---- The card ----------------------------------------------------------------------------------------


def card_for(db_session, n):
    settings.teams_notifications_enabled = True  # restored by the autouse fixture below
    post = TeamsPost(kind="notification", notification_id=n.id)  # not saved: the builder only reads it
    payload = teams.CARD_BUILDERS["notification"](db_session, post)
    return payload["attachments"][0]["content"]


def test_the_card_mentions_the_person_and_says_what_happened(client, db_session):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)
    n.text = "#42 Printer jam was assigned to you"
    n.actor_email = "ann@example.com"
    db_session.add(n)
    db_session.commit()

    card = card_for(db_session, n)

    [entity] = card["msteams"]["entities"]
    assert entity["type"] == "mention" and entity["mentioned"] == {"id": "bob@example.com", "name": "Bob Builder"}
    text = card["body"][0]["text"]
    assert text.startswith("<at>Bob Builder</at> ") and "Printer jam" in text
    assert entity["text"] == "<at>Bob Builder</at>"  # the entity must match the text exactly
    assert {"title": "What", "value": "You were mentioned"} in card["body"][1]["facts"]
    assert {"title": "By", "value": teams.plain("ann@example.com")} in card["body"][1]["facts"]  # as on the new-ticket card


def test_the_card_links_to_the_ticket_when_the_address_is_known(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org/")
    bob, team = an_agent(client, db_session)
    ticket = Ticket(subject="Printer", requester_email="r@example.com", team_id=team.id)
    db_session.add(ticket)
    db_session.commit()
    n = due_notification(db_session, bob)
    n.ticket_id = ticket.id
    db_session.add(n)
    db_session.commit()

    card = card_for(db_session, n)

    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open ticket", "url": f"https://tickets.example.org/tickets/{ticket.number}"}
    ]
    monkeypatch.setattr(settings, "public_base_url", "")
    assert "actions" not in card_for(db_session, n)


def test_the_card_text_has_the_current_reference(client, db_session, monkeypatch):
    from app.models import TicketType
    from app.notifications import REF

    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org/")
    bob, team = an_agent(client, db_session)
    ticket = Ticket(subject="Laptop", requester_email="r@example.com", team_id=team.id, type=TicketType.SERVICE_REQUEST)
    db_session.add(ticket)
    db_session.commit()
    n = due_notification(db_session, bob)
    n.kind, n.text, n.ticket_id = "assigned", f"{REF} Laptop was assigned to you", ticket.id
    db_session.add(n)
    db_session.commit()

    card = card_for(db_session, n)

    assert f"SR\\-{ticket.number} Laptop was assigned to you" in card["body"][0]["text"] and REF not in card["body"][0]["text"]


def test_words_from_a_requester_cannot_make_markup_or_links_in_the_card(client, db_session):
    bob, _ = an_agent(client, db_session)
    bob.display_name = "Bob </at><b>*Boss*</b>"
    db_session.add(bob)
    n = due_notification(db_session, bob)
    n.text = "#7 [Reset your password](https://evil.example) was assigned to you"
    db_session.add(n)
    db_session.commit()

    card = card_for(db_session, n)

    text = card["body"][0]["text"]
    assert "[Reset your password](" not in text and "\\[Reset your password\\]\\(" in text  # shown as typed
    name = card["msteams"]["entities"][0]["mentioned"]["name"]
    assert not set("<>*[]()") & set(name) and text.startswith(f"<at>{name}</at> ")  # cannot close the tag


def test_a_person_with_no_usable_name_is_mentioned_by_the_start_of_their_email(client, db_session):
    bob, _ = an_agent(client, db_session)
    bob.display_name = "<>**"
    db_session.add(bob)
    n = due_notification(db_session, bob)
    db_session.commit()
    assert card_for(db_session, n)["msteams"]["entities"][0]["mentioned"]["name"] == "bob"


def test_underscores_are_left_out_of_the_mention_name(client, db_session):
    bob, _ = an_agent(client, db_session)
    bob.display_name = "bob_the_builder"
    db_session.add(bob)
    n = due_notification(db_session, bob)
    db_session.commit()
    assert card_for(db_session, n)["msteams"]["entities"][0]["mentioned"]["name"] == "bobthebuilder"


def test_a_queued_post_is_not_sent_if_the_switch_was_turned_off_since(client, db_session, on, monkeypatch):
    bob, _ = an_agent(client, db_session)
    due_notification(db_session, bob)
    teams_copies(db_session)
    db_session.commit()
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    sent = []

    assert teams.deliver_pending(db_session, send=sent.append) == (0, 1)
    db_session.commit()

    assert sent == [] and posts(db_session)[0].attempts == teams.MAX_ATTEMPTS


def test_a_post_whose_notification_has_gone_is_unsendable_not_retried(client, db_session, on):
    post = TeamsPost(kind="notification", notification_id=None)
    db_session.add(post)
    db_session.commit()
    with pytest.raises(teams.Unsendable):
        teams.CARD_BUILDERS["notification"](db_session, post)


def test_a_queued_copy_is_not_sent_once_the_person_can_no_longer_open_the_ticket(client, db_session, on):
    from tests.test_notifications import agent_in_desk, back_to
    from tests.test_raising import desk
    from tests.test_team_activity import classified, ticket_named

    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Secret subject")
    ticket = ticket_named(db_session, "Secret subject")
    n = due_notification(db_session, bob, ticket_id=ticket.id)
    post = teams.queue_notification(db_session, n)
    db_session.commit()
    assert teams.CARD_BUILDERS["notification"](db_session, post)

    moved = db_session.get(Ticket, ticket.id)
    moved.team_id = create_team(db_session, "Network").id
    db_session.add(moved)
    db_session.commit()

    with pytest.raises(teams.Unsendable):
        teams.CARD_BUILDERS["notification"](db_session, post)


# ---- Through the shared sender ------------------------------------------------------------------------


def test_a_due_notification_travels_all_the_way_and_is_recorded_as_sent(client, db_session, on):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob)
    sent = []

    teams_copies(db_session)
    db_session.commit()
    result = teams.deliver_pending(db_session, send=sent.append)
    db_session.commit()

    assert result == (1, 0)
    [payload] = sent
    assert payload["attachments"][0]["content"]["msteams"]["entities"][0]["mentioned"]["id"] == "bob@example.com"
    [post] = posts(db_session)
    assert post.sent_at is not None and post.notification_id == n.id


def test_a_failed_send_is_retried_with_the_shared_backoff_and_the_url_is_never_kept(client, db_session, on):
    import httpx

    bob, _ = an_agent(client, db_session)
    due_notification(db_session, bob)
    teams_copies(db_session)
    db_session.commit()

    def refuse(payload):
        raise httpx.ConnectError("could not reach https://example.invalid/hook-secret")

    assert teams.deliver_pending(db_session, send=refuse, now=NOW) == (0, 1)
    db_session.commit()

    [post] = posts(db_session)
    assert post.attempts == 1 and post.sent_at is None and post.next_attempt_at > NOW
    assert "hook-secret" not in (post.last_error or "")


def test_a_notification_of_a_kind_with_no_teams_channel_is_never_wanted_there(client, db_session):
    from app.notifications import notify

    bob, _ = an_agent(client, db_session)
    n = notify(db_session, bob, "some_future_kind", None, "x", "ann@example.com")
    db_session.commit()
    assert n is not None and n.teams_wanted is False


def test_a_notification_read_in_time_is_retired_and_not_sent(client, db_session, on):
    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob, read_at=datetime.now(timezone.utc))

    teams_copies(db_session)
    db_session.commit()

    assert posts(db_session) == []
    assert db_session.get(Notification, n.id).teams_handled_at is not None


def test_a_hidden_teams_only_row_cannot_be_opened_by_id(client, db_session):
    from app.notifications import mark_read

    bob, _ = an_agent(client, db_session)
    n = due_notification(db_session, bob, read_at=None)
    n.bell = False
    db_session.add(n)
    db_session.commit()
    assert mark_read(db_session, bob, n.id) is None


def test_both_kinds_of_post_are_built(client, db_session, on):
    assert set(teams.CARD_BUILDERS) == {"new_ticket", "notification"}


# ---- The settings page and the schema ------------------------------------------------------------------


def test_the_settings_page_stops_saying_teams_is_not_connected_once_it_is(client, db_session, monkeypatch):
    an_agent(client, db_session)
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    assert "not connected yet" in client.get("/settings/notifications").text
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    page = client.get("/settings/notifications").text
    assert "Teams" in page and "Teams <span" not in page.replace("\n", " ")


def test_a_post_can_point_at_a_notification_and_only_once(db_session):
    columns = {c["name"] for c in inspect(db_session.get_bind()).get_columns("teamspost")}
    assert "notification_id" in columns
    uniques = inspect(db_session.get_bind()).get_unique_constraints("teamspost")
    assert any(u["column_names"] == ["notification_id"] for u in uniques)

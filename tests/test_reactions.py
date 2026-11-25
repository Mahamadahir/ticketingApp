"""#298: reactions in Teams are recorded and shown, and have soft effects only. The Teams side is posted to the real endpoint
(signed by the test key set of test_teams_bot.py, the Bot Connector is its fake); the shared model and the one place that
decides effects (app/reactions.py) are also tested directly, as the email reactions (#299) will use them. Nothing here
touches the network."""

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import action_links, messages, reactions, teams
from app.auto_close_chase import run_auto_close, run_chase
from app.models import (
    BotMessage, OutboundEmail, TicketFeedback, TicketReaction, Ticket, TicketStatus, User, UserRole,
)
from app.settings_store import (
    AUTO_CLOSE_AFTER_DAYS, AUTO_CLOSE_ENABLED, CHASE_AFTER_DAYS, CHASE_ENABLED, CHASE_RESOLVE_AFTER_DAYS, REACTION_SENTIMENTS, REGISTRY,
    get_setting, set_setting,
)
from app.team_activity import load_timeline
from app.ticket_service import set_status
from tests.conftest import login_as, set_role
from tests.test_teams_bot import activity, bot, connector, make_user, metadata, post, public, token  # noqa: F401  (fixtures)
from tests.test_teams_intake import OID, OTHER_OID, an_agent, ann, isolate, say, tickets  # noqa: F401  (ann and isolate are fixtures)
from tests.test_outbound_mail import dev_mail  # noqa: F401  (a fixture)
from tests.test_subject_access import storage, world  # noqa: F401  (fixtures)

_ids = itertools.count(1)
CONVERSATION = "a:conv-1"


def react(public, to, *added, removed=(), oid=OID, name="Ann Able", at=None, **extra):
    """What Teams posts when someone reacts to (or takes back a reaction to) a message: `added` and `removed` are reaction types
    (or whole entries, as received)."""
    entry = lambda value: value if isinstance(value, dict) else {"type": value}
    body = activity(
        "messageReaction", oid=oid, id=f"re-{next(_ids)}", replyToId=to, reactionsAdded=[entry(a) for a in added],
        reactionsRemoved=[entry(r) for r in removed], **({"timestamp": at} if at else {}), **extra,
    )
    body["from"]["name"] = name
    response = post(public, body, token())
    assert response.status_code == 200
    return response


def rows(db_session):
    db_session.expire_all()
    return db_session.exec(select(TicketReaction).order_by(TicketReaction.id)).all()


def sent_message(db_session, kind):
    db_session.expire_all()
    return db_session.exec(select(BotMessage).where(BotMessage.kind == kind).order_by(BotMessage.id.desc())).first()


def feedback(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(TicketFeedback).where(TicketFeedback.ticket_id == ticket.id).order_by(TicketFeedback.id)).all()


def make_ticket(public, db_session, status):
    """A ticket raised in Teams by Ann, triaged and in `status`, with an agent's message to her in Teams delivered by the bot."""
    say(public, "Printer is jammed")
    ticket = tickets(db_session)[0]
    ticket.triaged_at = ticket.created_at
    ticket.status = status
    if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        ticket.resolved_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(ticket)
    db_session.commit()
    messages.email_requester(db_session, ticket, an_agent(db_session), "Please try again and tell me how it goes")
    db_session.commit()
    assert teams.deliver_pending(db_session) == (1, 0)
    db_session.commit()
    return db_session.get(Ticket, ticket.id)


@pytest.fixture
def resolved(public, connector, db_session, ann):
    return make_ticket(public, db_session, TicketStatus.RESOLVED)


@pytest.fixture
def waiting(public, connector, db_session, ann):
    return make_ticket(public, db_session, TicketStatus.AWAITING_USER)


# ---- Recording ---------------------------------------------------------------------------------------------


def test_the_bot_keeps_the_id_of_every_message_it_sends_about_a_ticket(resolved, db_session):
    kinds = {b.kind: b for b in db_session.exec(select(BotMessage)).all()}

    assert set(kinds) == {"other", "resolution"}  # the acknowledgement, and the agent's message on a resolved ticket
    assert kinds["resolution"].ticket_id == resolved.id and kinds["resolution"].conversation_id == CONVERSATION
    assert kinds["resolution"].message_id is not None and kinds["other"].message_id is None
    assert kinds["resolution"].activity_id.startswith("sent-")


def test_the_kind_of_a_message_follows_the_ticket_when_it_is_sent(waiting, db_session):
    assert sent_message(db_session, "awaiting_user").ticket_id == waiting.id


def test_a_notification_card_is_kept_with_its_kind(db_session):
    assert reactions.kind_of_notification("ticket_resolved") == reactions.RESOLUTION
    assert reactions.kind_of_notification("awaiting_you") == reactions.AWAITING_USER
    assert reactions.kind_of_notification("approval_waiting") == reactions.APPROVAL
    assert reactions.kind_of_notification("assigned") == reactions.OTHER


def test_a_reaction_to_a_stored_bot_message_is_recorded_against_the_right_ticket(public, resolved, db_session, ann):
    target = sent_message(db_session, "resolution")

    react(public, target.activity_id, "like", at="2026-11-24T09:30:00Z")

    [row] = rows(db_session)
    assert (row.ticket_id, row.channel, row.message_kind, row.message_ref) == (resolved.id, "teams", "resolution", target.activity_id)
    assert (row.raw_type, row.reactor_ref, row.reactor_name, row.user_id) == ("like", OID, "Ann Able", ann.id)
    assert (row.sentiment, row.by_party, row.removed_at, row.message_id) == ("positive", True, None, target.message_id)
    assert row.message_sent_at == target.created_at
    assert row.reacted_at == datetime(2026, 11, 24, 9, 30, tzinfo=timezone.utc)


def test_a_reaction_to_a_message_we_do_not_know_is_ignored(public, resolved, db_session):
    react(public, "someone-elses-message", "like")
    react(public, "", "like")

    assert rows(db_session) == []


def test_a_reaction_in_a_group_chat_or_from_nobody_we_can_name_is_ignored(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    body = activity("messageReaction", conversation_type="groupChat", id="re-x", replyToId=target, reactionsAdded=[{"type": "like"}])
    assert post(public, body, token()).status_code == 200
    body = activity("messageReaction", oid="", id="re-y", replyToId=target, reactionsAdded=[{"type": "like"}])
    assert post(public, body, token()).status_code == 200

    assert rows(db_session) == []


def test_the_same_reaction_delivered_twice_is_one_row(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    react(public, target, "like")

    assert len(rows(db_session)) == 1
    assert len(feedback(db_session, resolved)) == 1


def test_several_reactions_in_one_activity_are_each_recorded(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like", "heart")

    assert [r.raw_type for r in rows(db_session)] == ["like", "heart"]


def test_removing_a_reaction_marks_it_removed_and_keeps_the_row(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")

    react(public, target, removed=["like"], at="2026-11-24T09:45:00Z")

    [row] = rows(db_session)
    assert row.removed_at == datetime(2026, 11, 24, 9, 45, tzinfo=timezone.utc) and row.raw_type == "like"
    assert reactions.open_negative_ids(db_session, [resolved.id]) == set()


def test_taking_back_a_reaction_that_was_never_recorded_does_nothing(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, removed=["like"])

    assert rows(db_session) == []


def test_a_reaction_added_again_after_it_was_removed_is_a_new_row(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    react(public, target, removed=["like"])
    react(public, target, "like")

    assert [(r.raw_type, r.removed_at is None) for r in rows(db_session)] == [("like", False), ("like", True)]


# ---- The mapping -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected",
    [("like", "positive"), ("heart", "positive"), ("laugh", "positive"), ("sad", "negative"), ("angry", "negative"), ("surprised", "neutral")],
)
def test_the_default_mapping(db_session, kind, expected):
    assert reactions.sentiment_of(db_session, kind) == expected
    assert reactions.sentiment_of(db_session, kind.upper()) == expected  # the name is not case sensitive


def test_a_reaction_that_is_not_in_the_mapping_is_stored_as_received_and_is_neutral(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id

    react(public, target, {"type": "Party-Popper", "emoji": "\U0001F389"}, "\U0001F680")

    popper, rocket = rows(db_session)
    assert (popper.raw_type, popper.emoji, popper.sentiment) == ("Party-Popper", "\U0001F389", "neutral")
    assert (rocket.raw_type, rocket.emoji, rocket.sentiment) == ("\U0001F680", None, "neutral")
    assert feedback(db_session, resolved) == []  # neutral: shown, never feedback
    assert reactions.display_of(popper.raw_type, popper.emoji) == "\U0001F389"
    assert reactions.display_of(rocket.raw_type, rocket.emoji) == "\U0001F680"
    assert reactions.display_of("like") == "\U0001F44D like"


def test_the_default_is_a_setting_and_an_admin_can_change_it(client, public, resolved, db_session):
    assert get_setting(db_session, REACTION_SENTIMENTS) == REGISTRY[REACTION_SENTIMENTS].default
    target = sent_message(db_session, "resolution").activity_id
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)

    saved = client.post("/admin/settings/group", data={"group": "teams", f"v_{REACTION_SENTIMENTS}": "Like=negative,  surprised = positive\nheart=neutral"}, follow_redirects=False)

    assert saved.status_code in (302, 303)
    db_session.expire_all()
    assert get_setting(db_session, REACTION_SENTIMENTS) == "like=negative, surprised=positive, heart=neutral"
    react(public, target, "like", "surprised", "heart", "sad")  # sad is no longer listed: neutral
    assert [(r.raw_type, r.sentiment) for r in rows(db_session)] == [
        ("like", "negative"), ("surprised", "positive"), ("heart", "neutral"), ("sad", "neutral"),
    ]


def test_a_mapping_that_cannot_be_read_is_refused(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)

    refused = client.post("/admin/settings/group", data={"group": "teams", f"v_{REACTION_SENTIMENTS}": "like=great"}, follow_redirects=False)

    assert "name=positive" in refused.text
    db_session.expire_all()
    assert get_setting(db_session, REACTION_SENTIMENTS) == REGISTRY[REACTION_SENTIMENTS].default


def test_parse_mapping():
    assert reactions.parse_mapping("") == {}
    assert reactions.parse_mapping("Like=Positive, sad=negative") == {"like": "positive", "sad": "negative"}
    for bad in ("like", "like=", "=positive", "like=happy", "a=positive,b"):
        with pytest.raises(ValueError):
            reactions.parse_mapping(bad)


def test_an_unreadable_stored_mapping_falls_back_to_the_default(db_session):
    set_setting(db_session, REACTION_SENTIMENTS, "garbage")
    assert reactions.sentiment_of(db_session, "like") == "positive"


# ---- Feedback ----------------------------------------------------------------------------------------------


def test_a_positive_reaction_on_the_resolution_is_a_quick_rating_and_does_not_close_the_ticket(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like")

    [rating] = feedback(db_session, resolved)
    assert (rating.rating, rating.from_reaction, rating.token_id, rating.comment) == (5, True, None, "")
    assert rating.reaction_id == rows(db_session)[0].id and rating.resolved_by_id == resolved.resolved_by_id
    ticket = db_session.get(Ticket, resolved.id)
    assert ticket.status == TicketStatus.RESOLVED and ticket.closed_at is None


def test_a_negative_reaction_on_the_resolution_is_a_rating_of_one(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "angry")

    assert [(f.rating, f.from_reaction) for f in feedback(db_session, resolved)] == [(1, True)]
    assert db_session.get(Ticket, resolved.id).status == TicketStatus.RESOLVED


def test_a_neutral_reaction_is_not_a_rating(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "surprised")

    assert feedback(db_session, resolved) == []


def test_a_reaction_on_another_kind_of_message_is_not_a_rating(public, resolved, db_session):
    react(public, sent_message(db_session, "other").activity_id, "like")

    assert feedback(db_session, resolved) == [] and len(rows(db_session)) == 1


def test_the_latest_reaction_decides_and_there_is_one_quick_rating(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like", at="2026-11-24T09:00:00Z")
    react(public, target, "sad", at="2026-11-24T09:05:00Z")

    assert [(f.rating, f.from_reaction) for f in feedback(db_session, resolved)] == [(1, True)]


def test_taking_the_reaction_back_removes_the_quick_rating(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    react(public, target, removed=["like"])

    assert feedback(db_session, resolved) == []
    assert len(rows(db_session)) == 1  # the history stays


def test_taking_one_of_two_reactions_back_leaves_the_other_deciding(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like", at="2026-11-24T09:00:00Z")
    react(public, target, "sad", at="2026-11-24T09:05:00Z")
    react(public, target, removed=["sad"])

    assert [(f.rating, f.from_reaction) for f in feedback(db_session, resolved)] == [(5, True)]


def test_a_real_feedback_answer_replaces_the_quick_rating_and_later_reactions_never_override_it(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    token_value = action_links.mint(db_session, resolved, action_links.FEEDBACK)
    db_session.commit()
    row = action_links.find(db_session, action_links.FEEDBACK, token_value)

    action_links.submit_feedback(db_session, row, 2, "Not fixed properly")
    db_session.commit()

    assert [(f.rating, f.from_reaction, f.comment) for f in feedback(db_session, resolved)] == [(2, False, "Not fixed properly")]
    react(public, target, "heart")  # a later reaction does not override the answer
    assert [(f.rating, f.from_reaction) for f in feedback(db_session, resolved)] == [(2, False)]


def test_taking_a_reaction_back_after_the_ticket_was_reopened_still_removes_the_quick_rating(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    ticket = db_session.get(Ticket, resolved.id)
    ticket.status = TicketStatus.OPEN  # reopened
    db_session.add(ticket)
    db_session.commit()
    assert len(feedback(db_session, resolved)) == 1  # a standing reaction's rating is history

    react(public, target, removed=["like"])

    assert feedback(db_session, resolved) == []


def test_a_reaction_to_an_earlier_resolution_message_is_not_this_resolutions_rating_or_acknowledgement(db_session, ann):
    ticket = Ticket(subject="Printer", requester_email="ann@example.com", triaged_at=datetime.now(timezone.utc), status=TicketStatus.RESOLVED)
    ticket.resolved_at = datetime.now(timezone.utc) - timedelta(hours=1)  # resolved again an hour ago
    db_session.add(ticket)
    db_session.commit()
    who = reactions.Reactor(ref="ann-oid", name="Ann", user=ann)

    reactions.record_reaction(  # to the message of the first resolution, sent two days ago
        db_session, ticket, channel="teams", message_kind="resolution", message_ref="old", raw_type="sad", reactor=who,
        message_sent_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.commit()
    assert feedback(db_session, ticket) == []
    assert reactions.acknowledged_since(db_session, ticket.id, ticket.resolved_at) is False

    reactions.record_reaction(
        db_session, ticket, channel="teams", message_kind="resolution", message_ref="new", raw_type="like", reactor=who,
        message_sent_at=datetime.now(timezone.utc) - timedelta(minutes=50),
    )
    db_session.commit()
    assert [(f.rating, f.from_reaction) for f in feedback(db_session, ticket)] == [(5, True)]
    assert reactions.acknowledged_since(db_session, ticket.id, ticket.resolved_at) is True


def test_a_real_answer_to_a_later_resolution_leaves_an_earlier_quick_rating_alone(db_session):
    ticket = Ticket(subject="Printer", requester_email="ann@example.com", triaged_at=datetime.now(timezone.utc), status=TicketStatus.RESOLVED)
    ticket.resolved_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    db_session.add(TicketFeedback(
        ticket_id=ticket.id, rating=5, from_reaction=True, created_at=ticket.resolved_at - timedelta(days=3)  # an earlier round's
    ))
    db_session.add(TicketFeedback(ticket_id=ticket.id, rating=1, from_reaction=True))  # this round's
    db_session.commit()

    reactions.drop_soft_feedback(db_session, ticket)
    db_session.commit()

    assert [(f.rating, f.from_reaction) for f in feedback(db_session, ticket)] == [(5, True)]


def test_the_same_reaction_arriving_at_the_same_moment_is_recorded_once_without_an_error(public, resolved, db_session, monkeypatch):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like")
    ann = db_session.get(User, rows(db_session)[0].user_id)
    monkeypatch.setattr(reactions, "_standing", lambda *args: None)  # both requests passed the look-before-insert check

    again = reactions.record_reaction(
        db_session, resolved, channel="teams", message_kind="resolution", message_ref=target, raw_type="like",
        reactor=reactions.Reactor(ref=OID, name="Ann Able", user=ann),
    )

    assert again is None and len(rows(db_session)) == 1
    db_session.commit()  # the session is still usable


def test_a_reaction_on_a_ticket_that_is_not_resolved_is_not_a_rating(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    ticket = db_session.get(Ticket, resolved.id)
    ticket.status, ticket.resolved_at = TicketStatus.OPEN, None  # reopened since
    db_session.add(ticket)
    db_session.commit()

    react(public, target, "like")

    assert feedback(db_session, resolved) == []


def test_the_quick_rating_says_it_is_from_a_reaction_on_the_ticket_and_the_feedback_pages(client, public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like")
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)

    assert "from a reaction" in client.get(f"/tickets/{resolved.number}").text
    assert "from a reaction" in client.get("/admin/feedback").text


# ---- Acknowledgement and the chase -------------------------------------------------------------------------


def chase_settings(db_session):
    for key, value in ((CHASE_ENABLED, "on"), (AUTO_CLOSE_ENABLED, "on"), (CHASE_AFTER_DAYS, "3"), (CHASE_RESOLVE_AFTER_DAYS, "3"), (AUTO_CLOSE_AFTER_DAYS, "7")):
        set_setting(db_session, key, value)
    db_session.commit()


def chase_emails(db_session):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "chase")).all()


def test_a_positive_reaction_on_a_request_for_a_reply_stops_the_chase(public, waiting, db_session, dev_mail):
    chase_settings(db_session)
    react(public, sent_message(db_session, "awaiting_user").activity_id, "like")

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=4))
    db_session.commit()

    assert chase_emails(db_session) == []
    assert db_session.get(Ticket, waiting.id).chased_at is None and db_session.get(Ticket, waiting.id).status == TicketStatus.AWAITING_USER


def test_without_a_reaction_the_same_ticket_is_chased(public, waiting, db_session, dev_mail):
    chase_settings(db_session)

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=4))
    db_session.commit()

    assert len(chase_emails(db_session)) == 1


def test_taking_the_reaction_back_starts_the_chase_again(public, waiting, db_session, dev_mail):
    chase_settings(db_session)
    target = sent_message(db_session, "awaiting_user").activity_id
    react(public, target, "like")
    react(public, target, removed=["like"])

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=4))
    db_session.commit()

    assert len(chase_emails(db_session)) == 1


def test_a_neutral_or_negative_reaction_does_not_stop_the_chase(public, waiting, db_session, dev_mail):
    chase_settings(db_session)
    react(public, sent_message(db_session, "awaiting_user").activity_id, "surprised", "sad")

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=4))
    db_session.commit()

    assert len(chase_emails(db_session)) == 1


def test_a_chased_ticket_that_is_acknowledged_is_not_auto_resolved(public, waiting, db_session, dev_mail):
    chase_settings(db_session)
    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=4))
    db_session.commit()
    for email in chase_emails(db_session):
        email.sent_at = datetime.now(timezone.utc) + timedelta(days=4)
        db_session.add(email)
    db_session.commit()
    react(public, sent_message(db_session, "awaiting_user").activity_id, "heart")

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=9))
    db_session.commit()

    assert db_session.get(Ticket, waiting.id).status == TicketStatus.AWAITING_USER


def test_a_positive_reaction_on_the_resolution_does_not_change_the_auto_close_timer(public, resolved, db_session):
    chase_settings(db_session)
    react(public, sent_message(db_session, "resolution").activity_id, "like")

    run_auto_close(db_session, datetime.now(timezone.utc) + timedelta(days=8))
    db_session.commit()

    assert db_session.get(Ticket, resolved.id).status == TicketStatus.CLOSED  # closed on its normal timer, not before


def test_a_reaction_does_not_close_it_early(public, resolved, db_session):
    chase_settings(db_session)
    react(public, sent_message(db_session, "resolution").activity_id, "like")

    run_auto_close(db_session, datetime.now(timezone.utc) + timedelta(days=2))
    db_session.commit()

    assert db_session.get(Ticket, resolved.id).status == TicketStatus.RESOLVED


# ---- The red chip ------------------------------------------------------------------------------------------


def agent_page(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)


def test_a_negative_reaction_shows_the_chip_on_the_ticket_and_in_the_queue_until_an_agent_dismisses_it(client, public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "angry")
    agent_page(client, db_session)

    assert "Negative reaction" in client.get(f"/tickets/{resolved.number}").text
    assert "Negative reaction" in client.get("/tickets").text

    dismissed = client.post(f"/tickets/{resolved.number}/reactions/dismiss")

    assert dismissed.status_code == 200 and "Negative reaction" not in dismissed.text.split('id="thread"')[0]
    assert "Negative reaction" not in client.get("/tickets").text
    [row] = rows(db_session)
    assert row.dismissed_at is not None and row.dismissed_by_id is not None and row.removed_at is None


def test_a_new_negative_reaction_shows_the_chip_again(client, public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "angry")
    agent_page(client, db_session)
    client.post(f"/tickets/{resolved.number}/reactions/dismiss")
    assert "Negative reaction" not in client.get(f"/tickets/{resolved.number}").text

    react(public, target, "sad")

    assert "Negative reaction" in client.get(f"/tickets/{resolved.number}").text


def test_a_negative_reaction_on_any_message_to_the_requester_shows_the_chip(public, resolved, db_session):
    react(public, sent_message(db_session, "other").activity_id, "sad")

    assert reactions.open_negative_ids(db_session, [resolved.id]) == {resolved.id}


def test_taking_a_negative_reaction_back_clears_the_chip(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "sad")
    react(public, target, removed=["sad"])

    assert reactions.open_negative_ids(db_session, [resolved.id]) == set()


def test_a_positive_reaction_shows_no_chip(client, public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like")
    agent_page(client, db_session)

    assert "Negative reaction" not in client.get(f"/tickets/{resolved.number}").text


def test_only_someone_who_can_work_the_ticket_can_dismiss_the_chip(client, public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "angry")
    login_as(client, "requester@example.com")  # an end user

    assert client.post(f"/tickets/{resolved.number}/reactions/dismiss").status_code in (401, 403)
    assert reactions.open_negative_ids(db_session, [resolved.id]) == {resolved.id}


# ---- Who reacted -------------------------------------------------------------------------------------------


def test_a_reaction_from_someone_who_is_not_the_requester_or_a_follower_is_shown_and_does_nothing(client, public, resolved, db_session):
    make_user(db_session, "bob@example.com", OTHER_OID, "Bob Baker")
    target = sent_message(db_session, "resolution").activity_id

    react(public, target, "like", "angry", oid=OTHER_OID, name="Bob Baker")

    like, angry = rows(db_session)
    assert (like.by_party, angry.by_party, like.sentiment, angry.sentiment) == (False, False, "positive", "negative")
    assert feedback(db_session, resolved) == []
    assert reactions.open_negative_ids(db_session, [resolved.id]) == set()
    assert reactions.acknowledged_since(db_session, resolved.id, datetime(2000, 1, 1, tzinfo=timezone.utc)) is False
    agent_page(client, db_session)
    page = client.get(f"/tickets/{resolved.number}").text
    assert "Bob Baker" in page and "no effect" in page and "Negative reaction" not in page


def test_someone_with_no_account_is_recorded_by_the_name_teams_gave_and_has_no_effect(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like", oid="cccccccc-0000-0000-0000-000000000003", name="Cleo Visitor")

    [row] = rows(db_session)
    assert (row.user_id, row.reactor_name, row.by_party) == (None, "Cleo Visitor", False)
    assert feedback(db_session, resolved) == []


def test_a_follower_is_a_party_and_is_matched_through_the_person_not_the_address(public, resolved, db_session):
    from app.followers import add_follower

    bob = make_user(db_session, "bob@example.com", OTHER_OID, "Bob Baker")
    add_follower(db_session, resolved, bob.email, "agent@example.com")
    db_session.commit()

    react(public, sent_message(db_session, "other").activity_id, "sad", oid=OTHER_OID, name="Bob Baker")

    [row] = rows(db_session)
    assert (row.by_party, row.user_id) == (True, bob.id)
    assert reactions.open_negative_ids(db_session, [resolved.id]) == {resolved.id}


def test_the_person_is_matched_by_object_id_never_by_display_name(public, resolved, db_session, ann):
    react(public, sent_message(db_session, "resolution").activity_id, "like", oid="dddddddd-0000-0000-0000-000000000004", name="Ann Able")

    [row] = rows(db_session)
    assert row.user_id is None and row.by_party is False and feedback(db_session, resolved) == []


# ---- Showing them ------------------------------------------------------------------------------------------


def test_the_thread_shows_a_reaction_line_under_the_message_with_who_and_when_and_removed(client, public, resolved, db_session):
    target = sent_message(db_session, "resolution")
    react(public, target.activity_id, "like", at="2026-11-24T09:30:00Z")
    react(public, target.activity_id, "sad", at="2026-11-24T09:31:00Z")
    react(public, target.activity_id, removed=["sad"], at="2026-11-24T09:32:00Z")
    agent_page(client, db_session)

    page = client.get(f"/tickets/{resolved.number}").text

    assert 'aria-label="Reactions"' in page
    assert "\U0001F44D like" in page and "\U0001F622 sad" in page
    assert page.count("Ann Able") >= 2 and "removed" in page
    assert "tp-reaction-gone" in page  # the taken-back one is struck through


def test_a_reaction_to_a_bot_message_that_is_not_a_ticket_message_has_a_box_of_its_own(client, public, resolved, db_session):
    react(public, sent_message(db_session, "other").activity_id, {"type": "like"})
    agent_page(client, db_session)

    page = client.get(f"/tickets/{resolved.number}").text

    assert "Reacted to a message from the bot in Teams" in page


def test_reactions_are_not_shown_to_the_requester_in_the_portal(client, public, resolved, db_session, ann):
    react(public, sent_message(db_session, "resolution").activity_id, "like")
    login_as(client, "ann@example.com")

    page = client.get(f"/portal/{resolved.number}")

    assert page.status_code == 200 and "Reactions" not in page.text and "\U0001F44D" not in page.text


def test_team_activity_shows_a_reaction_as_a_requester_step(public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like")
    react(public, sent_message(db_session, "resolution").activity_id, "sad")
    react(public, sent_message(db_session, "resolution").activity_id, removed=["sad"])

    timeline = load_timeline(db_session, None, "UTC")

    reaction_cards = [c for c in timeline.cards if any(s.kind == "reaction" for s in c.steps)]
    assert reaction_cards and all(c.role == "requester" for c in reaction_cards)
    phrases = [s.phrase for c in reaction_cards for s in c.steps if s.kind == "reaction"]
    assert "Reacted \U0001F44D like" in phrases and "Reacted \U0001F622 sad" in phrases and "Took back \U0001F622 sad" in phrases
    assert all(c.title for c in reaction_cards)


def test_team_activity_leaves_out_a_reaction_by_someone_who_is_not_the_requester_or_a_follower(public, resolved, db_session):
    make_user(db_session, "bob@example.com", OTHER_OID, "Bob Baker")
    react(public, sent_message(db_session, "other").activity_id, "like", oid=OTHER_OID, name="Bob Baker")

    timeline = load_timeline(db_session, None, "UTC")

    assert not any(s.kind == "reaction" for c in timeline.cards for s in c.steps)
    assert len(rows(db_session)) == 1  # it is on the ticket's thread


def test_taking_a_reaction_back_is_a_step_at_the_time_it_was_taken_back(public, resolved, db_session):
    target = sent_message(db_session, "resolution").activity_id
    react(public, target, "like", at="2026-11-24T08:00:00Z")
    react(public, target, removed=["like"], at="2026-11-24T09:30:00Z")

    steps = [s for c in load_timeline(db_session, None, "UTC").cards for s in c.steps if s.kind == "reaction"]

    assert sorted((s.at.hour, s.phrase) for s in steps) == [(8, "Reacted \U0001F44D like"), (9, "Took back \U0001F44D like")]


def test_team_activity_page_and_filters_still_work_with_reactions(client, public, resolved, db_session):
    react(public, sent_message(db_session, "resolution").activity_id, "like")
    agent_page(client, db_session)

    assert "Reacted" in client.get("/tickets/activity").text
    assert "Reacted" in client.get("/tickets/activity?kind=conversation").text
    assert "Reacted" not in client.get("/tickets/activity?kind=events").text


# ---- Person data, the subject access export, anonymisation -------------------------------------------------


def test_the_new_columns_are_classified_and_the_table_is_exported_and_cleared():
    from app.anonymisation import STEPS
    from app.person_data import COLUMNS, DELETE, SUBJECT, WITH_ROW
    from app.subject_access import EXPORTED_TABLES

    assert COLUMNS["ticket_reaction.user_id"].role == SUBJECT and COLUMNS["ticket_reaction.user_id"].anon == DELETE
    assert COLUMNS["ticket_reaction.reactor_ref"].anon == DELETE
    assert COLUMNS["ticket_reaction.reactor_name"].anon == WITH_ROW and COLUMNS["ticket_reaction.reactor_address"].anon == WITH_ROW
    assert "ticket_reaction.user_id" in STEPS and "ticket_reaction.reactor_ref" in STEPS
    assert EXPORTED_TABLES["ticket_reaction"] == "reactions.json"


def reaction_world(db_session, world):
    """Ann reacted by object id and Bob by address, to a message on Ann's ticket; Bob's is not hers."""
    own, ann, bob = world["own"], world["ann"], world["bob"]
    now = datetime.now(timezone.utc)
    mine = reactions.record_reaction(
        db_session, own, channel="teams", message_kind="other", message_ref="m-1", raw_type="like",
        reactor=reactions.Reactor(ref="ann-oid", name="Ann Able", user=ann), at=now,
    )
    by_address = reactions.record_reaction(
        db_session, own, channel="email", message_kind="other", message_ref="e-1", raw_type="sad",
        reactor=reactions.Reactor(ref="ann@b.example", name="Ann", address="ann@b.example"), at=now,
    )
    theirs = reactions.record_reaction(
        db_session, own, channel="teams", message_kind="other", message_ref="m-1", raw_type="like",
        reactor=reactions.Reactor(ref="bob-oid", name="Bob Brown", user=bob), at=now,
    )
    db_session.commit()
    return mine, by_address, theirs


def test_the_subject_access_export_lists_the_persons_reactions_and_only_theirs(client, db_session, world):
    from tests.test_subject_access import admin_client, export, part

    reaction_world(db_session, world)
    admin_client(client, db_session)

    archive = export(client)

    listed = part(archive, "reactions.json")
    assert sorted((r["channel"], r["reaction"]) for r in listed) == [("email", "sad"), ("teams", "like")]
    assert "Bob Brown" not in json.dumps(listed) and "reactions.json" in part(archive, "summary.json")["files"]


def test_anonymising_a_person_deletes_their_reactions_and_keeps_everyone_elses(client, db_session, world):
    from tests.test_anonymisation import anonymise
    from tests.test_subject_access import admin_client

    _, _, theirs = reaction_world(db_session, world)
    # A quick rating made from Ann's reaction is kept (the rating is not hers once she is anonymised), without its link.
    db_session.add(TicketFeedback(ticket_id=world["own"].id, rating=5, from_reaction=True, reaction_id=rows(db_session)[0].id))
    db_session.commit()
    admin_client(client, db_session)

    response = anonymise(client)

    assert response.status_code == 200, response.text
    left = rows(db_session)
    assert [r.id for r in left] == [theirs.id]
    kept = [f for f in feedback(db_session, world["own"]) if f.from_reaction]
    assert len(kept) == 1 and kept[0].reaction_id is None and kept[0].rating == 5


def test_merging_accounts_moves_a_reaction_to_the_main_person():
    from app.linked_identities import classified_keys

    assert "ticket_reaction.user_id" in classified_keys() and "ticket_reaction.dismissed_by_id" in classified_keys()


# ---- The shared model, as email will use it ---------------------------------------------------------------


def test_an_email_reaction_uses_the_same_record_and_effects(db_session):
    """#299 reuses record_reaction / remove_reaction unchanged: another channel, another message id, the same rules."""
    ann = User(email="ann@example.com", display_name="Ann", role=UserRole.END_USER)
    db_session.add(ann)
    ticket = Ticket(subject="Printer", requester_email="ann@example.com", triaged_at=datetime.now(timezone.utc))
    db_session.add(ticket)
    db_session.commit()
    set_status(db_session, ticket, TicketStatus.AWAITING_USER, "agent@example.com")
    db_session.commit()
    who = reactions.Reactor(ref="ann@example.com", name="Ann", address="ann@example.com", user=ann)

    row = reactions.record_reaction(
        db_session, ticket, channel="email", message_kind="awaiting_user", message_ref="<graph-id-1>", raw_type="like", reactor=who, emoji="\U0001F44D"
    )
    db_session.commit()

    assert (row.channel, row.sentiment, row.by_party) == ("email", "positive", True)
    assert reactions.acknowledged_since(db_session, ticket.id, datetime(2000, 1, 1, tzinfo=timezone.utc)) is True
    assert reactions.record_reaction(
        db_session, ticket, channel="email", message_kind="awaiting_user", message_ref="<graph-id-1>", raw_type="like", reactor=who
    ) is None  # recorded already
    reactions.remove_reaction(db_session, ticket, channel="email", message_ref="<graph-id-1>", reactor_ref="ann@example.com", raw_type="like")
    db_session.commit()
    assert reactions.acknowledged_since(db_session, ticket.id, datetime(2000, 1, 1, tzinfo=timezone.utc)) is False
    assert reactions.record_reaction(db_session, ticket, channel="fax", message_kind="other", message_ref="x", raw_type="like", reactor=who) is None


def test_a_bot_message_without_an_id_or_a_ticket_is_not_kept(db_session):
    assert reactions.record_bot_message(db_session, None, "a1", "c1", "other") is None
    assert reactions.record_bot_message(db_session, 1, None, "c1", "other") is None
    assert reactions.record_bot_message(db_session, 1, "a1", "c1", "bogus") is None

"""8F (#193): the reply channel and the per-person Teams reply setting, one flow across email and Teams (every open ticket
the person requested, "Not this one?", paging, the reference prefix), files held while the bot asks, matching people at
install and the portal links on requester cards. Signed activities go to the real endpoint, the Bot Connector is a fake,
nothing touches the network."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import anonymisation, bot_cards, bot_connector, messages, teams, teams_conversations, teams_intake, teams_pending
from app.anonymisation import purge_pending_blobs
from app.config import settings
from app.models import (
    Attachment, AuditLog, Category, Message, OutboundEmail, PendingBlobDelete, TeamsConversation, TeamsHeldFile, TeamsIntake, TeamsPost,
    Ticket, TicketEvent, TicketFollower, TicketSource, TicketStatus, User, UserRole,
)
from app.reply_channel import EMAIL, PORTAL, TEAMS_CHANNEL, note_requester_wrote, requester_channel
from app.settings_store import TEAMS_PENDING_CHOICE_HOURS, set_setting
from app.storage import LocalStorage
from tests.test_teams_bot import SERVICE, activity, make_user, post, token  # noqa: F401
from tests.test_teams_intake import (  # noqa: F401  (fixtures)
    OID, OTHER_OID, PDF, Sent, an_agent, ann, card_buttons, card_of, connector, dev_mail, downloads, file_info, isolate, last_text,
    messages_of, press, public, replies, roster, say, tickets, triaged,
)
from tests.test_teams_bot import bot, metadata  # noqa: F401


@pytest.fixture
def storage(tmp_path, monkeypatch):
    local = LocalStorage(tmp_path)
    monkeypatch.setattr(teams_pending, "get_storage", lambda: local)
    return local


def an_email_ticket(db_session, subject="By email", email="ann@example.com", **fields):
    ticket = Ticket(subject=subject, requester_email=email, source=TicketSource.EMAIL, triaged_at=datetime.now(timezone.utc), status=TicketStatus.OPEN, **fields)
    db_session.add(ticket)
    db_session.commit()
    return ticket


# ---- Which channel the requester last used ---------------------------------------------------------------


def test_a_ticket_starts_on_the_channel_it_arrived_by(db_session):
    for source, channel in ((TicketSource.EMAIL, EMAIL), (TicketSource.PORTAL, PORTAL), (TicketSource.TEAMS, TEAMS_CHANNEL), (TicketSource.AGENT, EMAIL)):
        assert requester_channel(Ticket(subject="x", requester_email="a@b.c", source=source)) == channel


def test_a_teams_message_on_an_email_ticket_switches_it_to_teams_and_a_later_email_switches_it_back(public, db_session, ann):
    ticket = an_email_ticket(db_session)
    assert requester_channel(ticket) == EMAIL

    say(public, "Still broken, writing from Teams")
    db_session.expire_all()
    assert requester_channel(db_session.get(Ticket, ticket.id)) == TEAMS_CHANNEL
    assert db_session.get(Ticket, ticket.id).last_requester_channel == TEAMS_CHANNEL

    from app.intake import record_reply

    record_reply(
        db_session, db_session.get(Ticket, ticket.id), sender="ann@example.com", body="Back by email", author_id=None, text="Back by email",
        has_attachment=False, action="replied_by_email", how="x", channel=EMAIL,
    )
    db_session.commit()
    assert requester_channel(db_session.get(Ticket, ticket.id)) == EMAIL


def test_a_portal_comment_switches_the_channel_to_the_portal(client, db_session):
    from tests.conftest import login_as

    ann_id = login_as(client, "ann@example.com", "Ann Able")
    ticket = an_email_ticket(db_session)
    ticket.last_requester_channel = TEAMS_CHANNEL
    db_session.add(ticket)
    db_session.commit()
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "From the site"}).status_code == 200
    db_session.expire_all()
    assert requester_channel(db_session.get(Ticket, ticket.id)) == PORTAL


def test_a_followers_reply_never_moves_the_channel(db_session, ann):
    ticket = an_email_ticket(db_session)
    assert note_requester_wrote(db_session, ticket, "carol@example.com", TEAMS_CHANNEL) is False
    assert ticket.last_requester_channel is None
    assert note_requester_wrote(db_session, ticket, "ANN@example.com", TEAMS_CHANNEL) is True  # the requester: any case
    assert ticket.last_requester_channel == TEAMS_CHANNEL


def test_another_address_of_the_requester_counts_as_the_requester(public, db_session, ann):
    from app.models import LinkedIdentity

    other = make_user(db_session, email="ann@alias.example", oid=OTHER_OID, name="Ann Alias")
    db_session.add(LinkedIdentity(main_user_id=ann.id, secondary_user_id=other.id, email="ann@alias.example", entra_object_id=OTHER_OID, method="admin", status="active"))
    db_session.commit()
    assert note_requester_wrote(db_session, Ticket(subject="x", requester_email="ann@alias.example", source=TicketSource.EMAIL), "ann@example.com", TEAMS_CHANNEL)


# ---- Where an agent's reply goes -------------------------------------------------------------------------


def chat_with(db_session):
    return db_session.exec(select(TeamsConversation)).first()


def test_a_reply_on_a_ticket_last_used_in_teams_goes_to_teams_only_by_default(public, db_session, ann, dev_mail):
    ticket = an_email_ticket(db_session)
    say(public, "Writing from Teams")  # the ticket is now on Teams
    messages.email_requester(db_session, db_session.get(Ticket, ticket.id), an_agent(db_session), "We are on it")
    db_session.commit()

    assert db_session.exec(select(OutboundEmail)).all() == []
    [post_row] = db_session.exec(select(TeamsPost)).all()
    assert post_row.kind == "requester_message"


def test_after_the_requester_writes_by_email_again_replies_go_by_email(public, db_session, ann, dev_mail):
    ticket = an_email_ticket(db_session)
    ticket.last_requester_channel = EMAIL
    db_session.add(ticket)
    db_session.commit()
    say(public, "From Teams")
    ticket = db_session.get(Ticket, ticket.id)
    ticket.last_requester_channel = EMAIL  # their next email
    db_session.add(ticket)
    db_session.commit()

    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1 and db_session.exec(select(TeamsPost)).all() == []


def test_a_ticket_raised_in_teams_replies_by_email_once_the_requester_wrote_by_email(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    ticket.last_requester_channel = EMAIL
    db_session.add(ticket)
    db_session.commit()
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1 and db_session.exec(select(TeamsPost)).all() == []


def test_the_person_can_choose_teams_and_email(public, db_session, ann, dev_mail):
    ann.teams_replies_also_email = True
    db_session.add(ann)
    db_session.commit()
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1 and len(db_session.exec(select(TeamsPost)).all()) == 1


def test_automatic_notices_follow_the_same_rule(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.record_notice(db_session, ticket, "Your request has been approved.")
    db_session.commit()
    assert db_session.exec(select(OutboundEmail)).all() == [] and len(db_session.exec(select(TeamsPost)).all()) == 1

    ann.teams_replies_also_email = True
    db_session.add(ann)
    db_session.commit()
    messages.record_notice(db_session, ticket, "And now it is done.")
    db_session.commit()
    assert [r.kind for r in db_session.exec(select(OutboundEmail)).all()] == ["notice"]


def test_a_reply_that_copies_followers_still_goes_by_email(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    db_session.add(TicketFollower(ticket_id=ticket.id, email="carol@example.com", added_by_email="ann@example.com"))
    db_session.commit()
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    [email] = db_session.exec(select(OutboundEmail)).all()
    assert email.kind == "reply" and len(db_session.exec(select(TeamsPost)).all()) == 1  # the followers' copy cannot be sent without the To


def test_with_no_chat_the_reply_goes_by_email(db_session, ann, dev_mail, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(settings, "teams_bot_tenant_id", "22222222-2222-2222-2222-222222222222")
    ticket = Ticket(subject="x", requester_email="ann@example.com", source=TicketSource.TEAMS, triaged_at=datetime.now(timezone.utc), status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1 and db_session.exec(select(TeamsPost)).all() == []


def test_with_the_bot_off_the_reply_goes_by_email(db_session, ann, dev_mail):
    ticket = Ticket(subject="x", requester_email="ann@example.com", source=TicketSource.TEAMS, triaged_at=datetime.now(timezone.utc), status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1 and db_session.exec(select(TeamsPost)).all() == []


def queued_reply(public, db_session, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    message = messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    return ticket, message


def refused(status):
    request = httpx.Request("POST", SERVICE + "v3/conversations/x/activities")

    def send(delivery):
        raise httpx.HTTPStatusError("refused", request=request, response=httpx.Response(status, request=request))

    return send


@pytest.mark.parametrize("status", [400, 403, 404])
def test_when_the_bot_gives_up_the_reply_is_emailed_so_it_is_never_lost(public, db_session, ann, dev_mail, status):
    ticket, message = queued_reply(public, db_session, dev_mail)
    assert db_session.exec(select(OutboundEmail)).all() == []

    assert teams.deliver_pending(db_session, bot_send=refused(status)) == (0, 1)
    db_session.commit()

    [email] = db_session.exec(select(OutboundEmail)).all()
    assert (email.message_id, email.ticket_id, email.kind, email.to_address) == (message.id, ticket.id, "reply", "ann@example.com")


def test_a_reply_waiting_for_a_retry_is_not_emailed_yet(public, db_session, ann, dev_mail):
    queued_reply(public, db_session, dev_mail)
    assert teams.deliver_pending(db_session, bot_send=refused(503)) == (0, 1)
    db_session.commit()
    assert db_session.exec(select(OutboundEmail)).all() == []  # Teams may still take it


def test_when_the_retries_run_out_the_reply_is_emailed(public, db_session, ann, dev_mail):
    _, message = queued_reply(public, db_session, dev_mail)
    now = datetime.now(timezone.utc)
    for attempt in range(teams.MAX_ATTEMPTS):
        teams.deliver_pending(db_session, bot_send=refused(503), now=now + timedelta(days=attempt))
        db_session.commit()
    assert [e.message_id for e in db_session.exec(select(OutboundEmail)).all()] == [message.id]


def test_a_reply_that_goes_both_ways_is_not_emailed_twice_when_teams_gives_up(public, db_session, ann, dev_mail):
    ann.teams_replies_also_email = True
    db_session.add(ann)
    db_session.commit()
    queued_reply(public, db_session, dev_mail)
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1
    teams.deliver_pending(db_session, bot_send=refused(403))
    db_session.commit()
    assert len(db_session.exec(select(OutboundEmail)).all()) == 1


def test_an_internal_note_is_never_rescued_by_email(public, db_session, ann, dev_mail):
    ticket, _ = queued_reply(public, db_session, dev_mail)
    note = messages.add_note(db_session, ticket, db_session.exec(select(User).where(User.role == UserRole.AGENT)).one(), "internal")
    db_session.add(TeamsPost(kind="requester_message", ticket_id=ticket.id, message_id=note.id))
    db_session.commit()
    assert teams.deliver_pending(db_session, bot_send=lambda d: None) == (1, 1)  # the real reply goes; the note is refused
    db_session.commit()
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.message_id == note.id)).all() == []


# ---- The setting on My settings --------------------------------------------------------------------------


def test_my_settings_offers_the_choice_when_the_bot_is_on_and_saves_it(client, db_session, bot):
    from tests.conftest import login_as

    ann_id = login_as(client, "ann@example.com", "Ann Able")
    page = client.get("/settings/notifications").text
    assert "How I get replies to my requests in Teams" in page and 'value="teams" checked' in page

    assert client.post("/settings/notifications", data={"teams_replies": "both"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(User, ann_id).teams_replies_also_email is True
    assert 'value="both" checked' in client.get("/settings/notifications").text
    audit = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "notification_settings")).all()
    assert any("Teams and email" in a.detail for a in audit)

    client.post("/settings/notifications", data={"teams_replies": "teams"})
    db_session.expire_all()
    assert db_session.get(User, ann_id).teams_replies_also_email is False


def test_agents_have_it_too_and_it_is_hidden_when_the_bot_is_off(client, db_session):
    from tests.conftest import login_as, set_role

    agent_id = login_as(client, "agent@example.com", "Alex Agent")
    set_role(db_session, agent_id, UserRole.AGENT)
    assert "How I get replies" not in client.get("/settings/notifications").text  # the bot is off
    client.post("/settings/notifications", data={"teams_replies": "both"})
    db_session.expire_all()
    assert db_session.get(User, agent_id).teams_replies_also_email is False  # nothing to choose without the bot


def test_a_made_up_value_changes_nothing(client, db_session, bot):
    from tests.conftest import login_as

    ann_id = login_as(client, "ann@example.com", "Ann Able")
    client.post("/settings/notifications", data={"teams_replies": "everywhere"})
    db_session.expire_all()
    assert db_session.get(User, ann_id).teams_replies_also_email is False


# ---- Which tickets the bot considers ---------------------------------------------------------------------


def test_every_open_ticket_the_person_requested_counts_whatever_its_source(public, connector, db_session, ann):
    by_mail = an_email_ticket(db_session, "By email")
    by_portal = Ticket(subject="By portal", requester_email="ann@example.com", source=TicketSource.PORTAL, triaged_at=by_mail.triaged_at, status=TicketStatus.OPEN)
    db_session.add(by_portal)
    db_session.commit()

    say(public, "It is about both of them")

    card = card_of(connector)
    assert {t for t in card_buttons(card)} == {f"Add to {by_portal.reference}", f"Add to {by_mail.reference}", "A new request"}
    assert len(tickets(db_session)) == 2


def test_a_ticket_the_person_only_follows_is_left_out(public, db_session, ann):
    bob = an_email_ticket(db_session, "Bob's", email="bob@example.com")
    db_session.add(TicketFollower(ticket_id=bob.id, email="ann@example.com", added_by_email="bob@example.com"))
    db_session.commit()
    say(public, "My own problem")
    assert len(tickets(db_session)) == 2 and messages_of(db_session, bob) == []  # a new ticket, not a reply on Bob's


def test_closed_tickets_split_parts_and_other_peoples_tickets_are_left_out(public, db_session, ann):
    closed = an_email_ticket(db_session, "Closed")
    closed.status = TicketStatus.CLOSED
    part = an_email_ticket(db_session, "Part")
    original = an_email_ticket(db_session, "Original")
    part.split_from_id = original.id
    db_session.add_all([closed, part])
    db_session.commit()

    seen = [t.subject for t in teams_intake.teams_tickets(db_session, db_session.exec(select(User)).first())]
    assert seen == ["Original"]


def test_a_person_with_one_open_email_ticket_has_the_message_added_to_it(public, connector, db_session, ann):
    ticket = an_email_ticket(db_session)
    say(public, "More detail")
    assert [m.body for m in messages_of(db_session, ticket)] == ["More detail"] and len(tickets(db_session)) == 1


# ---- "Added to ... Not this one?" ------------------------------------------------------------------------


def test_the_answer_names_the_ticket_and_offers_a_new_request(public, connector, db_session, ann):
    category = Category(name="Printing")
    db_session.add(category)
    db_session.commit()
    ticket = an_email_ticket(db_session, "Printer *is* jammed", category_id=category.id)

    say(public, "It is on floor 3")

    card = card_of(connector)
    text = json.dumps(card)
    assert f"Added to {ticket.reference}." in text and "Printing" in text and "Printer \\\\*is\\\\* jammed" in text  # subject made plain
    assert f"{ticket.created_at.day} {ticket.created_at.strftime('%b %Y')}" in text
    button = card_buttons(card)["Not this one? Start a new request"]
    assert button["verb"] == bot_cards.VERB_MOVE


def test_not_this_one_moves_the_message_and_its_files_to_a_new_ticket(public, connector, db_session, ann, downloads, storage):
    downloads.served["https://files.example.net/d/1"] = PDF
    old = an_email_ticket(db_session)
    say(public, "My monitor flickers\nand it started today", id="m2", attachments=[file_info()])
    [moved_message] = messages_of(db_session, old)
    assert len(db_session.exec(select(Attachment)).all()) == 1
    button = card_buttons(card_of(connector))["Not this one? Start a new request"]

    answer = press(public, button)

    new = tickets(db_session)[1]
    assert new.source == TicketSource.TEAMS and new.subject == "My monitor flickers" and new.status == TicketStatus.NEW
    assert f"it is no longer on {old.reference}" in json.dumps(answer["value"]) and "logged as " + new.reference in json.dumps(answer["value"])
    assert messages_of(db_session, old) == [] and [m.id for m in messages_of(db_session, new)] == [moved_message.id]
    [stored] = db_session.exec(select(Attachment)).all()
    assert stored.ticket_id == new.id and stored.message_id == moved_message.id
    for ticket, other in ((old, new), (new, old)):
        [event] = db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "message_moved")).all()
        assert other.reference in (event.to_value or event.from_value)
    assert db_session.exec(select(TeamsIntake).order_by(TeamsIntake.id)).all()[-1].outcome == "moved"


def test_it_is_done_once(public, connector, db_session, ann):
    an_email_ticket(db_session)
    say(public, "My monitor flickers", id="m2")
    button = card_buttons(card_of(connector))["Not this one? Start a new request"]
    press(public, button)
    again = press(public, button)
    assert again["value"] == teams_intake.ALREADY_DONE and len(tickets(db_session)) == 2


def test_someone_else_cannot_move_the_message(public, connector, db_session, ann):
    make_user(db_session, email="bob@example.com", oid=OTHER_OID, name="Bob Baker")
    old = an_email_ticket(db_session)
    say(public, "My monitor flickers", id="m2")
    button = card_buttons(card_of(connector))["Not this one? Start a new request"]
    assert press(public, button, oid=OTHER_OID)["value"] == teams_intake.ALREADY_DONE
    assert len(tickets(db_session)) == 1 and len(messages_of(db_session, old)) == 1


def test_a_ticket_closed_since_cannot_have_a_message_taken_from_it(public, connector, db_session, ann):
    old = an_email_ticket(db_session)
    say(public, "My monitor flickers", id="m2")
    old = db_session.get(Ticket, old.id)
    old.status = TicketStatus.CLOSED
    db_session.add(old)
    db_session.commit()
    answer = press(public, card_buttons(card_of(connector))["Not this one? Start a new request"])
    assert answer["value"] == teams_intake.NO_LONGER_OPEN and len(tickets(db_session)) == 1


def test_a_reply_that_moved_the_ticket_to_in_progress_is_put_back_when_moved(public, connector, db_session, ann):
    old = an_email_ticket(db_session)
    old.status = TicketStatus.AWAITING_USER
    db_session.add(old)
    db_session.commit()
    say(public, "My monitor flickers", id="m2")
    assert tickets(db_session)[0].status == TicketStatus.IN_PROGRESS

    press(public, card_buttons(card_of(connector))["Not this one? Start a new request"])

    assert tickets(db_session)[0].status == TicketStatus.AWAITING_USER


def test_a_follower_update_still_waiting_for_the_moved_message_is_dropped(public, connector, db_session, ann, dev_mail):
    old = an_email_ticket(db_session)
    db_session.add(TicketFollower(ticket_id=old.id, email="carol@example.com", added_by_email="ann@example.com"))
    db_session.commit()
    say(public, "My monitor flickers", id="m2")
    assert [e.kind for e in db_session.exec(select(OutboundEmail)).all()] == ["follower_update"]

    press(public, card_buttons(card_of(connector))["Not this one? Start a new request"])

    assert db_session.exec(select(OutboundEmail)).all() == []


def test_choosing_from_the_card_has_no_not_this_one_button(public, connector, db_session, ann):
    first = an_email_ticket(db_session, "One")
    an_email_ticket(db_session, "Two")
    say(public, "Which?", id="q1")
    answer = press(public, card_buttons(card_of(connector))[f"Add to {first.reference}"])
    assert "Not this one" not in json.dumps(answer["value"])


def test_a_reference_prefix_never_offers_not_this_one(public, connector, db_session, ann):
    ticket = an_email_ticket(db_session)
    say(public, f"{ticket.reference}: still broken")
    assert replies(connector)[-1] == ("text", f"Added to {ticket.reference}.")  # a plain answer: the person named it themselves


# ---- The choice card -------------------------------------------------------------------------------------


def test_each_entry_shows_reference_subject_category_date_and_status(public, connector, db_session, ann):
    category = Category(name="Network")
    db_session.add(category)
    db_session.commit()
    first = an_email_ticket(db_session, "Wifi drops", category_id=category.id)
    second = an_email_ticket(db_session, "Laptop slow")
    say(public, "Which one?", id="q1")

    text = json.dumps(card_of(connector))
    for ticket in (first, second):
        assert ticket.reference in text and ticket.subject in text and "Open" in text
    assert "Network" in text and "Not categorised yet" in text and f"{first.created_at.day} {first.created_at.strftime('%b %Y')}" in text


def seven(db_session, count=12):
    return [an_email_ticket(db_session, f"Request {n}") for n in range(count)]


def shown(card):
    return [t.removeprefix("Add to ") for t in card_buttons(card) if t.startswith("Add to ")]


def test_the_card_shows_the_five_most_recently_updated_and_pages_through_the_rest(public, connector, db_session, ann):
    made = seven(db_session)
    ordered = [t.reference for t in reversed(made)]  # newest first (equal times fall back to the highest id)
    say(public, "Which?", id="q1")
    card = card_of(connector)
    assert shown(card) == ordered[:5] and "Showing 1 to 5 of your 12" in json.dumps(card)
    assert "Show newer" not in card_buttons(card)

    second = press(public, card_buttons(card)["Show more"])
    assert second["type"] == "application/vnd.microsoft.card.adaptive"
    page2 = second["value"]
    assert shown(page2) == ordered[5:10] and "Showing 6 to 10 of your 12" in json.dumps(page2) and "Show newer" in card_buttons(page2)

    third = press(public, card_buttons(page2)["Show more"])["value"]
    assert shown(third) == ordered[10:] and "Show more" not in card_buttons(third)

    back = press(public, card_buttons(third)["Show newer"])["value"]
    assert shown(back) == ordered[5:10]
    # Paging is not an answer: the question is still open and a ticket can be picked from any page.
    assert db_session.exec(select(TeamsIntake).order_by(TeamsIntake.id.desc())).first().outcome == "asked"
    picked = press(public, card_buttons(third)[f"Add to {ordered[11]}"])
    assert f"Added to {ordered[11]}." in json.dumps(picked["value"])


def test_paging_is_for_the_person_who_was_asked(public, connector, db_session, ann):
    make_user(db_session, email="bob@example.com", oid=OTHER_OID, name="Bob Baker")
    seven(db_session)
    say(public, "Which?", id="q1")
    answer = press(public, card_buttons(card_of(connector))["Show more"], oid=OTHER_OID)
    assert answer["type"] == "application/vnd.microsoft.activity.message" and answer["value"] == teams_intake.ALREADY_DONE


def test_the_page_comes_from_the_current_list_so_a_ticket_closed_meanwhile_is_gone(public, connector, db_session, ann):
    made = seven(db_session, 7)
    say(public, "Which?", id="q1")
    card = card_of(connector)
    newest = db_session.get(Ticket, made[-1].id)
    newest.status = TicketStatus.CLOSED
    db_session.add(newest)
    db_session.commit()
    page = press(public, card_buttons(card)["Show more"])["value"]
    assert made[-1].reference not in json.dumps(page)


# ---- The reference prefix --------------------------------------------------------------------------------


def test_a_message_starting_with_a_reference_goes_straight_to_that_ticket(public, connector, db_session, ann):
    first = an_email_ticket(db_session, "One")
    second = an_email_ticket(db_session, "Two")

    say(public, f"{first.reference}: still broken")

    assert [m.body for m in messages_of(db_session, first)] == ["still broken"] and messages_of(db_session, second) == []
    assert len(replies(connector)) == 1 and replies(connector)[0][0] == "text" and f"Added to {first.reference}." in last_text(connector)


@pytest.mark.parametrize("form", ["{ref}: still broken", "{lower}: still broken", "{ref} still broken", "  {ref}:still broken"])
def test_the_reference_can_be_written_in_the_usual_ways(public, db_session, ann, form):
    first = an_email_ticket(db_session, "One")
    an_email_ticket(db_session, "Two")
    say(public, form.format(ref=first.reference, lower=first.reference.lower()))
    assert [m.body for m in messages_of(db_session, first)] == ["still broken"]


def test_the_reference_of_someone_elses_ticket_is_refused_with_no_details(public, connector, db_session, ann):
    theirs = an_email_ticket(db_session, "Bob's secret project", email="bob@example.com")
    say(public, f"{theirs.reference}: let me in")

    text = last_text(connector)
    assert text == teams_intake.NOT_YOURS.format(reference=theirs.reference)
    assert "secret" not in text and "bob" not in text.lower()
    assert messages_of(db_session, theirs) == [] and len(tickets(db_session)) == 1


@pytest.mark.parametrize("reference", ["INC-99999", "SR-1"])
def test_a_reference_that_does_not_exist_gets_the_same_answer(public, connector, db_session, ann, reference):
    say(public, f"{reference}: hello")
    assert last_text(connector) == teams_intake.NOT_YOURS.format(reference=reference) and len(tickets(db_session)) == 0


def test_a_followed_only_closed_or_split_ticket_is_not_theirs_to_add_to(public, connector, db_session, ann):
    closed = an_email_ticket(db_session, "Closed")
    closed.status = TicketStatus.CLOSED
    db_session.add(closed)
    db_session.commit()
    say(public, f"{closed.reference}: hello")
    assert last_text(connector) == teams_intake.NOT_YOURS.format(reference=closed.reference) and messages_of(db_session, closed) == []


def test_a_reference_with_nothing_after_it_asks_for_the_message(public, connector, db_session, ann):
    ticket = an_email_ticket(db_session)
    say(public, f"{ticket.reference}:")
    assert last_text(connector) == teams_intake.NEEDS_TEXT_AFTER_REFERENCE.format(reference=ticket.reference) and messages_of(db_session, ticket) == []


def test_a_reference_in_the_middle_of_a_message_is_just_words(public, connector, db_session, ann):
    ticket = an_email_ticket(db_session)
    say(public, f"About {ticket.reference} and more")
    assert [m.body for m in messages_of(db_session, ticket)] == [f"About {ticket.reference} and more"]  # the one open ticket anyway
    other = an_email_ticket(db_session, "Another")
    say(public, f"Also see {ticket.reference}", id="q2")
    assert replies(connector)[-1][0] == "card"  # two open: it asks, because the reference is not at the start


def test_new_still_forces_a_new_ticket(public, db_session, ann):
    ticket = an_email_ticket(db_session)
    say(public, "new: something else")
    assert len(tickets(db_session)) == 2 and messages_of(db_session, ticket) == []


def test_a_resolved_ticket_named_by_reference_is_reopened_by_the_message(public, db_session, ann):
    ticket = an_email_ticket(db_session)
    ticket.status = TicketStatus.RESOLVED
    ticket.resolved_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    say(public, f"{ticket.reference}: it is back")
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).status != TicketStatus.RESOLVED


# ---- Files held while the bot asks -----------------------------------------------------------------------


def two_tickets(db_session):
    return an_email_ticket(db_session, "One"), an_email_ticket(db_session, "Two")


def asking_with_a_file(public, connector, downloads, storage):
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "Photo of the error", id="q1", attachments=[file_info("err.pdf")])
    return card_of(connector)


def test_a_file_sent_while_the_bot_asks_is_downloaded_once_and_held(public, connector, db_session, ann, downloads, storage):
    two_tickets(db_session)
    card = asking_with_a_file(public, connector, downloads, storage)

    assert len(downloads.calls) == 1
    [held] = db_session.exec(select(TeamsHeldFile)).all()
    assert (held.filename, held.size, held.skipped) == ("err.pdf", len(PDF), None) and storage.read(held.blob_key) == PDF
    assert db_session.exec(select(Attachment)).all() == []  # no ticket yet
    assert "1 file will be added to the request you choose" in json.dumps(card)
    assert held.intake_id == db_session.exec(select(TeamsIntake).where(TeamsIntake.outcome == "asked")).one().id


def test_picking_a_ticket_attaches_the_held_files_to_it(public, connector, db_session, ann, downloads, storage):
    first, second = two_tickets(db_session)
    card = asking_with_a_file(public, connector, downloads, storage)

    answer = press(public, card_buttons(card)[f"Add to {second.reference}"])

    assert f"Added to {second.reference}." in json.dumps(answer["value"])
    [stored] = db_session.exec(select(Attachment)).all()
    assert (stored.ticket_id, stored.filename, stored.content_type) == (second.id, "err.pdf", "application/pdf")
    assert stored.message_id == messages_of(db_session, second)[0].id
    assert storage.read(stored.blob_key) == PDF
    assert db_session.exec(select(TeamsHeldFile)).all() == []  # released
    assert db_session.exec(select(PendingBlobDelete)).all() == []  # and the stored file is in use, so it stays
    assert len(downloads.calls) == 1  # not fetched again


def test_choosing_a_new_request_attaches_them_to_the_new_ticket(public, connector, db_session, ann, downloads, storage):
    two_tickets(db_session)
    card = asking_with_a_file(public, connector, downloads, storage)
    press(public, card_buttons(card)["A new request"])
    new = tickets(db_session)[2]
    assert [a.ticket_id for a in db_session.exec(select(Attachment)).all()] == [new.id]


def test_a_file_that_could_not_be_fetched_is_named_on_the_ticket_chosen(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    downloads.served["https://files.example.net/d/1"] = httpx.ConnectError("down")
    say(public, "Photo", id="q1", attachments=[file_info("err.pdf")])
    card = card_of(connector)
    assert "err.pdf (could not be downloaded)" in json.dumps(card)

    press(public, card_buttons(card)[f"Add to {first.reference}"])

    assert db_session.exec(select(Attachment)).all() == []
    event = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one()
    assert (event.ticket_id, event.to_value, event.detail) == (first.id, "err.pdf", "could not be downloaded")


def test_a_file_the_rules_refuse_is_noted_when_the_choice_is_made_and_the_stored_copy_is_queued_for_deletion(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    downloads.served["https://files.example.net/d/1"] = b"MZ\x90\x00 an executable"
    say(public, "Run this", id="q1", attachments=[file_info("tool.exe")])
    key = db_session.exec(select(TeamsHeldFile)).one().blob_key

    press(public, card_buttons(card_of(connector))[f"Add to {first.reference}"])

    assert db_session.exec(select(Attachment)).all() == []
    assert "cannot be attached" in db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one().detail
    assert [p.blob_key for p in db_session.exec(select(PendingBlobDelete)).all()] == [key]


def test_the_same_bytes_in_two_pending_questions_are_one_stored_file_kept_while_anything_uses_it(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "One", id="q1", attachments=[file_info()])
    card_one = card_of(connector)
    say(public, "Two", id="q2", attachments=[file_info()])
    assert len({h.blob_key for h in db_session.exec(select(TeamsHeldFile)).all()}) == 1

    press(public, card_buttons(card_one)[f"Add to {first.reference}"])  # the first question is answered: its attachment uses the file
    assert db_session.exec(select(PendingBlobDelete)).all() == [] and len(db_session.exec(select(TeamsHeldFile)).all()) == 1

    row = db_session.exec(select(TeamsIntake).where(TeamsIntake.outcome == "asked")).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=48)
    db_session.add(row)
    db_session.commit()
    teams_pending.expire_pending(db_session)
    db_session.commit()
    assert db_session.exec(select(TeamsHeldFile)).all() == [] and db_session.exec(select(PendingBlobDelete)).all() == []  # still in use


def test_an_unanswered_question_expires_and_its_files_are_deleted_through_the_blob_queue(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    asking_with_a_file(public, connector, downloads, storage)
    held = db_session.exec(select(TeamsHeldFile)).one()
    key = held.blob_key
    row = db_session.exec(select(TeamsIntake).where(TeamsIntake.outcome == "asked")).one()

    row.created_at = datetime.now(timezone.utc) - timedelta(hours=23)  # not yet
    db_session.add(row)
    db_session.commit()
    assert teams_pending.expire_pending(db_session) == 0
    assert len(db_session.exec(select(TeamsHeldFile)).all()) == 1

    row = db_session.get(TeamsIntake, row.id)
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db_session.add(row)
    db_session.commit()
    assert teams_pending.expire_pending(db_session) == 1
    db_session.commit()

    assert db_session.exec(select(TeamsHeldFile)).all() == []
    assert db_session.get(TeamsIntake, row.id).outcome == "expired"
    assert [p.blob_key for p in db_session.exec(select(PendingBlobDelete)).all()] == [key]
    assert storage.exists(key)
    assert purge_pending_blobs(db_session, storage) == (1, 0)  # the existing cleanup deletes it from storage
    db_session.commit()
    assert not storage.exists(key)


def test_the_expiry_is_a_setting(public, connector, db_session, ann, downloads, storage):
    two_tickets(db_session)
    asking_with_a_file(public, connector, downloads, storage)
    row = db_session.exec(select(TeamsIntake).where(TeamsIntake.outcome == "asked")).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db_session.add(row)
    set_setting(db_session, TEAMS_PENDING_CHOICE_HOURS, "2")
    db_session.commit()
    assert teams_pending.expire_pending(db_session) == 1


def test_pressing_a_button_after_it_expired_says_so_and_does_nothing(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    card = asking_with_a_file(public, connector, downloads, storage)
    row = db_session.exec(select(TeamsIntake).where(TeamsIntake.outcome == "asked")).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
    db_session.add(row)
    db_session.commit()
    teams_pending.expire_pending(db_session)
    db_session.commit()

    answer = press(public, card_buttons(card)[f"Add to {first.reference}"])

    assert answer["value"] == teams_intake.QUESTION_EXPIRED and db_session.exec(select(Attachment)).all() == []
    assert press(public, card_buttons(card)["A new request"])["value"] == teams_intake.QUESTION_EXPIRED
    assert len(tickets(db_session)) == 2


def test_the_expiry_runs_as_a_recurring_job(db_session):
    from app import scheduled_jobs

    assert [j for j in scheduled_jobs.SCHEDULED_JOBS if j.name == "teams-pending"][0].run is teams_pending.expire_pending


def test_an_answered_question_is_not_expired(public, connector, db_session, ann, downloads, storage):
    first, _ = two_tickets(db_session)
    card = asking_with_a_file(public, connector, downloads, storage)
    press(public, card_buttons(card)[f"Add to {first.reference}"])
    row = db_session.exec(select(TeamsIntake).order_by(TeamsIntake.id.desc())).first()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=100)
    db_session.add(row)
    db_session.commit()
    assert teams_pending.expire_pending(db_session) == 0 and len(db_session.exec(select(Attachment)).all()) == 1


def test_anonymising_the_requester_drops_their_held_files(public, connector, db_session, ann, downloads, storage):
    two_tickets(db_session)
    asking_with_a_file(public, connector, downloads, storage)
    key = db_session.exec(select(TeamsHeldFile)).one().blob_key

    assert teams_pending.release_for_tickets(db_session, [t.id for t in db_session.exec(select(Ticket)).all()]) == 1
    db_session.commit()

    assert db_session.exec(select(TeamsHeldFile)).all() == []
    assert [p.blob_key for p in db_session.exec(select(PendingBlobDelete)).all()] == [key]


def test_the_classification_and_the_anonymisation_step_exist_for_the_new_table():
    from app.person_data import COLUMNS

    assert COLUMNS["teams_held_file.filename"].anon == "delete"
    assert "teams_held_file.filename" in anonymisation.STEPS


def test_a_held_file_is_part_of_the_subject_access_export(db_session, ann, public, connector, downloads, storage):
    from app.person_data import find_person
    from app.subject_access import _teams_pending_files

    two_tickets(db_session)
    asking_with_a_file(public, connector, downloads, storage)
    [listed] = _teams_pending_files(db_session, find_person(db_session, "ann@example.com"))
    assert (listed["filename"], listed["size"]) == ("err.pdf", len(PDF))
    assert _teams_pending_files(db_session, find_person(db_session, "bob@example.com")) == []


def test_a_message_too_long_for_the_card_holds_and_fetches_nothing(public, connector, db_session, ann, downloads, storage):
    two_tickets(db_session)
    say(public, "z" * 4000, attachments=[file_info()])
    assert last_text(connector) == teams_intake.TOO_LONG_TO_ASK and downloads.calls == [] and db_session.exec(select(TeamsHeldFile)).all() == []


# ---- Matching people to their chat at install ------------------------------------------------------------


def install(public, **fields):
    response = post(public, activity("installationUpdate", action="add", **fields), token())
    assert response.status_code == 200


def test_installing_for_someone_with_no_object_id_on_record_links_the_chat_through_their_address(public, db_session, roster):
    roster["member"] = {"objectId": OID, "email": "Ann.Able@Example.com", "name": "Ann Able"}
    account = User(email="ann.able@example.com", display_name="Ann", role=UserRole.END_USER)
    db_session.add(account)
    db_session.commit()

    install(public)

    row = db_session.exec(select(TeamsConversation)).one()
    assert row.user_id == account.id and row.entra_object_id == OID
    assert teams_conversations.reference_for(db_session, account).id == row.id
    assert ("teams_bot_linked", "ann.able@example.com") in [(a.action, a.actor_email) for a in db_session.exec(select(AuditLog)).all()]
    assert roster["calls"] == [(SERVICE, "a:conv-1", "29:teams-user")]
    db_session.expire_all()
    assert db_session.get(User, account.id).entra_object_id is None  # signing in writes that, not install


def test_the_link_survives_the_persons_next_message(public, db_session, roster):
    roster["member"] = {"objectId": OID, "email": "ann.able@example.com"}
    account = User(email="ann.able@example.com", display_name="Ann", role=UserRole.END_USER)
    db_session.add(account)
    db_session.commit()
    install(public)
    say(public, "hello")
    assert db_session.exec(select(TeamsConversation)).one().user_id == account.id


def test_a_linked_identitys_address_matches_the_main_person(public, db_session, roster):
    from app.models import LinkedIdentity

    main = User(email="ann@example.com", display_name="Ann", role=UserRole.END_USER)
    second = User(email="ann@alias.example", display_name="Ann alias", role=UserRole.END_USER, is_active=False)
    db_session.add_all([main, second])
    db_session.commit()
    db_session.add(LinkedIdentity(main_user_id=main.id, secondary_user_id=second.id, email="ann@alias.example", method="admin", status="active"))
    db_session.commit()
    roster["member"] = {"objectId": OID, "email": "ann@alias.example"}
    install(public)
    assert db_session.exec(select(TeamsConversation)).one().user_id == main.id


def test_the_display_name_is_never_used(public, db_session, roster):
    roster["member"] = {"objectId": OID, "name": "Ann Able"}  # no address at all
    db_session.add(User(email="someone@example.com", display_name="Ann Able", role=UserRole.END_USER))
    db_session.commit()
    install(public)
    assert db_session.exec(select(TeamsConversation)).one().user_id is None


@pytest.mark.parametrize("member", [
    {"objectId": OTHER_OID, "email": "ann.able@example.com"},  # Teams describes someone else
    {"objectId": OID, "email": "not an address"},
    {"objectId": OID, "email": "nobody@example.com"},  # no account has it
    httpx.ConnectError("down"),
])
def test_nothing_is_linked_when_the_address_is_missing_wrong_or_unknown(public, db_session, roster, member):
    roster["member"] = member
    db_session.add(User(email="ann.able@example.com", display_name="Ann", role=UserRole.END_USER))
    db_session.commit()
    install(public)
    row = db_session.exec(select(TeamsConversation)).one()  # the chat is still stored
    assert row.user_id is None


def test_an_account_bound_to_another_directory_object_is_not_linked(public, db_session, roster):
    roster["member"] = {"objectId": OID, "email": "ann.able@example.com"}
    db_session.add(User(email="ann.able@example.com", display_name="Ann", role=UserRole.END_USER, entra_object_id=OTHER_OID))
    db_session.commit()
    install(public)
    assert db_session.exec(select(TeamsConversation)).one().user_id is None


def test_an_inactive_or_anonymised_account_is_not_linked(public, db_session, roster):
    roster["member"] = {"objectId": OID, "email": "ann.able@example.com"}
    db_session.add(User(email="ann.able@example.com", display_name="Ann", role=UserRole.END_USER, is_active=False))
    db_session.commit()
    install(public)
    assert db_session.exec(select(TeamsConversation)).one().user_id is None


def test_a_known_object_id_needs_no_lookup(public, db_session, roster, ann):
    install(public)
    assert roster["calls"] == [] and db_session.exec(select(TeamsConversation)).one().user_id == ann.id


# ---- Links on requester cards ----------------------------------------------------------------------------


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(settings, "web_base_url", "https://web.example.org")
    monkeypatch.setattr(settings, "public_base_url", "https://public.example.org")


def test_the_link_depends_on_the_audience(db_session, base):
    ticket = an_email_ticket(db_session)
    assert teams.link_for(ticket, teams.AUDIENCE_AGENT) == f"https://web.example.org/tickets/{ticket.number}"
    assert teams.link_for(ticket, teams.AUDIENCE_REQUESTER) == f"https://web.example.org/portal/{ticket.number}"


def test_an_agent_card_opens_the_console_and_a_requester_card_the_portal(db_session, base):
    from app.models import Notification

    ticket = an_email_ticket(db_session)
    note = Notification(user_id=1, kind="replied", text="x")
    [agent_button] = bot_cards.what_happened_card(note, ticket)["actions"]
    [requester_button] = bot_cards.what_happened_card(note, ticket, teams.AUDIENCE_REQUESTER)["actions"]
    assert (agent_button["title"], agent_button["url"]) == ("Open ticket", f"https://web.example.org/tickets/{ticket.number}")
    assert (requester_button["title"], requester_button["url"]) == ("Open in the portal", f"https://web.example.org/portal/{ticket.number}")


def test_the_audience_is_the_persons_role(db_session):
    agent = User(email="a@example.com", display_name="A", role=UserRole.AGENT)
    admin = User(email="s@example.com", display_name="S", role=UserRole.SUPER_ADMIN)
    person = User(email="p@example.com", display_name="P", role=UserRole.END_USER)
    assert [bot_cards.audience_of(u) for u in (agent, admin, person)] == ["agent", "agent", "requester"]


def test_a_notification_card_for_a_requester_links_to_the_portal(db_session, base):
    from app.models import Notification

    person = make_user(db_session, email="p@example.com", oid=OTHER_OID, name="P")
    ticket = an_email_ticket(db_session, email="p@example.com")
    note = Notification(user_id=person.id, kind="replied", text="x", ticket_id=ticket.id)
    db_session.add(note)
    db_session.commit()
    [button] = bot_cards.notification_card(db_session, note, ticket)["actions"]
    assert button["url"] == f"https://web.example.org/portal/{ticket.number}"


def test_the_added_card_and_the_agent_reply_message_carry_the_portal_link(public, connector, db_session, ann, base, dev_mail):
    ticket = an_email_ticket(db_session)
    say(public, "More detail")
    card = card_of(connector)
    assert [a["url"] for a in card["actions"] if a["type"] == "Action.OpenUrl"] == [f"https://web.example.org/portal/{ticket.number}"]

    ticket = db_session.get(Ticket, ticket.id)
    messages.email_requester(db_session, ticket, an_agent(db_session), "Reply")
    db_session.commit()
    sent = Sent()
    assert sent.deliver(db_session) == (1, 0)
    assert sent.direct[0].text.endswith(f"Open in the portal: https://web.example.org/portal/{ticket.number}")


def test_with_no_address_known_there_is_no_link(public, db_session, ann, monkeypatch):
    monkeypatch.setattr(settings, "web_base_url", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    ticket = an_email_ticket(db_session)
    assert teams.link_for(ticket, teams.AUDIENCE_REQUESTER) is None and bot_cards.open_ticket_action(ticket, teams.AUDIENCE_REQUESTER) == []

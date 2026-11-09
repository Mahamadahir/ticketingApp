"""8D: ticket intake from Teams. Messages are signed by the test key set of test_teams_bot.py and posted to the real
endpoint; tickets, replies, files and agent replies go through the real code. The Bot Connector is the fake of
test_teams_bot.py, and the file download is replaced where a test needs a file. Nothing here touches the network."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import bot_connector, intake, messages, rate_limit, teams, teams_intake
from app.bot_connector import FileRefused
from app.config import settings
from app.email_templates import save_template
from app.models import (
    Attachment, AuditLog, Message, MessageDirection, Notification, OutboundEmail, TeamsConversation, TeamsIntake, TeamsPost, Ticket,
    TicketEvent, TicketSource, TicketStatus, User, UserRole,
)
from app.never_follow import add_listed
from app.raising import source_label
from app.settings_store import INBOUND_MAX_ATTACHMENTS, TEAMS_MAX_PER_PERSON_HOUR, set_setting
from app.storage import LocalStorage
from app.thanks_rules import add_rule
from tests.test_teams_bot import APP_ID, SERVICE, TENANT, activity, bot, connector, make_user, metadata, post, public, token  # noqa: F401  (fixtures)

OID = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_OID = "bbbbbbbb-0000-0000-0000-000000000002"
PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"

_ids = iter(range(1, 10_000))


def say(public, text="My printer is jammed", *, oid=OID, id=None, **fields):
    """A person's message to the bot, posted to the endpoint. Returns nothing: look at the database and the connector."""
    body = activity("message", oid=oid, id=id or f"msg-{next(_ids)}", text=text, **fields)
    body["from"]["name"] = "Ann Able" if oid == OID else "Bob Baker"
    response = post(public, body, token())
    assert response.status_code == 200
    return response


def tickets(db_session):
    db_session.expire_all()
    return db_session.exec(select(Ticket).order_by(Ticket.id)).all()


def messages_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()


def replies(connector):
    """What the bot said to the person: the text of each message, or 'CARD' plus its JSON for a card."""
    said = []
    for request in connector.requests:
        if request.method != "POST":
            continue
        body = json.loads(request.content)
        if body.get("attachments"):
            said.append(("card", body["attachments"][0]["content"]))
        else:
            said.append(("text", body["text"]))
    return said


def last_text(connector):
    kind, value = replies(connector)[-1]
    assert kind == "text"
    return value


def card_buttons(card):
    """Every Action.Execute button of a card by title, in its actions or inside its body (the choice card has one per request)."""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Action.Execute":
                found[node["title"]] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return found


def press(public, button, oid=OID, **overrides):
    """The press of one button of a card the bot sent: Teams sends the action's verb and data as an invoke."""
    data = {**button["data"], **overrides}
    body = activity(
        "invoke", oid=oid, id=f"press-{next(_ids)}", name="adaptiveCard/action",
        value={"action": {"type": "Action.Execute", "verb": button["verb"], "data": data}, "trigger": "manual"},
    )
    response = post(public, body, token())
    assert response.status_code == 200
    return response.json()


def card_of(connector, index=-1):
    kind, value = replies(connector)[index]
    assert kind == "card"
    return value


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", False)
    monkeypatch.setattr(settings, "graph_shared_mailbox", "itsupport@example.com")
    rate_limit.TEAMS_PERSON.reset()
    rate_limit.TEAMS_PERSON_NOTICE.reset()
    monkeypatch.setattr(intake, "get_storage", lambda: LocalStorage(tmp_path))
    monkeypatch.setattr(settings, "teams_file_host_suffixes", ".example.net")
    monkeypatch.setattr(bot_connector, "resolve", lambda host: ["93.184.216.34"])  # a public address, whatever the name


@pytest.fixture
def ann(db_session):
    return make_user(db_session)


def an_agent(db_session, email="agent@example.com"):
    agent = User(email=email, display_name="Alex Agent", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.commit()
    return agent


# ---- A new ticket ----------------------------------------------------------------------------------------


def test_a_message_raises_a_teams_ticket_in_new_and_the_bot_acknowledges_it(public, connector, db_session, ann):
    say(public, "  Printer on 3 is jammed  \nIt prints grey lines and then stops.\nThanks")

    [ticket] = tickets(db_session)
    assert (ticket.source, ticket.status, ticket.requester_email, ticket.subject) == (
        TicketSource.TEAMS, TicketStatus.NEW, "ann@example.com", "Printer on 3 is jammed"
    )
    assert ticket.triaged_at is None  # for triage, like email
    [first] = messages_of(db_session, ticket)
    assert first.body == "Printer on 3 is jammed  \nIt prints grey lines and then stops.\nThanks"
    assert (first.direction, first.sender, first.author_user_id) == (MessageDirection.INBOUND, "ann@example.com", ann.id)
    assert source_label(db_session, ticket) == "Raised in Teams by Ann Able"
    assert last_text(connector) == f"Your issue has been logged as {ticket.reference} with IT. We'll be in touch."
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "created_by_teams")).one()
    assert audit.entity_id == ticket.id
    assert db_session.exec(select(TeamsIntake)).one().outcome == "ticket_created"


def test_the_acknowledgement_is_the_admin_edited_template(public, connector, db_session, ann):
    save_template(db_session, "acknowledgement", None, "Got it: {reference}.", "admin@example.com")
    db_session.commit()
    say(public)
    assert last_text(connector) == f"Got it: {tickets(db_session)[0].reference}."


def test_a_long_first_line_is_cut_for_the_subject_and_the_whole_message_is_kept(public, db_session, ann):
    say(public, "x" * 500)
    [ticket] = tickets(db_session)
    assert ticket.subject == "x" * 200 and messages_of(db_session, ticket)[0].body == "x" * 500


def test_a_message_over_the_length_limit_is_cut_like_an_email(public, db_session, ann):
    say(public, "y" * 20_000)
    body = messages_of(db_session, tickets(db_session)[0])[0].body
    assert len(body) == 10_000 and body.endswith("[cut short]")


def test_the_ticket_is_announced_like_any_new_ticket(public, db_session, ann, monkeypatch):
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", True)
    say(public)
    assert [p.kind for p in db_session.exec(select(TeamsPost)).all()] == ["new_ticket"]


def test_an_empty_message_or_help_gets_the_help_text_and_makes_nothing(public, connector, db_session, ann):
    say(public, "   ")
    say(public, "Help")
    assert tickets(db_session) == [] and all("Start a message with new:" in t for k, t in replies(connector))


# ---- Replying --------------------------------------------------------------------------------------------


def test_a_message_while_a_ticket_is_open_is_added_to_it(public, connector, db_session, ann):
    say(public, "Printer is jammed")
    [ticket] = tickets(db_session)
    agent = an_agent(db_session)
    ticket.assigned_agent_id = agent.id
    db_session.add(ticket)
    db_session.commit()

    say(public, "It is the one on floor 3")

    [ticket] = tickets(db_session)
    assert [m.body for m in messages_of(db_session, ticket)] == ["Printer is jammed", "It is the one on floor 3"]
    assert f"Added to {ticket.reference}." in json.dumps(card_of(connector))  # a card that names it (tests/test_teams_followups.py)
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "replied_by_teams")).one().entity_id == ticket.id
    told = db_session.exec(select(Notification).where(Notification.user_id == agent.id)).all()
    assert [n.kind for n in told] == ["replied"]  # the assignee hears of it, as for mail


def test_a_reply_on_a_ticket_waiting_for_the_requester_hands_it_back(public, db_session, ann):
    say(public)
    [ticket] = tickets(db_session)
    ticket.triaged_at, ticket.status = ticket.created_at, TicketStatus.AWAITING_USER
    db_session.add(ticket)
    db_session.commit()

    say(public, "Restarted it, still broken")

    assert tickets(db_session)[0].status == TicketStatus.IN_PROGRESS


def test_a_ticket_raised_by_email_is_added_to_when_the_person_is_its_requester(public, db_session, ann):
    other = Ticket(subject="By email", requester_email="ann@example.com", source=TicketSource.EMAIL)
    db_session.add(other)
    db_session.commit()
    say(public, "More about that")
    assert len(tickets(db_session)) == 1 and [m.body for m in messages_of(db_session, other)] == ["More about that"]


def test_someone_elses_ticket_is_never_added_to(public, db_session, ann):
    bob = make_user(db_session, email="bob@example.com", oid=OTHER_OID, name="Bob Baker")
    say(public, "Bob's problem", oid=OTHER_OID)
    say(public, "Ann's own problem")
    first, second = tickets(db_session)
    assert (first.requester_email, second.requester_email) == (bob.email, "ann@example.com")
    assert len(messages_of(db_session, first)) == 1


def test_new_always_raises_a_new_ticket(public, connector, db_session, ann):
    say(public, "Printer is jammed")
    say(public, "New: my laptop will not boot")
    say(public, " NEW :another one")

    first, second, third = tickets(db_session)
    assert (second.subject, third.subject) == ("my laptop will not boot", "another one")
    assert messages_of(db_session, second)[0].body == "my laptop will not boot"
    assert len(messages_of(db_session, first)) == 1


def test_new_with_nothing_after_it_asks_for_the_request(public, connector, db_session, ann):
    say(public, "new:")
    assert tickets(db_session) == [] and "after new:" in last_text(connector)


# ---- The choice of ticket --------------------------------------------------------------------------------


def two_open(public, db_session):
    say(public, "Printer is jammed")
    say(public, "new: laptop will not boot")
    return tickets(db_session)


def test_with_two_open_tickets_a_card_asks_which(public, connector, db_session, ann):
    first, second = two_open(public, db_session)
    connector.requests.clear()

    say(public, "It is on floor 3", id="the-question")

    assert len(tickets(db_session)) == 2 and messages_of(db_session, first) != [] and len(messages_of(db_session, first)) == 1
    card = card_of(connector)
    buttons = card_buttons(card)
    assert set(buttons) == {f"Add to {first.reference}", f"Add to {second.reference}", "A new request"}
    assert "It is on floor 3" in json.dumps(card)
    assert db_session.exec(select(TeamsIntake).order_by(TeamsIntake.id.desc())).first().outcome == "asked"


def test_choosing_a_ticket_adds_the_message_to_it_and_replaces_the_card(public, connector, db_session, ann):
    first, second = two_open(public, db_session)
    say(public, "It is on floor 3", id="q1")
    buttons = card_buttons(card_of(connector))

    answer = press(public, buttons[f"Add to {second.reference}"])

    assert answer["statusCode"] == 200 and answer["type"] == "application/vnd.microsoft.card.adaptive"
    assert f"Added to {second.reference}." in json.dumps(answer["value"]) and "Action.Execute" not in json.dumps(answer["value"])
    assert [m.body for m in messages_of(db_session, second)] == ["laptop will not boot", "It is on floor 3"]
    assert len(messages_of(db_session, first)) == 1


def test_choosing_a_new_request_raises_one_from_the_message(public, connector, db_session, ann):
    two_open(public, db_session)
    say(public, "Please reset my VPN", id="q1")

    answer = press(public, card_buttons(card_of(connector))["A new request"])

    assert "logged as" in json.dumps(answer["value"])
    third = tickets(db_session)[2]
    assert (third.subject, third.source) == ("Please reset my VPN", TicketSource.TEAMS)


def test_a_card_is_answered_once(public, connector, db_session, ann):
    first, second = two_open(public, db_session)
    say(public, "It is on floor 3", id="q1")
    buttons = card_buttons(card_of(connector))

    press(public, buttons["A new request"])
    again = press(public, buttons[f"Add to {first.reference}"])

    assert again["type"] == "application/vnd.microsoft.activity.message" and again["value"] == teams_intake.ALREADY_DONE
    assert len(tickets(db_session)) == 3 and len(messages_of(db_session, first)) == 1


def test_a_card_cannot_be_used_by_someone_else(public, connector, db_session, ann):
    first, second = two_open(public, db_session)
    make_user(db_session, email="bob@example.com", oid=OTHER_OID, name="Bob Baker")
    say(public, "It is on floor 3", id="q1")
    buttons = card_buttons(card_of(connector))

    # Bob presses Ann's forwarded card: it names Ann's ticket, which is not his; nothing changes.
    named = press(public, buttons[f"Add to {first.reference}"], oid=OTHER_OID)
    assert named["value"] == teams_intake.NO_LONGER_OPEN
    # Bob presses "a new request": the question was Ann's, not his, so there is nothing for him to answer.
    new = press(public, buttons["A new request"], oid=OTHER_OID)
    assert new["value"] == teams_intake.ALREADY_DONE

    assert len(tickets(db_session)) == 2 and len(messages_of(db_session, first)) == 1
    assert db_session.exec(select(TeamsIntake).order_by(TeamsIntake.id.desc())).first().outcome == "asked"  # still Ann's to answer


def test_a_press_naming_a_ticket_that_is_not_open_is_refused_and_the_card_stays(public, connector, db_session, ann):
    first, second = two_open(public, db_session)
    say(public, "It is on floor 3", id="q1")
    buttons = card_buttons(card_of(connector))
    first = db_session.get(Ticket, first.id)
    first.status = TicketStatus.CLOSED
    db_session.add(first)
    db_session.commit()

    answer = press(public, buttons[f"Add to {first.reference}"])

    assert answer["value"] == teams_intake.NO_LONGER_OPEN
    assert press(public, buttons["A new request"])["type"] == "application/vnd.microsoft.card.adaptive"  # the other buttons still work


def test_more_than_five_open_tickets_offer_the_five_latest_first(public, connector, db_session, ann):
    for number in range(7):
        say(public, f"new: request {number}")
    say(public, "and more", id="q1")

    card = card_of(connector)
    titles = [t for t in card_buttons(card) if t.startswith("Add to")]
    assert len(titles) == 5 and "Showing 1 to 5 of your 7" in json.dumps(card) and "Show more" in card_buttons(card)
    assert "request 6" in json.dumps(card) and "request 0" not in json.dumps(card)


def test_a_message_too_long_to_hold_while_asking_says_what_to_do(public, connector, db_session, ann):
    two_open(public, db_session)
    say(public, "z" * 4000)
    assert last_text(connector) == teams_intake.TOO_LONG_TO_ASK and len(tickets(db_session)) == 2


# ---- Resolved and closed tickets -------------------------------------------------------------------------


def resolved_ticket(public, db_session):
    say(public, "Printer is jammed")
    ticket = tickets(db_session)[0]
    ticket.triaged_at, ticket.status = ticket.created_at, TicketStatus.RESOLVED
    ticket.resolved_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_a_thank_you_after_resolution_is_added_and_leaves_the_ticket_resolved(public, connector, db_session, ann):
    ticket = resolved_ticket(public, db_session)
    add_rule(db_session, "equals", "thanks", 10, "admin@example.com")
    db_session.commit()

    say(public, "Thanks!")

    assert len(tickets(db_session)) == 1 and tickets(db_session)[0].status == TicketStatus.RESOLVED
    assert len(messages_of(db_session, ticket)) == 2
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "thanks_ignored")).one().ticket_id == ticket.id


def test_anything_else_after_resolution_asks_and_choosing_the_ticket_reopens_it(public, connector, db_session, ann):
    ticket = resolved_ticket(public, db_session)

    say(public, "It is jammed again")
    card = card_of(connector)
    buttons = card_buttons(card)
    assert "adding to it reopens it" in json.dumps(card)
    assert tickets(db_session)[0].status == TicketStatus.RESOLVED  # nothing yet
    answer = press(public, buttons[f"Add to {ticket.reference}"])

    assert "It has been reopened." in json.dumps(answer["value"])
    assert tickets(db_session)[0].status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)


def test_a_closed_ticket_is_not_added_to_the_next_message_raises_a_new_one(public, db_session, ann):
    ticket = resolved_ticket(public, db_session)
    ticket.status = TicketStatus.CLOSED
    db_session.add(ticket)
    db_session.commit()

    say(public, "Different problem")

    assert len(tickets(db_session)) == 2 and len(messages_of(db_session, ticket)) == 1


# ---- Agent replies go to Teams as well as by email -------------------------------------------------------


class Sent:
    def __init__(self):
        self.direct = []

    def deliver(self, db_session):
        result = teams.deliver_pending(db_session, send=lambda p: pytest.fail("channel"), bot_send=self.direct.append)
        db_session.commit()
        return result


@pytest.fixture
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def triaged(db_session, ticket):
    ticket = db_session.get(Ticket, ticket.id)
    ticket.triaged_at, ticket.status = ticket.created_at, TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_an_agents_email_to_a_teams_requester_goes_to_teams_and_by_email_and_is_the_first_response(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    agent = an_agent(db_session)

    sent_message = messages.email_requester(db_session, ticket, agent, "Please power it off and on")
    db_session.commit()

    assert db_session.exec(select(OutboundEmail)).all() == []  # Teams only, the default: the email does not go (tests/test_teams_followups.py)
    [post_row] = db_session.exec(select(TeamsPost)).all()
    assert (post_row.kind, post_row.message_id, post_row.ticket_id) == ("requester_message", sent_message.id, ticket.id)
    sent = Sent()
    assert sent.deliver(db_session) == (1, 0)
    [delivery] = sent.direct
    assert (delivery.service_url, delivery.conversation_id, delivery.card) == (SERVICE, "a:conv-1", None)
    assert delivery.text == f"[{ticket.reference}] Printer is jammed\n\nPlease power it off and on\n\nOpen in the portal: https://tickets.example.org/portal/{ticket.number}"
    assert db_session.get(Ticket, ticket.id).first_responded_at is not None
    assert db_session.exec(select(TeamsPost)).one().sent_at is not None


def test_the_text_is_sent_as_a_plain_message_into_the_persons_chat(public, connector, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(db_session, ticket, an_agent(db_session), "See *this* [link](https://x.example)")
    db_session.commit()
    connector.requests.clear()

    assert teams.deliver_pending(db_session) == (1, 0)

    request = connector.requests[0]
    assert str(request.url) == "https://smba.example.net/emea/v3/conversations/a%3Aconv-1/activities"
    body = json.loads(request.content)
    assert body["textFormat"] == "plain" and "See *this* [link](https://x.example)" in body["text"]


def test_with_mail_off_the_teams_message_still_goes(public, db_session, ann):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(db_session, ticket, an_agent(db_session), "Hello")
    db_session.commit()
    assert db_session.exec(select(OutboundEmail)).all() == [] and len(db_session.exec(select(TeamsPost)).all()) == 1


def test_an_automatic_notice_also_goes_to_teams(public, db_session, ann):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.record_notice(db_session, ticket, "Your request has been approved.")
    db_session.commit()
    sent = Sent()
    assert sent.deliver(db_session) == (1, 0) and "approved" in sent.direct[0].text


def test_an_internal_note_never_reaches_teams(public, db_session, ann):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    note = messages.add_note(db_session, ticket, an_agent(db_session), "Requester is difficult, escalate quietly")
    db_session.commit()
    assert db_session.exec(select(TeamsPost)).all() == []

    # Even a row that points at a note is refused when it is sent: the same rule as the email.
    db_session.add(TeamsPost(kind="requester_message", ticket_id=ticket.id, message_id=note.id))
    db_session.commit()
    sent = Sent()
    assert sent.deliver(db_session) == (0, 1) and sent.direct == []
    assert "may not see" in db_session.exec(select(TeamsPost)).one().last_error


def test_only_teams_raised_tickets_and_people_with_a_chat_get_a_teams_copy(public, db_session, ann):
    say(public, "Printer is jammed")
    mine = triaged(db_session, tickets(db_session)[0])
    by_mail = Ticket(subject="By mail", requester_email="ann@example.com", source=TicketSource.EMAIL, triaged_at=mine.created_at, status=TicketStatus.OPEN)
    nobody = Ticket(subject="No chat", requester_email="carol@example.com", source=TicketSource.TEAMS, triaged_at=mine.created_at, status=TicketStatus.OPEN)
    db_session.add_all([by_mail, nobody])
    db_session.commit()
    agent = an_agent(db_session)
    for ticket in (by_mail, nobody):
        messages.email_requester(db_session, ticket, agent, "Hello")
    db_session.commit()
    assert db_session.exec(select(TeamsPost)).all() == []


def test_a_message_for_a_requester_who_left_is_not_sent(public, db_session, ann):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(db_session, ticket, an_agent(db_session), "Hello")
    ann.is_active = False
    db_session.add(ann)
    db_session.commit()
    sent = Sent()
    assert sent.deliver(db_session) == (0, 1) and sent.direct == []


def test_a_message_is_never_sent_to_teams_twice(public, db_session, ann):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    message = messages.email_requester(db_session, ticket, an_agent(db_session), "Hello")
    db_session.commit()
    db_session.add(TeamsPost(kind="requester_message", ticket_id=ticket.id, message_id=message.id))
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


# ---- Files -----------------------------------------------------------------------------------------------


def file_info(name="report.pdf", url="https://files.example.net/d/1"):
    return {"contentType": "application/vnd.microsoft.teams.file.download.info", "name": name, "content": {"downloadUrl": url, "fileType": "pdf"}}


@pytest.fixture
def downloads(monkeypatch):
    served = {}
    calls = []

    def fetch(url, max_bytes, *, service_url=None, with_token=False):
        calls.append((url, with_token))
        item = served[url]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(bot_connector, "fetch_file", fetch)
    fetch.served, fetch.calls = served, calls
    return fetch


def test_a_file_is_stored_on_the_ticket_through_the_attachment_rules(public, db_session, ann, downloads, tmp_path):
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "See the report", attachments=[file_info(), {"contentType": "text/html", "content": "<p>See the report</p>"}])

    [ticket] = tickets(db_session)
    [stored] = db_session.exec(select(Attachment)).all()
    assert (stored.ticket_id, stored.filename, stored.content_type, stored.size) == (ticket.id, "report.pdf", "application/pdf", len(PDF))
    assert stored.message_id == messages_of(db_session, ticket)[0].id
    assert LocalStorage(tmp_path).read(stored.blob_key) == PDF
    assert downloads.calls == [("https://files.example.net/d/1", False)]  # the bot's token is not sent to a download address


def test_a_file_sent_alone_makes_a_ticket_named_after_it(public, db_session, ann, downloads):
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "", attachments=[file_info()])
    [ticket] = tickets(db_session)
    assert ticket.subject == "File: report.pdf" and messages_of(db_session, ticket)[0].body == "(files only)"
    assert len(db_session.exec(select(Attachment)).all()) == 1


def test_a_file_over_the_size_limit_is_noted_not_kept_and_the_person_is_told(public, connector, db_session, ann, downloads):
    downloads.served["https://files.example.net/d/1"] = FileRefused("larger than the limit")
    say(public, "See the report", attachments=[file_info()])

    [ticket] = tickets(db_session)
    assert db_session.exec(select(Attachment)).all() == []
    event = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one()
    assert (event.ticket_id, event.to_value) == (ticket.id, "report.pdf") and "MB limit" in event.detail
    assert "Not kept: report.pdf (larger than the" in last_text(connector)
    assert last_text(connector).startswith("Your issue has been logged as")  # the ticket was still made


def test_a_file_the_rules_refuse_is_noted_and_the_message_still_counts(public, db_session, ann, downloads):
    downloads.served["https://files.example.net/d/1"] = b"MZ\x90\x00 an executable"
    say(public, "Run this", attachments=[file_info("tool.exe")])
    assert db_session.exec(select(Attachment)).all() == []
    assert "cannot be attached" in db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one().detail
    assert len(tickets(db_session)) == 1


def test_files_beyond_the_count_limit_are_not_fetched(public, db_session, ann, downloads):
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "1")
    db_session.commit()
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "Two files", attachments=[file_info("a.pdf"), file_info("b.pdf", "https://files.example.net/d/2")])

    assert [a.filename for a in db_session.exec(select(Attachment)).all()] == ["a.pdf"]
    assert [c[0] for c in downloads.calls] == ["https://files.example.net/d/1"]
    event = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one()
    assert event.to_value == "b.pdf" and "more than 1 files" in event.detail


def test_a_file_that_cannot_be_fetched_is_noted_and_does_not_stop_the_message(public, db_session, ann, downloads):
    downloads.served["https://files.example.net/d/1"] = httpx.ConnectError("down")
    say(public, "See the report", attachments=[file_info()])
    assert len(tickets(db_session)) == 1
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one().detail == "could not be downloaded"


def test_a_pasted_picture_is_fetched_with_the_bot_token_only_from_the_service_address(public, db_session, ann, downloads):
    picture = f"{SERVICE}v3/attachments/abc/views/original"
    downloads.served[picture] = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    say(public, "Screenshot", attachments=[{"contentType": "image/png", "name": "image.png", "contentUrl": picture}])
    assert downloads.calls == [(picture, True)]


# ---- The same rules as email -----------------------------------------------------------------------------


def test_words_from_a_never_follow_address_are_ignored_and_audited(public, connector, db_session, ann):
    add_listed(db_session, "ann@example.com", "a shared account", "admin@example.com")
    db_session.commit()

    say(public, "Hello?")

    assert tickets(db_session) == [] and replies(connector) == []
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "mail_ignored")).one().detail.startswith("ann@example.com:")


def test_the_shared_mailbox_is_never_a_requester(public, connector, db_session):
    make_user(db_session, email="itsupport@example.com", name="IT Support")
    say(public, "Hello?")
    assert tickets(db_session) == [] and replies(connector) == []


def test_an_inactive_or_anonymised_account_is_never_a_requester(public, connector, db_session, ann):
    ann.is_active = False
    db_session.add(ann)
    db_session.commit()
    say(public, "Hello?")
    assert tickets(db_session) == [] and last_text(connector) == teams_intake.NOT_ACTIVE

    ann.is_active, ann.email = True, "anonymised-7@invalid"
    db_session.add(ann)
    db_session.commit()
    say(public, "Hello again?")
    assert tickets(db_session) == []


def test_one_persons_messages_are_capped_per_hour(public, connector, db_session, ann):
    set_setting(db_session, TEAMS_MAX_PER_PERSON_HOUR, "2")
    db_session.commit()

    say(public, "one")
    say(public, "two")
    say(public, "three")
    say(public, "four")

    [ticket] = tickets(db_session)
    assert len(messages_of(db_session, ticket)) == 2
    texts = [t for k, t in replies(connector)]
    assert texts.count(teams_intake.SLOW_DOWN) == 1  # told once, not once per message
    set_setting(db_session, TEAMS_MAX_PER_PERSON_HOUR, "3")
    db_session.commit()
    say(public, "five")
    assert len(messages_of(db_session, ticket)) == 3


def test_the_cap_is_per_person_and_only_counts_the_last_hour(public, db_session, ann):
    make_user(db_session, email="bob@example.com", oid=OTHER_OID, name="Bob Baker")
    set_setting(db_session, TEAMS_MAX_PER_PERSON_HOUR, "1")
    db_session.commit()
    say(public, "Ann one")
    say(public, "Bob one", oid=OTHER_OID)
    say(public, "Ann two")  # over her cap
    assert len(tickets(db_session)) == 2 and len(messages_of(db_session, tickets(db_session)[0])) == 1

    for row in db_session.exec(select(TeamsIntake)).all():
        row.created_at = row.created_at - timedelta(hours=2)
        db_session.add(row)
    db_session.commit()
    say(public, "Ann later")
    assert len(messages_of(db_session, tickets(db_session)[0])) == 2


def test_teams_sending_a_message_again_does_nothing_more(public, connector, db_session, ann):
    say(public, "Printer is jammed", id="same")
    say(public, "Printer is jammed", id="same")
    assert len(tickets(db_session)) == 1 and len(messages_of(db_session, tickets(db_session)[0])) == 1
    assert len(replies(connector)) == 1  # and the person is not answered twice


def test_a_message_sent_again_is_not_fetched_or_looked_up_again(public, db_session, downloads, roster):
    downloads.served["https://files.example.net/d/1"] = PDF
    say(public, "See the report", id="same", attachments=[file_info()])
    say(public, "See the report", id="same", attachments=[file_info()])
    assert len(downloads.calls) == 1 and len(roster["calls"]) == 1 and len(db_session.exec(select(Attachment)).all()) == 1


def test_a_failure_leaves_nothing_half_made_and_the_person_is_told(public, connector, db_session, ann, monkeypatch):
    real = intake.record_inbound
    broken = [True]

    def boom(*args, **kwargs):
        if broken[0]:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(intake, "record_inbound", boom)
    say(public, "Printer is jammed", id="m1")
    assert tickets(db_session) == [] and db_session.exec(select(TeamsIntake)).all() == []
    assert last_text(connector) == teams_intake.FAILED

    broken[0] = False  # Teams sends it again later and it works
    say(public, "Printer is jammed", id="m1")
    assert len(tickets(db_session)) == 1


def test_a_group_chat_message_makes_nothing(public, connector, db_session, ann):
    post(public, activity("message", conversation_type="groupChat", text="hi", id="g1"), token())
    assert tickets(db_session) == [] and replies(connector) == []


# ---- A person with no account yet ------------------------------------------------------------------------


@pytest.fixture
def roster(monkeypatch):
    """What the chat says about its member."""
    state = {"member": {"objectId": OID, "email": "New.Person@Example.com", "name": "Nina Newcomer"}, "calls": []}

    def get_member(service_url, conversation_id, teams_user_id):
        state["calls"].append((service_url, conversation_id, teams_user_id))
        if isinstance(state["member"], Exception):
            raise state["member"]
        return state["member"]

    monkeypatch.setattr(bot_connector, "get_member", get_member)
    return state


def test_a_person_with_no_account_is_matched_by_object_id_and_gets_one_as_at_sign_in(public, connector, db_session, roster):
    say(public, "Printer is jammed")

    user = db_session.exec(select(User).where(User.email == "new.person@example.com")).one()
    assert (user.entra_object_id, user.display_name, user.role) == (OID, "Nina Newcomer", UserRole.END_USER)
    [ticket] = tickets(db_session)
    assert (ticket.requester_email, ticket.source) == ("new.person@example.com", TicketSource.TEAMS)
    assert messages_of(db_session, ticket)[0].author_user_id == user.id
    assert roster["calls"] == [(SERVICE, "a:conv-1", "29:teams-user")]
    assert db_session.exec(select(TeamsConversation)).one().user_id == user.id  # their chat is found by the account now

    say(public, "More detail")  # known by the id now: no second lookup
    assert len(roster["calls"]) == 1 and len(messages_of(db_session, ticket)) == 2


def test_an_account_made_from_an_email_is_the_same_person_and_gets_the_object_id(public, db_session, roster):
    existing = User(email="new.person@example.com", display_name="N Person", role=UserRole.END_USER)
    db_session.add(existing)
    db_session.commit()
    say(public, "Printer is jammed")
    db_session.expire_all()
    assert db_session.get(User, existing.id).entra_object_id == OID and len(db_session.exec(select(User)).all()) == 1


def test_an_address_bound_to_another_directory_identity_is_refused_and_nothing_is_made(public, connector, db_session, roster):
    db_session.add(User(email="new.person@example.com", display_name="Old", role=UserRole.END_USER, entra_object_id=OTHER_OID))
    db_session.commit()
    say(public, "Printer is jammed")
    assert tickets(db_session) == [] and "different account" in last_text(connector)
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "sso_oid_mismatch")).one()  # and it is audited


@pytest.mark.parametrize("member", [{"objectId": OID, "name": "No Mail"}, {"objectId": OTHER_OID, "email": "x@example.com"}, {"objectId": OID, "email": "not an address"}, httpx.ConnectError("down")])
def test_a_person_who_cannot_be_matched_is_told_and_nothing_is_made(public, connector, db_session, roster, member):
    roster["member"] = member
    say(public, "Printer is jammed")
    assert tickets(db_session) == [] and db_session.exec(select(User)).all() == []
    assert last_text(connector) == teams_intake.COULD_NOT_MATCH


def test_the_user_principal_name_is_used_when_there_is_no_email(public, db_session, roster):
    roster["member"] = {"objectId": OID, "userPrincipalName": "nina@example.com", "name": "Nina"}
    say(public, "Printer is jammed")
    assert tickets(db_session)[0].requester_email == "nina@example.com"


def test_a_linked_identity_is_the_main_person(public, db_session, ann):
    from app.models import LinkedIdentity

    other = make_user(db_session, email="ann@alias.example", oid=OTHER_OID, name="Ann Alias")
    db_session.add(LinkedIdentity(main_user_id=ann.id, secondary_user_id=other.id, email="ann@alias.example", entra_object_id=OTHER_OID, method="admin", status="active"))
    db_session.commit()
    say(public, "From the alias", oid=OTHER_OID)
    assert tickets(db_session)[0].requester_email == "ann@example.com"


# ---- The pieces ------------------------------------------------------------------------------------------


def test_the_key_of_a_message_holds_neither_the_person_nor_the_activity_id_and_needs_the_secret_to_check(monkeypatch):
    key = teams_intake.activity_key(OID, "act-77")
    assert len(key) == 64 and OID not in key and "act-77" not in key
    assert key != teams_intake.activity_key(OTHER_OID, "act-77") and key != teams_intake.activity_key(OID, "act-78")
    import hashlib

    assert key != hashlib.sha256(f"{OID}:act-77".encode()).hexdigest()  # not a plain hash of guessable parts
    monkeypatch.setattr(settings, "session_secret_key", "another-secret-entirely")
    assert teams_intake.activity_key(OID, "act-77") != key


def fake_files(monkeypatch, handler):
    monkeypatch.setattr(bot_connector, "new_file_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))


def test_fetch_file_reads_a_file_within_the_limit_without_the_bot_token(monkeypatch):
    seen = []
    fake_files(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, content=PDF))[1])
    assert bot_connector.fetch_file("https://files.example.net/d/1?x=1", 1000) == PDF
    assert "Authorization" not in seen[0].headers


def test_fetch_file_sends_the_token_only_to_the_service_host(monkeypatch):
    seen = []
    monkeypatch.setattr(bot_connector.TOKENS, "get", lambda: "tok")
    fake_files(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, content=b"x"))[1])
    bot_connector.fetch_file("https://smba.example.net/emea/v3/attachments/1", 10, service_url=SERVICE, with_token=True)
    bot_connector.fetch_file("https://files.example.net/f", 10, service_url=SERVICE)
    assert seen[0].headers["Authorization"] == "Bearer tok" and "Authorization" not in seen[1].headers
    with pytest.raises(FileRefused, match="Teams service address"):  # the token never goes to another host
        bot_connector.fetch_file("https://files.example.net/f", 10, service_url=SERVICE, with_token=True)
    assert len(seen) == 2


@pytest.mark.parametrize("url", ["http://files.example.net/f", "https://127.0.0.1/f", "https://[::1]/f", "https://u:p@files.example.net/f", "ftp://files.example.net/f", "", "files.example.net/f"])
def test_fetch_file_takes_only_https_names(monkeypatch, url):
    fake_files(monkeypatch, lambda request: pytest.fail("fetched"))
    with pytest.raises(FileRefused):
        bot_connector.fetch_file(url, 1000)


def test_fetch_file_stops_at_the_limit_whatever_the_server_declares(monkeypatch):
    fake_files(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(FileRefused, match="larger"):
        bot_connector.fetch_file("https://files.example.net/f", 1000)
    fake_files(monkeypatch, lambda request: httpx.Response(200, headers={"Content-Length": "9999"}, content=b"x"))
    with pytest.raises(FileRefused, match="larger"):
        bot_connector.fetch_file("https://files.example.net/f", 1000)


def test_fetch_file_does_not_follow_redirects_and_reports_a_missing_file(monkeypatch):
    fake_files(monkeypatch, lambda request: httpx.Response(302, headers={"Location": "https://evil.example/f"}))
    with pytest.raises(httpx.HTTPStatusError):
        bot_connector.fetch_file("https://files.example.net/f", 1000)
    fake_files(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(FileRefused, match="no longer"):
        bot_connector.fetch_file("https://files.example.net/f", 1000)


def test_get_member_asks_the_conversation_for_the_member(connector):
    connector.script = [200]
    bot_connector.get_member(SERVICE, "a:conv-1", "29:teams-user")
    request = connector.requests[0]
    assert (request.method, str(request.url)) == ("GET", "https://smba.example.net/emea/v3/conversations/a%3Aconv-1/members/29%3Ateams-user")
    assert request.headers["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize("url", [
    "https://evil.example.com/f",  # not a Microsoft file host
    "https://example.net.evil.com/f",  # a suffix match is on the end of the name
    "https://localhost/f",
    "https://internal-service/f",  # a single label
    "https://2852039166/f",  # a decimal IP address: no dot, and not a name
])
def test_fetch_file_refuses_hosts_that_are_not_microsoft_file_hosts_or_not_public(monkeypatch, url):
    fake_files(monkeypatch, lambda request: pytest.fail("fetched"))
    with pytest.raises(FileRefused):
        bot_connector.fetch_file(url, 1000)


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fd00::1"])
def test_fetch_file_refuses_a_name_that_resolves_to_a_private_address(monkeypatch, address):
    monkeypatch.setattr(bot_connector, "resolve", lambda host: ["93.184.216.34", address])
    fake_files(monkeypatch, lambda request: pytest.fail("fetched"))
    with pytest.raises(FileRefused, match="public"):
        bot_connector.fetch_file("https://files.example.net/f", 1000)


def test_fetch_file_refuses_a_name_that_does_not_resolve(monkeypatch):
    def nowhere(host):
        raise OSError("no such host")

    monkeypatch.setattr(bot_connector, "resolve", nowhere)
    with pytest.raises(FileRefused):
        bot_connector.fetch_file("https://files.example.net/f", 1000)


def test_a_file_from_a_host_that_is_not_allowed_is_noted_and_does_not_stop_the_message(public, db_session, ann, monkeypatch):
    fake_files(monkeypatch, lambda request: pytest.fail("fetched"))
    say(public, "See the report", attachments=[file_info(url="https://evil.example.com/d/1")])
    assert len(tickets(db_session)) == 1
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one().detail == "The file is not on a Microsoft file host"


def test_downloads_stop_when_the_time_for_them_is_used_up(public, db_session, ann, downloads, monkeypatch):
    downloads.served["https://files.example.net/d/1"] = PDF
    clock = iter([0.0, 1.0, 100.0, 100.0])

    class Clock:  # only this module's clock, not the real one the HTTP stack uses
        monotonic = staticmethod(lambda: next(clock))

    monkeypatch.setattr(teams_intake, "time", Clock)
    say(public, "Two files", attachments=[file_info("a.pdf"), file_info("b.pdf", "https://files.example.net/d/2")])
    assert [a.filename for a in db_session.exec(select(Attachment)).all()] == ["a.pdf"]
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "attachment_skipped")).one().detail == "took too long to download"


# ---- Review findings -------------------------------------------------------------------------------------


def test_the_parts_of_a_split_request_are_not_conversations_of_their_own(public, connector, db_session, ann):
    say(public, "Printer is jammed")
    original = tickets(db_session)[0]
    for number in range(3):  # what splitting and automatic sub-requests make: same requester and source, linked to the original
        db_session.add(Ticket(subject=f"Part {number}", requester_email="ann@example.com", source=TicketSource.TEAMS, split_from_id=original.id))
    db_session.commit()

    say(public, "It is on floor 3")

    assert f"Added to {original.reference}." in json.dumps(card_of(connector)) and "Action.Execute" in json.dumps(card_of(connector))  # no question: one conversation
    assert len(messages_of(db_session, original)) == 2


def test_a_card_too_big_for_teams_is_not_sent_and_the_person_is_told_what_to_do(public, connector, db_session, ann):
    two_open(public, db_session)
    say(public, "\u00e9\U0001f600" * 1400)  # 2800 characters that need many bytes each in the card's six copies
    assert last_text(connector) == teams_intake.TOO_LONG_TO_ASK and len(tickets(db_session)) == 2


def test_if_the_card_cannot_be_sent_the_person_is_told_to_send_it_again(public, connector, db_session, ann):
    two_open(public, db_session)
    connector.script = [400, 400]  # the card is refused, then the plain text goes
    say(public, "It is on floor 3")
    assert last_text(connector) == teams_intake.CARD_FAILED


def test_questions_count_towards_the_hourly_cap(public, connector, db_session, ann):
    two_open(public, db_session)
    set_setting(db_session, TEAMS_MAX_PER_PERSON_HOUR, "3")
    db_session.commit()
    say(public, "question one")  # a card: counts as the third
    connector.requests.clear()
    say(public, "question two")
    assert replies(connector) == [("text", teams_intake.SLOW_DOWN)]
    assert len(db_session.exec(select(TeamsIntake)).all()) == 3


def test_one_persons_burst_of_messages_is_cut_off_whatever_they_are(public, connector, db_session, ann, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_teams_person_per_minute", 3)
    for _ in range(3):
        say(public, "help")
    connector.requests.clear()
    say(public, "help")
    say(public, "help")
    assert replies(connector) == [("text", teams_intake.SLOW_DOWN)]  # told once, then nothing
    say(public, "help", oid=OTHER_OID)  # another person is not affected
    assert last_text(connector) != teams_intake.SLOW_DOWN


def test_no_database_transaction_is_held_open_while_files_download(public, db_session, ann, downloads, monkeypatch):
    from sqlalchemy import text

    from app.database import engine

    idle = []
    inner = bot_connector.fetch_file

    def watching(url, max_bytes, **kwargs):
        with engine.connect() as other:
            idle.append(other.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
            )).scalar())
        return inner(url, max_bytes, **kwargs)

    downloads.served["https://files.example.net/d/1"] = PDF
    monkeypatch.setattr(bot_connector, "fetch_file", watching)
    say(public, "See the report", attachments=[file_info()])
    assert idle == [0] and len(db_session.exec(select(Attachment)).all()) == 1

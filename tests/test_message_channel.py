"""#201: every message records its channel (email, portal, teams, agent, system), set where it is created, and the agent timeline
draws it in the kind chip. One test per way a message is created, and a guard so a new way cannot forget it."""

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app import messages, msp_actions
from app.config import settings
from app.messages import agent_timeline, kind_label, record_inbound, record_notice
from app.models import Message, MessageDirection, MessageVisibility, Ticket, TicketSource, TicketStatus, User, UserRole
from app.reply_channel import AGENT, EMAIL, MESSAGE_CHANNELS, PORTAL, SYSTEM, TEAMS_CHANNEL
from tests.conftest import login_as, set_role
from tests.test_email_preview import ticket_for_email
from tests.test_inbound_mail import ingest_mail, mail, open_ticket
from tests.test_msp_mail import IN_HOURS, from_msp, msp_world, take, with_msp  # noqa: F401  (msp_world is a fixture)
from tests.test_raising import desk, last_ticket, raise_it
from tests.test_teams_bot import SERVICE, activity, make_user, post, token  # noqa: F401
from tests.test_teams_bot import bot, metadata  # noqa: F401
from tests.test_teams_intake import (  # noqa: F401  (fixtures)
    OID, an_agent, ann, connector, dev_mail, downloads, isolate, messages_of, public, replies, roster, say, tickets, triaged,
)


def channels(db_session, ticket):
    db_session.expire_all()
    return [m.channel for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()]


# ---- Every way a message is created, one test each ---------------------------------------------------------


def test_email_intake_records_email_for_the_first_message_and_for_a_reply(db_session):
    mail_in = ingest_mail(db_session, mail("n1", sender="kim@example.com", subject="Brand new"))
    db_session.commit()
    assert channels(db_session, mail_in.ticket) == [EMAIL]

    ticket = open_ticket(db_session)
    ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x", body="Me again"))
    db_session.commit()
    assert channels(db_session, ticket) == [EMAIL, EMAIL]


def test_a_portal_reply_records_portal(client, db_session):
    login_as(client, "jo@example.com", "Jo Bloggs")
    raise_it(client)
    ticket = last_ticket(db_session)
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Still broken"})
    assert channels(db_session, ticket) == [PORTAL, PORTAL]  # the raise form's description, then the comment


def test_a_portal_reply_after_closure_records_portal_on_the_new_request(client, db_session):
    login_as(client, "jo@example.com", "Jo Bloggs")
    raise_it(client)
    ticket = last_ticket(db_session)
    ticket.status = TicketStatus.CLOSED
    db_session.add(ticket)
    db_session.commit()
    client.post(f"/portal/{ticket.number}/comment", data={"body": "It is back"})
    follow_on = last_ticket(db_session)
    assert follow_on.id != ticket.id and channels(db_session, follow_on) == [PORTAL]


def test_raising_for_yourself_records_portal_and_for_someone_else_records_agent(client, db_session):
    team, category, _ = desk(client, db_session)
    raise_it(client, subject="For me")
    assert channels(db_session, last_ticket(db_session)) == [PORTAL]
    raise_it(client, subject="For them", for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low")
    assert channels(db_session, last_ticket(db_session)) == [AGENT]


def test_teams_intake_records_teams_for_the_first_message_and_for_a_reply(public, db_session, ann):
    say(public, "My printer is jammed")
    [ticket] = tickets(db_session)
    say(public, "Still jammed")
    assert channels(db_session, ticket) == [TEAMS_CHANNEL, TEAMS_CHANNEL]


def test_the_console_composer_records_where_the_email_went_email_for_an_email_requester(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Please restart it"})
    assert channels(db_session, ticket)[-1] == EMAIL


def test_the_console_composer_records_teams_when_the_reply_went_to_the_requesters_chat(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    agent = an_agent(db_session)
    message = messages.email_requester(db_session, ticket, agent, "Please power it off and on")
    db_session.commit()
    assert message.channel == TEAMS_CHANNEL
    ann.teams_replies_also_email = True  # Teams and email: it still went to Teams
    db_session.add(ann)
    db_session.commit()
    assert messages.email_requester(db_session, ticket, agent, "And again").channel == TEAMS_CHANNEL


def test_a_reply_to_a_teams_requester_that_could_only_go_by_email_records_email(db_session, dev_mail):
    ticket = Ticket(subject="x", requester_email="ann@example.com", source=TicketSource.TEAMS, triaged_at=datetime.now(timezone.utc), status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()  # the bot is off, so the Teams requester is emailed (tests/test_teams_followups.py)
    assert messages.email_requester(db_session, ticket, an_agent(db_session), "Reply").channel == EMAIL


def test_an_internal_note_records_agent(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Checked the log"})
    assert channels(db_session, ticket)[-1] == AGENT


def test_an_automatic_notice_records_system_whatever_route_it_took(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    assert record_notice(db_session, ticket, "Your request has been approved.").channel == SYSTEM  # to Teams
    other = Ticket(subject="x", requester_email="bob@example.com", triaged_at=datetime.now(timezone.utc), status=TicketStatus.OPEN)
    db_session.add(other)
    db_session.commit()
    assert record_notice(db_session, other, "Done").channel == SYSTEM  # by email


def test_mail_from_the_msp_records_email_whether_or_not_the_user_was_on_it(db_session, msp_world):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    take(db_session, from_msp(ticket), IN_HOURS)
    take(db_session, from_msp(ticket, to=("someone.else@msp.example",)), IN_HOURS)
    assert channels(db_session, ticket) == [EMAIL, EMAIL]


def test_the_notes_the_app_writes_for_an_agents_action_record_agent(client, db_session):
    """Escalation, the MSP handoff and splitting write their own notes through add_note."""
    from tests.test_splitting import split, subs_of, world

    original, _ = world(client, db_session)
    split(client, original, ("Keyboard", "E key"), ("Office", ""))
    first, _second = subs_of(db_session, original)
    assert AGENT in channels(db_session, first)
    assert None not in [m.channel for m in db_session.exec(select(Message)).all()]


# ---- The guard -------------------------------------------------------------------------------------------


APP = Path(__file__).resolve().parent.parent / "app"


def test_no_code_but_the_one_helper_builds_a_message():
    """A Message is built in app/messages.py `_record` and nowhere else in the app, so its channel cannot be forgotten."""
    sites = []

    class Finder(ast.NodeVisitor):
        def __init__(self, path):
            self.path, self.stack = path, ["<module>"]

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if getattr(node.func, "id", getattr(node.func, "attr", None)) == "Message":
                sites.append((self.path, self.stack[-1]))
            self.generic_visit(node)

    for path in sorted(APP.rglob("*.py")):
        Finder(path.relative_to(APP).as_posix()).visit(ast.parse(path.read_text()))
    assert set(sites) <= {("messages.py", "_record")}, f"a Message is built outside _record: {sorted(set(sites) - {('messages.py', '_record')})}"
    assert ("messages.py", "_record") in sites


@pytest.mark.parametrize("function", [messages._record, messages.record_inbound])
def test_the_channel_is_required_by_the_helpers_that_create_messages(function):
    parameter = inspect.signature(function).parameters["channel"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty


def test_the_creation_helpers_that_have_a_fixed_channel_name_it():
    for function in (messages.email_requester, messages.record_notice, messages.add_note, messages.record_msp_message):
        assert "channel=" in inspect.getsource(function)


def test_an_unknown_channel_is_refused(db_session):
    ticket = Ticket(subject="x", requester_email="a@b.example")
    db_session.add(ticket)
    db_session.commit()
    with pytest.raises(ValueError):
        record_inbound(db_session, ticket, sender="a", body="b", channel="carrier-pigeon")
    assert set(MESSAGE_CHANNELS) == {"email", "portal", "teams", "agent", "system"}


def test_no_other_way_into_the_message_table_is_hidden_in_the_msp_actions():
    assert "Message(" not in inspect.getsource(msp_actions)


# ---- The chips -------------------------------------------------------------------------------------------


def message_of(channel, direction, **fields):
    return Message(ticket_id=1, sender="x", body="b", direction=direction, channel=channel, **fields)


@pytest.mark.parametrize(
    "channel, direction, sender_kind, label",
    [
        (EMAIL, MessageDirection.INBOUND, "requester", "Email from requester"),
        (TEAMS_CHANNEL, MessageDirection.INBOUND, "requester", "Teams message from requester"),
        (PORTAL, MessageDirection.INBOUND, "requester", "Portal reply"),
        (EMAIL, MessageDirection.INBOUND, "follower", "Email from follower"),
        (PORTAL, MessageDirection.INBOUND, "follower", "Portal reply from follower"),
        (AGENT, MessageDirection.INBOUND, "requester", "Logged by an agent"),
        (EMAIL, MessageDirection.OUTBOUND, None, "Email to requester"),
        (TEAMS_CHANNEL, MessageDirection.OUTBOUND, None, "Teams reply to requester"),
        (SYSTEM, MessageDirection.OUTBOUND, None, "Automatic message to requester"),
        (None, MessageDirection.OUTBOUND, None, "Message to requester"),
        (None, MessageDirection.INBOUND, "requester", "From requester"),
        (None, MessageDirection.INBOUND, "follower", "From follower"),
    ],
)
def test_the_kind_chip_says_where_the_message_came_from_or_went(channel, direction, sender_kind, label):
    assert kind_label(message_of(channel, direction), sender_kind) == label


def test_the_ticket_page_draws_the_chips(client, db_session):
    ticket = ticket_for_email(client, db_session)
    for channel, direction in (
        (EMAIL, MessageDirection.INBOUND), (TEAMS_CHANNEL, MessageDirection.INBOUND), (PORTAL, MessageDirection.INBOUND),
        (TEAMS_CHANNEL, MessageDirection.OUTBOUND), (SYSTEM, MessageDirection.OUTBOUND), (None, MessageDirection.INBOUND),
    ):
        messages._record(
            db_session, ticket, sender="walkin@example.com", body=f"{channel} {direction.value}", direction=direction,
            visibility=MessageVisibility.PUBLIC, channel=channel or EMAIL,
        ).channel = channel
    db_session.commit()
    client.post(f"/tickets/{ticket.number}/email", data={"body": "A real reply"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "A note"})

    page = client.get(f"/tickets/{ticket.number}").text
    for chip in (
        "Email from requester", "Teams message from requester", "Portal reply", "Teams reply to requester",
        "Automatic message to requester", "Email to requester", "Internal note",
    ):
        assert chip in page, chip
    assert ">From requester<" in page.replace("\n", "").replace("  ", "")  # a message with no recorded channel: plain words


def test_the_requester_never_sees_a_channel_chip(client, db_session):
    """The channel is display information for agents, not access: the portal page does not draw it and the visible list is unchanged."""
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "A real reply"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "A secret note"})
    client.post("/auth/logout")
    login_as(client, "walkin@example.com", "Walk In")
    page = client.get(f"/portal/{ticket.number}").text
    assert "A real reply" in page and "A secret note" not in page
    assert "Email to requester" not in page and "Teams reply" not in page

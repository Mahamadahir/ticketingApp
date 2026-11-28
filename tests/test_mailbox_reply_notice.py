"""#363: the one automatic email to a requester who replies by email to a finished mailbox request. Said once per ticket, never for a
thank-you, an ordinary ticket or a portal comment; the words are an editable template."""

import pytest
from sqlmodel import select

from app import outbox
from app.inbound import ingest_mail
from app.models import Message, OutboundEmail, TicketEvent
from tests.conftest import login_as
from tests.test_inbound_mail import mail as incoming
from tests.test_inbound_mail import mailbox, storage  # noqa: F401  (fixtures)
from tests.test_mailbox_reopen import BASE, OWNER, finished_request, urls  # noqa: F401  (urls is an autouse fixture)
from tests.test_portal_mailbox import directory, fake_exchange, friend, world  # noqa: F401  (fixtures)


def notices(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.NOTICE)).all()


def events(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def reply(db_session, ticket, message_id, body="Please turn it back on", sender=OWNER):
    result = ingest_mail(db_session, incoming(message_id, sender=sender, subject=f"Re: [{ticket.reference}] {ticket.subject}", body=body))
    db_session.commit()
    return result


def test_the_first_emailed_reply_gets_one_email_with_the_link_and_a_second_gets_none(client, db_session, world, friend, mailbox):
    ticket = finished_request(client, db_session, world)
    assert notices(db_session, ticket) == []

    reply(db_session, ticket, "e1")

    [row] = notices(db_session, ticket)
    assert db_session.get(Message, row.message_id).body == (
        "This request has finished, so your reply did not reopen it. To set it up again, make a new request: "
        f"{BASE}/portal/new?request_type_id={world.type.id}&again={ticket.number}"
    )
    assert row.to_address == OWNER
    assert len(events(db_session, ticket, "reply_notice_sent")) == 1

    reply(db_session, ticket, "e2", body="Hello? Is anyone there?")

    assert len(notices(db_session, ticket)) == 1  # said once per ticket
    assert len(events(db_session, ticket, "reply_not_reopened")) == 2  # both replies were still recorded


def test_a_thank_you_reply_gets_no_email(client, db_session, world, friend, mailbox):
    from app.thanks_rules import add_rule

    ticket = finished_request(client, db_session, world)
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    db_session.commit()

    reply(db_session, ticket, "t1", body="Thanks!")

    assert notices(db_session, ticket) == []
    assert len(events(db_session, ticket, "thanks_ignored")) == 1 and events(db_session, ticket, "reply_not_reopened") == []


def test_no_email_for_a_reply_to_an_ordinary_resolved_ticket(db_session, mailbox):
    from tests.test_inbound_mail import open_ticket, triage_by_hand
    from tests.test_reopening import finish

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)

    reply(db_session, ticket, "o1", body="Still broken", sender="jo@example.com")

    assert notices(db_session, ticket) == []


def test_a_followers_reply_does_not_email_the_requester(client, db_session, world, friend, mailbox):
    ticket = finished_request(client, db_session, world)

    reply(db_session, ticket, "f1", sender="someone.else@example.com")

    assert notices(db_session, ticket) == []


def test_a_portal_comment_gets_the_notice_on_the_page_and_no_email(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    page = client.post(f"/portal/{ticket.number}/comment", data={"body": "Can you turn it back on?"}).text

    assert "has not reopened this request" in page
    assert notices(db_session, ticket) == []


def test_the_template_is_editable_and_its_placeholders_are_checked(client, db_session, world, friend, mailbox):
    from tests.test_email_templates import admin, save

    ticket = finished_request(client, db_session, world)
    admin(client, db_session)
    assert save(client, "mailbox_reply_notice", "No reopen for {reference}. Start again: {link}", subject="").status_code == 303
    assert save(client, "mailbox_reply_notice", "No link at all", subject="").status_code != 303  # {link} is required
    assert save(client, "mailbox_reply_notice", "Start again: {link} {nonsense}", subject="").status_code != 303
    client.post("/auth/logout")
    login_as(client, OWNER, "Alex Owner")

    reply(db_session, ticket, "x1")

    [row] = notices(db_session, ticket)
    assert db_session.get(Message, row.message_id).body.startswith(f"No reopen for {ticket.reference}. Start again: {BASE}/portal/new")

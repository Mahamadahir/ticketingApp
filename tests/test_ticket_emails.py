"""#349: every email sent or queued about a ticket shows in the agent thread (its kind, recipients, state and folded text, never an
approval link), and Send anyway releases one held back by the per-ticket limit. The portal is unchanged."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import mail, outbox
from app.config import settings
from app.main import app
from app.messages import record_notice, requester_visible_messages
from app.models import AuditLog, OutboundEmail, Ticket, TicketEvent, User, UserRole
from app.settings_store import AUTOMATIC_EMAIL_CAP, set_setting
from app.teams import MAX_ATTEMPTS
from app.ticket_emails import KIND_LABELS
from tests.conftest import login_as, set_role
from tests.test_approvals import person, raised_and_triaged, request_type
from tests.test_email_preview import follow, ticket_for_email
from tests.test_raising import raise_it
from tests.test_outbound_mail import dev_mail, email, rows  # noqa: F401  (a fixture)
from tests.test_ticket_thread import boxes, thread_html

AUTOMATIC = "Sent an automatic message"


@pytest.fixture
def second():
    with TestClient(app) as other:
        yield other


def queue(db, ticket, kind, to="walkin@example.com", **extra):
    row = OutboundEmail(kind=kind, ticket_id=ticket.id, to_address=to, **extra)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def automatic_boxes(client, ticket):
    return [b for b in boxes(thread_html(client, ticket)) if AUTOMATIC in b]


def held_ticket(client, db_session, notices=3, cap="2"):
    """A ticket whose automatic emails to the requester ran into the cap: some sent, the rest held."""
    ticket = ticket_for_email(client, db_session)
    t = db_session.get(Ticket, ticket.id)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, cap)
    db_session.commit()
    for n in range(notices):
        record_notice(db_session, t, f"Notice {n}")
    db_session.commit()
    now = datetime.now(timezone.utc)
    mail.deliver_mail(db_session, now=now)
    held = [r for r in rows(db_session) if r.sent_at is None]
    assert held, "the cap should have held something back"
    return ticket, held


# ---- Every kind shows, once ---------------------------------------------------------------------------------------


def test_every_outbox_kind_tied_to_a_ticket_shows_once_with_its_kind_recipients_and_state(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)  # also queues one raised-for-you email
    follow(client, ticket, "boss@example.com")
    kinds = [k for k in KIND_LABELS if k not in (outbox.REPLY, outbox.NOTICE, outbox.RAISED_FOR)]
    for kind in kinds:
        queue(db_session, ticket, kind, to=f"{kind.replace('_', '')}@example.com", cc="copy@example.com")

    html = thread_html(client, ticket)
    shown = [b for b in boxes(html) if AUTOMATIC in b]

    assert len(shown) == len(kinds) + 1  # plus the raised-for-you email of the set-up
    for kind in kinds:
        mine = [b for b in shown if KIND_LABELS[kind] in b]
        assert len(mine) == 1, kind
        assert f"{kind.replace('_', '')}@example.com" in mine[0] and "Cc: copy@example.com" in mine[0] and "Queued" in mine[0]
        assert "IT Support (automatic)" in mine[0]
    assert sum(KIND_LABELS[outbox.RAISED_FOR] in b for b in shown) == 1


def test_an_agent_reply_is_one_entry_not_two(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Please restart it")
    mail.deliver_mail(db_session)

    html = thread_html(client, ticket)
    reply_boxes = [b for b in boxes(html) if "Please restart it" in b]

    assert len(reply_boxes) == 1
    assert "Emailed the requester" in reply_boxes[0] and "Reply to the requester" in reply_boxes[0] and "Sent " in reply_boxes[0]
    assert html.count("Reply to the requester") == 1
    assert not any("Reply to the requester" in b and AUTOMATIC in b for b in boxes(html))


def test_an_automatic_notice_shows_once_on_its_message(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    record_notice(db_session, db_session.get(Ticket, ticket.id), "Your request was approved")
    db_session.commit()

    html = thread_html(client, ticket)

    assert html.count("Your request was approved") == 1
    [notice] = [b for b in boxes(html) if "Your request was approved" in b]
    assert AUTOMATIC in notice and "Automatic notice to the requester" in notice and "Queued" in notice


def test_a_sent_email_shows_its_time_and_a_failed_one_its_short_reason(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    queue(db_session, ticket, outbox.CHASE, sent_at=datetime.now(timezone.utc), body="Is this still an issue?")
    queue(db_session, ticket, outbox.ACKNOWLEDGEMENT, attempts=MAX_ATTEMPTS, last_error="Graph answered 400")
    queue(db_session, ticket, outbox.FOLLOWER_UPDATE, to="boss@example.com", attempts=1, last_error="Graph answered 503")

    shown = automatic_boxes(client, ticket)

    chase = next(b for b in shown if "Chase" in b)
    assert "Sent " in chase and "Is this still an issue?" in chase and "<details" in chase
    ack = next(b for b in shown if "Acknowledgement" in b)
    assert "Failed" in ack and "Graph answered 400" in ack
    retry = next(b for b in shown if "Update to a follower" in b)
    assert "Failed" in retry and "Graph answered 503" in retry and "tried again" in retry


def test_an_approval_email_shows_its_record_never_a_link_code(client, second, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    mail.deliver_mail(db_session)
    [row] = rows(db_session, outbox.APPROVAL)
    assert "/approvals/" not in row.body
    admin = login_as(second, "root@example.com", "Root")
    set_role(db_session, admin, UserRole.SUPER_ADMIN)

    html = thread_html(second, ticket)
    [approval] = [b for b in boxes(html) if "Approval request" in b]

    assert "link left out" in approval and "/approvals/" not in html and "token" not in approval.lower()
    assert "boss@example.com" in approval and "Sent " in approval


def test_an_email_waiting_for_a_setting_says_so_and_has_no_text_yet(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    queue(db_session, ticket, outbox.APPROVAL, last_error="PUBLIC_BASE_URL is not set, so the email has no link to answer from")
    [box] = [b for b in automatic_boxes(client, ticket) if "Approval request" in b]
    assert "Queued" in box and "PUBLIC_BASE_URL is not set" in box and "The text is written when the email is sent." in box


def test_an_email_about_no_ticket_is_not_in_any_thread(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    db_session.add(OutboundEmail(kind=outbox.EMERGENCY_RESET, ticket_id=None, to_address="agent@example.com"))
    db_session.commit()
    assert "Emergency" not in thread_html(client, ticket)


# ---- Held back by the limit ---------------------------------------------------------------------------------------


def test_a_held_email_says_why_and_offers_send_anyway(client, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)

    html = thread_html(client, ticket)
    held_boxes = [b for b in boxes(html) if "Held back by the limit" in b]

    assert len(held_boxes) == len(held)
    for b in held_boxes:
        assert "Held back: the limit on automatic emails to this person on this ticket was reached" in b
        assert "Send anyway" in b and "/send-anyway" in b


def test_send_anyway_sends_that_one_row_through_the_outbox_and_records_who(client, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session, notices=4, cap="2")
    assert len(held) >= 2
    first, other = held[0], held[1]
    now = datetime.now(timezone.utc)

    response = client.post(f"/tickets/{ticket.number}/emails/{first.id}/send-anyway")
    assert response.status_code == 200

    db_session.expire_all()
    assert db_session.get(OutboundEmail, first.id).cap_override_by == "agent@example.com"
    assert mail.deliver_mail(db_session, now=now) == (1, 0)  # that one row, not the others: the cap still holds them
    assert db_session.get(OutboundEmail, first.id).sent_at is not None
    assert all(db_session.get(OutboundEmail, r.id).sent_at is None for r in held[1:])
    assert db_session.get(OutboundEmail, other.id).cap_override_at is None
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "email_sent_anyway")).all()
    assert (audit.entity_type, audit.entity_id, audit.actor_email) == ("ticket", ticket.id, "agent@example.com")
    [event] = db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "email_sent_anyway")
    ).all()
    assert event.actor_email == "agent@example.com"
    html = thread_html(client, ticket)
    assert "Sent an email anyway that the limit on automatic emails had held back" in html
    assert len([b for b in boxes(html) if "Held back by the limit" in b]) == len(held) - 1


def test_a_second_click_sends_and_records_nothing_more(client, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)
    url = f"/tickets/{ticket.number}/emails/{held[0].id}/send-anyway"
    assert client.post(url).status_code == 200
    mail.deliver_mail(db_session)
    sent_at = db_session.get(OutboundEmail, held[0].id).sent_at
    before = len(db_session.exec(select(AuditLog).where(AuditLog.action == "email_sent_anyway")).all())

    assert client.post(url).status_code == 200  # nothing to do, and no error

    db_session.expire_all()
    assert db_session.get(OutboundEmail, held[0].id).sent_at == sent_at
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "email_sent_anyway")).all()) == before == 1
    assert len(db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "email_sent_anyway")).all()) == 1
    assert mail.deliver_mail(db_session) == (0, 0)


def test_send_anyway_on_an_email_that_is_not_held_does_nothing(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    row = queue(db_session, ticket, outbox.CHASE)  # queued, not held

    assert client.post(f"/tickets/{ticket.number}/emails/{row.id}/send-anyway").status_code == 200

    db_session.expire_all()
    assert db_session.get(OutboundEmail, row.id).cap_override_at is None
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "email_sent_anyway")).all() == []


def test_an_email_of_another_ticket_is_not_found(client, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)
    raise_it(client, subject="Another", for_email="walkin@example.com", category_id=ticket.category_id, impact="low", urgency="low")
    db_session.expire_all()
    other = db_session.exec(select(Ticket).where(Ticket.subject == "Another")).one()
    assert client.post(f"/tickets/{other.number}/emails/{held[0].id}/send-anyway").status_code == 404


def test_a_requester_a_follower_and_an_agent_who_cannot_work_the_ticket_are_refused(client, second, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)
    url = f"/tickets/{ticket.number}/emails/{held[0].id}/send-anyway"
    follow(client, ticket, "boss@example.com")

    login_as(second, "walkin@example.com", "Walk In")  # the requester
    assert second.post(url).status_code in (403, 404)
    second.post("/auth/logout")
    login_as(second, "boss@example.com", "Boss")  # a follower with no agent role
    assert second.post(url).status_code in (403, 404)
    second.post("/auth/logout")
    outsider = login_as(second, "outsider@example.com", "Outsider")  # an agent of no team that holds the ticket
    set_role(db_session, outsider, UserRole.AGENT)
    assert second.post(url).status_code in (403, 404)
    assert "Send anyway" not in thread_html_of(second, ticket)

    db_session.expire_all()
    assert db_session.get(OutboundEmail, held[0].id).cap_override_at is None
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "email_sent_anyway")).all() == []


def thread_html_of(client, ticket):
    response = client.get(f"/tickets/{ticket.number}")
    return response.text.split('id="thread"')[1].split("</ol>")[0] if 'id="thread"' in response.text else ""


def test_a_super_admin_may_send_anyway(client, second, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)
    admin = login_as(second, "root@example.com", "Root")
    set_role(db_session, admin, UserRole.SUPER_ADMIN)

    assert second.post(f"/tickets/{ticket.number}/emails/{held[0].id}/send-anyway").status_code == 200

    db_session.expire_all()
    assert db_session.get(OutboundEmail, held[0].id).cap_override_by == "root@example.com"


# ---- The portal is unchanged --------------------------------------------------------------------------------------


def test_the_portal_thread_is_unchanged(client, second, db_session, dev_mail):
    ticket, held = held_ticket(client, db_session)
    queue(db_session, ticket, outbox.CHASE, body="Is this still an issue?")
    login_as(second, "walkin@example.com", "Walk In")

    portal = second.get(f"/portal/{ticket.number}").text

    assert AUTOMATIC not in portal and "Send anyway" not in portal and "Held back" not in portal
    assert "Is this still an issue?" not in portal
    visible = requester_visible_messages(db_session, db_session.get(Ticket, ticket.id))
    assert all("Chase" not in m.body for m in visible)

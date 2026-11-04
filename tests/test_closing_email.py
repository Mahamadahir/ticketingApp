"""#131: closing a ticket that was not resolved emails the requester (app/closing.py): the "closed" template
(no reopen link, a feedback link), the preview step, the cc rules, one bell entry per person, and the closes
that send nothing."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import mail, outbox
from app.action_links import FEEDBACK_MARKER, REOPEN_MARKER
from app.closing import CloseError, close_ticket, preview_closing
from app.config import settings
from app.email_templates import definition, save_template
from app.models import ActionToken, AuditLog, Message, Notification, OutboundEmail, Ticket, TicketStatus, User
from app.notifications import AGENT_EMAILED, TICKET_CLOSED, TICKET_RESOLVED
from app.reopening import reopen_ticket
from app.auto_close_chase import run_auto_close
from tests.test_email_preview import follow, recorded, ticket_for_email
from tests.test_requester_bell import agent_session, entries, kinds, with_accounts
from tests.test_resolution import dev_mail, fresh, resolve  # noqa: F401  (dev_mail is a fixture)
from tests.test_splitting import split, subs_of

NOTES = "Duplicate of INC-1"


def close(client, ticket, notes=NOTES, **extra):
    return client.post(f"/tickets/{ticket.number}/close", data={"closing_notes": notes, **extra})


def close_preview(client, ticket, notes=NOTES, **extra):
    return client.post(f"/tickets/{ticket.number}/close/preview", data={"closing_notes": notes, **extra})


def outbound(db_session, ticket):
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()


def test_the_closed_template_is_registered_with_no_reopen_link_and_requires_the_notes():
    template = definition("closed")
    assert "closing_notes" in template.required and "feedback_link" in template.body_placeholders
    assert "reopen_link" not in template.body_placeholders and "{reopen_link}" not in template.body_default


def test_a_preview_shows_the_email_and_records_nothing(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    before = recorded(db_session)

    page = close_preview(client, ticket).text

    assert "To:</b> walkin@example.com" in page and "boss@example.com" in page
    assert f"Subject:</b> [{ticket.reference}] Printer jam" in page
    assert NOTES in page and "has been closed" in page and FEEDBACK_MARKER in page
    assert REOPEN_MARKER not in page and "Close and send email" in page
    assert recorded(db_session) == before and fresh(db_session, ticket).status == TicketStatus.OPEN


def test_closing_emails_the_requester_copying_followers_and_counts_as_a_response(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")

    assert close(client, ticket).status_code == 200

    closed = fresh(db_session, ticket)
    assert closed.status == TicketStatus.CLOSED and closed.first_responded_at is not None
    [message] = outbound(db_session, ticket)
    assert message.cc == "boss@example.com, other@example.com" and NOTES in message.body
    assert message.visibility == "public" and message.author_user_id is not None
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)).all()
    built = mail.build_message_mail(db_session, row)
    assert (built.to, built.cc) == ("walkin@example.com", ["boss@example.com", "other@example.com"])
    assert NOTES in built.body and "reopen" not in built.body.lower()
    # The feedback link is a real one, the reopen one is not offered on a closed ticket.
    assert "/feedback/" in built.body and "/reopen/" not in built.body and FEEDBACK_MARKER not in built.body
    assert [t.kind for t in db_session.exec(select(ActionToken)).all()] == ["feedback"]


def test_unticked_followers_are_left_off_and_a_stranger_is_refused_before_anything_changes(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")

    refused = close(client, ticket, cc_form="1", cc=["stranger@example.com"])
    assert refused.status_code == 400 and "Not a follower" in refused.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN and fresh(db_session, ticket).resolution_notes is None
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).all() == []

    close(client, ticket, cc_form="1", cc=["boss@example.com"])
    [message] = outbound(db_session, ticket)
    assert message.cc == "boss@example.com"


def test_an_admin_edit_of_the_template_changes_the_email(client, db_session):
    ticket = ticket_for_email(client, db_session)
    save_template(db_session, "closed", None, "Shut: {closing_notes} ({reference})", "admin@example.com")
    db_session.commit()

    assert f"Shut: {NOTES} ({ticket.reference})" in close_preview(client, ticket).text
    close(client, ticket)
    [message] = outbound(db_session, ticket)
    assert message.body == f"Shut: {NOTES} ({ticket.reference})"


def test_notes_are_required_and_a_bad_close_changes_nothing(client, db_session):
    ticket = ticket_for_email(client, db_session)
    for notes in ("", "  "):
        assert close_preview(client, ticket, notes).status_code == 400
        assert close(client, ticket, notes).status_code == 400
    assert close(client, ticket, "x" * 20000).status_code in (400, 422)
    assert fresh(db_session, ticket).status == TicketStatus.OPEN and outbound(db_session, ticket) == []

    assert close(client, ticket).status_code == 200
    again = close(client, ticket, "Again")
    assert again.status_code == 400 and "already resolved or closed" in again.text
    assert len(outbound(db_session, ticket)) == 1 and fresh(db_session, ticket).resolution_notes == NOTES


def test_the_helper_raises_close_error_before_changing_anything(client, db_session):
    ticket = ticket_for_email(client, db_session)
    agent = db_session.exec(select(User).where(User.email == "agent@example.com")).first() or db_session.exec(select(User)).first()
    with pytest.raises(CloseError):
        close_ticket(db_session, ticket, agent, "  ")
    with pytest.raises(CloseError):
        preview_closing(db_session, ticket, "")
    db_session.rollback()
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_the_status_buttons_refuse_to_close_and_the_page_offers_close(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Preview closing" in page and 'name="status" value="closed"' not in page
    response = client.post(f"/tickets/{ticket.number}/status", data={"status": "closed", "resolution_notes": NOTES})
    assert response.status_code == 400 and "Close action" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_only_an_agent_who_can_work_the_ticket_may_close(client, db_session):
    from app.models import UserRole
    from tests.conftest import create_team, login_as, set_role

    ticket = ticket_for_email(client, db_session)
    client.post("/auth/logout")
    other = create_team(db_session, "Network")
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT, other.id)
    assert close(client, ticket).status_code == 403 and close_preview(client, ticket).status_code == 403
    client.post("/auth/logout")
    login_as(client, "walkin@example.com")
    assert close(client, ticket).status_code == 403
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


# ---- One bell entry per person (#130) ---------------------------------------------------------------


def test_closing_gives_one_closed_entry_not_an_emailed_one(client, db_session):
    with_accounts(client, db_session, "walkin@example.com", "boss@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")

    assert close(client, ticket).status_code == 200

    for email in ("walkin@example.com", "boss@example.com"):
        assert kinds(db_session, email) == [TICKET_CLOSED]


def test_someone_with_the_closed_entry_off_is_told_by_the_emailed_one(client, db_session):
    from app.models import NotificationPreference
    from app.notification_settings import IN_APP

    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    user = db_session.exec(select(User).where(User.email == "walkin@example.com")).one()
    db_session.add(NotificationPreference(user_id=user.id, kind=TICKET_CLOSED, channel=IN_APP, enabled=False))
    db_session.commit()

    assert close(client, ticket).status_code == 200

    assert kinds(db_session, "walkin@example.com") == [AGENT_EMAILED]


# ---- The closes that send nothing -------------------------------------------------------------------


def test_closing_a_resolved_ticket_by_hand_sends_no_second_email(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    assert len(outbound(db_session, ticket)) == 1
    assert 'name="status" value="closed"' in client.get(f"/tickets/{ticket.number}").text
    assert "Preview closing" not in client.get(f"/tickets/{ticket.number}").text

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"}).status_code == 200

    assert fresh(db_session, ticket).status == TicketStatus.CLOSED and len(outbound(db_session, ticket)) == 1
    assert close(client, ticket).status_code == 400  # already closed
    assert "Resolved by" in client.get(f"/tickets/{ticket.number}").text


def test_the_auto_close_sends_no_closed_email(client, db_session, dev_mail):
    from app.settings_store import AUTO_CLOSE_AFTER_DAYS, AUTO_CLOSE_ENABLED, set_setting

    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    set_setting(db_session, AUTO_CLOSE_ENABLED, "on")
    set_setting(db_session, AUTO_CLOSE_AFTER_DAYS, "7")
    db_session.commit()
    resolved = fresh(db_session, ticket)
    resolved.resolved_at = datetime.now(timezone.utc) - timedelta(days=30)
    db_session.add(resolved)
    db_session.commit()

    run_auto_close(db_session)
    db_session.commit()

    assert fresh(db_session, ticket).status == TicketStatus.CLOSED and len(outbound(db_session, ticket)) == 1


def test_a_reclose_sends_no_closed_email(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    reopen_ticket(db_session, fresh(db_session, ticket), "agent@example.com", "It came back")
    db_session.commit()
    sent = len(outbound(db_session, ticket))
    assert client.post(f"/tickets/{ticket.number}/reclose", data={"reason": "duplicate"}).status_code == 200
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED and len(outbound(db_session, ticket)) == sent


def test_a_split_original_cannot_be_closed_by_hand_and_sends_no_closed_email(client, db_session, dev_mail):
    original = ticket_for_email(client, db_session, "Two things")
    split(client, original, ("Keyboard", ""), ("Office", ""))
    page = client.get(f"/tickets/{original.number}").text
    assert "Preview closing" not in page  # its status follows its parts
    assert close(client, original).status_code == 400


def test_the_preview_refuses_what_the_close_refuses(client, db_session):
    ticket = ticket_for_email(client, db_session)
    assert close_preview(client, ticket, cc_form="1", cc=["stranger@example.com"]).status_code == 400
    ticket.triaged_at = None
    ticket.status = TicketStatus.NEW
    db_session.add(ticket)
    db_session.commit()
    refused = close_preview(client, ticket)
    assert refused.status_code == 400 and "Triage the ticket" in refused.text
    assert close(client, ticket).status_code == 400 and fresh(db_session, ticket).status == TicketStatus.NEW


def test_a_closed_without_resolving_ticket_says_closed_by_and_not_resolved_by(client, db_session):
    ticket = ticket_for_email(client, db_session)
    close(client, ticket)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Closed by" in page and "Resolved by" not in page and NOTES in page

"""Building and sending the emails that app/outbox.py queued (phase 3D).

An email is built when it is sent, not when it is queued, and the rules are asked again then:
- A reply or notice goes only if its message is one the requester may see (messages.py decides
  that, as for the portal) and the ticket is triaged.
- "A ticket was raised for you" goes only if the person is still to be told (the Settings switch
  and their own choice, raising.should_tell_requester), so an opt-out made after queuing wins.
- An approval email goes only while that approval is pending and the ticket still waits for it. Its
  link is signed at send time, so it has its full life however long the email waited, and it is
  never stored: the row keeps the text with the link left out.
- Automatic emails to one ticket's requester are capped per day (a setting) to stop a mail loop; over the cap an email is held back until the count allows it, not lost.

The sender is one row at a time, committed as it goes, so an email that went is never sent again
because a later one failed. Delivery is still at least once: if the process dies between Graph
accepting a message and the commit, that one email goes again. Retries back off like the Teams outbox
(app/teams.py), whose limits it shares.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from sqlalchemy import or_
from sqlmodel import Session, select

from app.approval_links import make_token
from app.approvals import PENDING
from app.config import settings
from app.email_templates import LINK_LEFT_OUT, REQUEST_BLOCK, render_template
from app.graph_client import graph_client
from app.messages import about_text, in_a_split, is_triaged, mail_subject, requester_visible_messages
from app.models import (
    Message,
    MessageDirection,
    MessageVisibility,
    OutboundEmail,
    Ticket,
    TicketApproval,
    TicketStatus,
    User,
)
from app.outbox import APPROVAL, AUTOMATIC_TO_REQUESTER, NOTICE, RAISED_FOR, REPLY
from app.raising import should_tell_requester
from app.settings_store import APPROVAL_LINK_MINUTES, AUTOMATIC_EMAIL_CAP, get_int_setting
from app.teams import MAX_ATTEMPTS, MAX_BACKOFF_MINUTES, Unsendable

log = logging.getLogger("app.mail")

BATCH = 20
CAP_WINDOW = timedelta(hours=24)
NOT_READY_WAIT = timedelta(minutes=10)  # how long an email that cannot go yet waits before the next look
NOT_READY_LIMIT = timedelta(days=3)  # after this long since it was queued, it is given up on
MAX_QUOTED = 1500  # characters of the requester's own words put into an email to someone else
# A 4xx means Graph refused the message itself, which retrying cannot fix, except: 401 and 403 are
# a misconfigured or not yet consented app, fixed on the Azure side while the email waits; 408 and
# 429 are "try later".
RETRYABLE_CLIENT_ERRORS = (401, 403, 408, 429)


class NotReady(Exception):
    """Raised by a builder when the email cannot go yet but may later (a setting is missing, the ticket
    is not triaged yet). The message is kept on the row and the email is looked at again every ten
    minutes without using up its tries; after three days it is given up on."""


@dataclass
class Mail:
    to: str
    cc: list[str]
    subject: str
    body: str  # what is sent
    record: str  # what is kept on the row: the same, minus anything secret
    automatic: bool  # true for mail nobody just wrote, so receivers should not auto-reply to it
    reference: str


def _ticket_of(session: Session, row: OutboundEmail) -> Ticket:
    ticket = session.get(Ticket, row.ticket_id)
    if ticket is None:
        raise Unsendable("Ticket not found")
    return ticket


def _guard_recipient(row: OutboundEmail) -> None:
    mailbox = settings.graph_shared_mailbox.strip().lower()
    if not row.to_address.strip():
        raise Unsendable("No address to send to")
    if mailbox and row.to_address.strip().lower() == mailbox:
        raise Unsendable("The address is the shared mailbox itself (a loop)")


def _with_about(session: Session, ticket: Ticket, body: str) -> str:
    """A part of a split request names itself, as the preview shows."""
    return f"{about_text(ticket)}\n\n{body}" if in_a_split(session, ticket) else body


def _ticket_values(ticket: Ticket) -> dict[str, str]:
    """The placeholders every template shares."""
    return {"reference": ticket.reference, "ticket_number": str(ticket.number), "subject": ticket.subject}


def own_first_message(session: Session, ticket: Ticket) -> Optional[Message]:
    """The requester's first message on this ticket itself (not the shared conversation of a split
    request, whose earliest message belongs to the original)."""
    return session.exec(
        select(Message)
        .where(
            Message.ticket_id == ticket.id,
            Message.direction == MessageDirection.INBOUND,
            Message.visibility == MessageVisibility.PUBLIC,
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
    ).first()


def quoted(text: str) -> str:
    """The requester's words, cut to a sensible length, for an email that is not to them."""
    text = text.strip()
    return text if len(text) <= MAX_QUOTED else text[:MAX_QUOTED].rstrip() + " [cut short]"


def build_message_mail(session: Session, row: OutboundEmail) -> Mail:
    """A reply or notice: the recorded message, to the requester, copying who the message recorded."""
    ticket = _ticket_of(session, row)
    message = session.get(Message, row.message_id) if row.message_id else None
    if message is None:
        raise Unsendable("Message not found")
    if not is_triaged(ticket):
        raise NotReady("The ticket has not been triaged yet, so it cannot be emailed")
    if message.id not in {m.id for m in requester_visible_messages(session, ticket)}:
        raise Unsendable("The requester may not see this message")
    _guard_recipient(row)
    body = _with_about(session, ticket, message.body)
    return Mail(
        to=row.to_address,
        cc=[a.strip() for a in (message.cc or "").split(",") if a.strip()],
        subject=mail_subject(session, ticket),
        body=body,
        record=body,
        automatic=row.kind == NOTICE,
        reference=ticket.reference,
    )


def build_raised_for_mail(session: Session, row: OutboundEmail) -> Mail:
    ticket = _ticket_of(session, row)
    if not should_tell_requester(session, ticket.requester_email):
        raise Unsendable("The person is not to be told any more")
    _guard_recipient(row)
    creator = session.get(User, ticket.created_by_id) if ticket.created_by_id else None
    first = own_first_message(session, ticket)
    rendered = render_template(session, "raised_for", {
        **_ticket_values(ticket),
        "raised_by": creator.display_name if creator else "IT Support",
        "request_text": first.body if first else "",
    })
    return Mail(
        to=row.to_address, cc=[], subject=rendered.subject or mail_subject(session, ticket),
        body=rendered.body, record=rendered.body, automatic=True, reference=ticket.reference,
    )


def build_approval_mail(session: Session, row: OutboundEmail) -> Mail:
    ticket = _ticket_of(session, row)
    approval = session.get(TicketApproval, row.approval_id) if row.approval_id else None
    if approval is None:
        raise Unsendable("Approval not found")
    if approval.decision != PENDING or ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise Unsendable("The request no longer needs this approval")
    approver = session.get(User, approval.approver_id)
    if approver is None:
        raise Unsendable("Approver not found")
    _guard_recipient(row)
    if not settings.public_base_url:
        raise NotReady("PUBLIC_BASE_URL is not set, so the email has no link to answer from")
    link = f"{settings.public_base_url.rstrip('/')}/approvals/{make_token(approval.id)}"
    days = round((get_int_setting(session, APPROVAL_LINK_MINUTES) or 20160) / 1440)
    first = own_first_message(session, ticket)
    values = {
        **_ticket_values(ticket),
        "requester_email": ticket.requester_email,
        # Their words, not IT's: said so, set apart and cut, since they sit beside a genuine link.
        "request_block": REQUEST_BLOCK.format(words=quoted(first.body)) if first else "",
        "link_days": str(days),
    }
    rendered = render_template(session, "approval_request", {**values, "link": link})
    # The row keeps the text with the link left out; the link is never stored.
    record = render_template(session, "approval_request", {**values, "link": LINK_LEFT_OUT})
    return Mail(
        to=row.to_address, cc=[], subject=rendered.subject or f"[{ticket.reference}] {ticket.subject}",
        body=rendered.body, record=record.body, automatic=True, reference=ticket.reference,
    )


# One builder per kind: it turns the row into the email, or raises Unsendable (never) or NotReady
# (not yet). A new kind of email is one builder and one entry here; the sending is shared.
BUILDERS: dict[str, Callable[[Session, OutboundEmail], Mail]] = {
    REPLY: build_message_mail,
    NOTICE: build_message_mail,
    RAISED_FOR: build_raised_for_mail,
    APPROVAL: build_approval_mail,
}


def automatic_cap(session: Session) -> int:
    return get_int_setting(session, AUTOMATIC_EMAIL_CAP) or 10


def cap_release(session: Session, row: OutboundEmail, now: datetime) -> Optional[datetime]:
    """None if this email may go. Otherwise when it may: this ticket's requester has already had the
    most automatic emails allowed in the last 24 hours, and the earliest moment the count drops below
    the limit is returned, so the email is held back, not lost. Counts those actually sent."""
    if row.kind not in AUTOMATIC_TO_REQUESTER:
        return None
    sent = session.exec(
        select(OutboundEmail.sent_at).where(
            OutboundEmail.ticket_id == row.ticket_id,
            OutboundEmail.kind.in_(AUTOMATIC_TO_REQUESTER),
            OutboundEmail.sent_at.is_not(None),
            OutboundEmail.sent_at >= now - CAP_WINDOW,
        ).order_by(OutboundEmail.sent_at)
    ).all()
    limit = automatic_cap(session)
    if len(sent) < limit:
        return None
    return sent[len(sent) - limit] + CAP_WINDOW


def describe_failure(error: Exception) -> str:
    """Why a send failed, without the address (httpx and Graph put it in their messages)."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"Graph answered {error.response.status_code}"
    return type(error).__name__


def is_permanent(error: Exception) -> bool:
    return (
        isinstance(error, httpx.HTTPStatusError)
        and 400 <= error.response.status_code < 500
        and error.response.status_code not in RETRYABLE_CLIENT_ERRORS
    )


def send_through_graph(mail: Mail) -> None:
    headers = {"X-Ticket-Reference": mail.reference}
    if mail.automatic:
        headers["X-Auto-Response-Suppress"] = "All"  # tell the receiving Exchange not to auto-reply
    graph_client.send_mail(mail.to, mail.subject, mail.body, cc=mail.cc, headers=headers)


def record_only(mail: Mail) -> None:
    """The dev sink: nothing leaves the machine. The row keeps the text for /admin/outbound-mail."""


def give_up(row: OutboundEmail, reason: str) -> None:
    row.attempts = MAX_ATTEMPTS
    row.last_error = reason


def _next_due(session: Session, now: datetime) -> Optional[OutboundEmail]:
    return session.exec(
        select(OutboundEmail)
        .where(
            OutboundEmail.sent_at.is_(None),
            OutboundEmail.attempts < MAX_ATTEMPTS,
            or_(OutboundEmail.next_attempt_at.is_(None), OutboundEmail.next_attempt_at <= now),
        )
        .order_by(OutboundEmail.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()


def deliver_mail(
    session: Session,
    send: Optional[Callable[[Mail], None]] = None,
    limit: int = BATCH,
    now: Optional[datetime] = None,
) -> tuple[int, int]:
    """Sends the oldest queued emails that are due, up to `limit`, one at a time and committing after
    each. Returns (sent, failed). A failure waits longer each time and is given up on at once when
    Graph refuses the message itself; a row that can never go (Unsendable, over the cap) is given up
    with its reason. With MAIL_SENDING off it does nothing."""
    if settings.mail_sending == "off":
        return 0, 0
    if send is None:
        if settings.mail_sending == "dev":
            send = record_only
        elif settings.graph_configured and settings.graph_shared_mailbox:
            send = send_through_graph
        else:
            log.warning("Emails are waiting but GRAPH_* and GRAPH_SHARED_MAILBOX are not all set in this process")
            return 0, 0
    now = now or datetime.now(timezone.utc)
    sent = failed = 0
    for _ in range(limit):
        row = _next_due(session, now)
        if row is None:
            break
        row_id = row.id
        builder = BUILDERS.get(row.kind)
        try:
            if builder is None:
                raise Unsendable("Unknown kind")
            release = cap_release(session, row, now)
            if release is not None:
                # Held back until the day's count allows it: not a failure and no try used.
                row.last_error = "Held back: the limit on automatic emails to this ticket was reached"
                row.next_attempt_at = release
                session.add(row)
                session.commit()
                continue
            mail = builder(session, row)
        except Unsendable as reason:
            give_up(row, str(reason))
            failed += 1
        except NotReady as reason:
            if now - row.created_at > NOT_READY_LIMIT:
                give_up(row, f"Gave up after {NOT_READY_LIMIT.days} days: {reason}")
                failed += 1
            else:
                row.last_error = str(reason)
                row.next_attempt_at = now + NOT_READY_WAIT
        except Exception as error:
            # A bug or a database error on this one email must not block the ones behind it: the
            # failed transaction is rolled back and the row backs off like any other failure.
            log.exception("Email %s could not be built", row_id)
            session.rollback()
            row = session.get(OutboundEmail, row_id)
            row.attempts += 1
            row.last_error = f"Could not be built: {type(error).__name__}"
            row.next_attempt_at = now + timedelta(minutes=min(2**row.attempts, MAX_BACKOFF_MINUTES))
            failed += 1
        else:
            try:
                send(mail)
            except Exception as error:
                row.attempts += 1
                row.last_error = describe_failure(error)
                if is_permanent(error):
                    row.attempts = MAX_ATTEMPTS
                else:
                    row.next_attempt_at = now + timedelta(minutes=min(2**row.attempts, MAX_BACKOFF_MINUTES))
                log.warning("Email %s failed (attempt %s): %s", row.id, row.attempts, row.last_error)
                failed += 1
            else:
                # What was sent is kept only for an email that went, so a failed row never shows
                # text that nobody received.
                row.subject, row.cc, row.body = mail.subject, ", ".join(mail.cc) or None, mail.record
                row.sent_at = datetime.now(timezone.utc)
                row.last_error = None
                sent += 1
        session.add(row)
        session.commit()  # now, so an email that went is recorded even if the next one fails
    return sent, failed

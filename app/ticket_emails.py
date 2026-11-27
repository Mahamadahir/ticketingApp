"""Every email sent or queued about a ticket, as the agent thread shows it (#349), and "Send anyway" for one held back by the
per-ticket limit on automatic emails.

The outbox (`OutboundEmail`, app/outbox.py) is the record of what the app emails; this module only describes a row for the
thread and releases a held one. Agents only: the portal's thread is still `messages.requester_visible_messages`.

- One entry per email. An email that carries a thread message (an agent's reply, an automatic notice) is shown on that message's
  box, as its delivery line; every other kind (acknowledgement, chase, approval mail, follower updates, MSP mail, ...) is a box of
  its own, "IT Support (automatic) / Sent an automatic message".
- Never a secret. The text shown is the text kept on the row (`OutboundEmail.body`), which for an approval email is the record
  with its link left out ("link code, not kept"); the body that was sent is never rebuilt or shown here.
- Send anyway sets `cap_override_*` on that one row; the worker then sends it through the normal outbox (app/mail.py skips the
  cap for a row with an override). Nothing else about sending changes.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app import outbox
from app.audit import log_action
from app.mail import HELD_BACK
from app.models import OutboundEmail, Ticket, User
from app.teams import MAX_ATTEMPTS
from app.ticket_service import record_event

EVENT_SENT_ANYWAY = "email_sent_anyway"  # detail: the kind of email (never its text or address)

KIND_LABELS = {
    outbox.REPLY: "Reply to the requester",
    outbox.NOTICE: "Automatic notice to the requester",
    outbox.RAISED_FOR: "Ticket raised for the requester",
    outbox.APPROVAL: "Approval request",
    outbox.APPROVAL_REMINDER: "Approval reminder",
    outbox.APPROVAL_OVERDUE: "Approval overdue warning",
    outbox.APPROVAL_WITHDRAWN: "Approval withdrawn",
    outbox.ACKNOWLEDGEMENT: "Acknowledgement",
    outbox.SPLIT_NOTICE: "Split notice",
    outbox.CHASE: "Chase: is this still an issue?",
    outbox.MSP_HANDOFF: "Sent to the MSP",
    outbox.MSP_RELEASE: "No longer needed, to the MSP",
    outbox.MSP_FORWARD: "MSP mail passed on to the user",
    outbox.MAILBOX_CHANGE: "Mailbox change notice",
    outbox.FOLLOWER_UPDATE: "Update to a follower",
}

QUEUED, SENT, FAILED, HELD = "queued", "sent", "failed", "held"
STATE_TONES = {QUEUED: "gray", SENT: "green", FAILED: "red", HELD: "amber"}


@dataclass
class Delivery:
    """One email as the thread draws it."""

    row_id: int
    label: str  # what kind of email it is, in plain words
    to: str
    cc: Optional[str]
    state: str  # queued, sent, failed or held
    tone: str
    sent_at: Optional[datetime] = None
    reason: Optional[str] = None  # failed: the short reason; queued: why it is waiting
    text: Optional[str] = None  # what was kept of it; None until it has been built and sent
    can_send_anyway: bool = False  # held, and the viewer may work the ticket
    released_by: Optional[str] = None  # released with Send anyway and not sent yet: who


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())


def is_held(row: OutboundEmail) -> bool:
    """Waiting on the limit, not yet released by an agent. A row that went, failed for good or was released is not held."""
    return (
        row.sent_at is None and row.attempts < MAX_ATTEMPTS and row.last_error == HELD_BACK and row.cap_override_at is None
    )


def state_of(row: OutboundEmail) -> tuple[str, Optional[str]]:
    """(state, reason) of an outbox row: Queued, Sent, Failed with the short reason, or Held back by the limit."""
    if row.sent_at is not None:
        return SENT, None
    if row.attempts >= MAX_ATTEMPTS:
        return FAILED, row.last_error or "Could not be sent"
    if is_held(row):
        return HELD, HELD_BACK
    if row.attempts > 0 and row.last_error:
        return FAILED, f"{row.last_error}. It will be tried again"
    return QUEUED, row.last_error if row.last_error != HELD_BACK else None  # only "not ready yet" (a setting is missing)


def describe(row: OutboundEmail, can_work: bool) -> Delivery:
    state, reason = state_of(row)
    return Delivery(
        row_id=row.id, label=kind_label(row.kind), to=row.to_address, cc=row.cc, state=state, tone=STATE_TONES[state],
        sent_at=row.sent_at, reason=reason, text=row.body, can_send_anyway=state == HELD and can_work,
        released_by=row.cap_override_by if row.sent_at is None else None,
    )


def ticket_emails(session: Session, ticket: Ticket) -> list[OutboundEmail]:
    return list(session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id).order_by(OutboundEmail.id)).all())


class SendAnywayError(Exception):
    pass


def send_anyway(session: Session, ticket: Ticket, row_id: int, user: User) -> bool:
    """Releases one held email from the limit: the worker sends it next, through the normal outbox. Records an audit entry and a
    ticket event (who, when). True if this call released it, False if there was nothing to do (already released, sent or no
    longer held), so a second click changes and records nothing. The caller has checked the viewer may work the ticket.
    Does not commit."""
    row = session.exec(
        select(OutboundEmail).where(OutboundEmail.id == row_id, OutboundEmail.ticket_id == ticket.id).with_for_update()
    ).first()
    if row is None:
        raise SendAnywayError("Email not found on this ticket")
    if not is_held(row):
        return False
    now = datetime.now(timezone.utc)
    row.cap_override_by, row.cap_override_at = user.email, now
    row.next_attempt_at = None  # due now
    session.add(row)
    label = kind_label(row.kind)
    log_action(session, user.email, "ticket", ticket.id, "email_sent_anyway", f"{label} (email {row.id})")
    record_event(session, ticket, EVENT_SENT_ANYWAY, user.email, detail=label)
    return True

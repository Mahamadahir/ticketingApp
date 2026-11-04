"""Sending a ticket to the MSP, putting it on hold, and the automatic handoff (phase 5A, issue #138). Nothing
here commits.

One action, `send_to_msp`, is used three ways: an agent's Send to MSP button, an escalation whose target is the
MSP (app/escalation.py), and the automatic handoff job (`run_msp_handoff`, registered in app/scheduled_jobs.py).
The ticket stays in its team with status With MSP, so nobody's access changes. The email to the MSP is built once,
by `compose`, which is also what the preview shows, and is queued with exactly that text (app/outbox.py); the
requester is told by an ordinary notice from the admin template `msp_sent_notice`. Where the MSP is, and when the
handoff runs, are admin settings (app/settings_store.py); with no MSP address nothing is sent and the job records why.

The automatic handoff. Each day from the handoff hour (in the default time zone) until the next business morning,
the open tickets of teams that are included (`Team.msp_handoff_included`) that are due to breach before that morning
and are not held go to the MSP; a ticket that becomes due or eligible later in the night goes at the next run. What is due is worked out from the tickets, not from the last run, so a late or
repeated run does the same and a second run finds nothing left. A hold lasts until it is released (an accepted risk
in docs/plan.md). The job does not run in business hours: a handoff missed overnight is not made the next day.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.config import settings
from app.categories import path_of
from app.email_templates import render_template, ticket_values
from app.followers import add_follower, followers_of
from app.mail import own_first_message
from app.messages import add_note, mail_subject, record_notice, requester_visible_messages
from app.models import (
    FINISHED_STATUSES,
    Attachment,
    AuditLog,
    Category,
    Message,
    MessageDirection,
    Team,
    Ticket,
    TicketEscalator,
    TicketStatus,
    User,
)
from app.notifications import notify_mentions
from app.outbox import is_on, queue_msp_handoff
from app.settings_store import (
    MSP_ADDRESS,
    MSP_BANK_HOLIDAYS,
    MSP_BUSINESS_MORNING_HOUR,
    MSP_HANDOFF_HOUR,
    REGISTRY,
    get_int_setting,
    get_setting,
)
from app.splitting import has_sub_tickets
from app.ticket_service import record_event, set_status
from app.time_zones import default_zone, format_moment, morning_of, next_business_morning, parse_dates

log = logging.getLogger("app.worker")

SCOPE_THREAD = "thread"
SCOPE_ORIGINAL = "original"
SCOPES = (SCOPE_THREAD, SCOPE_ORIGINAL)

EVENT_SENT_TO_MSP = "sent_to_msp"  # detail: the reason; to_value: thread, original or automatic
EVENT_MSP_HOLD = "msp_hold"
EVENT_MSP_HOLD_RELEASED = "msp_hold_released"

AUDIT_ACTOR = "system"
AUDIT_ENTITY = "msp_handoff"  # the job's own entries (a skipped day); entity_id 0

# Statuses the automatic handoff takes. Awaiting user is excluded (the SLA clock is stopped and the ball is with the
# requester), as are With supplier, Scheduled, Awaiting approval and New (untriaged).
HANDOFF_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)

NO_ADDRESS = "No MSP address is set, so nothing can be sent to the MSP. A super admin sets it under Admin > Settings."


class MspError(ValueError):
    """Why a ticket cannot be sent to the MSP or put on hold, worded for the agent."""


@dataclass
class MspPreview:
    to: str
    cc: list[str]
    subject: str
    body: str
    scope: str
    notice: str  # what the requester is told
    mail_is_off: bool  # nothing would actually be emailed (MAIL_SENDING is off)

    @property
    def digest(self) -> str:
        """A fingerprint of everything that would go, carried by the preview's confirm form so a thread that changed
        in between (a reply, a note) is shown again instead of being sent unseen."""
        text = "\x1f".join([self.to, ", ".join(self.cc), self.subject, self.body, self.scope, self.notice])
        return hashlib.sha256(text.encode()).hexdigest()


def msp_address(session: Session) -> str:
    return get_setting(session, MSP_ADDRESS).strip().lower()


def check_can_send(session: Session, ticket: Ticket) -> str:
    """The MSP address if this ticket may be sent now, else MspError saying why not."""
    address = msp_address(session)
    if not address:
        raise MspError(NO_ADDRESS)
    if address == settings.graph_shared_mailbox.strip().lower():
        raise MspError("The MSP address is the shared mailbox itself, so the email would loop. A super admin sets it under Admin > Settings.")
    if ticket.triaged_at is None:
        raise MspError("Triage the ticket first, so it has a category and a priority to send")
    if ticket.status in FINISHED_STATUSES:
        raise MspError("A resolved or closed ticket cannot be sent to the MSP")
    if ticket.status == TicketStatus.WITH_MSP:
        raise MspError("This ticket is already with the MSP")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise MspError("A ticket waiting for approval cannot be sent to the MSP until it is decided")
    if has_sub_tickets(session, ticket):
        raise MspError("This request was split: send its parts to the MSP, not the original")
    return address


# ---- The email ---------------------------------------------------------------------------------------------


def _person(session: Session, email: str) -> str:
    account = session.exec(select(User).where(User.email == email)).first()
    name = (account.display_name or "").strip() if account is not None else ""
    return f"{name} ({email})" if name and name != email else email


def _sla_text(ticket: Ticket, zone: str) -> str:
    parts = []
    if ticket.first_responded_at is None and ticket.first_response_due_at is not None:
        parts.append(f"first response by {format_moment(ticket.first_response_due_at, zone)}")
    if ticket.resolution_due_at is not None:
        parts.append(f"resolution by {format_moment(ticket.resolution_due_at, zone)}")
    return "; ".join(parts) or "none set"


def _entry(message: Message, zone: str) -> str:
    when = format_moment(message.created_at, zone)
    if message.direction == MessageDirection.INTERNAL:
        who = f"internal note by {message.sender}"
    elif message.direction == MessageDirection.OUTBOUND:
        who = f"email sent by IT ({message.sender})"
    else:
        who = f"from the user ({message.sender})"
    return f"--- {when}, {who} ---\n{message.body.strip()}"


def _conversation(session: Session, ticket: Ticket, scope: str, zone: str) -> str:
    """The thread (the user's messages, the emails IT sent, and the comments and notes agents left), or only the
    first message from the user. Ticket events are never part of it."""
    if scope == SCOPE_ORIGINAL:
        first = own_first_message(session, ticket)
        if first is None:
            return "The original request:\n\n(No message from the user is recorded on this ticket.)"
        return "The original request:\n\n" + _entry(first, zone)
    messages = list(requester_visible_messages(session, ticket))
    seen = {m.id for m in messages}
    notes = session.exec(
        select(Message).where(Message.ticket_id == ticket.id, Message.direction == MessageDirection.INTERNAL)
    ).all()
    messages += [m for m in notes if m.id not in seen]
    messages.sort(key=lambda m: (m.created_at, m.id))
    if not messages:
        return "The thread:\n\n(No messages are recorded on this ticket.)"
    return "The thread (internal notes are marked):\n\n" + "\n\n".join(_entry(m, zone) for m in messages)


def _attachments(session: Session, ticket: Ticket) -> str:
    files = session.exec(
        select(Attachment).where(Attachment.ticket_id == ticket.id, Attachment.is_inline.is_(False)).order_by(Attachment.id)
    ).all()
    if not files:
        return ""
    lines = [f"- {f.filename} ({max(1, round(f.size / 1024))} KB)" for f in files]
    return "Attachments on the ticket (names only; the files are not attached to this email):\n" + "\n".join(lines)


def _handoff_label(session: Session) -> str:
    return f"{_handoff_hour(session):02d}:00"


def compose(
    session: Session, ticket: Ticket, *, scope: str, reason: str, actor: Optional[User] = None
) -> MspPreview:
    """The email to the MSP exactly as it would go, and what the requester would be told. Changes nothing (the
    preview step), and is what send_to_msp queues. `actor` None is the automatic handoff. Raises MspError."""
    if scope not in SCOPES:
        raise MspError("Choose the thread or only the original request")
    address = check_can_send(session, ticket)
    zone = default_zone(session)
    category = session.get(Category, ticket.category_id) if ticket.category_id else None
    requested_by = (
        _person(session, actor.email) if actor is not None else f"IT Support (the automatic {_handoff_label(session)} handoff)"
    )
    values = {
        **ticket_values(ticket),
        "end_user": _person(session, ticket.requester_email),
        "requested_by": requested_by,
        "priority": ticket.priority.upper() if ticket.priority else "not set",
        "sla_due": _sla_text(ticket, zone),
        "category": path_of(session, category) if category else "none",
        "reason": reason.strip(),
        "conversation": _conversation(session, ticket, scope, zone),
        "attachments": _attachments(session, ticket),
    }
    cc = [f.email for f in followers_of(session, ticket) if f.is_internal and f.email != address]
    if actor is not None and actor.email not in cc:
        cc.append(actor.email)  # whoever sends it is copied, as an escalator is on every message
    return MspPreview(
        to=address, cc=cc, subject=mail_subject(session, ticket),
        body=render_template(session, "msp_handoff", values).body, scope=scope,
        notice=render_template(session, "msp_sent_notice", ticket_values(ticket)).body,
        mail_is_off=not is_on(),
    )


# ---- Sending -----------------------------------------------------------------------------------------------


def _lock(session: Session, ticket: Ticket) -> Ticket:
    """Re-reads the ticket under a row lock, so two clicks (or a click and the job) send it once."""
    return session.exec(
        select(Ticket).where(Ticket.id == ticket.id).with_for_update().execution_options(populate_existing=True)
    ).one()


def send_to_msp(
    session: Session, ticket: Ticket, actor: Optional[User], reason: str, scope: str = SCOPE_THREAD,
    expect_digest: Optional[str] = None,
) -> MspPreview:
    """Sends the ticket to the MSP: queues the email, moves it to With MSP (it stays in its team, assigned as it
    was), tells the requester, and records an event and an audit entry. An agent (`actor`) also becomes an escalator
    of it, as with any escalation (the reason is also an internal note, they keep access and are copied on its mail).
    `actor` None is the automatic handoff. `expect_digest` is the fingerprint of the preview the agent confirmed: if
    what would go is not that any more, nothing is sent. Raises MspError and changes nothing when it cannot be sent."""
    ticket = _lock(session, ticket)
    if actor is not None and not reason.strip():
        raise MspError("Say why it is being sent to the MSP")
    reason = reason.strip() or f"The automatic {_handoff_label(session)} handoff: due to breach before the next business morning."
    preview = compose(session, ticket, scope=scope, reason=reason, actor=actor)
    if expect_digest is not None and preview.digest != expect_digest:
        raise MspError("The ticket changed since the preview (a new message or note, for instance). Preview it again to see what will be sent.")

    now = datetime.now(timezone.utc)
    actor_email = actor.email if actor is not None else AUDIT_ACTOR
    if actor is not None:
        ticket.is_escalated = True
        ticket.escalation_reason = reason
        ticket.escalated_by_id = actor.id
        ticket.escalated_at = now
        try:
            with session.begin_nested():  # a double click is a quiet no-op
                session.add(TicketEscalator(ticket_id=ticket.id, user_id=actor.id))
                session.flush()
        except IntegrityError:
            pass
        add_follower(session, ticket, actor.email, actor.email, internal_user=actor)
    ticket.msp_hold = False  # the hold was about the automatic handoff; the ticket has gone
    ticket.msp_sent_at = now
    set_status(session, ticket, TicketStatus.WITH_MSP, actor_email, detail="sent to the MSP")
    queue_msp_handoff(session, ticket, preview.to, preview.cc, preview.subject, preview.body)
    record_notice(session, ticket, preview.notice)
    record_event(
        session, ticket, EVENT_SENT_TO_MSP, actor_email,
        to_value=scope if actor is not None else "automatic", detail=reason,
    )
    if actor is not None:
        note = add_note(
            session, ticket, actor,
            f"Sent to the MSP ({'only the original request' if scope == SCOPE_ORIGINAL else 'the thread'}). Reason: {reason}",
        )
        notify_mentions(session, ticket, note, actor, text=reason)  # only what the agent wrote can mention
    log_action(session, actor_email, "ticket", ticket.id, "sent_to_msp", reason[:200])
    return preview


# ---- Hold --------------------------------------------------------------------------------------------------


def may_hold(ticket: Ticket, user: User) -> bool:
    """Only the assigned agent puts a ticket on hold, and only one the automatic handoff could take."""
    return (
        ticket.assigned_agent_id == user.id and ticket.triaged_at is not None
        and ticket.status not in FINISHED_STATUSES and ticket.status != TicketStatus.WITH_MSP and not ticket.msp_hold
    )


def hold_ticket(session: Session, ticket: Ticket, actor: User) -> None:
    """Keeps the ticket out of the automatic handoff until someone releases it (there is no expiry: an accepted risk)."""
    if ticket.msp_hold:
        raise MspError("This ticket is already on hold")
    if ticket.assigned_agent_id != actor.id:
        raise MspError("Only the agent the ticket is assigned to can put it on hold")
    if ticket.status in FINISHED_STATUSES or ticket.status == TicketStatus.WITH_MSP:
        raise MspError("A ticket that is finished or already with the MSP needs no hold")
    ticket.msp_hold = True
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    record_event(session, ticket, EVENT_MSP_HOLD, actor.email, detail="kept out of the automatic MSP handoff until released")
    log_action(session, actor.email, "ticket", ticket.id, "msp_hold", None)


def release_hold(session: Session, ticket: Ticket, actor: User) -> None:
    """Puts the ticket back in the automatic handoff. Anyone who works the ticket may release it (the caller checks
    access), so a hold left by someone who has gone is not stuck."""
    if not ticket.msp_hold:
        raise MspError("This ticket is not on hold")
    ticket.msp_hold = False
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    record_event(session, ticket, EVENT_MSP_HOLD_RELEASED, actor.email)
    log_action(session, actor.email, "ticket", ticket.id, "msp_hold_released", None)


# ---- The automatic handoff ---------------------------------------------------------------------------------


def _hour(session: Session, key: str) -> int:
    value = get_int_setting(session, key)
    return value if value is not None and 0 <= value <= 23 else int(REGISTRY[key].default)


def _handoff_hour(session: Session) -> int:
    return _hour(session, MSP_HANDOFF_HOUR)


def _holidays(session: Session) -> frozenset:
    try:
        return frozenset(parse_dates(get_setting(session, MSP_BANK_HOLIDAYS)))
    except ValueError:
        return frozenset()  # a bad stored value must not stop the handoff; the settings page validates what it saves


def handoff_window(session: Session, now: datetime) -> Optional[tuple[datetime, datetime]]:
    """(start, end) of the handoff now in force: from the latest handoff moment that has passed, to the next business
    morning after it. None in business hours (between that morning and the next handoff moment)."""
    zone = default_zone(session)
    hour = _handoff_hour(session)
    today = now.astimezone(ZoneInfo(zone)).date()
    starts = [morning_of(today - timedelta(days=1), zone, hour), morning_of(today, zone, hour)]
    start = max((moment for moment in starts if moment <= now), default=None)
    if start is None:
        return None
    end = next_business_morning(start, zone, _hour(session, MSP_BUSINESS_MORNING_HOUR), _holidays(session))
    return (start, end) if now < end else None


def _due_before(ticket: Ticket, end: datetime) -> bool:
    """Whether a target still to be met falls due before `end` (already late counts)."""
    if ticket.first_responded_at is None and ticket.first_response_due_at is not None and ticket.first_response_due_at < end:
        return True
    return ticket.resolution_due_at is not None and ticket.resolution_due_at < end


def _eligible(session: Session, ticket: Ticket, start: datetime, end: datetime) -> bool:
    if ticket.status not in HANDOFF_STATUSES or ticket.triaged_at is None or ticket.msp_hold:
        return False
    if not _due_before(ticket, end):
        return False
    team = session.get(Team, ticket.team_id) if ticket.team_id else None
    return team is not None and team.msp_handoff_included and not has_sub_tickets(session, ticket)


def _handoff_candidates(session: Session, start: datetime, end: datetime) -> list[int]:
    return list(
        session.exec(
            select(Ticket.id)
            .join(Team, Team.id == Ticket.team_id)
            .where(
                Team.msp_handoff_included.is_(True),
                Ticket.status.in_(HANDOFF_STATUSES),
                Ticket.triaged_at.is_not(None),
                Ticket.msp_hold.is_(False),
                or_(
                    and_(Ticket.first_responded_at.is_(None), Ticket.first_response_due_at < end),
                    Ticket.resolution_due_at < end,
                ),
            )
            .order_by(Ticket.id)
        ).all()
    )


def _record_skip(session: Session, start: datetime, why: str) -> None:
    """Writes why the handoff did nothing, once for each handoff (the job runs every few minutes)."""
    detail = f"Handoff of {start.astimezone(ZoneInfo(default_zone(session))).date().isoformat()} skipped: {why}"
    exists = session.exec(
        select(AuditLog.id).where(AuditLog.entity_type == AUDIT_ENTITY, AuditLog.action == "skipped", AuditLog.detail == detail).limit(1)
    ).first()
    if exists is None:
        log_action(session, AUDIT_ACTOR, AUDIT_ENTITY, 0, "skipped", detail)
        log.warning("MSP handoff skipped: %s", why)


def _hand_off_one(session: Session, ticket_id: int, start: datetime, end: datetime) -> bool:
    ticket = session.exec(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update(skip_locked=True).execution_options(populate_existing=True)
    ).first()
    # Read again under the lock: an agent may have held it, changed its status or sent it by hand since the list was read.
    if ticket is None or not _eligible(session, ticket, start, end):
        return False
    send_to_msp(session, ticket, None, "")
    return True


def hand_off_due_tickets(session: Session, now: Optional[datetime] = None) -> int:
    """The handoff: sends every eligible ticket (see the module note). Returns how many went. A ticket that cannot be
    handled is logged and skipped (in a savepoint) so it does not block the rest."""
    now = now or datetime.now(timezone.utc)
    window = handoff_window(session, now)
    if window is None:
        return 0
    start, end = window
    if not msp_address(session):
        _record_skip(session, start, "no MSP address is set")
        return 0
    if not is_on():
        # A ticket moved to With MSP with nothing emailed would be stranded: nothing retries it.
        _record_skip(session, start, "email sending is switched off (MAIL_SENDING), so nothing could be sent")
        return 0
    sent = 0
    for ticket_id in _handoff_candidates(session, start, end):
        try:
            with session.begin_nested():
                sent += _hand_off_one(session, ticket_id, start, end)
        except Exception:
            log.exception("MSP handoff skipped ticket %s", ticket_id)
    if sent:
        log.info("MSP handoff sent %s ticket(s)", sent)
    return sent


def run_msp_handoff(session: Session) -> None:
    """The recurring job (registered in app/scheduled_jobs.py)."""
    hand_off_due_tickets(session)

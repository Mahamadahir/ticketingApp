"""Sending a ticket to the MSP, putting it on hold, and the automatic handoff (phase 5A, issue #138). Nothing
here commits.

One action, `send_to_msp`, is used three ways: an agent's Send to MSP button, an escalation whose target is the
MSP (app/escalation.py), and the automatic handoff job (`run_msp_handoff`, registered in app/scheduled_jobs.py).
The ticket stays in its team with status With MSP, so nobody's access changes. The email to the MSP is built once,
by `compose`, which is also what the preview shows, and is queued with exactly that text (app/outbox.py); the
requester is told by an ordinary notice from the admin template `msp_sent_notice`. Where the MSP is, and when the
handoff runs, are admin settings (app/settings_store.py); with no MSP address nothing is sent and the job records why.

Mail from the MSP (phase 5B, issue #139). The MSP deals with the user directly and keeps the IT mailbox copied, so its
replies arrive in the shared mailbox (app/inbound.py calls `receive_mail`). The MSP is never the requester or a
follower: its mail only ever lands on a ticket that was sent to it, and never makes a ticket. A message the requester
was on (To or Cc) is recorded as an ordinary public message; one that left them off is agents-only (`pending`) until it
is forwarded. In business hours (settings, organisation time zone, Monday to Friday, bank holidays out) the agent is
prompted and previews the forward (`compose_forward`, then `forward_message`, or `dismiss_forward`); out of hours it is
forwarded to the user automatically with no preview, an accepted risk (docs/plan.md), and the recurring job
`run_msp_forwards` forwards what was still waiting when business hours ended. A message from the MSP changes no status:
the assigned agent is told and decides, as with any reply on a With MSP ticket.

The automatic handoff. Each day from the handoff hour (in the default time zone) until the next business morning,
the open tickets of teams that are included (`Team.msp_handoff_included`) that are due to breach before that morning
and are not held go to the MSP; a ticket that becomes due or eligible later in the night goes at the next run. What is due is worked out from the tickets, not from the last run, so a late or
repeated run does the same and a second run finds nothing left. A hold lasts until it is released (an accepted risk
in docs/plan.md). The job does not run in business hours: a handoff missed overnight is not made the next day.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.audit import log_action
from app.config import settings
from app.categories import path_of
from app.email_templates import render_template, ticket_values
from app.followers import add_follower, followers_of
from app.mail import own_first_message
from app.never_follow import is_msp
from app.messages import add_note, mail_subject, record_msp_message, record_notice, requester_visible_messages
from app.models import (
    FINISHED_STATUSES,
    Attachment,
    AuditLog,
    Category,
    Message,
    MessageDirection,
    MessageVisibility,
    Team,
    Ticket,
    TicketEscalator,
    TicketEvent,
    TicketStatus,
    User,
)
from app.notifications import notify_mentions
from app.outbox import is_on, queue_msp_forward, queue_msp_handoff
from app.settings_store import (
    MSP_ADDRESS,
    MSP_ATTACH_MAX_MB,
    MSP_BANK_HOLIDAYS,
    MSP_BUSINESS_END,
    MSP_BUSINESS_MORNING_HOUR,
    MSP_BUSINESS_START,
    MSP_HANDOFF_HOUR,
    REGISTRY,
    count_setting,
    get_int_setting,
    get_setting,
)
from app.splitting import has_sub_tickets
from app.ticket_service import record_event, set_status
from app.time_zones import default_zone, format_moment, in_business_hours, morning_of, next_business_morning, parse_clock, parse_dates

log = logging.getLogger("app.worker")

SCOPE_THREAD = "thread"
SCOPE_ORIGINAL = "original"
SCOPES = (SCOPE_THREAD, SCOPE_ORIGINAL)

EVENT_SENT_TO_MSP = "sent_to_msp"  # detail: the reason; to_value: thread, original or automatic
EVENT_MSP_HOLD = "msp_hold"
EVENT_MSP_HOLD_RELEASED = "msp_hold_released"
EVENT_MSP_MAIL = "msp_mail_received"  # to_value: not_needed (the user was on it) or pending (they were left off)
EVENT_MSP_FORWARDED = "msp_forwarded"  # to_value: agent or automatic (the reporting data: manual against automatic)
EVENT_MSP_FORWARD_DISMISSED = "msp_forward_dismissed"

# Message.msp_forward values.
FORWARD_NOT_NEEDED = "not_needed"
FORWARD_PENDING = "pending"
FORWARD_FORWARDED = "forwarded"
FORWARD_DISMISSED = "dismissed"
BY_AGENT = "agent"
BY_AUTOMATIC = "automatic"

# Files go inline in sendMail, base64-encoded (a third larger), and a request is limited to about 4 MB, so no more than
# 2 MB of files in all; more needs a draft and an upload session, which is not built. The setting can lower this.
GRAPH_INLINE_LIMIT_MB = 2

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
    attachment_ids: list[int] = field(default_factory=list)  # the ticket's files that are attached to the email

    @property
    def digest(self) -> str:
        """A fingerprint of everything that would go, carried by the preview's confirm form so a thread that changed
        in between (a reply, a note) is shown again instead of being sent unseen."""
        text = "\x1f".join([self.to, ", ".join(self.cc), self.subject, self.body, self.scope, self.notice, str(self.attachment_ids)])
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
    elif message.msp_forward is not None:
        who = f"the MSP ({message.sender})"
    elif message.direction == MessageDirection.OUTBOUND:
        who = f"email sent by IT ({message.sender})"
    else:
        who = f"from the user ({message.sender})"
    return f"--- {when}, {who} ---\n{message.body.strip()}"


def _thread_messages(session: Session, ticket: Ticket, scope: str) -> list[Message]:
    """The messages that go to the MSP: the thread (the user's messages, the emails IT sent, and the comments and notes
    agents left), or only the first message from the user. Ticket events are never part of it, nor is an MSP message
    the user has not been given (agents-only until forwarded)."""
    if scope == SCOPE_ORIGINAL:
        first = own_first_message(session, ticket)
        return [first] if first is not None else []
    messages = list(requester_visible_messages(session, ticket))
    seen = {m.id for m in messages}
    notes = session.exec(
        select(Message).where(Message.ticket_id == ticket.id, Message.direction == MessageDirection.INTERNAL)
    ).all()
    messages += [m for m in notes if m.id not in seen]
    messages.sort(key=lambda m: (m.created_at, m.id))
    return messages


def _conversation(session: Session, ticket: Ticket, scope: str, zone: str) -> str:
    messages = _thread_messages(session, ticket, scope)
    if scope == SCOPE_ORIGINAL:
        if not messages:
            return "The original request:\n\n(No message from the user is recorded on this ticket.)"
        return "The original request:\n\n" + _entry(messages[0], zone)
    if not messages:
        return "The thread:\n\n(No messages are recorded on this ticket.)"
    return "The thread (internal notes are marked):\n\n" + "\n\n".join(_entry(m, zone) for m in messages)


def _budget_bytes(session: Session) -> int:
    return min(count_setting(session, MSP_ATTACH_MAX_MB), GRAPH_INLINE_LIMIT_MB) * 1024 * 1024


def plan_files(session: Session, files: list[Attachment]) -> tuple[str, list[int]]:
    """Which of these files are attached to an email, and the text that says so. In order, each file that still fits
    the total allowed (a setting, at most what Graph takes in one send) is attached; the rest are listed by name as
    not attached."""
    if not files:
        return "", []
    budget, used = _budget_bytes(session), 0
    attached: list[Attachment] = []
    left: list[Attachment] = []
    for f in files:
        if used + f.size <= budget:
            attached.append(f)
            used += f.size
        else:
            left.append(f)

    def line(f: Attachment) -> str:
        return f"- {f.filename} ({max(1, round(f.size / 1024))} KB)"

    parts = []
    if attached:
        parts.append("Attached to this email:\n" + "\n".join(line(f) for f in attached))
    if left:
        parts.append(
            "Also on the ticket but too large to attach (ask the IT mailbox for them):\n" + "\n".join(line(f) for f in left)
        )
    return "\n\n".join(parts), [f.id for f in attached]


def attachment_plan(session: Session, ticket: Ticket, scope: str = SCOPE_THREAD) -> tuple[str, list[int]]:
    """The files that go to the MSP with the email, by the same rule as the text: only those that arrived with a
    message that is sent (all of the thread, or only the first message) and, for the thread, files added to the ticket
    itself. A file on an agents-only message that is not sent (an unforwarded MSP reply) never goes. Pictures embedded
    in a message body (signature logos) are left out."""
    ids = [m.id for m in _thread_messages(session, ticket, scope)]
    where = Attachment.message_id.in_(ids) if ids else Attachment.id < 0
    if scope == SCOPE_THREAD:
        where = or_(where, and_(Attachment.ticket_id == ticket.id, Attachment.message_id.is_(None)))
    files = session.exec(select(Attachment).where(where, Attachment.is_inline.is_(False)).order_by(Attachment.id)).all()
    return plan_files(session, list(files))


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
    attachment_text, attachment_ids = attachment_plan(session, ticket, scope)
    values = {
        **ticket_values(ticket),
        "end_user": _person(session, ticket.requester_email),
        "requested_by": requested_by,
        "priority": ticket.priority.upper() if ticket.priority else "not set",
        "sla_due": _sla_text(ticket, zone),
        "category": path_of(session, category) if category else "none",
        "reason": reason.strip(),
        "conversation": _conversation(session, ticket, scope, zone),
        "attachments": attachment_text,
    }
    cc = [f.email for f in followers_of(session, ticket) if f.is_internal and f.email != address]
    if actor is not None and actor.email not in cc:
        cc.append(actor.email)  # whoever sends it is copied, as an escalator is on every message
    return MspPreview(
        to=address, cc=cc, subject=mail_subject(session, ticket),
        body=render_template(session, "msp_handoff", values).body, scope=scope,
        notice=render_template(session, "msp_sent_notice", ticket_values(ticket)).body,
        mail_is_off=not is_on(), attachment_ids=attachment_ids,
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
    queue_msp_handoff(session, ticket, preview.to, preview.cc, preview.subject, preview.body, preview.attachment_ids)
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


# ---- Mail from the MSP (5B) --------------------------------------------------------------------------------


def business_hours(session: Session) -> tuple[time, time]:
    """(start, end) of business hours, from the settings (a bad stored value falls back to the default)."""

    def read(key: str, fallback: bool = False) -> time:
        return (None if fallback else parse_clock(get_setting(session, key))) or parse_clock(REGISTRY[key].default)

    start, end = read(MSP_BUSINESS_START), read(MSP_BUSINESS_END)
    # An end at or before the start would make every moment "out of hours" and send every MSP message to the user
    # unseen, so a stored pair like that is read as the placeholders (the settings page refuses to save one).
    return (start, end) if start < end else (read(MSP_BUSINESS_START, True), read(MSP_BUSINESS_END, True))


def is_business_time(session: Session, now: Optional[datetime] = None) -> bool:
    """Whether now is in business hours: Monday to Friday, bank holidays out, in the organisation time zone."""
    start, end = business_hours(session)
    return in_business_hours(now or datetime.now(timezone.utc), default_zone(session), start, end, _holidays(session))


def is_msp_sender(session: Session, sender: str) -> bool:
    return is_msp(session, sender)


def sent_to_msp(session: Session, ticket: Ticket) -> bool:
    """Whether the ticket, or a part split from it, was sent to the MSP: mail about any part of a split request carries
    the original's reference (one thread subject for the family), so it finds the original."""
    if ticket.msp_sent_at is not None:
        return True
    return session.exec(
        select(Ticket.id).where(Ticket.split_from_id == ticket.id, Ticket.msp_sent_at.is_not(None)).limit(1)
    ).first() is not None


def receive_mail(
    session: Session, ticket: Ticket, *, sender: str, body: str, recipients: list[str], now: Optional[datetime] = None
) -> Message:
    """Records a mail from the MSP on the ticket it belongs to (app/inbound.py found the ticket and checked it was
    sent to the MSP; `body` is the mail's own new text, uniqueBody). `recipients` is everyone on To and Cc. If the
    requester was one of them they already have it and it is an ordinary public message. If the MSP left them off, it
    is agents-only until forwarded: out of business hours it is forwarded to them with no preview, and in
    business hours the agent is told and previews it (`forward_if_out_of_hours` does the out-of-hours forward, once the
    files are stored). Never changes the ticket's status. Does not commit."""
    requester = ticket.requester_email.strip().lower()
    on_it = requester in {r.strip().lower() for r in recipients}
    state = FORWARD_NOT_NEEDED if on_it else FORWARD_PENDING
    message = record_msp_message(session, ticket, sender=sender, body=body, forward=state)
    session.flush()
    record_event(
        session, ticket, EVENT_MSP_MAIL, sender, to_value=state,
        detail="The user was on it" if on_it else "The user was left off To and Cc",
    )
    log_action(session, sender, "ticket", ticket.id, "msp_mail_received", state)
    return message


def forward_if_out_of_hours(session: Session, ticket: Ticket, message: Message, now: Optional[datetime] = None) -> bool:
    """Out of business hours (with mail on), forwards a message the MSP left the user off, at once with no preview.
    Called once the mail's files are stored, so they go with it. True if it was forwarded."""
    if message.msp_forward != FORWARD_PENDING or not is_on() or is_business_time(session, now):
        return False
    forward_message(session, ticket, message, None)
    return True


def pending_forwards(session: Session, ticket: Ticket) -> list[Message]:
    return list(
        session.exec(
            select(Message).where(Message.ticket_id == ticket.id, Message.msp_forward == FORWARD_PENDING).order_by(Message.id)
        ).all()
    )


@dataclass
class ForwardPreview:
    to: str
    subject: str
    body: str
    mail_is_off: bool
    attachment_ids: list[int] = field(default_factory=list)  # the MSP's files that are attached to the forward

    @property
    def digest(self) -> str:
        return hashlib.sha256("\x1f".join([self.to, self.subject, self.body, str(self.attachment_ids)]).encode()).hexdigest()


def compose_forward(session: Session, ticket: Ticket, message: Message) -> ForwardPreview:
    """The email to the user passing on an MSP message they were left off, exactly as it would go (the preview
    step; forward_message queues this text). Changes nothing. Raises MspError."""
    if message.ticket_id != ticket.id or message.msp_forward != FORWARD_PENDING:
        raise MspError("That message from the MSP is not waiting to be forwarded")
    files = session.exec(
        select(Attachment).where(Attachment.message_id == message.id, Attachment.is_inline.is_(False)).order_by(Attachment.id)
    ).all()
    attachment_text, attachment_ids = plan_files(session, list(files))
    values = {
        **ticket_values(ticket), "msp_name": message.sender, "msp_message": message.body.strip(), "attachments": attachment_text,
    }
    return ForwardPreview(
        to=ticket.requester_email, subject=mail_subject(session, ticket),
        body=render_template(session, "msp_forward", values).body, mail_is_off=not is_on(), attachment_ids=attachment_ids,
    )


def forward_message(
    session: Session, ticket: Ticket, message: Message, actor: Optional[User], expect_digest: Optional[str] = None
) -> ForwardPreview:
    """Forwards a message the MSP left the user off to the user: queues the email, and from then on the message is
    one the user may see. `actor` None is the automatic forward out of hours (no preview). `expect_digest` is the
    fingerprint of the preview the agent confirmed. Raises MspError and changes nothing when it cannot be forwarded."""
    if not is_on():
        raise MspError("Email sending is switched off on this server, so nothing can be forwarded")
    locked = session.exec(
        select(Message).where(Message.id == message.id).with_for_update().execution_options(populate_existing=True)
    ).one()  # two clicks, or a click and the job, forward it once
    preview = compose_forward(session, ticket, locked)
    if expect_digest is not None and preview.digest != expect_digest:
        raise MspError("That message changed since the preview. Preview it again to see what will be sent.")
    locked.msp_forward = FORWARD_FORWARDED
    locked.visibility = MessageVisibility.PUBLIC
    session.add(locked)
    queue_msp_forward(session, ticket, locked, preview.to, preview.subject, preview.body, preview.attachment_ids)
    by = BY_AGENT if actor is not None else BY_AUTOMATIC
    who = actor.email if actor is not None else AUDIT_ACTOR
    record_event(
        session, ticket, EVENT_MSP_FORWARDED, who, to_value=by,
        detail="Passed on to the user" + ("" if actor is not None else " automatically, out of business hours, with no preview"),
    )
    log_action(session, who, "ticket", ticket.id, "msp_forwarded", by)
    return preview


def dismiss_forward(session: Session, ticket: Ticket, message: Message, actor: User) -> None:
    """The agent decides the user does not need this MSP message (it stays on the ticket, agents only)."""
    locked = session.exec(
        select(Message).where(Message.id == message.id).with_for_update().execution_options(populate_existing=True)
    ).one()
    if locked.ticket_id != ticket.id or locked.msp_forward != FORWARD_PENDING:
        raise MspError("That message from the MSP is not waiting to be forwarded")
    locked.msp_forward = FORWARD_DISMISSED
    session.add(locked)
    record_event(session, ticket, EVENT_MSP_FORWARD_DISMISSED, actor.email, detail="Not passed on to the user")
    log_action(session, actor.email, "ticket", ticket.id, "msp_forward_dismissed", None)


def forward_waiting_messages(session: Session, now: Optional[datetime] = None) -> int:
    """Out of business hours, forwards every MSP message still waiting for an agent (the prompt was not answered
    before business hours ended). In business hours, with mail off, or with nothing waiting it does nothing. Each is
    done in a savepoint so one that cannot be does not block the rest. Returns how many went."""
    if not is_on() or is_business_time(session, now):
        return 0
    rows = session.exec(
        select(Message.id, Message.ticket_id).where(Message.msp_forward == FORWARD_PENDING).order_by(Message.id)
    ).all()
    sent = 0
    for message_id, ticket_id in rows:
        try:
            with session.begin_nested():
                forward_message(session, session.get(Ticket, ticket_id), session.get(Message, message_id), None)
                sent += 1
        except MspError:
            continue  # taken by someone in the meantime
        except Exception:
            log.exception("MSP forward skipped message %s", message_id)
    if sent:
        log.info("MSP forwards sent %s message(s)", sent)
    return sent


def run_msp_forwards(session: Session) -> None:
    """The recurring job (registered in app/scheduled_jobs.py)."""
    forward_waiting_messages(session)


# ---- Reporting data (the reports themselves are phase 7) --------------------------------------------------


def handoff_counts(session: Session, since: datetime, until: datetime) -> dict[str, int]:
    """How many tickets went to the MSP, and how many MSP messages were forwarded to users, between two moments:
    by the system (automatic) against by an agent (manual). Data only; read from the ticket events."""

    def count(event_type: str, *conditions) -> int:
        return session.exec(
            select(func.count()).select_from(TicketEvent).where(
                TicketEvent.event_type == event_type, TicketEvent.created_at >= since, TicketEvent.created_at < until,
                *conditions,
            )
        ).one()

    return {
        "sent_automatic": count(EVENT_SENT_TO_MSP, TicketEvent.to_value == BY_AUTOMATIC),
        "sent_manual": count(EVENT_SENT_TO_MSP, TicketEvent.to_value != BY_AUTOMATIC),
        "forwarded_automatic": count(EVENT_MSP_FORWARDED, TicketEvent.to_value == BY_AUTOMATIC),
        "forwarded_manual": count(EVENT_MSP_FORWARDED, TicketEvent.to_value == BY_AGENT),
        "forward_dismissed": count(EVENT_MSP_FORWARD_DISMISSED),
    }

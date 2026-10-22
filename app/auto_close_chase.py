"""Auto-close and chase (phase 4H): two recurring jobs on the database-driven scheduler
(app/scheduled_jobs.py), each with an on/off switch and its days under Admin > Settings. Both are off
until staging proves mail.

Auto-close (`auto_close_resolved`): a ticket that has been Resolved for `auto_close_after_days` (placeholder 7)
is set to Closed by the system, with an `auto_closed` event and an audit entry. A reopen or a reply before then
has already taken it out of Resolved, so it is never closed under someone who just came back.

Chase (`chase_quiet_tickets`): a ticket in Awaiting user for `chase_after_days` (placeholder 3) with no reply
is emailed "Is this still an issue?" (the `chase` template, kind `chase`, which counts toward the automatic-email
cap). `chased_at` and `chase_count` on the ticket make this happen once per wait. If it is still Awaiting user
`chase_resolve_after_days` (placeholder 3) after that email was *sent*, it is resolved automatically
(app/resolution.py, no agent, the `auto_resolve_note` text, and the resolution email with the reopen link).
Anything that takes the ticket out of Awaiting user (a reply by mail or portal, the reopen link, an agent's status
change) clears the chase in `set_status`, so a ticket that goes back to Awaiting user starts a fresh wait.

- Only Awaiting user is chased, so With supplier, With MSP, Scheduled (and every other status) never are.
- A split original is never chased: its status follows its parts, so each part is chased on its own. An
  original that has been Resolved for long enough is closed like any ticket.
- If the chase email is held (the cap, mail down) the clock waits for it to go; if it is given up on, the ticket
  is left for an agent rather than resolved without the requester ever having been asked.
- With MAIL_SENDING off nothing is chased (the email would never exist), but a ticket already chased is still
  resolved when its time comes.

Idempotent by data: what is due comes from the ticket's status, `resolved_at`, the status events and the chase
email's `sent_at`, and is re-checked under a row lock taken with SKIP LOCKED (like app/approval_reminders.py),
so a ticket an agent is working on is left to the next run and the job never waits on a lock. Run late, a ticket
is closed or chased once; run twice, the second run finds nothing due. Nothing here commits.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.audit import log_action
from app.models import OutboundEmail, Ticket, TicketEvent, TicketStatus
from app.mail import give_up
from app.outbox import CHASE, is_on, queue_chase
from app.resolution import ResolveError, auto_resolution_note, resolve_ticket
from app.settings_store import (
    AUTO_CLOSE_AFTER_DAYS,
    AUTO_CLOSE_ENABLED,
    CHASE_AFTER_DAYS,
    CHASE_ENABLED,
    CHASE_RESOLVE_AFTER_DAYS,
    count_setting,
    get_flag,
)
from app.teams import MAX_ATTEMPTS
from app.splitting import SplitError, check_manual_status, has_sub_tickets
from app.ticket_service import EVENT_STATUS_CHANGED, record_event, set_status

log = logging.getLogger("app.worker")

AUDIT_ACTOR = "system"
EVENT_AUTO_CLOSED = "auto_closed"  # detail: how long it had been resolved
EVENT_CHASED = "chased"  # detail: how long it had been waiting
AUDIT_AUTO_CLOSED = "auto_closed"
AUDIT_CHASED = "chased"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _lock(session: Session, ticket_id: int) -> Optional[Ticket]:
    """Locks the ticket (its original first, like everything in a family) without waiting, and returns it
    freshly read; None if someone holds a lock or it is gone. The caller re-checks what made it due."""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return None
    if ticket.split_from_id is not None:
        original = session.exec(
            select(Ticket.id).where(Ticket.id == ticket.split_from_id).with_for_update(skip_locked=True)
        ).first()
        if original is None:
            return None
    return session.exec(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update(skip_locked=True).execution_options(populate_existing=True)
    ).first()


def _has_parts(ticket_id):
    return select(Ticket.id).where(Ticket.split_from_id == ticket_id).exists()


def waiting_since(session: Session, ticket: Ticket) -> datetime:
    """When the ticket went into Awaiting user this time: its latest status event to that status (a ticket
    that left and came back starts again)."""
    moment = session.exec(
        select(func.max(TicketEvent.created_at)).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EVENT_STATUS_CHANGED,
            TicketEvent.to_value == TicketStatus.AWAITING_USER.value,
        )
    ).first()
    return _aware(moment or ticket.updated_at)


def _each(session: Session, ticket_ids, work, *args) -> None:
    """Runs work(session, ticket_id, *args) for each ticket in a savepoint, so one ticket that cannot be handled
    (an unexpected error) is logged and skipped instead of rolling back, and blocking, every other ticket."""
    for ticket_id in ticket_ids:
        try:
            with session.begin_nested():
                work(session, ticket_id, *args)
        except Exception:
            log.exception("Auto-close/chase skipped ticket %s", ticket_id)


def _close_one(session: Session, ticket_id: int, now: datetime, days: int) -> None:
    ticket = _lock(session, ticket_id)
    # Read again under the lock: a reply or reopen since the list was read has taken it out of Resolved.
    if ticket is None or ticket.status != TicketStatus.RESOLVED or ticket.resolved_at is None:
        return
    if _aware(ticket.resolved_at) > now - timedelta(days=days):
        return
    if ticket.split_from_id is not None:
        original = session.get(Ticket, ticket.split_from_id)
        if original is not None and original.status == TicketStatus.RESOLVED:
            return  # a family closes from the original: reopening it brings back only its Resolved parts
    try:
        check_manual_status(session, ticket, TicketStatus.CLOSED)  # a split original with an unfinished part stays
    except SplitError:
        return
    set_status(session, ticket, TicketStatus.CLOSED, None, detail="auto-closed")
    record_event(session, ticket, EVENT_AUTO_CLOSED, None, detail=f"Resolved for {days} days")
    log_action(session, AUDIT_ACTOR, "ticket", ticket.id, AUDIT_AUTO_CLOSED, f"Resolved for {days} days")


def close_resolved_tickets(session: Session, now: datetime) -> None:
    days = count_setting(session, AUTO_CLOSE_AFTER_DAYS)
    ids = session.exec(
        select(Ticket.id).where(
            Ticket.status == TicketStatus.RESOLVED,
            Ticket.resolved_at.is_not(None),
            Ticket.resolved_at <= now - timedelta(days=days),
        ).order_by(Ticket.id)
    ).all()
    _each(session, ids, _close_one, now, days)


def _last_chase_email(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    return session.exec(
        select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == CHASE)
        .order_by(OutboundEmail.id.desc()).limit(1)
    ).first()


def _chase_one(session: Session, ticket_id: int, now: datetime, days: int) -> None:
    ticket = _lock(session, ticket_id)
    if ticket is None or ticket.status != TicketStatus.AWAITING_USER or ticket.chased_at is not None or has_sub_tickets(session, ticket):
        return
    since = waiting_since(session, ticket)
    if now < since + timedelta(days=days):
        return
    # A chase from an earlier wait that never went (held by the cap, say) must not go out beside this one.
    for old in session.exec(
        select(OutboundEmail).where(
            OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == CHASE, OutboundEmail.sent_at.is_(None),
            OutboundEmail.attempts < MAX_ATTEMPTS,
        )
    ).all():
        give_up(old, "Replaced by a newer chase")
        session.add(old)
    if queue_chase(session, ticket) is None:
        return
    ticket.chased_at = now
    ticket.chase_count += 1
    session.add(ticket)
    waited = (now - since).days
    record_event(session, ticket, EVENT_CHASED, None, detail=f"No reply for {waited} days")
    log_action(session, AUDIT_ACTOR, "ticket", ticket.id, AUDIT_CHASED, f"No reply for {waited} days")


def send_chases(session: Session, now: datetime) -> None:
    """Queues the chase email for each Awaiting user ticket that has waited long enough and has not been chased."""
    if not is_on():
        return
    days = count_setting(session, CHASE_AFTER_DAYS)
    ids = session.exec(
        select(Ticket.id).where(
            Ticket.status == TicketStatus.AWAITING_USER,
            Ticket.triaged_at.is_not(None),
            Ticket.chased_at.is_(None),
            ~_has_parts(Ticket.id),
        ).order_by(Ticket.id)
    ).all()
    _each(session, ids, _chase_one, now, days)


def _resolve_one(session: Session, ticket_id: int, now: datetime, days: int) -> None:
    ticket = _lock(session, ticket_id)
    if ticket is None or ticket.status != TicketStatus.AWAITING_USER or ticket.chased_at is None or has_sub_tickets(session, ticket):
        return
    email = _last_chase_email(session, ticket)
    # Counted from when the chase actually went: not while it is held or retrying, and never if it was given up on.
    if email is None or email.sent_at is None or now < _aware(email.sent_at) + timedelta(days=days):
        return
    waited = max(1, (now - waiting_since(session, ticket)).days)
    try:
        resolve_ticket(
            session, ticket, None, auto_resolution_note(session, ticket, waited),
            detail=f"auto-resolved: no reply after the chase ({waited} days)",
        )
    except ResolveError as error:
        log.info("Ticket %s not auto-resolved: %s", ticket.id, error)


def resolve_unanswered(session: Session, now: datetime) -> None:
    """Resolves the chased tickets that are still waiting `chase_resolve_after_days` after the chase email went."""
    days = count_setting(session, CHASE_RESOLVE_AFTER_DAYS)
    ids = session.exec(
        select(Ticket.id).where(
            Ticket.status == TicketStatus.AWAITING_USER, Ticket.chased_at.is_not(None), ~_has_parts(Ticket.id)
        ).order_by(Ticket.id)
    ).all()
    _each(session, ids, _resolve_one, now, days)


def run_auto_close(session: Session, now: Optional[datetime] = None) -> None:
    """The auto-close job. Does not commit (the runner does)."""
    if not get_flag(session, AUTO_CLOSE_ENABLED):
        return
    close_resolved_tickets(session, now or datetime.now(timezone.utc))


def run_chase(session: Session, now: Optional[datetime] = None) -> None:
    """The chase job. Does not commit (the runner does)."""
    if not get_flag(session, CHASE_ENABLED):
        return
    now = now or datetime.now(timezone.utc)
    resolve_unanswered(session, now)
    send_chases(session, now)

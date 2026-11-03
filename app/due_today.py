"""The morning of a Scheduled ticket's Due date (issue #121). Nothing here commits.

Scheduled is work planned for a day, and only an agent sets it. From the SCHEDULED_MORNING_HOUR on the Due date,
in the requester's time zone, this job moves the ticket back to Open, tells the assignee in the bell ("INC-1042 is
due today"), and records a status event and an audit entry by `system`. It is a recurring job
(app/scheduled_jobs.py): each ticket is locked with FOR UPDATE SKIP LOCKED, so a second worker skips it, and
re-checked under the lock, so running late or twice does nothing the second time. A ticket whose status follows its
sub-tickets is left to them.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.audit import log_action
from app.models import Ticket, TicketStatus
from app.notifications import notify_due_today
from app.splitting import has_sub_tickets
from app.ticket_service import set_status
from app.time_zones import morning_of, scheduled_morning_hour, zone_for_email  # morning_of: re-exported for tests

log = logging.getLogger("app.worker")

AUDIT_ACTOR = "system"
AUDIT_DUE_TODAY = "due_today"


def _wake_one(session: Session, ticket_id: int, now: datetime, hour: int) -> bool:
    ticket = session.exec(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update(skip_locked=True).execution_options(populate_existing=True)
    ).first()
    # Read again under the lock: an agent may have moved the ticket or its date since the list was read.
    if ticket is None or ticket.status != TicketStatus.SCHEDULED or ticket.due_on is None:
        return False
    if now < morning_of(ticket.due_on, zone_for_email(session, ticket.requester_email), hour):
        return False
    if has_sub_tickets(session, ticket):
        return False
    set_status(session, ticket, TicketStatus.OPEN, None, detail="due today")
    log_action(session, AUDIT_ACTOR, "ticket", ticket.id, AUDIT_DUE_TODAY, f"Scheduled ticket due {ticket.due_on.isoformat()}, back to Open")
    notify_due_today(session, ticket)
    return True


def wake_scheduled_tickets(session: Session, now: Optional[datetime] = None) -> int:
    """Moves each Scheduled ticket whose Due morning has come back to Open. Returns how many moved. One ticket
    that cannot be handled is logged and skipped (in a savepoint) rather than blocking the rest."""
    now = now or datetime.now(timezone.utc)
    hour = scheduled_morning_hour(session)
    # Candidates: due today or earlier somewhere on Earth (a day of slack either side covers every zone);
    # the exact moment is checked per ticket under its lock.
    ids = session.exec(
        select(Ticket.id).where(
            Ticket.status == TicketStatus.SCHEDULED,
            Ticket.due_on.is_not(None),
            Ticket.due_on <= (now + timedelta(days=1)).date(),
        ).order_by(Ticket.id)
    ).all()
    woken = 0
    for ticket_id in ids:
        try:
            with session.begin_nested():
                woken += _wake_one(session, ticket_id, now, hour)
        except Exception:
            log.exception("Scheduled wake-up skipped ticket %s", ticket_id)
    return woken


def run_scheduled_due(session: Session) -> None:
    """The recurring job (registered in app/scheduled_jobs.py)."""
    wake_scheduled_tickets(session)

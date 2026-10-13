"""Logging tickets and fixes after the fact. Nothing here commits.

An agent can enter when the problem was reported and when it was fixed, up to 30 days
back and never in the future. `reported_at` and `resolved_at` hold the dates entered;
`created_at` keeps the real entry time and the audit log records who changed what. Once
a ticket has been backdated it is flagged for good: it counts towards volume but is
excluded from SLA figures (app/sla.py), because its clock would measure when someone
got round to logging it, not how fast it was handled.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session

from app.models import FINISHED_STATUSES, Ticket
from app.sla import recalculate_deadlines
from app.ticket_service import record_event

EVENT_BACKDATED = "backdated"

MAX_DAYS_BACK = 30
FORMAT = "%Y-%m-%dT%H:%M"  # what an HTML datetime-local input sends


class BackdateError(ValueError):
    """A reason the dates cannot be accepted, worded for the agent."""


def parse_moment(text: Optional[str]) -> Optional[datetime]:
    """A date and time typed as 2026-10-01T14:30, read as UTC (viewer timezones come
    with the timezone work). Blank means "leave as it is"."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise BackdateError("Enter the date and time as YYYY-MM-DDTHH:MM")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def backdate_ticket(
    session: Session,
    ticket: Ticket,
    actor_email: str,
    *,
    reported_at: Optional[datetime] = None,
    resolved_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Sets the reported and/or resolved moments. Returns whether anything changed.
    Resolved can only be set on a finished ticket, and never before the report."""
    now = now or datetime.now(timezone.utc)
    earliest = now - timedelta(days=MAX_DAYS_BACK)

    new_reported = reported_at if reported_at is not None else _aware(ticket.reported_at)
    if reported_at is not None:
        if reported_at > now:
            raise BackdateError("The reported time cannot be in the future")
        if reported_at < earliest:
            raise BackdateError(f"The reported time cannot be more than {MAX_DAYS_BACK} days back")

    new_resolved = ticket.resolved_at
    if resolved_at is not None:
        if ticket.status not in FINISHED_STATUSES:
            raise BackdateError("Only a resolved or closed ticket has a resolved time")
        if resolved_at > now:
            raise BackdateError("The resolved time cannot be in the future")
        if resolved_at < earliest:
            raise BackdateError(f"The resolved time cannot be more than {MAX_DAYS_BACK} days back")
        new_resolved = resolved_at
    if new_resolved is not None and _aware(new_resolved) < new_reported:
        raise BackdateError("The resolved time cannot be before the reported time")

    changes = []
    if reported_at is not None and reported_at != _aware(ticket.reported_at):
        changes.append(("reported", ticket.reported_at, reported_at))
        ticket.reported_at = reported_at
    if resolved_at is not None and (ticket.resolved_at is None or resolved_at != _aware(ticket.resolved_at)):
        changes.append(("resolved", ticket.resolved_at, resolved_at))
        ticket.resolved_at = resolved_at
    if not changes:
        return False

    ticket.is_backdated = True
    recalculate_deadlines(session, ticket)  # a backdated ticket has none
    ticket.updated_at = now
    session.add(ticket)
    for what, before, after in changes:
        record_event(
            session, ticket, EVENT_BACKDATED, actor_email,
            before.strftime(FORMAT) if before else None, after.strftime(FORMAT), detail=what,
        )
    return True

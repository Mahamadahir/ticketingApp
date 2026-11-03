"""Logging tickets and fixes after the fact. Nothing here commits.

For a ticket an agent raised on someone's behalf (source "agent", e.g. the person came
to the desk and it was fixed in person) the agent can enter when the problem was reported
and when it was fixed, up to 30 days back and never in the future. Tickets the requester
opened themselves cannot be backdated. `reported_at` and `resolved_at` hold the dates entered;
`created_at` keeps the real entry time and the audit log records who changed what. Once
a ticket has been backdated it is flagged for good: it counts towards volume but is
excluded from SLA figures (app/sla.py), because its clock would measure when someone
got round to logging it, not how fast it was handled.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.models import FINISHED_STATUSES, Ticket, TicketSource
from app.sla import recalculate_deadlines
from app.ticket_service import record_event
from app.time_zones import valid_zone

EVENT_BACKDATED = "backdated"

MAX_DAYS_BACK = 30
FORMAT = "%Y-%m-%dT%H:%M"  # what an HTML datetime-local input sends


class BackdateError(ValueError):
    """A reason the dates cannot be accepted, worded for the agent."""


def may_backdate(ticket: Ticket) -> bool:
    """Only a ticket an agent raised on someone's behalf (source "agent") can be backdated:
    the requester came to IT in person and it was fixed on the spot. A ticket the
    requester opened themselves (email, portal, Teams) keeps its real clock."""
    return ticket.source == TicketSource.AGENT


def parse_moment(text: Optional[str], zone_name: str = "UTC") -> Optional[datetime]:
    """A date and time typed as 2026-10-01T14:30 (seconds are accepted too), read in the typist's time
    zone (UTC when none is given) and returned as UTC. Blank means "leave as it is"."""
    text = (text or "").strip()
    if not text:
        return None
    for form in (FORMAT, FORMAT + ":%S"):
        try:
            return datetime.strptime(text, form).replace(tzinfo=ZoneInfo(valid_zone(zone_name) or "UTC")).astimezone(timezone.utc)
        except ValueError:
            continue
    raise BackdateError("Enter the date and time as YYYY-MM-DDTHH:MM")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _minute(value: datetime) -> datetime:
    """The form shows and sends minutes, so a stored moment with seconds must compare
    equal to its own pre-filled value: saving the form untouched changes nothing."""
    return _aware(value).replace(second=0, microsecond=0)


def backdate_ticket(
    session: Session,
    ticket: Ticket,
    actor_email: str,
    *,
    reported_at: Optional[datetime] = None,
    resolved_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> list[tuple[str, Optional[datetime], datetime]]:
    """Sets the reported and/or resolved moments. Returns what changed as
    (which, before, after) triples, empty when nothing did. Only a moment that actually
    changes is checked against the 30-day window, so an old ticket's other date can
    still be edited. Resolved can only be set on a finished ticket, and never before
    the report."""
    if not may_backdate(ticket):
        raise BackdateError("Only a ticket an agent raised on someone's behalf can be backdated")
    now = now or datetime.now(timezone.utc)
    earliest = now - timedelta(days=MAX_DAYS_BACK)

    reported_changes = reported_at is not None and reported_at != _minute(ticket.reported_at)
    resolved_changes = resolved_at is not None and (
        ticket.resolved_at is None or resolved_at != _minute(ticket.resolved_at)
    )

    if reported_changes:
        if reported_at > now:
            raise BackdateError("The reported time cannot be in the future")
        if reported_at < earliest:
            raise BackdateError(f"The reported time cannot be more than {MAX_DAYS_BACK} days back")
    if resolved_changes:
        if ticket.status not in FINISHED_STATUSES:
            raise BackdateError("Only a resolved or closed ticket has a resolved time")
        if resolved_at > now:
            raise BackdateError("The resolved time cannot be in the future")
        if resolved_at < earliest:
            raise BackdateError(f"The resolved time cannot be more than {MAX_DAYS_BACK} days back")

    final_reported = reported_at if reported_changes else _aware(ticket.reported_at)
    final_resolved = resolved_at if resolved_changes else ticket.resolved_at
    if final_resolved is not None and (reported_changes or resolved_changes) and _aware(final_resolved) < final_reported:
        raise BackdateError("The resolved time cannot be before the reported time")

    changes = []
    if reported_changes:
        changes.append(("reported", ticket.reported_at, reported_at))
        ticket.reported_at = reported_at
    if resolved_changes:
        changes.append(("resolved", ticket.resolved_at, resolved_at))
        ticket.resolved_at = resolved_at
    if not changes:
        return []

    ticket.is_backdated = True
    recalculate_deadlines(session, ticket)  # a backdated ticket has none
    ticket.updated_at = now
    session.add(ticket)
    for what, before, after in changes:
        record_event(
            session, ticket, EVENT_BACKDATED, actor_email,
            before.strftime(FORMAT) if before else None, after.strftime(FORMAT), detail=what,
        )
    return changes
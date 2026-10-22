"""The Required by date on a service request (SRP-1). Nothing here commits.

A request type can offer a date (`RequestType.offers_required_by`, off by default). When a ticket has
one it replaces the SLA resolution target (app/sla.py: the deadline is the end of that day, UTC) and the
agents' queue ranks the ticket from `lead time` days before it (the REQUIRED_BY_LEAD_DAYS setting). The
requester gives it when raising the request or, until triage, afterwards; then only agents who work the
ticket can change it (app/visibility.py, `may_set_required_by`). A date before today is refused unless an
agent is backdating (a ticket raised for someone with earlier dates, app/backdating.py). Every change is a
ticket event and an audit entry. See docs/plan.md, "Required by date on service requests".
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session

from app.audit import log_action
from app.models import FINISHED_STATUSES, RequestType, Ticket
from app.sla import recalculate_deadlines
from app.ticket_service import EVENT_REQUIRED_BY_CHANGED, record_event

MAX_DAYS_AHEAD = 3650  # a sanity bound on typing, not a business rule


class RequiredByError(ValueError):
    """A reason the date cannot be accepted, worded for whoever is filling in the form."""


def parse_required_by(text: Optional[str]) -> Optional[date]:
    """A date typed as 2026-10-30 (what an HTML date input sends). Blank means none."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise RequiredByError("Enter the Required by date as YYYY-MM-DD")


def offers_required_by(session: Session, ticket: Ticket) -> bool:
    """Whether the ticket's request type offers the field; an ordinary ticket never does."""
    if ticket.request_type_id is None:
        return False
    request_type = session.get(RequestType, ticket.request_type_id)
    return request_type is not None and request_type.offers_required_by


def set_required_by(
    session: Session,
    ticket: Ticket,
    value: Optional[date],
    actor_email: str,
    *,
    backdating: bool = False,
    today: Optional[date] = None,
) -> bool:
    """Sets, changes or clears the date and works the deadline out again. Returns whether
    anything changed. The caller has already checked who may (`may_set_required_by`);
    `backdating` is true when an agent is backdating the ticket, which lets a past date in. A
    date already on the ticket is not checked again, so an old one is not trapped, and clearing
    is always allowed."""
    if value == ticket.required_by:
        return False
    if ticket.status in FINISHED_STATUSES:
        raise RequiredByError("The Required by date of a resolved or closed request cannot be changed")
    if value is not None:
        if not offers_required_by(session, ticket):
            raise RequiredByError("This kind of request does not take a Required by date")
        today = today or datetime.now(timezone.utc).date()
        if value < today and not backdating:
            raise RequiredByError("The Required by date cannot be in the past (dates are UTC)")
        if value > today + timedelta(days=MAX_DAYS_AHEAD):
            raise RequiredByError("That Required by date is too far ahead")
    before = ticket.required_by
    ticket.required_by = value
    recalculate_deadlines(session, ticket)
    session.add(ticket)
    shown = (before.isoformat() if before else None, value.isoformat() if value else None)
    record_event(session, ticket, EVENT_REQUIRED_BY_CHANGED, actor_email, shown[0], shown[1])
    log_action(session, actor_email, "ticket", ticket.id, "required_by_changed", f"{shown[0] or 'none'} -> {shown[1] or 'none'}")
    return True

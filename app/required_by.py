"""The Required by date and the Due date on a ticket (SRP-1, issue #92; the Due date, issue #121). Nothing
here commits.

A request type can offer a Required by date (`RequestType.offers_required_by`, off by default): the day the
requester needs the thing. Alongside it a ticket has a **Due date** (`Ticket.due_on`): the SLA resolution target
is the end of that day in the requester's time zone (app/sla.py, app/time_zones.py), and the agents' queue ranks
the ticket from the request type's queue lead time before it.

- Due defaults to Required by minus N days, N being the request type's `due_days_before` or, when blank, the
  DUE_DAYS_BEFORE_REQUIRED_BY setting (a room needed on the 23rd is due by the end of the 22nd). The default is
  never earlier than today and never later than Required by.
- An agent who works the ticket can move Due (`set_due`, `visibility.may_set_due`): every move needs a reason,
  recorded as a ticket event and an audit entry, and Due is never later than Required by. A Due set by hand
  stays when Required by changes (clamped so it is never after Required by); otherwise it is recalculated.
- A ticket with no Required by can still be given a Due date by an agent. Scheduled needs one
  (`prepare_scheduled`, called by every way of setting that status).
- Awaiting user pauses the SLA clock but never moves Due.

The requester gives Required by when raising the request and can change it in the portal at any time while the
ticket is open; agents who work the ticket can set, change or clear it (`visibility.may_set_required_by`). A date
before today is refused unless an agent is backdating (app/backdating.py), and one more than ten years ahead is
always refused. Every change is a ticket event and an audit entry. See docs/plan.md, "Required by date on
service requests".
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.audit import log_action
from app.models import FINISHED_STATUSES, RequestType, Ticket, TicketStatus
from app.settings_store import DUE_DAYS_BEFORE_REQUIRED_BY, count_setting
from app.sla import recalculate_deadlines
from app.ticket_service import EVENT_DUE_CHANGED, EVENT_REQUIRED_BY_CHANGED, record_event
from app.time_zones import morning_of, scheduled_morning_hour, zone_for_email

MAX_DAYS_AHEAD = 3650  # a sanity bound on typing, not a business rule
MAX_REASON_LENGTH = 500


class RequiredByError(ValueError):
    """A reason the date cannot be accepted, worded for whoever is filling in the form."""


def parse_required_by(text: Optional[str], what: str = "Required by") -> Optional[date]:
    """A date typed as 2026-10-30 (what an HTML date input sends). Blank means none."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise RequiredByError(f"Enter the {what} date as YYYY-MM-DD")


def parse_due(text: Optional[str]) -> Optional[date]:
    return parse_required_by(text, "Due")


def offers_required_by(session: Session, ticket: Ticket) -> bool:
    """Whether the ticket's request type offers the field; an ordinary ticket never does."""
    request_type = _request_type(session, ticket)
    return request_type is not None and request_type.offers_required_by


def _request_type(session: Session, ticket: Ticket) -> Optional[RequestType]:
    return session.get(RequestType, ticket.request_type_id) if ticket.request_type_id is not None else None


def due_days_before(session: Session, ticket: Ticket) -> int:
    """How many days before Required by the ticket is due: its request type's own number, else the setting."""
    request_type = _request_type(session, ticket)
    if request_type is not None and request_type.due_days_before is not None:
        return request_type.due_days_before
    return count_setting(session, DUE_DAYS_BEFORE_REQUIRED_BY)


def requester_today(session: Session, ticket: Ticket, now: Optional[datetime] = None) -> date:
    """Today's date where the requester is: what "not in the past" means for a Due date."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(zone_for_email(session, ticket.requester_email))).date()


def default_due(session: Session, ticket: Ticket, required_by: date, today: date, days: Optional[int] = None) -> date:
    """Required by minus the days for this ticket (`days`, else the ticket's own: its request type's or the setting),
    but not before today (a request made for tomorrow with a two day lead is due today, not already late) and
    never after Required by."""
    days = due_days_before(session, ticket) if days is None else days
    return min(max(required_by - timedelta(days=days), today), required_by)


def sub_request_due(session: Session, parent: Ticket, days_before: Optional[int], today: Optional[date] = None) -> Optional[date]:
    """The Due date of an automatic sub-request (#118): the parent's Required by minus the template's days (blank:
    the Due date setting), never before today where the requester is and never after Required by. None when the
    parent has no Required by."""
    if parent.required_by is None:
        return None
    days = days_before if days_before is not None else count_setting(session, DUE_DAYS_BEFORE_REQUIRED_BY)
    return default_due(session, parent, parent.required_by, today or requester_today(session, parent), days)


def set_required_by(
    session: Session,
    ticket: Ticket,
    value: Optional[date],
    actor_email: str,
    *,
    backdating: bool = False,
    today: Optional[date] = None,
) -> bool:
    """Sets, changes or clears the date, brings Due in line and works the deadline out again. Returns whether
    anything changed. The caller has already checked who may (`may_set_required_by`);
    `backdating` is true when an agent is backdating the ticket, which lets a past date in. A
    date already on the ticket is not checked again, so an old one is not trapped, and clearing
    is always allowed."""
    if value == ticket.required_by:
        return False
    given_today = today
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
    due_before = ticket.due_on
    floor = given_today or requester_today(session, ticket)  # a default Due is not before today where the requester is
    ticket.required_by = value
    _follow_required_by(session, ticket, floor)
    recalculate_deadlines(session, ticket)
    session.add(ticket)
    shown = (before.isoformat() if before else None, value.isoformat() if value else None)
    record_event(session, ticket, EVENT_REQUIRED_BY_CHANGED, actor_email, shown[0], shown[1])
    log_action(session, actor_email, "ticket", ticket.id, "required_by_changed", f"{shown[0] or 'none'} -> {shown[1] or 'none'}")
    if ticket.due_on != due_before:
        _record_due(session, ticket, actor_email, due_before, "follows the Required by date")
    return True


def _follow_required_by(session: Session, ticket: Ticket, today: date) -> None:
    """Brings Due in line with a changed Required by: recalculated from the default offset unless an agent set it
    by hand or the ticket is Scheduled, and then only clamped so it is never after Required by."""
    if ticket.required_by is None:
        if not ticket.due_set_by_hand and ticket.status != TicketStatus.SCHEDULED:
            ticket.due_on = None
    elif (ticket.due_set_by_hand or ticket.status == TicketStatus.SCHEDULED) and ticket.due_on is not None:
        # An agent's day, or a day work is planned for (Scheduled), is not moved by the requester: only clamped.
        ticket.due_on = min(ticket.due_on, ticket.required_by)
    else:
        ticket.due_on = default_due(session, ticket, ticket.required_by, today)


def _record_due(session: Session, ticket: Ticket, actor_email: str, before: Optional[date], reason: Optional[str]) -> None:
    shown = (before.isoformat() if before else None, ticket.due_on.isoformat() if ticket.due_on else None)
    record_event(session, ticket, EVENT_DUE_CHANGED, actor_email, shown[0], shown[1], reason or None)
    log_action(
        session, actor_email, "ticket", ticket.id, "due_changed",
        f"{shown[0] or 'none'} -> {shown[1] or 'none'}" + (f": {reason}" if reason else ""),
    )


def set_due(
    session: Session,
    ticket: Ticket,
    value: Optional[date],
    reason: Optional[str],
    actor_email: str,
    *,
    today: Optional[date] = None,
    reopening: bool = False,
) -> bool:
    """An agent sets or moves the Due date. Returns whether anything changed. The caller has already checked who
    may (`may_set_due`). Moving an existing Due date needs a reason (setting the first one does not); Due is never
    after Required by, never in the past (in the requester's time zone) unless the ticket is backdated, and never
    more than ten years ahead. Blank puts it back to the default from Required by (or clears it when there is none,
    which a Scheduled ticket cannot do). A date set here stays when Required by changes."""
    reason = (reason or "").strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise RequiredByError(f"Keep the reason under {MAX_REASON_LENGTH} characters")
    if ticket.status in FINISHED_STATUSES and not reopening:
        raise RequiredByError("The Due date of a resolved or closed request cannot be changed")
    today = today or requester_today(session, ticket)
    if value is None:
        if ticket.required_by is None:
            if ticket.status == TicketStatus.SCHEDULED:
                raise RequiredByError("A Scheduled ticket needs a Due date")
            new, by_hand = None, False
        else:
            new, by_hand = default_due(session, ticket, ticket.required_by, today), False
    else:
        if ticket.required_by is not None and value > ticket.required_by:
            raise RequiredByError("The Due date cannot be after the Required by date")
        if value < today and not ticket.is_backdated and value != ticket.due_on:
            raise RequiredByError("The Due date cannot be in the past")
        if value > today + timedelta(days=MAX_DAYS_AHEAD):
            raise RequiredByError("That Due date is too far ahead")
        new, by_hand = value, True
    if new == ticket.due_on and by_hand == ticket.due_set_by_hand:
        return False
    if new != ticket.due_on and ticket.due_on is not None and not reason:
        raise RequiredByError("Say why the Due date is being moved")
    before = ticket.due_on
    ticket.due_on, ticket.due_set_by_hand = new, by_hand
    recalculate_deadlines(session, ticket)
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    if new != before:
        _record_due(session, ticket, actor_email, before, reason)
    return True


def prepare_scheduled(
    session: Session, ticket: Ticket, due_text: Optional[str], reason: Optional[str], actor_email: str
) -> None:
    """Called before ANY way of setting Scheduled (the status buttons, the Email user and Add note status
    dropdown, reopening to it): the status needs a date, which is the Due date. The date typed in the form is
    used (and moves Due, with the reason, when it differs); with none typed the ticket's own is used; with
    neither the status is refused. A date already past is refused, as the ticket would only come straight back."""
    typed = parse_due(due_text)
    value = typed or ticket.due_on
    if value is None:
        raise RequiredByError("Scheduled needs a date: enter the Due date")
    if typed is not None and typed != ticket.due_on:
        # A resolved ticket being reopened to Scheduled is about to be open again.
        set_due(session, ticket, typed, reason, actor_email, reopening=ticket.status in FINISHED_STATUSES)
    elif value < requester_today(session, ticket):
        raise RequiredByError("The Due date is already past: choose the day the work is planned for")
    if morning_of(value, zone_for_email(session, ticket.requester_email), scheduled_morning_hour(session)) <= datetime.now(timezone.utc):
        # The morning job would take it straight back to Open and tell the assignee it is due today.
        raise RequiredByError("The morning of that day has already started: choose a later day for Scheduled")


def apply_form_dates(
    session: Session,
    ticket: Ticket,
    actor_email: str,
    required_by_text: Optional[str],
    due_text: Optional[str],
    reason: Optional[str],
) -> None:
    """The Required by and Due boxes on the triage form. None means the box was not on the form (the request
    type does not offer Required by) and leaves that date alone. Required by goes first, as Due follows it; a
    blank Due box leaves Due as it is (the Due form puts it back to the default), and so does one still holding
    the Due the form was drawn with (a changed Required by moves a default Due by itself). Raises RequiredByError."""
    due_when_drawn = ticket.due_on
    if required_by_text is not None:
        value = parse_required_by(required_by_text)
        if value != ticket.required_by:
            set_required_by(session, ticket, value, actor_email, backdating=ticket.is_backdated)
    typed = parse_due(due_text)
    if typed is not None and typed != ticket.due_on and typed != due_when_drawn:
        set_due(session, ticket, typed, reason, actor_email)


def refresh_default_due(session: Session, ticket: Ticket, actor_email: str) -> None:
    """The kind of request changed (at triage): a Due date that was only the default from Required by is worked
    out again with the new kind's days. One an agent set by hand, or no Required by, is left alone."""
    if ticket.required_by is None or ticket.due_set_by_hand or ticket.status in FINISHED_STATUSES:
        return
    before = ticket.due_on
    ticket.due_on = default_due(session, ticket, ticket.required_by, requester_today(session, ticket))
    if ticket.due_on != before:
        recalculate_deadlines(session, ticket)
        session.add(ticket)
        _record_due(session, ticket, actor_email, before, "follows the kind of request")

"""Priority and the SLA clock. Nothing here commits.

Impact (how many people) x urgency (how bad for them) gives a priority, P1 to P4.
The priority picks two targets, first response and resolution, which are admin
settings (app/settings_store.py), never constants. The clock is 24/7 and stops while
the ticket is Awaiting user or Scheduled (a Required by date keeps running).

Deadlines are stored on the ticket: created_at + target + time spent paused. They
move only when the priority changes or a pause ends, so the queue can sort on them
in SQL. While a ticket is paused its stored deadlines are not final, so it is shown
as paused and sorted after tickets whose clock is running.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import and_, case, null, or_
from sqlmodel import Session

from app.models import FINISHED_STATUSES, Ticket, TicketStatus
from app.settings_store import SLA_AT_RISK_PERCENT, get_int_setting, sla_key
from app.ticket_service import EVENT_PRIORITY_CHANGED, record_event


class Level(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Priority(str, Enum):
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"
    P4 = "p4"


IMPACT_LABELS = {
    Level.LOW: "One person",
    Level.MEDIUM: "Several people (a team)",
    Level.HIGH: "Many people (a department or more)",
}
URGENCY_LABELS = {
    Level.LOW: "Can wait, or there is a workaround",
    Level.MEDIUM: "Work is slowed",
    Level.HIGH: "Cannot work at all, or a deadline is at stake",
}

_SCORE = {Level.LOW: 1, Level.MEDIUM: 2, Level.HIGH: 3}


def priority_for(impact: Level, urgency: Level) -> Priority:
    """High/high is P1; low/low is P4. The usual ITIL grid:
    HH P1; HM, MH P2; HL, MM, LH P3; ML, LM, LL P4."""
    score = _SCORE[impact] + _SCORE[urgency]
    if score >= 6:
        return Priority.P1
    if score == 5:
        return Priority.P2
    if score == 4:
        return Priority.P3
    return Priority.P4


def target_minutes(session: Session, priority: Priority, which: str) -> Optional[int]:
    """The target for a priority, or None when the setting is blank (not tracked)."""
    return get_int_setting(session, sla_key(priority.value, which))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def required_by_deadline(day: date) -> datetime:
    """A Required by date means any time on that day (UTC), so the deadline is the next midnight."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)


def recalculate_deadlines(session: Session, ticket: Ticket) -> None:
    """Sets both deadlines from created_at, the current targets and the time already
    spent paused. A ticket with no priority has none, and neither has a backdated one.
    A Required by date replaces the resolution target (app/required_by.py): it is fixed
    by the date, so time spent paused does not move it, and it needs no priority."""
    priority = Priority(ticket.priority) if ticket.priority else None
    if ticket.is_backdated or (priority is None and ticket.required_by is None):
        ticket.first_response_due_at = ticket.resolution_due_at = None
        return
    base = _aware(ticket.created_at) + timedelta(seconds=ticket.sla_paused_seconds)
    first = target_minutes(session, priority, "first_response") if priority else None
    resolution = target_minutes(session, priority, "resolution") if priority else None
    ticket.first_response_due_at = base + timedelta(minutes=first) if first else None
    if ticket.required_by is not None:
        ticket.resolution_due_at = required_by_deadline(ticket.required_by)
    else:
        ticket.resolution_due_at = base + timedelta(minutes=resolution) if resolution else None


def apply_impact_urgency(session: Session, ticket: Ticket, impact: Level, urgency: Level) -> bool:
    """Sets impact, urgency and the priority they give, and works the deadlines out again.
    The one place that derivation lives; returns whether anything changed. Records no
    event: triage carries the first priority on its own event."""
    priority = priority_for(impact, urgency)
    if (ticket.impact, ticket.urgency, ticket.priority) == (impact.value, urgency.value, priority.value):
        return False
    ticket.impact, ticket.urgency, ticket.priority = impact.value, urgency.value, priority.value
    recalculate_deadlines(session, ticket)
    return True


def set_impact_urgency(
    session: Session, ticket: Ticket, impact: Level, urgency: Level, actor_email: Optional[str]
) -> bool:
    """Records impact and urgency, derives the priority and restarts the deadlines
    from the targets. Returns whether anything changed; an event records the old and
    new priority when it did."""
    previous = describe_priority(ticket)
    if not apply_impact_urgency(session, ticket, impact, urgency):
        return False
    session.add(ticket)
    record_event(session, ticket, EVENT_PRIORITY_CHANGED, actor_email, previous, describe_priority(ticket))
    return True


def describe_priority(ticket: Ticket) -> Optional[str]:
    if not ticket.priority:
        return None
    return f"{ticket.priority.upper()} ({ticket.impact} impact, {ticket.urgency} urgency)"


# Statuses in which the SLA clock is paused (unless the ticket has a Required by date).
PAUSING_STATUSES = (TicketStatus.AWAITING_USER, TicketStatus.SCHEDULED)


def on_status_change(
    session: Session, ticket: Ticket, previous: TicketStatus, new: TicketStatus, now: datetime
) -> None:
    """Awaiting user and Scheduled stop the clock; leaving them starts the clock again and pushes
    the deadlines out by the time spent waiting. Moving between the two stays paused. A ticket with
    a Required by date shows as running in either (sla_view, queue_order) and its date never moves."""
    if new in PAUSING_STATUSES and ticket.sla_paused_at is None:
        ticket.sla_paused_at = now
    elif previous in PAUSING_STATUSES and new not in PAUSING_STATUSES and ticket.sla_paused_at is not None:
        waited = max(0, int((now - _aware(ticket.sla_paused_at)).total_seconds()))
        ticket.sla_paused_seconds += waited
        ticket.sla_paused_at = None
        recalculate_deadlines(session, ticket)


def note_first_response(ticket: Ticket, now: datetime) -> None:
    """The first email an agent sends the requester meets the first-response target."""
    if ticket.first_responded_at is None:
        ticket.first_responded_at = now


# ---- What to show -----------------------------------------------------------------


@dataclass
class SlaView:
    state: str  # none | paused | met | breached | ok | at_risk
    text: str
    due: Optional[datetime] = None
    which: Optional[str] = None  # first response | resolution | required by


def _span(delta: timedelta) -> str:
    minutes = int(abs(delta).total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def sla_view(ticket: Ticket, now: datetime, at_risk_percent: int) -> SlaView:
    """The SLA position of a ticket, for display. The next deadline is the first
    response until an agent has emailed, then the resolution."""
    if (not ticket.priority and ticket.required_by is None) or ticket.is_backdated:
        return SlaView("none", "")
    if ticket.status in FINISHED_STATUSES:
        due = ticket.resolution_due_at
        if due is None or ticket.resolved_at is None:
            return SlaView("none", "")
        late = _aware(ticket.resolved_at) > due
        text = "resolved late" if late else "resolved in time"
        if ticket.required_by is not None:
            text += f" (required by {ticket.required_by.isoformat()})"
        return SlaView("breached" if late else "met", text, due, "required by" if ticket.required_by else None)
    if ticket.sla_paused_at is not None and ticket.required_by is None:  # a stated date keeps running
        return SlaView("paused", "paused (scheduled)" if ticket.status == TicketStatus.SCHEDULED else "paused (awaiting user)")
    if ticket.first_responded_at is None and ticket.first_response_due_at is not None:
        due, which = ticket.first_response_due_at, "first response"
    elif ticket.resolution_due_at is not None:
        due, which = ticket.resolution_due_at, "required by" if ticket.required_by else "resolution"
    else:
        return SlaView("none", "")
    # "required by 2026-10-30" names the date the requester gave; the others name the target.
    label = f"required by {ticket.required_by.isoformat()}," if which == "required by" else which
    remaining = _aware(due) - now
    if remaining.total_seconds() < 0:
        return SlaView("breached", f"{label.rstrip(',')} overdue {_span(remaining)}", due, which)
    window = _aware(due) - _aware(ticket.created_at)
    if which != "required by":
        window -= timedelta(seconds=ticket.sla_paused_seconds)
    if window.total_seconds() > 0 and remaining <= window * at_risk_percent / 100:
        return SlaView("at_risk", f"{label} due in {_span(remaining)}", due, which)
    return SlaView("ok", f"{label} due in {_span(remaining)}", due, which)


def at_risk_percent(session: Session) -> int:
    value = get_int_setting(session, SLA_AT_RISK_PERCENT)
    return value if value is not None and 0 < value < 100 else 25


def queue_order(required_by_lead_days: int):
    """The agents' queue: untriaged tickets pinned to the top (oldest first), then
    running clocks by time to breach, then paused clocks, then tickets without a
    target, then finished tickets; priority breaks ties, then the latest update.
    A ticket with a Required by date counts as due `required_by_lead_days` before the end
    of that day (the REQUIRED_BY_LEAD_DAYS setting), so work that takes time rises early."""
    resolution_due = case(
        (Ticket.required_by.is_not(None), Ticket.resolution_due_at - timedelta(days=required_by_lead_days)),
        else_=Ticket.resolution_due_at,
    )
    paused = and_(Ticket.sla_paused_at.is_not(None), Ticket.required_by.is_(None))  # a stated date keeps running
    next_due = case(
        (paused, null()),
        (
            and_(
                Ticket.first_responded_at.is_(None),
                or_(Ticket.first_response_due_at.is_not(None), Ticket.required_by.is_(None)),
            ),
            Ticket.first_response_due_at,
        ),
        else_=resolution_due,
    )
    finished = Ticket.status.in_(FINISHED_STATUSES)
    group = case(
        (Ticket.triaged_at.is_(None), 0),
        (finished, 4),
        (paused, 2),
        (next_due.is_(None), 3),
        else_=1,
    )
    return [
        group,
        case((Ticket.triaged_at.is_(None), Ticket.created_at)),
        next_due.asc().nulls_last(),
        Ticket.priority.asc().nulls_last(),
        Ticket.updated_at.desc(),
        Ticket.id.desc(),  # a total order, so pages never repeat or skip a ticket
    ]

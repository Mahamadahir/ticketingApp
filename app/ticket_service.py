"""Changes to tickets that more than one route makes. Nothing here commits:
the caller commits, so the change, its event and its audit entry land in one
transaction."""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.settings_store import DEFAULT_TRIAGE_TEAM, get_int_setting
from app.models import (
    FINISHED_STATUSES,
    TICKET_TYPE_PREFIXES,
    Ticket,
    TicketEvent,
    TicketSource,
    TicketStatus,
    TicketType,
)

EVENT_CREATED = "created"
EVENT_STATUS_CHANGED = "status_changed"
EVENT_ASSIGNED = "assigned"
EVENT_ESCALATED = "escalated"
EVENT_TRIAGED = "triaged"
EVENT_TYPE_CHANGED = "type_changed"
EVENT_TEAM_CHANGED = "team_changed"
EVENT_CATEGORY_REVIEWED = "category_reviewed"
EVENT_PRIORITY_CHANGED = "priority_changed"


class NotTriaged(Exception):
    """Raised when something that needs a triaged ticket is attempted on one that
    has not been triaged yet (emailing the requester, moving it between statuses)."""


# ASCII digits only (\d would also accept digits from other scripts), and only the real prefixes.
_PREFIXES = "|".join(sorted(TICKET_TYPE_PREFIXES.values()))
_NUMBER_FORM = re.compile(rf"^#?(?:(?:{_PREFIXES})-?)?([0-9]{{1,9}})$", re.IGNORECASE)


def parse_ticket_number(text: str) -> Optional[int]:
    """The ticket number in whatever form someone typed or pasted: 1042, #1042, INC-1042,
    sr-1042. The prefix is ignored because the number alone identifies a ticket, so
    an old link or email subject still finds it after its type changed."""
    match = _NUMBER_FORM.match((text or "").strip())
    return int(match.group(1)) if match else None


def find_ticket(session: Session, text: str) -> Optional[Ticket]:
    number = parse_ticket_number(text)
    if number is None:
        return None
    return session.exec(select(Ticket).where(Ticket.number == number)).first()


def record_event(
    session: Session,
    ticket: Ticket,
    event_type: str,
    actor_email: Optional[str],
    from_value: Optional[str] = None,
    to_value: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=event_type,
            actor_email=actor_email,
            from_value=from_value,
            to_value=to_value,
            detail=detail,
        )
    )


def create_ticket(
    session: Session,
    *,
    subject: str,
    requester_email: str,
    source: TicketSource,
    ticket_type: TicketType = TicketType.INCIDENT,
    team_id: Optional[int] = None,
    actor_email: Optional[str] = None,
) -> Ticket:
    """Creates a ticket in the New (untriaged) status and records its first
    event. The number is assigned on insert, so the ticket is flushed here.
    With no team given it arrives in the default triage team, if one is set."""
    if team_id is None:
        team_id = get_int_setting(session, DEFAULT_TRIAGE_TEAM)
    ticket = Ticket(
        subject=subject,
        requester_email=requester_email,
        source=source,
        type=ticket_type,
        team_id=team_id,
    )
    session.add(ticket)
    session.flush()
    record_event(session, ticket, EVENT_CREATED, actor_email, to_value=ticket.tag, detail=source.value)
    return ticket


def set_status(session: Session, ticket: Ticket, new_status: TicketStatus, actor_email: Optional[str]) -> bool:
    """Moves the ticket to a new status. Returns False, changing nothing,
    when it is already there."""
    previous = ticket.status
    if previous == new_status:
        return False
    if ticket.triaged_at is None and new_status != TicketStatus.NEW:
        # Leaving New is what triage does, and only triage.
        raise NotTriaged()

    now = datetime.now(timezone.utc)
    ticket.status = new_status
    ticket.updated_at = now
    if new_status in FINISHED_STATUSES:
        # Closing a resolved ticket keeps the original resolution time.
        if ticket.resolved_at is None:
            ticket.resolved_at = now
    else:
        ticket.resolved_at = None
    # Awaiting user stops the SLA clock and leaving it restarts it. Imported here
    # because app.sla builds on this module.
    from app.sla import on_status_change

    on_status_change(session, ticket, previous, new_status, now)
    session.add(ticket)
    record_event(session, ticket, EVENT_STATUS_CHANGED, actor_email, previous.value, new_status.value)
    return True

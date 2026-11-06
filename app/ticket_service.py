"""Changes to tickets that more than one route makes. Nothing here commits:
the caller commits, so the change, its event and its audit entry land in one
transaction."""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.settings_store import DEFAULT_TRIAGE_TEAM, get_int_setting
from app.teams import queue_new_ticket
from app.time_zones import note_requester
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
EVENT_APPROVAL_REQUESTED = "approval_requested"  # to_value: who was asked, detail: how many must approve
EVENT_APPROVAL_DECIDED = "approval_decided"  # actor: the approver, to_value: approved or declined
EVENT_APPROVAL_OUTCOME = "approval_outcome"  # to_value: approved, declined or waived
EVENT_APPROVAL_WAITING = "approval_waiting"  # nobody active could be asked, so it waits for approvers (#161; detail: the group)
EVENT_APPROVAL_VOTE_REMOVED = "approval_vote_removed"  # an inactive voter's unanswered vote was dropped (#161; to_value: their email)
EVENT_APPROVAL_WITHDRAWN = "approval_withdrawn"  # the requester withdrew the request while it waited (detail: how many had not answered)
EVENT_REQUIRED_BY_CHANGED = "required_by_changed"  # a service request's Required by date was set, changed or cleared
EVENT_DUE_CHANGED = "due_changed"  # the Due date was set, moved or cleared (detail: the reason)
EVENT_ANSWER_CHANGED = "answer_changed"  # a form answer was given or changed (detail: the question)
EVENT_SUBREQUEST = "subrequest_created"  # on the request: to_value is the sub-ticket made from a template (#118)
EVENT_SUBREQUEST_SKIPPED = "subrequest_skipped"  # on the request: to_value is the subject, detail why it was not made
EVENT_SUBREQUESTS_PLANNED = "subrequests_planned"  # on the request: the list confirmed at triage, made once approved
EVENT_REQUEST_TYPE = "request_type"  # a service request raised from the catalogue: to_value is its name


class AwaitingApproval(Exception):
    """Raised when something tries to move a ticket that is waiting for its approvers. Only the
    approval itself (app/approvals.py) moves it on."""


class ResolutionRequired(Exception):
    """Raised when something sets Resolved by hand. A ticket is resolved only through
    app/resolution.py, which takes the resolution notes."""


class ClosingNeedsNotes(Exception):
    """Raised when a ticket that is not Resolved is closed directly without resolution notes (#122).
    The agent's status buttons take the notes (`close_with_notes`); a decline or withdrawal, a reclose
    of a reopened ticket and a split original following its parts are exempt, and a Resolved ticket
    already has its notes."""


class NotTriaged(Exception):
    """Raised when something that needs a triaged ticket is attempted on one that
    has not been triaged yet (emailing the requester, moving it between statuses)."""


class ScheduledNeedsDate(Exception):
    """Raised when a ticket without a Due date is set to Scheduled: that status is a plan for a day."""


# ASCII digits only (\d would also accept digits from other scripts), no leading zero (one spelling
# per ticket), and only the real prefixes.
_PREFIXES = "|".join(sorted(TICKET_TYPE_PREFIXES.values()))
_NUMBER_FORM = re.compile(rf"^#?(?:(?:{_PREFIXES})-?)?([1-9][0-9]{{0,8}})$", re.IGNORECASE)


def parse_ticket_number(text: str) -> Optional[int]:
    """The ticket number in whatever form someone typed or pasted: 1042, #1042, INC-1042,
    sr-1042. The prefix is ignored because the number alone identifies a ticket, so
    an old link or email subject still finds it after its type changed."""
    match = _NUMBER_FORM.match((text or "").strip())
    return int(match.group(1)) if match else None


def get_by_number(session: Session, number: int) -> Optional[Ticket]:
    """The ticket with this number, or None. Route paths carry the number, never the internal id."""
    return session.exec(select(Ticket).where(Ticket.number == number)).first()


def find_ticket(session: Session, text: str) -> Optional[Ticket]:
    number = parse_ticket_number(text)
    return get_by_number(session, number) if number is not None else None


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
    created_by_id: Optional[int] = None,
    request_type_id: Optional[int] = None,
    announce: bool = True,
) -> Ticket:
    """Creates a ticket in the New (untriaged) status and records its first
    event. The number is assigned on insert, so the ticket is flushed here.
    With no team given it arrives in the default triage team, if one is set.
    A new ticket is announced in the Teams channel (app/teams.py) unless announce is False,
    which splitting uses: a part of a ticket already announced is not new."""
    if team_id is None:
        team_id = get_int_setting(session, DEFAULT_TRIAGE_TEAM)
    ticket = Ticket(
        subject=subject,
        requester_email=requester_email.strip().lower(),
        source=source,
        type=ticket_type,
        team_id=team_id,
        created_by_id=created_by_id,
        request_type_id=request_type_id,
    )
    session.add(ticket)
    session.flush()
    note_requester(session, ticket.requester_email)  # the worker reads their mailbox time zone (app/time_zones.py)
    record_event(session, ticket, EVENT_CREATED, actor_email, to_value=ticket.tag, detail=source.value)
    if announce:
        queue_new_ticket(session, ticket)
    return ticket


def set_status(
    session: Session,
    ticket: Ticket,
    new_status: TicketStatus,
    actor_email: Optional[str],
    detail: Optional[str] = None,
    by_approval: bool = False,
    by_resolution: bool = False,
    from_parts: bool = False,
    by_reclose: bool = False,
) -> bool:
    """Moves the ticket to a new status. Returns False, changing nothing,
    when it is already there. A sub-ticket's change re-derives its original's status.
    Resolved is refused unless by_resolution is set, which only app/resolution.py does. Scheduled is refused
    on a ticket with no Due date (callers ask for it first: app/required_by.py, `prepare_scheduled`), except
    for a split original whose status follows its parts (`from_parts`). Closed from any status but Resolved needs
    resolution notes on the ticket first (ClosingNeedsNotes), except by approval, `by_reclose` or `from_parts`."""
    previous = ticket.status
    if previous == new_status:
        return False
    if previous == TicketStatus.AWAITING_APPROVAL and not by_approval:
        raise AwaitingApproval()
    if new_status == TicketStatus.RESOLVED and not by_resolution:
        raise ResolutionRequired()
    if ticket.triaged_at is None and new_status != TicketStatus.NEW:
        # Leaving New is what triage does, and only triage.
        raise NotTriaged()
    if (
        new_status == TicketStatus.CLOSED and previous != TicketStatus.RESOLVED
        and not (by_approval or by_reclose or from_parts) and not (ticket.resolution_notes or "").strip()
    ):
        raise ClosingNeedsNotes()
    if new_status == TicketStatus.SCHEDULED and ticket.due_on is None and not from_parts:
        raise ScheduledNeedsDate()

    if ticket.split_from_id is not None:
        # Lock the original before changing the part, the same order as everything else
        # in the family, so a requester's reply and an agent's change cannot deadlock.
        from app.splitting import lock_ticket

        lock_ticket(session, ticket.split_from_id)
    now = datetime.now(timezone.utc)
    ticket.status = new_status
    ticket.updated_at = now
    if new_status in FINISHED_STATUSES:
        ticket.msp_hold = False  # a hold is about the automatic MSP handoff; a finished ticket has no use for it (app/msp.py)
        # Closing a resolved ticket keeps the original resolution time.
        if ticket.resolved_at is None:
            ticket.resolved_at = now
    else:
        ticket.resolved_at = None
        # A reopened ticket has no resolution until it is resolved again (the timeline, the audit
        # entry and the resolution email keep what was said).
        ticket.resolution_notes = None
        ticket.resolved_by_id = None
    # closed_at is when it was closed; moving off Closed (a reopen) clears it.
    if new_status == TicketStatus.CLOSED:
        ticket.closed_at = now
    elif previous == TicketStatus.CLOSED:
        ticket.closed_at = None
    if previous == TicketStatus.AWAITING_USER:
        # Any way out of Awaiting user (a reply, the reopen link, an agent) cancels a chase (app/auto_close_chase.py).
        ticket.chased_at = None
        ticket.chase_count = 0
    # Awaiting user stops the SLA clock and leaving it restarts it. Imported here
    # because app.sla builds on this module.
    from app.sla import on_status_change

    on_status_change(session, ticket, previous, new_status, now)
    session.add(ticket)
    record_event(session, ticket, EVENT_STATUS_CHANGED, actor_email, previous.value, new_status.value, detail)
    if not by_approval and not from_parts:
        # The requester's bell (#130). An approval's decline or withdrawal has its own outcome entry, and a split original's derived status is silent (each part told them). Imported
        # here because app.notifications builds on this module.
        from app.notifications import notify_status_for_requesters

        notify_status_for_requesters(session, ticket, new_status, actor_email)
    if (
        previous == TicketStatus.CLOSED and new_status not in FINISHED_STATUSES
        and new_status != TicketStatus.AWAITING_APPROVAL and not by_approval and not from_parts
    ):
        # A ticket closed by a decline or a withdrawal, set back to an open status, asks its approvers again.
        # Imported here because app.approvals builds on this module.
        from app.approvals import restart_after_reopen

        restart_after_reopen(session, ticket, actor_email)
    if ticket.split_from_id is not None:
        # The original's status follows its sub-tickets. Imported here because
        # app.splitting builds on this module.
        from app.splitting import sync_original_status

        sync_original_status(session, ticket.split_from_id)
    return True

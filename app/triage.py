"""Triage and re-assignment. Neither commits: the caller does, so the change, its
events and its audit entry land together.

Triage classifies a ticket (type, category, and so team) and is a separate act
from assignment: a triager may assign the ticket in the same step, or leave it
for the team. Re-assignment moves a triaged ticket to a team and an agent in it,
or leaves it unassigned in that team; it is not an escalation."""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.approvals import start_approvals, status_after_triage
from app.categories import is_selectable, path_of, team_id_for
from app.notifications import notify_assigned
from app.models import (
    Category,
    RequestType,
    Team,
    Ticket,
    TicketStatus,
    TicketType,
    User,
)
from app.sla import Level, apply_impact_urgency, describe_priority
from app.ticket_service import (
    EVENT_ASSIGNED,
    EVENT_ESCALATED,
    EVENT_REQUEST_TYPE,
    EVENT_TEAM_CHANGED,
    EVENT_TRIAGED,
    EVENT_TYPE_CHANGED,
    record_event,
    set_status,
)
from app.visibility import team_members


class TriageError(ValueError):
    """A reason triage or re-assignment cannot go ahead, worded for the user."""


def change_type(session: Session, ticket: Ticket, new_type: TicketType, actor_email: str) -> None:
    """Changes the ticket's type. The number stays, so links and email subjects keep
    working; only the display prefix follows the type (INC-12 shows as SR-12)."""
    if new_type == ticket.type:
        return
    old_type = ticket.type
    ticket.type = new_type
    session.add(ticket)
    record_event(session, ticket, EVENT_TYPE_CHANGED, actor_email, old_type.value, new_type.value)
    if new_type != TicketType.SERVICE_REQUEST and ticket.request_type_id is not None:
        # No longer a service request, so no longer one of the catalogue's kinds (and its
        # approvers have nothing to approve).
        request_type = session.get(RequestType, ticket.request_type_id)
        ticket.request_type_id = None
        record_event(
            session, ticket, EVENT_REQUEST_TYPE, actor_email,
            from_value=request_type.name if request_type else None, detail="retyped",
        )


def _assignee_for(session: Session, team_id: int, assignee: Optional[User]) -> Optional[User]:
    if assignee is not None and assignee.id not in {member.id for member in team_members(session, team_id)}:
        raise TriageError("The person you chose is not a member of that team")
    return assignee


def _set_assignee(session: Session, ticket: Ticket, assignee: Optional[User], actor: User) -> None:
    """Changes who the ticket is assigned to, recording it. None unassigns."""
    previous = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    if (previous.id if previous else None) == (assignee.id if assignee else None):
        return
    ticket.assigned_agent_id = assignee.id if assignee else None
    session.add(ticket)
    record_event(
        session, ticket, EVENT_ASSIGNED, actor.email,
        from_value=previous.email if previous else None,
        to_value=assignee.email if assignee else None,
    )
    if assignee is not None:
        notify_assigned(session, ticket, assignee, actor.email)


def triage_ticket(
    session: Session,
    ticket: Ticket,
    actor: User,
    *,
    ticket_type: TicketType,
    category: Category,
    impact: Level,
    urgency: Level,
    assignee: Optional[User] = None,
) -> None:
    """Classifies an untriaged ticket and moves it from New to Open. The category
    decides the team; the type may change (the number does not); impact and urgency give the priority and start the SLA deadlines.
    Assigning is optional and independent."""
    if ticket.triaged_at is not None:
        raise TriageError("This ticket has already been triaged")
    if not is_selectable(session, category):
        raise TriageError("That category is no longer in use")
    team_id = team_id_for(session, category)
    if team_id is None:
        raise TriageError(
            "That category has no team yet. Ask a super admin to give it one "
            "(for 'Other', set a default triage team under Settings)."
        )
    assignee = _assignee_for(session, team_id, assignee)

    now = datetime.now(timezone.utc)
    change_type(session, ticket, ticket_type, actor.email)

    previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
    new_team = session.get(Team, team_id)
    ticket.category_id = category.id
    ticket.team_id = team_id
    ticket.triaged_at = now
    ticket.triaged_by_id = actor.id
    ticket.needs_category_review = category.is_other
    ticket.updated_at = now
    session.add(ticket)
    # No priority event: the triaged event below carries the first priority.
    apply_impact_urgency(session, ticket, impact, urgency)

    record_event(
        session, ticket, EVENT_TRIAGED, actor.email,
        to_value=path_of(session, category),
        detail=f"{ticket_type.value}, team {new_team.name}, {describe_priority(ticket)}",
    )
    if previous_team is None or previous_team.id != team_id:
        record_event(
            session, ticket, EVENT_TEAM_CHANGED, actor.email,
            from_value=previous_team.name if previous_team else None, to_value=new_team.name,
        )
    # Triaged is what lets it leave New. Set after triaged_at so set_status allows it.
    # A catalogue request with approvers waits for them (Awaiting approval) instead of opening.
    target = status_after_triage(session, ticket)
    set_status(session, ticket, target, actor.email)
    if target == TicketStatus.AWAITING_APPROVAL:
        start_approvals(session, ticket, actor.email)
    if assignee is not None:
        _set_assignee(session, ticket, assignee, actor)
    elif ticket.assigned_agent_id is not None:
        # Choosing no one leaves the assignment as it is (the ticket may have been handed
        # to someone to triage), unless that person is not in the team it now belongs to:
        # access follows the team, so they could no longer open it.
        if ticket.assigned_agent_id not in {member.id for member in team_members(session, team_id)}:
            _set_assignee(session, ticket, None, actor)


MOVE_REASSIGN = "reassign"
MOVE_ESCALATE = "escalate"


def move_ticket(
    session: Session,
    ticket: Ticket,
    actor: User,
    team: Team,
    assignee: Optional[User],
    *,
    kind: str,
    reason: Optional[str] = None,
) -> bool:
    """The one place a ticket changes team: sets the team and the assignee (None leaves it
    unassigned there), records the move and any assignment change, and touches the ticket.
    Returns whether anything changed. Re-assign and escalation both go through it, and differ only
    in who chose the team and what else they do (the extras of an escalation are in
    app/escalation.py). The caller checks the move is allowed and that the assignee is in the team.

    One event records the team change, by kind: team_changed for a re-assign, escalated
    (carrying the reason) for an escalation, so a move is never recorded twice."""
    if kind not in (MOVE_REASSIGN, MOVE_ESCALATE):
        raise ValueError(f"Unknown kind of move: {kind}")
    changed = False
    if ticket.team_id != team.id:
        previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
        ticket.team_id = team.id
        session.add(ticket)
        record_event(
            session, ticket, EVENT_ESCALATED if kind == MOVE_ESCALATE else EVENT_TEAM_CHANGED, actor.email,
            from_value=previous_team.name if previous_team else None, to_value=team.name,
            detail=reason if kind == MOVE_ESCALATE else None,
        )
        changed = True

    before = ticket.assigned_agent_id
    _set_assignee(session, ticket, assignee, actor)
    changed = changed or ticket.assigned_agent_id != before
    if changed:
        ticket.updated_at = datetime.now(timezone.utc)
        session.add(ticket)
    return changed


def reassign_ticket(
    session: Session, ticket: Ticket, actor: User, *, team: Team, assignee: Optional[User] = None
) -> bool:
    """Moves a triaged ticket to a team and an agent in it, or leaves it
    unassigned there. Returns whether anything changed. No reason is needed and
    it is not an escalation."""
    if ticket.triaged_at is None:
        raise TriageError("Triage the ticket first; its category decides the team")
    assignee = _assignee_for(session, team.id, assignee)
    return move_ticket(session, ticket, actor, team, assignee, kind=MOVE_REASSIGN)

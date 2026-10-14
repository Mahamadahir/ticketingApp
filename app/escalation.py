"""Escalation: sending a triaged ticket to the escalation target of its category.

The agent does not choose where it goes. A main category has a target (a team, or the
MSP when none is set, which is the default); a sub-category follows its parent's unless
it overrides. If the ticket is already with that team the next step is the MSP, so a
second-line ticket cannot escalate back to itself.

Re-assign (app/triage.py) is the tool for handing a ticket to the right team; it is not
an escalation. The MSP handoff is phase 5 and is not built, so a ticket whose target is
the MSP cannot be escalated yet: the escalation is refused with a message that says so.

The escalating agent keeps working access to that one ticket (TicketEscalator, read in
app/visibility.py) and becomes an internal follower. Nothing here commits.
"""

from datetime import datetime, timezone

from sqlmodel import Session

from app.audit import log_action
from app.categories import escalation_team_id_for
from app.followers import add_follower
from app.models import FINISHED_STATUSES, Category, Team, Ticket, TicketEscalator, User
from app.ticket_service import EVENT_ASSIGNED, EVENT_ESCALATED, record_event

MSP_NOT_BUILT = (
    "Escalating from here goes to the MSP (the category's escalation team is the MSP, or the ticket is "
    "already with it), and the MSP handoff is not available yet."
)


class EscalationError(ValueError):
    """Why a ticket cannot be escalated, in words for the agent."""


def escalation_target(session: Session, ticket: Ticket) -> Team:
    """The team this ticket escalates to, or EscalationError saying why there is none."""
    if ticket.triaged_at is None:
        raise EscalationError("Triage the ticket first; its category decides where it escalates")
    if ticket.status in FINISHED_STATUSES:
        raise EscalationError("A resolved or closed ticket cannot be escalated")
    category = session.get(Category, ticket.category_id) if ticket.category_id else None
    if category is None:
        raise EscalationError("This ticket has no category, so there is nowhere for it to escalate")
    team_id = escalation_team_id_for(session, category)
    if team_id is None or team_id == ticket.team_id:
        raise EscalationError(MSP_NOT_BUILT)
    team = session.get(Team, team_id)
    if team is None:  # cannot happen with the foreign key; keeps the answer honest if it does
        raise EscalationError(MSP_NOT_BUILT)
    return team


def escalate_ticket(session: Session, ticket: Ticket, actor: User, reason: str) -> Team:
    """Moves the ticket to its escalation team, unassigned, and records who and why.
    Returns the team. Raises EscalationError and changes nothing when it cannot."""
    new_team = escalation_target(session, ticket)
    previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
    previous_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None

    now = datetime.now(timezone.utc)
    ticket.is_escalated = True
    ticket.escalation_reason = reason
    ticket.escalated_by_id = actor.id
    ticket.escalated_at = now
    ticket.team_id = new_team.id
    ticket.assigned_agent_id = None
    ticket.updated_at = now
    session.add(ticket)
    record_event(
        session, ticket, EVENT_ESCALATED, actor.email,
        from_value=previous_team.name if previous_team else None, to_value=new_team.name, detail=reason,
    )
    if previous_agent is not None:
        # Moving to another team unassigns the agent; the history must say so.
        record_event(session, ticket, EVENT_ASSIGNED, actor.email, from_value=previous_agent.email, to_value=None)
    if session.get(TicketEscalator, (ticket.id, actor.id)) is None:
        session.add(TicketEscalator(ticket_id=ticket.id, user_id=actor.id))
    # Copied on its mail from now on. Their access to the ticket comes from TicketEscalator,
    # not from following it.
    add_follower(session, ticket, actor.email, actor.email, internal_user=actor)
    log_action(session, actor.email, "ticket", ticket.id, "escalated", reason)
    return new_team

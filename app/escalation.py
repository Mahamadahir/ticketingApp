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

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.categories import escalation_team_id_for
from app.followers import add_follower
from app.messages import add_note
from app.models import FINISHED_STATUSES, Category, Team, Ticket, TicketEscalator, User
from app.notifications import notify_mentions
from app.triage import MOVE_ESCALATE, move_ticket

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
    current = session.get(Team, ticket.team_id) if ticket.team_id else None
    already_there = team_id == ticket.team_id or (
        current is not None and current.parent_team_id is not None and current.parent_team_id == team_id
    )
    if team_id is None or already_there:
        # In the target, or in one of its sub-teams (moving up to the parent would be
        # a step back, not an escalation): the next step is the MSP.
        raise EscalationError(MSP_NOT_BUILT)
    team = session.get(Team, team_id)
    if team is None:  # cannot happen with the foreign key; keeps the answer honest if it does
        raise EscalationError(MSP_NOT_BUILT)
    return team


def escalators_of(session: Session, ticket: Ticket) -> list[User]:
    """Everyone who escalated this ticket (in no particular order: the table keeps no
    timestamp). Each keeps working access to it permanently, so the ticket page lists them all."""
    return list(
        session.exec(
            select(User)
            .join(TicketEscalator, TicketEscalator.user_id == User.id)
            .where(TicketEscalator.ticket_id == ticket.id)
            .order_by(User.id)
        ).all()
    )


def escalate_ticket(session: Session, ticket: Ticket, actor: User, reason: str) -> Team:
    """Moves the ticket to its escalation team, unassigned, and records who and why.
    Returns the team. Raises EscalationError and changes nothing when it cannot."""
    new_team = escalation_target(session, ticket)

    # The move itself (team, unassigning the agent, the event and its history) is the same code
    # as a re-assign; what makes it an escalation is the rest of this function.
    move_ticket(session, ticket, actor, new_team, None, kind=MOVE_ESCALATE, reason=reason)
    ticket.is_escalated = True
    ticket.escalation_reason = reason
    ticket.escalated_by_id = actor.id
    ticket.escalated_at = datetime.now(timezone.utc)
    session.add(ticket)
    # The reason is also an internal note, so the team receiving it reads it in the conversation
    # and an @mention in it reaches the person named (the note is written after the move, so
    # people in the new team can be mentioned).
    note = add_note(session, ticket, actor, f"Escalated to {new_team.name}. Reason: {reason}")
    notify_mentions(session, ticket, note, actor)
    try:
        # A savepoint, so a double click (two requests adding the same row) is a quiet no-op.
        with session.begin_nested():
            session.add(TicketEscalator(ticket_id=ticket.id, user_id=actor.id))
            session.flush()
    except IntegrityError:
        pass
    # Copied on its mail from now on. Their access to the ticket comes from TicketEscalator,
    # not from following it.
    add_follower(session, ticket, actor.email, actor.email, internal_user=actor)
    log_action(session, actor.email, "ticket", ticket.id, "escalated", reason)
    return new_team

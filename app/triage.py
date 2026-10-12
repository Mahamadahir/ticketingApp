"""Triage and re-assignment. Neither commits: the caller does, so the change, its
events and its audit entry land together.

Triage classifies a ticket (type, category, and so team) and is a separate act
from assignment: a triager may assign the ticket in the same step, or leave it
for the team. Re-assignment moves a triaged ticket to a team and an agent in it,
or leaves it unassigned in that team; it is not an escalation."""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.categories import is_selectable, path_of, team_id_for
from app.models import (
    Category,
    Team,
    Ticket,
    TicketAlias,
    TicketStatus,
    TicketType,
    User,
    allocate_reference,
)
from app.ticket_service import (
    EVENT_ASSIGNED,
    EVENT_REFERENCE_CHANGED,
    EVENT_TEAM_CHANGED,
    EVENT_TRIAGED,
    EVENT_TYPE_CHANGED,
    record_event,
    set_status,
)
from app.visibility import team_members


class TriageError(ValueError):
    """A reason triage or re-assignment cannot go ahead, worded for the user."""


def reissue_reference(session: Session, ticket: Ticket, new_type: TicketType, actor_email: str) -> None:
    """Changes the ticket's type and gives it a reference of the new type
    (INC-12 becomes SR-3). The old reference stays as an alias so existing links,
    and the subjects of emails already sent, still find the ticket."""
    if new_type == ticket.type:
        return
    old_type, old_reference = ticket.type, ticket.reference
    number, reference = allocate_reference(session.connection(), new_type)

    session.add(TicketAlias(reference=old_reference, ticket_id=ticket.id))
    ticket.type, ticket.number, ticket.reference = new_type, number, reference
    session.add(ticket)
    # Flush now so the alias and the new reference are written together, and a
    # clash surfaces here rather than at commit.
    session.flush()
    record_event(session, ticket, EVENT_TYPE_CHANGED, actor_email, old_type.value, new_type.value)
    record_event(session, ticket, EVENT_REFERENCE_CHANGED, actor_email, old_reference, reference)


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


def triage_ticket(
    session: Session,
    ticket: Ticket,
    actor: User,
    *,
    ticket_type: TicketType,
    category: Category,
    assignee: Optional[User] = None,
) -> None:
    """Classifies an untriaged ticket and moves it from New to Open. The category
    decides the team; the type may change the reference (the old one is kept as
    an alias). Assigning is optional and independent."""
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
    reissue_reference(session, ticket, ticket_type, actor.email)

    previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
    new_team = session.get(Team, team_id)
    ticket.category_id = category.id
    ticket.team_id = team_id
    ticket.triaged_at = now
    ticket.triaged_by_id = actor.id
    ticket.needs_category_review = category.is_other
    ticket.updated_at = now
    session.add(ticket)

    record_event(
        session, ticket, EVENT_TRIAGED, actor.email,
        to_value=path_of(session, category), detail=f"{ticket_type.value}, team {new_team.name}",
    )
    if previous_team is None or previous_team.id != team_id:
        record_event(
            session, ticket, EVENT_TEAM_CHANGED, actor.email,
            from_value=previous_team.name if previous_team else None, to_value=new_team.name,
        )
    # Triaged is what lets it leave New. Set after triaged_at so set_status allows it.
    set_status(session, ticket, TicketStatus.OPEN, actor.email)
    if assignee is not None:
        # Choosing no one means leave the assignment as it is, not clear it: the
        # ticket may already have been handed to someone to triage.
        _set_assignee(session, ticket, assignee, actor)


def reassign_ticket(
    session: Session, ticket: Ticket, actor: User, *, team: Team, assignee: Optional[User] = None
) -> bool:
    """Moves a triaged ticket to a team and an agent in it, or leaves it
    unassigned there. Returns whether anything changed. No reason is needed and
    it is not an escalation."""
    if ticket.triaged_at is None:
        raise TriageError("Triage the ticket first; its category decides the team")
    assignee = _assignee_for(session, team.id, assignee)

    changed = False
    if ticket.team_id != team.id:
        previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
        ticket.team_id = team.id
        session.add(ticket)
        record_event(
            session, ticket, EVENT_TEAM_CHANGED, actor.email,
            from_value=previous_team.name if previous_team else None, to_value=team.name,
        )
        changed = True

    before = ticket.assigned_agent_id
    _set_assignee(session, ticket, assignee, actor)
    changed = changed or ticket.assigned_agent_id != before
    if changed:
        ticket.updated_at = datetime.now(timezone.utc)
        session.add(ticket)
    return changed

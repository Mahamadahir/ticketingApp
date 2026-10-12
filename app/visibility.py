"""Who can see and work which tickets. Every route that reads or changes a
ticket goes through here, so the rules live in one place.

- Super admins see and work everything.
- An agent works tickets of teams they are a member of, and reads tickets of
  teams they are a viewer of.
- Every agent works untriaged tickets (status New, or no team yet), so someone
  can triage them.
- A ticket assigned to an agent is theirs to work, wherever it sits.
- An agent who escalated a ticket keeps read access after it moves teams. (This
  becomes the internal follower once followers exist, in 1C.)
"""

from enum import IntEnum

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models import MembershipLevel, Ticket, TicketStatus, TeamMembership, User, UserRole


class Access(IntEnum):
    NONE = 0
    READ = 1
    WORK = 2


class NoTicketAccess(Exception):
    """Raised when someone opens a ticket they may not see. Shown as a 'no
    access' page rather than a 404, as the plan specifies."""


def membership_levels(session: Session, user: User) -> dict[int, MembershipLevel]:
    rows = session.exec(
        select(TeamMembership.team_id, TeamMembership.level).where(TeamMembership.user_id == user.id)
    ).all()
    return {team_id: level for team_id, level in rows}


def is_untriaged(ticket: Ticket) -> bool:
    return ticket.status == TicketStatus.NEW or ticket.team_id is None


def ticket_access(user: User, ticket: Ticket, levels: dict[int, MembershipLevel]) -> Access:
    if user.role == UserRole.SUPER_ADMIN:
        return Access.WORK
    if user.role != UserRole.AGENT:
        return Access.NONE

    if levels.get(ticket.team_id) == MembershipLevel.MEMBER:
        return Access.WORK
    if ticket.assigned_agent_id == user.id or is_untriaged(ticket):
        return Access.WORK
    if levels.get(ticket.team_id) == MembershipLevel.VIEWER or ticket.escalated_by_id == user.id:
        return Access.READ
    return Access.NONE


def visible_ticket_condition(user: User, levels: dict[int, MembershipLevel]):
    """SQL filter for the tickets this user can see at all (read or work).
    Mirrors ticket_access; a test keeps the two in step. None means no filter."""
    if user.role == UserRole.SUPER_ADMIN:
        return None
    if user.role != UserRole.AGENT:
        return Ticket.id.is_(None)  # nothing

    return or_(
        Ticket.team_id.in_(list(levels)),
        Ticket.team_id.is_(None),
        Ticket.status == TicketStatus.NEW,
        Ticket.assigned_agent_id == user.id,
        Ticket.escalated_by_id == user.id,
    )


def require_access(user: User, ticket: Ticket, levels: dict[int, MembershipLevel], needed: Access) -> None:
    if ticket_access(user, ticket, levels) < needed:
        raise NoTicketAccess()


def team_members(session: Session, team_id: int | None) -> list[User]:
    """Agents who can work a team's tickets (members, not viewers)."""
    if team_id is None:
        return []
    return list(
        session.exec(
            select(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .where(
                TeamMembership.team_id == team_id,
                TeamMembership.level == MembershipLevel.MEMBER,
                User.role == UserRole.AGENT,
            )
            .order_by(User.display_name)
        ).all()
    )

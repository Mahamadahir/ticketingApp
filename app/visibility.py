"""Who can see and work which tickets. Every route that reads or changes a
ticket goes through here, so the rules live in one place.

- Super admins see and work everything.
- An agent works tickets of teams they are a member of, and reads tickets of
  teams they are a viewer of.
- Every agent can read untriaged tickets (status New, or no team yet) and
  claim one by assigning it to themselves, after which they work it.
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
    # PLANNED CHANGE (docs/plan.md, "Per-inbox routing"): once teams have their own
    # inboxes, mail sent to team B's inbox arrives already in team B and must NOT
    # be visible to everyone as untriaged. This is the one place that will then
    # narrow to "in the shared triage queue" instead of "status New". Keep every
    # use of "untriaged" going through here.
    return ticket.status == TicketStatus.NEW or ticket.team_id is None


def ticket_access(user: User, ticket: Ticket, levels: dict[int, MembershipLevel]) -> Access:
    if user.role == UserRole.SUPER_ADMIN:
        return Access.WORK
    if user.role != UserRole.AGENT:
        return Access.NONE

    if levels.get(ticket.team_id) == MembershipLevel.MEMBER or ticket.assigned_agent_id == user.id:
        return Access.WORK
    # Untriaged tickets are visible to everyone so someone can pick them up, but
    # working one means claiming it first (see may_self_assign). Otherwise a
    # reply would move the ticket out of New and the replier would lose sight of it.
    if (
        levels.get(ticket.team_id) == MembershipLevel.VIEWER
        or ticket.escalated_by_id == user.id
        or is_untriaged(ticket)
    ):
        return Access.READ
    return Access.NONE


def may_self_assign(user: User, ticket: Ticket, levels: dict[int, MembershipLevel]) -> bool:
    """An agent can take a ticket they work, or claim an untriaged one that
    nobody has yet. An untriaged ticket that is already assigned (for example
    handed to whoever will triage it) cannot be taken from its assignee."""
    if user.role != UserRole.AGENT:
        return False
    access = ticket_access(user, ticket, levels)
    if access == Access.WORK:
        return True
    return access >= Access.READ and is_untriaged(ticket) and ticket.assigned_agent_id is None


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


def assignable_agents(session: Session, ticket: Ticket) -> list[User]:
    """Who a super admin may assign the ticket to: the team's members, or any
    agent while the ticket has no team yet."""
    if ticket.team_id is None:
        return list(session.exec(select(User).where(User.role == UserRole.AGENT).order_by(User.display_name)).all())
    return team_members(session, ticket.team_id)


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

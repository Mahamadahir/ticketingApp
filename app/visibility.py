"""Who can see and work which tickets. Every route that reads or changes a
ticket goes through here, so the rules live in one place.

- Super admins see and work everything.
- An agent works tickets of teams they are a member of, and reads tickets of
  teams they are a viewer of.
- Every agent can read untriaged tickets, triage them, and claim one by
  assigning it to themselves, after which they work it. That shared triage queue
  is the one exception to "access follows the team".
- Otherwise access comes from the ticket's team and nothing else. Being its
  assignee, having triaged or escalated it, or following it grants nothing: when a
  ticket moves to another team, everyone outside that team loses it.
- The one other way in is a team link, set by an admin: agents of team A get view or
  work access to team B's tickets. A link is one-way and one hop (A sees B and B
  sees C does not let A see C), and never gives more than the agent's own level in
  A. A team's "escalates to" team counts as a work link, so 1st line keeps working
  tickets it escalated to 2nd line.
"""

from enum import IntEnum

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models import (
    MembershipLevel,
    Team,
    TeamLink,
    TeamMembership,
    Ticket,
    User,
    UserRole,
)


class Access(IntEnum):
    NONE = 0
    READ = 1
    WORK = 2


class NoTicketAccess(Exception):
    """Raised when someone opens a ticket they may not see. Shown as a 'no
    access' page rather than a 404, as the plan specifies."""


class AccessContext(dict):
    """What a user's access depends on besides the ticket itself: a mapping of
    team id to the Access they have to that team's tickets (their own teams plus
    linked ones). Load it once per request with access_context()."""


_LEVEL_ACCESS = {MembershipLevel.MEMBER: Access.WORK, MembershipLevel.VIEWER: Access.READ}


def access_context(session: Session, user: User) -> AccessContext:
    own = {
        team_id: _LEVEL_ACCESS[level]
        for team_id, level in session.exec(
            select(TeamMembership.team_id, TeamMembership.level).where(TeamMembership.user_id == user.id)
        ).all()
    }
    effective = dict(own)
    if own:
        links = [
            (from_team_id, to_team_id, _LEVEL_ACCESS[level])
            for from_team_id, to_team_id, level in session.exec(
                select(TeamLink.from_team_id, TeamLink.to_team_id, TeamLink.level).where(
                    TeamLink.from_team_id.in_(list(own))
                )
            ).all()
        ]
        links += [
            (team_id, escalates_to, Access.WORK)
            for team_id, escalates_to in session.exec(
                select(Team.id, Team.escalates_to_team_id).where(
                    Team.id.in_(list(own)), Team.escalates_to_team_id.is_not(None)
                )
            ).all()
        ]
        for from_team_id, to_team_id, link_access in links:
            # One hop from the agent's own teams, capped by their level there.
            granted = min(own[from_team_id], link_access)
            effective[to_team_id] = max(effective.get(to_team_id, Access.NONE), granted)
    return AccessContext(effective)


def is_untriaged(ticket: Ticket) -> bool:
    # PLANNED CHANGE (docs/plan.md, "Per-inbox routing"): once teams have their own
    # inboxes, mail sent to team B's inbox arrives already in team B and must NOT
    # be visible to everyone as untriaged. This is the one place that will then
    # narrow to "in the shared triage queue". Keep every use of "untriaged" going
    # through here (the SQL twin is in visible_ticket_condition).
    return ticket.triaged_at is None


def ticket_access(user: User, ticket: Ticket, levels: AccessContext) -> Access:
    if user.role == UserRole.SUPER_ADMIN:
        return Access.WORK
    if user.role != UserRole.AGENT:
        return Access.NONE

    team_access = levels.get(ticket.team_id, Access.NONE)
    if team_access == Access.WORK:
        return Access.WORK
    # Untriaged tickets are visible to everyone so someone can pick them up, but
    # working one means claiming it first (see may_self_assign). Otherwise a
    # reply would move the ticket out of New and the replier would lose sight of it.
    if team_access == Access.READ or is_untriaged(ticket):
        return Access.READ
    return Access.NONE


def may_self_assign(user: User, ticket: Ticket, levels: AccessContext) -> bool:
    """An agent can take a ticket they work that nobody else holds, or claim an
    untriaged one that nobody has yet. A ticket assigned to someone else (for example
    one handed to whoever will triage it) is never taken this way: that is Re-assign."""
    if user.role != UserRole.AGENT:
        return False
    if ticket.assigned_agent_id not in (None, user.id):
        # Taking a ticket from its assignee is a deliberate re-assign, never a claim.
        return False
    access = ticket_access(user, ticket, levels)
    if access == Access.WORK:
        return True
    return access >= Access.READ and is_untriaged(ticket) and ticket.assigned_agent_id is None


def may_triage(user: User, ticket: Ticket, levels: AccessContext) -> bool:
    """Any agent (and any super admin) can triage an untriaged ticket they can
    see, without being in the team or assigned to it."""
    if user.role not in (UserRole.AGENT, UserRole.SUPER_ADMIN):
        return False
    return is_untriaged(ticket) and ticket_access(user, ticket, levels) >= Access.READ


def may_add_note(user: User, ticket: Ticket, levels: AccessContext) -> bool:
    """Notes are open to anyone who works the ticket, and to every agent while
    it is untriaged, so an agent can record what they learned without claiming
    or triaging it."""
    if user.role == UserRole.SUPER_ADMIN:
        return True
    if user.role != UserRole.AGENT:
        return False
    access = ticket_access(user, ticket, levels)
    return access == Access.WORK or (access >= Access.READ and is_untriaged(ticket))


def visible_ticket_condition(user: User, levels: AccessContext):
    """SQL filter for the tickets this user can see at all (read or work).
    Mirrors ticket_access; a test keeps the two in step. None means no filter."""
    if user.role == UserRole.SUPER_ADMIN:
        return None
    if user.role != UserRole.AGENT:
        return Ticket.id.is_(None)  # nothing

    return or_(Ticket.team_id.in_(list(levels)), Ticket.triaged_at.is_(None))


def require_access(user: User, ticket: Ticket, levels: AccessContext, needed: Access) -> None:
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

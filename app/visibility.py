"""Who can see and work which tickets. Every route that reads or changes a
ticket goes through here, so the rules live in one place.

- Super admins see and work everything.
- An agent works tickets of teams they are a member of, and reads tickets of
  teams they are a viewer of.
- Every agent can read untriaged tickets, triage them, and claim one by
  assigning it to themselves, after which they work it. That shared triage queue
  is the one exception to "access follows the team".
- Otherwise access comes from the ticket's team and nothing else. Being its
  assignee, having triaged it, or following it grants nothing: when a ticket moves
  to another team, everyone outside that team loses it.
- There are two other ways in. A parent team's members get their level on every
  sub-team's tickets (one level deep). And an agent who escalated a ticket to another
  team keeps working access to that one ticket (TicketEscalator).
"""

from enum import IntEnum

from sqlalchemy import event, or_, true
from sqlalchemy.orm import Session as OrmSession
from sqlmodel import Session, select

from app.models import (
    FINISHED_STATUSES,
    Attachment,
    MembershipLevel,
    Team,
    TeamMembership,
    Ticket,
    TicketEscalator,
    TicketFeedback,
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
    team id to the Access they have to that team's tickets (their own teams and their
    sub-teams), plus the ids of tickets they escalated and so keep
    working. Always get it from access_context(), which loads it once per session."""

    escalated_ticket_ids: frozenset = frozenset()


_LEVEL_ACCESS = {MembershipLevel.MEMBER: Access.WORK, MembershipLevel.VIEWER: Access.READ}


_CACHE_KEY = "access_context"


def access_context(session: Session, user: User) -> AccessContext:
    """The one way to find out what a user can reach. Built once per database session
    (one per request) and reused, so a page that checks access several times asks the
    database once. The copy is dropped whenever the session starts, commits or rolls back
    a transaction (a savepoint included), and when a flush changes memberships, teams,
    escalations or users, so a change made in the same request is never read back stale.

    Treat the result as read-only, and ask again after you change any of those: it is
    one shared object, and a copy you keep will not follow later changes. Changes made with
    a bulk update/delete statement (not through the ORM) do not clear it; commit afterwards."""
    key = (user.id, user.role)
    cached = session.info.get(_CACHE_KEY, {}).get(key)
    if cached is not None:
        return cached
    context = _load_access_context(session, user)
    # Looked up again: loading can autoflush, which drops the dict we started with.
    session.info.setdefault(_CACHE_KEY, {})[key] = context
    return context


# What an AccessContext is built from. A flush that touches none of these leaves it alone.
_ACCESS_MODELS = (TeamMembership, Team, TicketEscalator, User)


def _forget_access_contexts(session, *args) -> None:
    session.info.pop(_CACHE_KEY, None)


def _forget_after_flush(session, flush_context) -> None:
    if any(isinstance(obj, _ACCESS_MODELS) for obj in (*session.new, *session.dirty, *session.deleted)):
        session.info.pop(_CACHE_KEY, None)


# after_soft_rollback fires for savepoint rollbacks as well as full ones.
for _signal in ("after_begin", "after_commit", "after_soft_rollback"):
    event.listen(OrmSession, _signal, _forget_access_contexts)
event.listen(OrmSession, "after_flush", _forget_after_flush)


def _load_access_context(session: Session, user: User) -> AccessContext:
    own = {
        team_id: _LEVEL_ACCESS[level]
        for team_id, level in session.exec(
            select(TeamMembership.team_id, TeamMembership.level).where(TeamMembership.user_id == user.id)
        ).all()
    }
    effective = dict(own)
    if own:
        # A parent team's members hold the same level on its sub-teams.
        for sub_id, parent_id in session.exec(
            select(Team.id, Team.parent_team_id).where(Team.parent_team_id.in_(list(own)))
        ).all():
            effective[sub_id] = max(effective.get(sub_id, Access.NONE), own[parent_id])
    context = AccessContext(effective)
    if user.role == UserRole.AGENT:
        context.escalated_ticket_ids = frozenset(
            session.exec(select(TicketEscalator.ticket_id).where(TicketEscalator.user_id == user.id)).all()
        )
    return context


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
    if team_access == Access.WORK or ticket.id in levels.escalated_ticket_ids:
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
    if access == Access.WORK and levels.get(ticket.team_id, Access.NONE) == Access.WORK:
        # Working it through the team (or a parent team). An escalator's access to a ticket
        # they sent away is not a claim on it: the assignee must be someone in its team.
        return True
    return access >= Access.READ and is_untriaged(ticket) and ticket.assigned_agent_id is None


def may_triage(user: User, ticket: Ticket, levels: AccessContext) -> bool:
    """Any agent (and any super admin) can triage an untriaged ticket they can
    see, without being in the team or assigned to it."""
    if user.role not in (UserRole.AGENT, UserRole.SUPER_ADMIN):
        return False
    return is_untriaged(ticket) and ticket_access(user, ticket, levels) >= Access.READ


def may_set_required_by(user: User, ticket: Ticket, levels: AccessContext) -> bool:
    """Who may set or change a service request's Required by date (app/required_by.py).
    The requester, until the ticket is triaged; from then on only agents, and only those who
    work the ticket (or can still triage it). A follower never can. Whether the request type
    offers the field at all is checked where the date is set."""
    if ticket.status in FINISHED_STATUSES:
        return False
    if is_untriaged(ticket) and ticket.requester_email.strip().lower() == user.email.strip().lower():
        return True
    return ticket_access(user, ticket, levels) == Access.WORK or may_triage(user, ticket, levels)


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

    return or_(
        Ticket.team_id.in_(list(levels)),
        Ticket.triaged_at.is_(None),
        Ticket.id.in_(select(TicketEscalator.ticket_id).where(TicketEscalator.user_id == user.id)),
    )


def feedback_condition(user: User):
    """Which feedback rows a person may read: an admin all of it, an agent only the feedback given
    about tickets they themselves resolved (`resolved_by_id`). Being able to open the ticket grants
    nothing here, and feedback about an automatic resolve (no agent) is for admins only."""
    if user.role == UserRole.SUPER_ADMIN:
        return true()
    return TicketFeedback.resolved_by_id == user.id


def require_access(user: User, ticket: Ticket, levels: AccessContext, needed: Access) -> None:
    if ticket_access(user, ticket, levels) < needed:
        raise NoTicketAccess()


def may_view_attachment(
    session: Session, user: User, attachment: Attachment, ticket: Ticket, levels: AccessContext
) -> bool:
    """Whether this person may download the file. The only place that decides it; the download
    route asks and never looks at the ticket itself.

    An agent or super admin: whoever can read the ticket (internal notes' files included).
    Otherwise a requester, or someone following the ticket from outside: only files that arrived with a
    message the requester may see (app.messages.requester_visible_messages, which is the single
    rule for that), or a file they added themselves. Never an internal note's file."""
    if user.role in (UserRole.AGENT, UserRole.SUPER_ADMIN) and ticket_access(user, ticket, levels) >= Access.READ:
        return True
    # Anyone else, including an agent who raised or follows a ticket outside their teams, is held
    # to what the requester may see.
    # Imported here: both modules import this one.
    from app.followers import is_follower
    from app.messages import requester_visible_messages

    is_requester = ticket.requester_email.lower() == user.email.lower()
    if not (is_requester or is_follower(session, ticket, user.email, include_internal=False)):
        return False
    if attachment.message_id is None:
        return attachment.uploaded_by_id == user.id
    return attachment.message_id in {m.id for m in requester_visible_messages(session, ticket)}


def assignable_agents(session: Session, ticket: Ticket) -> list[User]:
    """Who a super admin may assign the ticket to: the team's members, or any
    agent while the ticket has no team yet."""
    if ticket.team_id is None:
        return list(session.exec(select(User).where(User.role == UserRole.AGENT).order_by(User.display_name)).all())
    return team_members(session, ticket.team_id)


def team_members(session: Session, team_id: int | None) -> list[User]:
    """Agents who can work a team's tickets (members, not viewers), including the members of
    its parent team, who work its tickets too (see access_context)."""
    if team_id is None:
        return []
    parent_id = session.exec(select(Team.parent_team_id).where(Team.id == team_id)).first()
    team_ids = [team_id] + ([parent_id] if parent_id is not None else [])
    return list(
        session.exec(
            select(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .where(
                TeamMembership.team_id.in_(team_ids),
                TeamMembership.level == MembershipLevel.MEMBER,
                User.role == UserRole.AGENT,
            )
            .order_by(User.display_name)
        ).all()
    )

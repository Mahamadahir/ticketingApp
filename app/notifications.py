"""In-app notifications (phase 2A): the bell. Something happens that an agent should know about,
a row is written for them, and the bell counts what they have not read.

Four things notify: a ticket being assigned to you, an @mention in an internal note, the
requester (or a follower) replying on your ticket, and your ticket being reopened.
Later phases add Teams (an unread notification is also sent to Teams after a delay, P1s and SLA
breaches) and skipping agents who are out of office. Which events reach which channel is each
person's choice (app/notification_settings.py); notify() honours the in-app one. Everything
that notifies goes through notify() so those arrive in one place.

Only agents are notified, and only about tickets they can open: a mention of someone outside
the ticket's team does nothing (access follows the team, and a notification would say what the
ticket is).
"""

import re
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import and_, func, or_, update
from sqlmodel import Session, select

from app.models import FINISHED_STATUSES, Message, Notification, Ticket, User, UserRole
from app.notification_settings import IN_APP, TEAMS, is_enabled, offers, wants_approval_email
from app.settings_store import TEAMS_UNREAD_MINUTES, get_int_setting
from app.visibility import Access, access_context, ticket_access, visible_ticket_condition

ASSIGNED = "assigned"
MENTIONED = "mentioned"
REPLIED = "replied"
REOPENED = "reopened"
APPROVAL_WITHDRAWN = "approval_withdrawn"
APPROVAL_OVERDUE = "approval_overdue"
DUE_TODAY = "due_today"
AGENT_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)

# @alice or @alice@example.com: letters, digits, dots, dashes and underscores.
_MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9][A-Za-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9.-]*)?)")


def notify(
    session: Session, recipient: User, kind: str, ticket: Optional[Ticket], text: str, actor_email: Optional[str]
) -> Optional[Notification]:
    """Tells one agent something. Nothing for the person who did it themselves. Does not commit."""
    if recipient.role not in AGENT_ROLES:
        return None
    if actor_email and recipient.email.lower() == actor_email.lower():
        return None
    bell = is_enabled(session, recipient, kind, IN_APP)
    teams = offers(kind, TEAMS) and is_enabled(session, recipient, kind, TEAMS)
    if not bell and not teams:
        return None  # they turned both off for this (Settings, notifications)
    notification = Notification(
        user_id=recipient.id, kind=kind, ticket_id=ticket.id if ticket is not None else None,
        actor_email=actor_email, text=text, bell=bell, teams_wanted=teams,
    )
    if not bell:
        # Teams only: the row is the worker's record of what to send, so it must exist, but it is
        # written already read and hidden from the bell, which never shows or counts it.
        notification.read_at = datetime.now(timezone.utc)
    session.add(notification)
    return notification


def notify_assigned(session: Session, ticket: Ticket, assignee: User, actor_email: Optional[str]) -> Optional[Notification]:
    if not _can_open(session, assignee, ticket):
        return None  # a notification says what the ticket is, so only someone who can open it gets one
    # The number with # rather than the reference: the prefix follows the ticket's type, which can change.
    return notify(session, assignee, ASSIGNED, ticket, f"{ticket.tag} {ticket.subject} was assigned to you", actor_email)


def _can_open(session: Session, person: User, ticket: Ticket) -> bool:
    return ticket_access(person, ticket, access_context(session, person)) >= Access.READ


def mentioned_in(session: Session, ticket: Ticket, body: str, author: User) -> list[User]:
    """The agents an internal note mentions who can open the ticket, each once, never the author.
    @alice matches the part of an email address before the @ (or the whole address, @alice@x.com);
    a name two accounts share matches neither, so nobody is notified by mistake."""
    tokens = {match.rstrip(".-_").lower() for match in _MENTION.findall(body or "")}
    tokens.discard("")
    if not tokens:
        return []
    agents = session.exec(select(User).where(User.role.in_(AGENT_ROLES))).all()
    levels_cache: dict[int, object] = {}
    found: list[User] = []
    for token in sorted(tokens):
        if "@" in token:
            matches = [a for a in agents if a.email.lower() == token]
        else:
            matches = [a for a in agents if a.email.lower().partition("@")[0] == token]
        if len(matches) != 1:
            continue
        person = matches[0]
        if person.id == author.id or person in found:
            continue
        levels = levels_cache.setdefault(person.id, access_context(session, person))
        if ticket_access(person, ticket, levels) >= Access.READ:
            found.append(person)
    return found


def notify_mentions(
    session: Session, ticket: Ticket, note: Message, author: User, text: Optional[str] = None
) -> list[Notification]:
    """Tells the agents the note mentions. `text` is what to read mentions from when part of the
    note was written by the system (an escalation's fixed lead-in), so only what the agent wrote counts."""
    notifications = []
    for person in mentioned_in(session, ticket, note.body if text is None else text, author):
        made = notify(
            session, person, MENTIONED, ticket,
            f"{author.display_name} mentioned you on {ticket.tag} {ticket.subject}", author.email,
        )
        if made is not None:
            notifications.append(made)
    return notifications


def notify_reply(session: Session, ticket: Ticket, replier_email: str) -> list[Notification]:
    """Tells the assigned agent ("Ticket updated by <name>") that the requester (or a follower) replied, whatever the ticket's status:
    a reply on a ticket with the MSP or a supplier moves nothing, so this is the only sign of it. A reply
    on a split original also tells the assignees of its open parts, since each of them sees the reply.
    Each agent once; never the replier; nobody who cannot open the ticket they are told about. A ticket
    nobody is assigned to tells nobody (an open question in docs/build-plan.md). Does not commit."""
    replier = session.exec(select(User).where(func.lower(User.email) == replier_email.lower())).first()
    name = (replier.display_name or "").strip() if replier is not None else ""
    name = name or replier_email  # a follower without an account, or no name: their email
    targets = [ticket]
    targets += session.exec(
        select(Ticket).where(Ticket.split_from_id == ticket.id, Ticket.status.not_in(FINISHED_STATUSES)).order_by(Ticket.id)
    ).all()
    told: set[int] = set()
    made: list[Notification] = []
    for target in targets:
        if target.assigned_agent_id is None or target.assigned_agent_id in told:
            continue
        agent = session.get(User, target.assigned_agent_id)
        if agent is None or not _can_open(session, agent, target):
            continue
        notification = notify(
            session, agent, REPLIED, target, f"{target.tag} {target.subject}: Ticket updated by {name}", replier_email
        )
        if notification is not None:
            told.add(agent.id)
            made.append(notification)
    return made


def visible_to(session: Session, user: User):
    """The SQL condition for the notifications this user may still see: their own, leaving out
    those about a ticket they can no longer open (it moved to another team). A notification would
    otherwise keep showing the subject of a ticket they have lost access to."""
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is None:  # a super admin can open every ticket
        return and_(Notification.user_id == user.id, Notification.bell.is_(True))
    return and_(
        Notification.user_id == user.id,
        Notification.bell.is_(True),  # not the Teams-only ones
        or_(Notification.ticket_id.is_(None), Notification.ticket_id.in_(select(Ticket.id).where(visible))),
    )


def count_unread(session: Session, user: User) -> int:
    return session.exec(
        select(func.count()).select_from(Notification).where(visible_to(session, user), Notification.read_at.is_(None))
    ).one()


def bell_tickets(session: Session, user: User, notifications) -> dict[int, Ticket]:
    """The tickets of these notifications that the user can open, by id. The bell shows a ticket's
    number or subject only from here, so one they cannot open never reaches a page, even if its
    notification was read in a moment when they still could."""
    ids = {n.ticket_id for n in notifications if n.ticket_id}
    if not ids:
        return {}
    query = select(Ticket).where(Ticket.id.in_(ids))
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is not None:  # None: a super admin can open every ticket
        query = query.where(visible)
    return {t.id: t for t in session.exec(query).all()}


def mark_read(session: Session, user: User, notification_id: int) -> Optional[Notification]:
    """Marks one of the user's own notifications read. None when it is not theirs, is Teams-only,
    or is about a ticket they can no longer open (the same rows the bell shows, see visible_to)."""
    notification = session.exec(
        select(Notification).where(Notification.id == notification_id, visible_to(session, user))
    ).first()
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        session.add(notification)
    return notification


def mark_all_read(session: Session, user: User) -> int:
    """Marks all of the user's unread notifications they can see read in one statement. Returns how many.
    Hidden ones (ticket no longer openable) are left alone, so the count shown matches."""
    result = session.exec(
        update(Notification)
        .where(visible_to(session, user), Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    return result.rowcount

# ---- The Teams copy (phase 2C-ii) -----------------------------------------------------------------------
#
# An agent can choose, per event, to be told in the bell, in Teams, or both. A notification that wants
# a Teams copy gets one when it is due: straight away if the bell is off for it (there is nothing to
# wait for), or once it has stayed unread in the bell for the person's delay. Reading it first means no
# copy. The worker picks the due ones up and hands each to whatever sends to Teams (app/teams.py); it is
# handed over once, and only if the person still wants it and can still open the ticket.

TEAMS_BATCH = 50


def default_teams_delay(session: Session) -> int:
    """The delay for everyone who has not set their own: the admin setting, 5 minutes unless changed."""
    return get_int_setting(session, TEAMS_UNREAD_MINUTES) or 5


def retire_read_for_teams(session: Session, now: Optional[datetime] = None) -> int:
    """Marks dealt with every bell notification the person has read before its Teams copy went out:
    they have seen it, so no copy. Without this a row read in time would stay waiting for ever, and
    the index the worker uses would grow with all the read history. Returns how many. Does not commit."""
    now = now or datetime.now(timezone.utc)
    result = session.exec(
        update(Notification)
        .where(
            Notification.teams_wanted.is_(True),
            Notification.teams_handled_at.is_(None),
            Notification.bell.is_(True),
            Notification.read_at.is_not(None),
        )
        .values(teams_handled_at=now)
    )
    return result.rowcount


def due_for_teams(session: Session, now: Optional[datetime] = None, limit: int = TEAMS_BATCH) -> list[Notification]:
    """The notifications whose Teams copy is due, oldest first, locked so a second worker cannot take the
    same rows. Not due: already dealt with, not wanted, read in the bell before the delay ran out."""
    now = now or datetime.now(timezone.utc)
    delay = func.coalesce(User.teams_delay_minutes, default_teams_delay(session))
    due_after_delay = Notification.created_at + func.make_interval(0, 0, 0, 0, 0, delay, 0)
    return list(
        session.exec(
            select(Notification)
            .join(User, User.id == Notification.user_id)
            .where(
                Notification.teams_wanted.is_(True),
                Notification.teams_handled_at.is_(None),
                or_(
                    Notification.bell.is_(False),
                    and_(Notification.read_at.is_(None), due_after_delay <= now),
                ),
            )
            .order_by(Notification.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Notification)
        ).all()
    )


def hand_to_teams(
    session: Session,
    enqueue: Callable[[Session, Notification], None],
    now: Optional[datetime] = None,
    limit: int = TEAMS_BATCH,
) -> tuple[int, int]:
    """Hands each due notification to `enqueue` (which writes the Teams post) and marks it dealt with.
    A notification is dropped, not sent, if the person has since turned Teams off for that event or can
    no longer open its ticket (a Teams message would say what the ticket is). Returns (handed, dropped).
    Does not commit; the worker commits after the job. If `enqueue` raises, nothing from this run is kept."""
    now = now or datetime.now(timezone.utc)
    retire_read_for_teams(session, now)
    handed = dropped = 0
    for notification in due_for_teams(session, now, limit):
        person = session.get(User, notification.user_id)
        ticket = session.get(Ticket, notification.ticket_id) if notification.ticket_id else None
        still_wanted = offers(notification.kind, TEAMS) and is_enabled(session, person, notification.kind, TEAMS)
        can_open = notification.ticket_id is None or (ticket is not None and _can_open(session, person, ticket))
        if still_wanted and can_open:
            enqueue(session, notification)
            handed += 1
        else:
            dropped += 1
        notification.teams_handled_at = now
        session.add(notification)
    return handed, dropped

DROP_BATCH = 1000  # dropping is cheap, so a backlog drains quickly


def drop_due_for_teams(session: Session, now: Optional[datetime] = None, limit: int = DROP_BATCH) -> int:
    """Marks the due Teams copies dealt with without sending them: what the worker does while sending
    notifications to Teams is switched off, so nothing is left waiting to arrive in a flood when it
    is switched on. Returns how many. Does not commit."""
    now = now or datetime.now(timezone.utc)
    retire_read_for_teams(session, now)
    rows = due_for_teams(session, now, limit)
    for notification in rows:
        notification.teams_handled_at = now
        session.add(notification)
    return len(rows)


def notify_reopened(session: Session, ticket: Ticket, actor_email: Optional[str], reason: str) -> Optional[Notification]:
    """Tells the assigned agent their resolved ticket was reopened (never the person who reopened it).
    Nobody assigned tells nobody; nobody who cannot open the ticket is told about it. Does not commit."""
    if ticket.assigned_agent_id is None:
        return None
    agent = session.get(User, ticket.assigned_agent_id)
    if agent is None or not _can_open(session, agent, ticket):
        return None
    return notify(
        session, agent, REOPENED, ticket, f"{ticket.tag} {ticket.subject} was reopened: {reason[:200]}", actor_email
    )


def notify_approval_withdrawn(session: Session, ticket: Ticket, approver: User, actor_email: Optional[str]) -> Optional[Notification]:
    """Tells an agent who was asked to approve that the requester withdrew the request. Only an agent has a
    bell, and only one who can open the ticket is told what it is. Does not commit."""
    if not _can_open(session, approver, ticket) or not wants_approval_email(session, approver):
        return None  # the bell has no setting of its own: switching off approval notifications covers it
    return notify(
        session, approver, APPROVAL_WITHDRAWN, ticket,
        f"{ticket.tag} {ticket.subject} was withdrawn by the requester, so it no longer needs your approval", actor_email,
    )


def notify_approval_overdue(session: Session, ticket: Ticket, super_admin: User, days: int, unanswered: int) -> Optional[Notification]:
    """Tells a super admin a request has waited too long for approval (app/approval_reminders.py), so they can
    chase the approvers or waive it. Only if they can open the ticket. Does not commit."""
    if not _can_open(session, super_admin, ticket):
        return None
    return notify(
        session, super_admin, APPROVAL_OVERDUE, ticket,
        f"{ticket.tag} {ticket.subject} has waited {days} days for approval ({unanswered} had not answered)", None,
    )


def notify_due_today(session: Session, ticket: Ticket) -> Optional[Notification]:
    """Tells the assigned agent their Scheduled ticket is due today (app/due_today.py), as the job moves it back to
    Open. Nobody assigned tells nobody; nobody who cannot open the ticket is told about it. Does not commit."""
    if ticket.assigned_agent_id is None:
        return None
    agent = session.get(User, ticket.assigned_agent_id)
    if agent is None or not _can_open(session, agent, ticket):
        return None
    return notify(session, agent, DUE_TODAY, ticket, f"{ticket.reference} is due today: {ticket.subject}", None)

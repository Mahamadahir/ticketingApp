"""In-app notifications (phase 2A): the bell. Something happens that an agent should know about,
a row is written for them, and the bell counts what they have not read.

For now two things notify: a ticket being assigned to you, and an @mention in an internal note.
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
from typing import Optional

from sqlalchemy import and_, func, or_, update
from sqlmodel import Session, select

from app.models import Message, Notification, Ticket, User, UserRole
from app.notification_settings import IN_APP, is_enabled
from app.visibility import Access, access_context, ticket_access, visible_ticket_condition

ASSIGNED = "assigned"
MENTIONED = "mentioned"
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
    if not is_enabled(session, recipient, kind, IN_APP):
        return None  # they turned the bell off for this (Settings, notifications)
    notification = Notification(
        user_id=recipient.id, kind=kind, ticket_id=ticket.id if ticket is not None else None,
        actor_email=actor_email, text=text,
    )
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


def visible_to(session: Session, user: User):
    """The SQL condition for the notifications this user may still see: their own, leaving out
    those about a ticket they can no longer open (it moved to another team). A notification would
    otherwise keep showing the subject of a ticket they have lost access to."""
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is None:  # a super admin can open every ticket
        return Notification.user_id == user.id
    return and_(
        Notification.user_id == user.id,
        or_(Notification.ticket_id.is_(None), Notification.ticket_id.in_(select(Ticket.id).where(visible))),
    )


def count_unread(session: Session, user: User) -> int:
    return session.exec(
        select(func.count()).select_from(Notification).where(visible_to(session, user), Notification.read_at.is_(None))
    ).one()


def mark_read(session: Session, user: User, notification_id: int) -> Optional[Notification]:
    """Marks one of the user's own notifications read. None when it is not theirs."""
    notification = session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        session.add(notification)
    return notification


def mark_all_read(session: Session, user: User) -> int:
    """Marks all of the user's unread notifications read in one statement. Returns how many."""
    result = session.exec(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    return result.rowcount
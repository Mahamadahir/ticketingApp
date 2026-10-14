"""In-app notifications (phase 2A): the bell. Something happens that an agent should know about,
a row is written for them, and the bell counts what they have not read.

For now two things notify: a ticket being assigned to you, and an @mention in an internal note.
Later phases add Teams (an unread notification is also sent to Teams after a delay, P1s and SLA
breaches), per-person settings for which events reach which channel, and skipping agents who are
out of office. Everything that notifies goes through notify() so those arrive in one place.

Only agents are notified, and only about tickets they can open: a mention of someone outside
the ticket's team does nothing (access follows the team, and a notification would say what the
ticket is).
"""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Message, Notification, Ticket, User, UserRole
from app.visibility import Access, access_context, ticket_access

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
    notification = Notification(
        user_id=recipient.id, kind=kind, ticket_id=ticket.id if ticket is not None else None,
        actor_email=actor_email, text=text,
    )
    session.add(notification)
    return notification


def notify_assigned(session: Session, ticket: Ticket, assignee: User, actor_email: Optional[str]) -> Optional[Notification]:
    return notify(session, assignee, ASSIGNED, ticket, f"{ticket.reference} {ticket.subject} was assigned to you", actor_email)


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


def notify_mentions(session: Session, ticket: Ticket, note: Message, author: User) -> list[Notification]:
    notifications = []
    for person in mentioned_in(session, ticket, note.body, author):
        made = notify(
            session, person, MENTIONED, ticket,
            f"{author.display_name} mentioned you on {ticket.reference} {ticket.subject}", author.email,
        )
        if made is not None:
            notifications.append(made)
    return notifications


def count_unread(session: Session, user: User) -> int:
    return session.exec(
        select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
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
    rows = session.exec(
        select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.read_at = now
        session.add(row)
    return len(rows)
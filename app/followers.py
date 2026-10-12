"""Followers: people copied on a ticket's public emails.

- A requester or an agent adds a follower by email. The follower is copied on
  every public email, and if they have a portal account they see the ticket
  under Following (read-only, public messages only).
- An internal follower is an agent who keeps read access to the ticket wherever
  it moves, and is copied on everything. Escalating a ticket makes the
  escalating agent one.
Nothing here sends mail; that arrives with Graph in phase 3. cc_list is what the
sender will use, and each outgoing message records it.
"""

import re
from typing import Optional

from sqlmodel import Session, func, select

from app.models import Ticket, TicketFollower, User
from app.ticket_service import record_event

MAX_EXTERNAL_FOLLOWERS = 20
EVENT_FOLLOWER_ADDED = "follower_added"
EVENT_FOLLOWER_REMOVED = "follower_removed"

# Deliberately plain: one @, something either side, a dot in the domain, no spaces.
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class InvalidEmail(ValueError):
    pass


class TooManyFollowers(ValueError):
    pass


def normalise_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise InvalidEmail(raw)
    return email


def followers_of(session: Session, ticket: Ticket) -> list[TicketFollower]:
    return list(
        session.exec(
            select(TicketFollower)
            .where(TicketFollower.ticket_id == ticket.id)
            .order_by(TicketFollower.is_internal.desc(), TicketFollower.id)
        ).all()
    )


def add_follower(
    session: Session,
    ticket: Ticket,
    raw_email: str,
    added_by_email: str,
    *,
    internal_user: Optional[User] = None,
) -> Optional[TicketFollower]:
    """Adds a follower and records the event. Returns None when there is nothing
    to add (already following, or the address is the requester's, who is
    already a recipient). Pass internal_user to add an agent as an internal
    follower, which grants them read access. Does not commit."""
    # An agent's own account address is trusted as it is: re-checking it with the
    # rules for typed-in addresses could make a valid account unable to escalate.
    email = internal_user.email.strip().lower() if internal_user else normalise_email(raw_email)
    if email == ticket.requester_email.lower():
        return None

    existing = session.exec(
        select(TicketFollower).where(TicketFollower.ticket_id == ticket.id, TicketFollower.email == email)
    ).first()
    if existing is not None:
        if internal_user is not None and not existing.is_internal:
            # An address already following becomes an internal follower too.
            existing.is_internal = True
            existing.user_id = internal_user.id
            session.add(existing)
            return existing
        return None

    if internal_user is None:
        external_count = len([f for f in followers_of(session, ticket) if not f.is_internal])
        if external_count >= MAX_EXTERNAL_FOLLOWERS:
            raise TooManyFollowers()

    follower = TicketFollower(
        ticket_id=ticket.id,
        email=email,
        user_id=internal_user.id if internal_user else None,
        is_internal=internal_user is not None,
        added_by_email=added_by_email,
    )
    session.add(follower)
    record_event(
        session, ticket, EVENT_FOLLOWER_ADDED, added_by_email,
        to_value=email, detail="internal" if internal_user else None,
    )
    return follower


def remove_follower(session: Session, ticket: Ticket, follower_id: int, removed_by_email: str) -> bool:
    """Removes a follower. Internal followers cannot be removed here: they hold
    read access that the ticket's history depends on. Returns whether one was
    removed. Does not commit."""
    follower = session.get(TicketFollower, follower_id)
    if follower is None or follower.ticket_id != ticket.id or follower.is_internal:
        return False
    session.delete(follower)
    record_event(session, ticket, EVENT_FOLLOWER_REMOVED, removed_by_email, from_value=follower.email)
    return True


def cc_list(session: Session, ticket: Ticket) -> list[str]:
    """Who is copied on an outgoing email: every follower, never the requester."""
    return [f.email for f in followers_of(session, ticket)]


def tickets_followed_by(session: Session, email: str) -> list[Ticket]:
    """Tickets this address follows from the portal's point of view: external
    followers only, and never a ticket they raised themselves."""
    email = email.lower()
    return list(
        session.exec(
            select(Ticket)
            .join(TicketFollower, TicketFollower.ticket_id == Ticket.id)
            .where(
                TicketFollower.email == email,
                TicketFollower.is_internal.is_(False),
                func.lower(Ticket.requester_email) != email,
            )
            .order_by(Ticket.updated_at.desc())
            .limit(100)
        ).all()
    )


def is_follower(session: Session, ticket: Ticket, email: str) -> bool:
    return (
        session.exec(
            select(TicketFollower.id).where(
                TicketFollower.ticket_id == ticket.id,
                TicketFollower.email == email.lower(),
                TicketFollower.is_internal.is_(False),
            )
        ).first()
        is not None
    )

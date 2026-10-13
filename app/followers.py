"""Followers: people copied on a ticket's public emails.

- A requester or an agent adds a follower by email. The follower is copied on
  every public email, and if they have a portal account they see the ticket
  under Following (read-only, public messages only).
- An internal follower is an agent who is always copied on the ticket's emails.
  Escalating a ticket makes the escalating agent one. It grants no access:
  that follows the ticket's team (app/visibility.py).
Nothing here sends mail; that arrives with Graph in phase 3. cc_list is what the
sender will use, and each outgoing message records it.
"""

import re
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.models import Ticket, TicketFollower, User
from app.ticket_service import record_event

MAX_EXTERNAL_FOLLOWERS = 20
EVENT_FOLLOWER_ADDED = "follower_added"
EVENT_FOLLOWER_REMOVED = "follower_removed"

# A plain address: a local part, then a domain of dotted labels that each start and
# end with a letter or digit, ending in a letter-only top-level label. This rejects
# things like a@b..c, a@.b.c and a@b.c. that would bounce once mail is sent. Written
# for ASCII domains; an internationalised domain has to be given in its xn-- form.
_LOCAL = re.compile(r"^[^@\s,;<>]+$")
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


class InvalidEmail(ValueError):
    pass


class TooManyFollowers(ValueError):
    pass


def normalise_email(raw: str) -> str:
    email = raw.strip().lower()
    local, at, domain = email.partition("@")
    if (
        len(email) > 254
        or not at
        or not _LOCAL.match(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _DOMAIN.match(domain)
    ):
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
    to add (already following, or the address is the requester's, who is already
    a recipient). Pass internal_user to add an agent as an internal follower, who is then
    always copied (no access comes with it); that is allowed even when the agent is
    the ticket's requester. Safe to call twice at once. Does not commit."""
    # An agent's own account address is trusted as it is: re-checking it with the
    # rules for typed-in addresses could make a valid account unable to escalate.
    email = internal_user.email.strip().lower() if internal_user else normalise_email(raw_email)
    if internal_user is None and email == ticket.requester_email.lower():
        return None

    existing = session.exec(
        select(TicketFollower).where(TicketFollower.ticket_id == ticket.id, TicketFollower.email == email)
    ).first()
    if existing is not None:
        if internal_user is not None and not existing.is_internal:
            # Someone already copied on the ticket is now an internal follower too.
            existing.is_internal = True
            existing.user_id = internal_user.id
            session.add(existing)
            record_event(
                session, ticket, EVENT_FOLLOWER_ADDED, added_by_email, to_value=email, detail="internal"
            )
            return existing
        return None

    if internal_user is None:
        external_count = session.exec(
            select(func.count())
            .select_from(TicketFollower)
            .where(TicketFollower.ticket_id == ticket.id, TicketFollower.is_internal.is_(False))
        ).one()
        if external_count >= MAX_EXTERNAL_FOLLOWERS:
            raise TooManyFollowers()

    follower = TicketFollower(
        ticket_id=ticket.id,
        email=email,
        user_id=internal_user.id if internal_user else None,
        is_internal=internal_user is not None,
        added_by_email=added_by_email,
    )
    try:
        # A savepoint, so losing a race to add the same address (two people, or a
        # double click) is a quiet no-op instead of an error that aborts the request.
        with session.begin_nested():
            session.add(follower)
            session.flush()
    except IntegrityError:
        return None

    record_event(
        session, ticket, EVENT_FOLLOWER_ADDED, added_by_email,
        to_value=email, detail="internal" if internal_user else None,
    )
    return follower


def remove_follower(session: Session, ticket: Ticket, follower_id: int, removed_by_email: str) -> Optional[str]:
    """Removes a follower and returns their address, or None if nothing was
    removed. Internal followers cannot be removed here: they are always copied
    by design. Does not commit."""
    follower = session.get(TicketFollower, follower_id)
    if follower is None or follower.ticket_id != ticket.id or follower.is_internal:
        return None
    email = follower.email
    session.delete(follower)
    record_event(session, ticket, EVENT_FOLLOWER_REMOVED, removed_by_email, from_value=email)
    return email


def cc_list(session: Session, ticket: Ticket) -> list[str]:
    """Who is copied on an outgoing email: every follower except the requester,
    who is the recipient (an agent who raised the ticket can be an internal
    follower and must not be copied on their own email)."""
    requester = ticket.requester_email.lower()
    return [f.email for f in followers_of(session, ticket) if f.email != requester]


def tickets_followed_by(session: Session, email: str) -> list[Ticket]:
    """Tickets this address follows, for the portal's Following list: never one
    they raised themselves. Internal followers are included, so an agent who
    becomes an internal follower does not lose a ticket they were already
    following; they only ever see its public messages here."""
    email = email.lower()
    return list(
        session.exec(
            select(Ticket)
            .join(TicketFollower, TicketFollower.ticket_id == Ticket.id)
            .where(TicketFollower.email == email, func.lower(Ticket.requester_email) != email)
            .order_by(Ticket.updated_at.desc())
            .limit(100)
        ).all()
    )


def is_follower(session: Session, ticket: Ticket, email: str, *, include_internal: bool = True) -> bool:
    conditions = [TicketFollower.ticket_id == ticket.id, TicketFollower.email == email.lower()]
    if not include_internal:
        conditions.append(TicketFollower.is_internal.is_(False))
    return session.exec(select(TicketFollower.id).where(*conditions)).first() is not None

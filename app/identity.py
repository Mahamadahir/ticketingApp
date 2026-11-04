"""Which tickets a signed-in person counts as their own in the portal.

Every portal comparison between a ticket and the signed-in person goes through
`requester_emails`, and nothing in the portal compares `user.email` to a stored
address directly. Addresses are matched lower-cased. Today it is one address; it
is a seam because one person may later be known by several (merged accounts), and
then only this function changes. Agent access is not decided here (app/visibility.py).
"""

from sqlalchemy import case, func
from sqlmodel import Session, select

from app.models import Ticket, TicketFollower, User


def requester_emails(user: User) -> set[str]:
    """Every address (lower case) under which this person is the requester or a follower."""
    return {user.email.strip().lower()}


def is_requester(ticket: Ticket, user: User) -> bool:
    return ticket.requester_email.strip().lower() in requester_emails(user)


def is_follower_of(session: Session, ticket: Ticket, user: User, *, include_internal: bool = True) -> bool:
    conditions = [TicketFollower.ticket_id == ticket.id, func.lower(TicketFollower.email).in_(requester_emails(user))]
    if not include_internal:
        conditions.append(TicketFollower.is_internal.is_(False))
    return session.exec(select(TicketFollower.id).where(*conditions)).first() is not None


def my_ticket_condition(user: User):
    """SQL: the ticket is one the person raised or follows (the portal's "My requests", before ordering)."""
    emails = requester_emails(user)
    own = func.lower(func.trim(Ticket.requester_email)).in_(emails)
    followed = Ticket.id.in_(select(TicketFollower.ticket_id).where(func.lower(TicketFollower.email).in_(emails)))
    return own | followed


def my_tickets_query(user: User):
    """Tickets the person raised, then tickets they follow, the ones they raised first.
    What they may read of each is decided on the ticket page, through app/messages.py."""
    emails = requester_emails(user)
    own = func.lower(func.trim(Ticket.requester_email)).in_(emails)
    followed = Ticket.id.in_(select(TicketFollower.ticket_id).where(func.lower(TicketFollower.email).in_(emails)))
    return (
        select(Ticket)
        .where(own | followed)
        .order_by(case((own, 0), else_=1), Ticket.updated_at.desc())
    )

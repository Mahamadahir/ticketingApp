"""Linking tickets to a parent Major Incident or Problem. Nothing here commits.

The link is one level deep: only a Major Incident or Problem can be a parent, a parent
is never itself linked to anything, and a ticket with linked tickets cannot be linked
under another. Each side gets an event, so both timelines say what happened. Who may
link is decided by the route (work access on the ticket, read access on the parent);
who may *see* a link is decided by visibility (a viewer sees only the linked tickets
they could open anyway)."""

from typing import Optional

from sqlmodel import Session, select

from app.models import Ticket, TicketAlias, TicketType
from app.ticket_service import record_event

EVENT_LINKED = "linked"  # on the ticket that was linked: to_value is the parent
EVENT_UNLINKED = "unlinked"
EVENT_CHILD_LINKED = "child_linked"  # on the parent: to_value is the linked ticket
EVENT_CHILD_UNLINKED = "child_unlinked"

PARENT_TYPES = (TicketType.MAJOR_INCIDENT, TicketType.PROBLEM)


class LinkError(ValueError):
    """A reason a link cannot be made, worded for the agent."""


def find_by_reference(session: Session, reference: str) -> Optional[Ticket]:
    """A ticket by its reference, or by an old one it kept as an alias."""
    reference = reference.strip().upper()
    if not reference:
        return None
    ticket = session.exec(select(Ticket).where(Ticket.reference == reference)).first()
    if ticket is None:
        alias = session.get(TicketAlias, reference)
        ticket = session.get(Ticket, alias.ticket_id) if alias is not None else None
    return ticket


def linked_tickets(session: Session, parent: Ticket) -> list[Ticket]:
    return list(
        session.exec(
            select(Ticket).where(Ticket.linked_parent_id == parent.id).order_by(Ticket.created_at, Ticket.id)
        ).all()
    )


def link_ticket(session: Session, ticket: Ticket, parent: Ticket, actor_email: str) -> None:
    if parent.id == ticket.id:
        raise LinkError("A ticket cannot be linked to itself")
    if parent.type not in PARENT_TYPES:
        raise LinkError("Only a Major Incident or a Problem can be a parent")
    if parent.triaged_at is None:
        raise LinkError("Triage the parent first")
    if parent.linked_parent_id is not None:
        raise LinkError("That ticket is itself linked to another, so it cannot be a parent")
    if linked_tickets(session, ticket):
        raise LinkError("This ticket has tickets linked to it, so it cannot be linked under another")
    if ticket.linked_parent_id == parent.id:
        raise LinkError(f"Already linked to {parent.reference}")
    if ticket.linked_parent_id is not None:
        raise LinkError("Unlink it from its current parent first")

    ticket.linked_parent_id = parent.id
    session.add(ticket)
    record_event(session, ticket, EVENT_LINKED, actor_email, to_value=parent.reference)
    record_event(session, parent, EVENT_CHILD_LINKED, actor_email, to_value=ticket.reference)


def unlink_ticket(session: Session, ticket: Ticket, actor_email: str) -> Optional[Ticket]:
    """Removes the link and returns the parent it had, or None if there was none."""
    if ticket.linked_parent_id is None:
        return None
    parent = session.get(Ticket, ticket.linked_parent_id)
    ticket.linked_parent_id = None
    session.add(ticket)
    record_event(session, ticket, EVENT_UNLINKED, actor_email, from_value=parent.reference if parent else None)
    if parent is not None:
        record_event(session, parent, EVENT_CHILD_UNLINKED, actor_email, from_value=ticket.reference)
    return parent

"""Linking tickets to a parent Major Incident or Problem. Nothing here commits.

The link is one level deep: only a Major Incident or Problem can be a parent, a parent
is never itself linked to anything, and a ticket with linked tickets cannot be linked
under another. Each side gets an event, so both timelines say what happened. Who may
link is decided by the route (work access on the ticket, read access on the parent);
who may *see* a link is decided by visibility (a viewer sees only the linked tickets
they could open anyway)."""

from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import FINISHED_STATUSES, Ticket, TicketAlias, TicketStatus, TicketType
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


MAX_LISTED = 50


def has_linked_tickets(session: Session, parent: Ticket) -> bool:
    return session.exec(select(Ticket.id).where(Ticket.linked_parent_id == parent.id).limit(1)).first() is not None


def linked_summary(session: Session, parent: Ticket, visible_condition) -> tuple[list[Ticket], int, int]:
    """The linked tickets one viewer may see (at most MAX_LISTED, oldest first), how many
    are linked in all, and how many of those the viewer could open. Counted in SQL, so a
    parent with hundreds of linked tickets costs two counts and one small query.
    isible_condition is visibility.visible_ticket_condition (None means no filter)."""
    base = Ticket.linked_parent_id == parent.id
    visible = base if visible_condition is None else (base & visible_condition)
    total = session.exec(select(func.count()).select_from(Ticket).where(base)).one()
    visible_count = session.exec(select(func.count()).select_from(Ticket).where(visible)).one()
    items = session.exec(select(Ticket).where(visible).order_by(Ticket.created_at, Ticket.id).limit(MAX_LISTED)).all()
    return list(items), total, visible_count


def link_ticket(session: Session, ticket: Ticket, parent: Ticket, actor_email: str) -> None:
    if parent.id == ticket.id:
        raise LinkError("A ticket cannot be linked to itself")
    # Lock both rows, in id order so two requests cannot deadlock, and re-read them: two
    # links made at once (A under B while B goes under C) would otherwise each pass the
    # checks below and leave a chain or a cycle.
    session.exec(
        select(Ticket.id).where(Ticket.id.in_([ticket.id, parent.id])).order_by(Ticket.id).with_for_update()
    ).all()
    session.refresh(ticket)
    session.refresh(parent)
    if ticket.triaged_at is None:
        raise LinkError("Triage this ticket before linking it")
    if ticket.status in FINISHED_STATUSES:
        raise LinkError("A resolved or closed ticket cannot be linked")
    if parent.status == TicketStatus.CLOSED:
        raise LinkError("That parent is closed")
    if parent.type not in PARENT_TYPES:
        raise LinkError("Only a Major Incident or a Problem can be a parent")
    if parent.triaged_at is None:
        raise LinkError("Triage the parent first")
    if parent.linked_parent_id is not None:
        raise LinkError("That ticket is itself linked to another, so it cannot be a parent")
    if has_linked_tickets(session, ticket):
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

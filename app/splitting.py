"""Splitting one request into sub-tickets. Nothing here commits.

An agent splits a triaged, unfinished ticket into parts, each with its own subject and
details. Every part is a real ticket (own number, type, category, assignee, status and
SLA) made by triaging a new ticket with the original's type, category, impact and urgency,
which the agent can then amend with the usual controls. It keeps the original's requester
and source. It is one level only: a sub-ticket is never split again. Who may split is
decided by the route (work access on the original); who may *see* the parts is decided by
visibility, as for links.

The original's derived status and the single mail thread come in the next part."""

from sqlalchemy import func
from sqlmodel import Session, select

from app.messages import add_note
from app.models import FINISHED_STATUSES, Category, Ticket, User
from app.sla import Level
from app.ticket_service import create_ticket, record_event
from app.triage import TriageError, triage_ticket

EVENT_SPLIT = "split"  # on the original: to_value is the new sub-ticket
EVENT_SPLIT_FROM = "split_from"  # on the sub-ticket: to_value is the original

MIN_PARTS = 2  # a split leaves at least two sub-tickets in all
MAX_PARTS = 10  # in all, across every split of the same ticket
MAX_LISTED = 50


class SplitError(ValueError):
    """A reason a ticket cannot be split, worded for the agent."""


def sub_ticket_count(session: Session, original: Ticket) -> int:
    return session.exec(select(func.count()).select_from(Ticket).where(Ticket.split_from_id == original.id)).one()


def sub_tickets_summary(session: Session, original: Ticket, visible_condition) -> tuple[list[Ticket], int, int]:
    """The sub-tickets one viewer may see (at most MAX_LISTED, in the order they were made),
    how many there are in all, and how many of those the viewer could open.
    visible_condition is visibility.visible_ticket_condition (None means no filter)."""
    base = Ticket.split_from_id == original.id
    visible = base if visible_condition is None else (base & visible_condition)
    total = session.exec(select(func.count()).select_from(Ticket).where(base)).one()
    visible_count = session.exec(select(func.count()).select_from(Ticket).where(visible)).one()
    items = session.exec(select(Ticket).where(visible).order_by(Ticket.id).limit(MAX_LISTED)).all()
    return list(items), total, visible_count


def split_ticket(
    session: Session, original: Ticket, actor: User, parts: list[tuple[str, str]]
) -> list[Ticket]:
    """Makes one sub-ticket per (subject, details) part. Blank-subject parts are the
    caller's to drop; here every subject must be filled in."""
    # Lock the original and re-read it so two splits at once cannot go past the limit.
    session.exec(select(Ticket.id).where(Ticket.id == original.id).with_for_update()).all()
    session.refresh(original)

    if original.split_from_id is not None:
        raise SplitError("This ticket is itself a sub-ticket, so it cannot be split again")
    if original.triaged_at is None:
        raise SplitError("Triage this ticket before splitting it")
    if original.status in FINISHED_STATUSES:
        raise SplitError("A resolved or closed ticket cannot be split")
    category = session.get(Category, original.category_id) if original.category_id else None
    if category is None or original.impact is None or original.urgency is None:
        raise SplitError("This ticket has no category, impact and urgency to give its parts")
    subjects = [(subject or "").strip() for subject, _ in parts]
    if any(not subject for subject in subjects):
        raise SplitError("Each part needs a subject")

    existing = sub_ticket_count(session, original)
    if existing + len(parts) < MIN_PARTS:
        raise SplitError(f"Split into at least {MIN_PARTS} parts")
    if existing + len(parts) > MAX_PARTS:
        raise SplitError(f"A ticket can have at most {MAX_PARTS} sub-tickets")

    made = []
    for subject, (_, details) in zip(subjects, parts):
        sub = create_ticket(
            session,
            subject=subject,
            requester_email=original.requester_email,
            source=original.source,
            ticket_type=original.type,
            team_id=original.team_id,
            actor_email=actor.email,
        )
        sub.split_from_id = original.id
        session.add(sub)
        try:
            triage_ticket(
                session, sub, actor,
                ticket_type=original.type, category=category,
                impact=Level(original.impact), urgency=Level(original.urgency),
            )
        except TriageError as error:
            raise SplitError(str(error))
        if (details or "").strip():
            add_note(session, sub, actor, details.strip())
        record_event(session, sub, EVENT_SPLIT_FROM, actor.email, to_value=original.tag)
        record_event(session, original, EVENT_SPLIT, actor.email, to_value=sub.tag)
        made.append(sub)
    return made

"""Splitting one request into sub-tickets. Nothing here commits.

An agent splits a triaged, unfinished ticket into parts, each with its own subject and
details. Every part is a real ticket (own number, type, category, assignee, status and
SLA) made by triaging a new ticket with the original's type, category, impact and urgency,
which the agent can then amend with the usual controls. It keeps the original's requester
and source. It is one level only: a sub-ticket is never split again. Who may split is
decided by the route (work access on the original); who may *see* the parts is decided by
visibility, as for links.

While it has sub-tickets the original's status is derived from them, not set by hand
(derived_status, sync_original_status). What the requester sees as one mail thread, and
which messages show on which ticket, is decided in app/messages.py."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.messages import add_note
from app.models import FINISHED_STATUSES, Category, Ticket, TicketStatus, User
from app.sla import Level, recalculate_deadlines
from app.ticket_service import create_ticket, record_event, set_status
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


def derived_status(statuses: list[TicketStatus]) -> Optional[TicketStatus]:
    """The status the original takes from its sub-tickets' statuses, or None for "leave it".
    All the same: that status. Exactly one still unfinished: that one's. Otherwise In
    progress. Resolved and Closed are never derived: when every part is finished the
    original stays as it is and an agent resolves it, which sends the one resolution
    email, so the requester is told it is done."""
    if not statuses:
        return None
    unfinished = [status for status in statuses if status not in FINISHED_STATUSES]
    if not unfinished:
        return None
    if len(set(statuses)) == 1:
        return statuses[0]
    if len(unfinished) == 1:
        return unfinished[0]
    return TicketStatus.IN_PROGRESS


def lock_ticket(session: Session, ticket_id: int) -> None:
    """Row-locks a ticket until the transaction ends. Everything that touches a family
    takes the original's lock before any part's, so two requests cannot wait on each other."""
    session.exec(select(Ticket.id).where(Ticket.id == ticket_id).with_for_update()).all()


def sync_original_status(session: Session, original_id: int) -> bool:
    """Re-derives the original's status from its sub-tickets and records a system event
    when it changes. Returns whether it changed. A finished or untriaged original is left alone."""
    # Lock the original and read the statuses after, so two parts changing at once end with
    # the last one seeing the other's change.
    lock_ticket(session, original_id)
    original = session.get(Ticket, original_id)
    if original is None:
        return False
    session.refresh(original)
    if original.status in FINISHED_STATUSES or original.triaged_at is None:
        return False
    statuses = list(session.exec(select(Ticket.status).where(Ticket.split_from_id == original_id)).all())
    target = derived_status(statuses)
    if target is None and statuses and original.status == TicketStatus.AWAITING_USER:
        # Every part is finished, so nothing is waiting on the requester: the SLA clock
        # restarts and the original waits for an agent to resolve it.
        target = TicketStatus.IN_PROGRESS
    if target is None or target == original.status:
        return False
    return set_status(session, original, target, None, detail="from its sub-tickets")


def has_sub_tickets(session: Session, original: Ticket) -> bool:
    return session.exec(select(Ticket.id).where(Ticket.split_from_id == original.id).limit(1)).first() is not None


def unfinished_sub_ticket_count(session: Session, original: Ticket) -> int:
    return session.exec(
        select(func.count()).select_from(Ticket).where(
            Ticket.split_from_id == original.id, Ticket.status.not_in(FINISHED_STATUSES)
        )
    ).one()


def all_sub_tickets_finished(session: Session, original: Ticket) -> bool:
    """True when it has sub-tickets and every one is resolved or closed."""
    return has_sub_tickets(session, original) and unfinished_sub_ticket_count(session, original) == 0


def check_manual_status(session: Session, ticket: Ticket, new_status: TicketStatus) -> None:
    """An original's status follows its sub-tickets, so an agent cannot set it by hand
    except to finish it, and only once every sub-ticket is finished."""
    if not has_sub_tickets(session, ticket):
        return
    if new_status not in FINISHED_STATUSES:
        raise SplitError("This ticket has sub-tickets, so its status follows them")
    open_parts = unfinished_sub_ticket_count(session, ticket)
    if open_parts:
        raise SplitError(f"Finish its sub-tickets first ({open_parts} still open)")


def refresh_open_sub_tickets(session: Session, original: Ticket, actor_email: str) -> None:
    """The requester replied on the original, and the system cannot tell which part the
    reply is about, so every open sub-ticket is refreshed: it moves up the queue, and one
    waiting on the requester goes back to In progress (which resumes its SLA clock)."""
    lock_ticket(session, original.id)  # the original first, as set_status on a part does
    now = datetime.now(timezone.utc)
    parts = session.exec(
        select(Ticket).where(Ticket.split_from_id == original.id, Ticket.status.not_in(FINISHED_STATUSES))
    ).all()
    for part in parts:
        part.updated_at = now
        session.add(part)
        if part.status == TicketStatus.AWAITING_USER:
            set_status(session, part, TicketStatus.IN_PROGRESS, actor_email, detail="requester replied")


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

    if not parts:
        raise SplitError("Enter a subject for at least one part")
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
            created_by_id=original.created_by_id,
        )
        try:
            triage_ticket(
                session, sub, actor,
                ticket_type=original.type, category=category,
                impact=Level(original.impact), urgency=Level(original.urgency),
            )
        except TriageError as error:
            # The parts take this ticket's category, so the cause is the original's.
            raise SplitError(f"The parts cannot take this ticket's category. {error}")
        # Linked after triage, so triaging the part does not re-derive the original's status
        # once per part; it is derived once, below, when every part exists.
        sub.split_from_id = original.id
        session.add(sub)
        if original.is_backdated:
            # A ticket logged after the fact has no SLA, and neither do its parts.
            sub.is_backdated = True
            recalculate_deadlines(session, sub)
        if (details or "").strip():
            add_note(session, sub, actor, details.strip())
        record_event(session, sub, EVENT_SPLIT_FROM, actor.email, to_value=original.tag)
        record_event(session, original, EVENT_SPLIT, actor.email, to_value=sub.tag)
        made.append(sub)
    original.updated_at = datetime.now(timezone.utc)
    session.add(original)
    sync_original_status(session, original.id)
    return made

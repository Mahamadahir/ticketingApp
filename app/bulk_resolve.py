"""Resolving several tickets at once with one note (phase 4I). Nothing here commits.

The agent picks tickets (or a Major Incident / Problem with the tickets linked under it), writes one
resolution note, sees ONE preview listing every ticket and who would be emailed, then confirms. Each
ticket goes through the 4D helper (`app/resolution.py`), so it gets its own status event, audit entry
and resolution email to its own requester (copying its followers, the same rule as a single resolve).

- Only tickets the agent can WORK are resolved (`app/visibility.py`). A ticket the agent cannot work,
  or that does not exist, is reported with the same wording, so a number reveals nothing.
- A ticket that cannot be resolved (already finished, awaiting approval, untriaged, a split original
  with unfinished parts) is skipped with the reason; the rest go ahead. Each ticket runs in a savepoint,
  so a refusal part-way through one ticket leaves nothing of it behind. The result is mixed, not all-or-nothing.
- Tickets are handled in a fixed order (family by family, parts before their original, ascending id),
  which agrees with the locks `resolve_ticket` takes (the original first), so two batches cannot deadlock.
"""

from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select

from app.messages import EmailPreview, UnknownRecipient
from app.models import Ticket, User
from app.resolution import ResolveError, clean_notes, preview_resolution, resolve_ticket
from app.settings_store import BULK_RESOLVE_MAX, count_setting
from app.splitting import SplitError
from app.ticket_service import get_by_number
from app.visibility import Access, AccessContext, ticket_access

NOT_WORKABLE = "Not found, or you cannot work this ticket"


class BulkError(ValueError):
    """The batch as a whole cannot go ahead, worded for the agent."""


@dataclass
class BulkItem:
    number: int
    reference: str = ""
    subject: str = ""
    preview: Optional[EmailPreview] = None  # set when the ticket can be resolved
    skipped: Optional[str] = None  # the reason, when it cannot
    resolved: bool = False


@dataclass
class BulkPlan:
    items: list[BulkItem] = field(default_factory=list)

    @property
    def ready(self) -> list[BulkItem]:
        return [i for i in self.items if i.skipped is None]

    @property
    def skipped(self) -> list[BulkItem]:
        return [i for i in self.items if i.skipped is not None]


def linked_numbers(session: Session, parent_number: int) -> list[int]:
    """The parent's own number and the numbers of the tickets linked under it. No access check:
    what is listed is checked per ticket by the caller."""
    parent = get_by_number(session, parent_number)
    if parent is None:
        return []
    children = session.exec(select(Ticket.number).where(Ticket.linked_parent_id == parent.id)).all()
    return [parent.number, *children]


def _lock_order(ticket: Ticket) -> tuple[int, int, int]:
    # Family root first (the original's id for a part), parts before their original, then id.
    root = ticket.split_from_id if ticket.split_from_id is not None else ticket.id
    return (root, 1 if ticket.split_from_id is None else 0, ticket.id)


def _load(session: Session, user: User, levels: AccessContext, numbers: list[int]) -> list[tuple[int, Optional[Ticket]]]:
    """(number, ticket) for each distinct number; ticket is None when it is missing or not workable."""
    seen: list[int] = []
    for number in numbers:
        if number not in seen:
            seen.append(number)
    found: list[tuple[int, Optional[Ticket]]] = []
    for number in seen:
        ticket = get_by_number(session, number)
        if ticket is not None and ticket_access(user, ticket, levels) != Access.WORK:
            ticket = None
        found.append((number, ticket))
    # Workable tickets in lock order; the ones that cannot be opened go last.
    found.sort(key=lambda pair: (pair[1] is None, _lock_order(pair[1]) if pair[1] else (0, 0, pair[0])))
    return found


def check_batch(session: Session, numbers: list[int], notes: str) -> str:
    """Validates the size of the batch and the note; returns the cleaned note. Raises BulkError."""
    if not numbers:
        raise BulkError("Choose at least one ticket")
    limit = count_setting(session, BULK_RESOLVE_MAX)
    if len(set(numbers)) > limit:
        raise BulkError(f"At most {limit} tickets can be resolved at once")
    try:
        return clean_notes(notes)
    except ResolveError as error:
        raise BulkError(str(error))


def preview_bulk(session: Session, user: User, levels: AccessContext, numbers: list[int], notes: str) -> BulkPlan:
    """What resolving would do for each ticket: who is emailed, or why it is skipped. Records nothing."""
    notes = check_batch(session, numbers, notes)
    plan = BulkPlan()
    for number, ticket in _load(session, user, levels, numbers):
        item = BulkItem(number=number)
        plan.items.append(item)
        if ticket is None:
            item.skipped = NOT_WORKABLE
            continue
        item.reference, item.subject = ticket.reference, ticket.subject
        try:
            item.preview = preview_resolution(session, ticket, notes)
        except (ResolveError, UnknownRecipient) as error:
            item.skipped = str(error)
    return plan


def resolve_bulk(session: Session, user: User, levels: AccessContext, numbers: list[int], notes: str) -> BulkPlan:
    """Resolves every ticket that still can be, each in its own savepoint; the rest are reported with
    their reason. The caller commits. Raises BulkError only for a bad batch or note."""
    notes = check_batch(session, numbers, notes)
    plan = BulkPlan()
    for number, ticket in _load(session, user, levels, numbers):
        item = BulkItem(number=number)
        plan.items.append(item)
        if ticket is None:
            item.skipped = NOT_WORKABLE
            continue
        item.reference, item.subject = ticket.reference, ticket.subject
        savepoint = session.begin_nested()
        try:
            resolve_ticket(session, ticket, user, notes, detail=f"Resolved with {len(numbers)} tickets in one go"
                           if len(numbers) > 1 else None)
        except (ResolveError, SplitError, UnknownRecipient) as error:
            savepoint.rollback()
            item.skipped = str(error)
        else:
            savepoint.commit()
            item.resolved = True
    return plan

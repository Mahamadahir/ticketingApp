"""What an agent does about the MSP besides sending a ticket (issue #150). Nothing here commits.

Three things, all built on app/msp.py:

1. Linked tickets go With MSP with their parent. When a Major Incident or Problem is sent to the MSP the panel offers
   to mark the tickets linked under it With MSP too (`linked_to_mark`, `mark_linked`) and to tell their requesters with
   the template `msp_sent_notice`. A linked ticket changes status only: nothing is sent to the MSP for it, and it
   gets the event "with the MSP through INC-1042". Finished tickets and tickets already With MSP are left alone, and so
   (technical limits) are tickets awaiting approval, untriaged, split into parts, or in a team the agent cannot work.
   The automatic handoff never touches linked tickets: nobody is there to tick the boxes, and each linked ticket is
   handed off by itself if it is due.

2. Telling the MSP when IT finishes with it. A ticket the MSP may still hold (`msp_may_hold`: it was sent to them and
   they have not been told since) that an agent resolves, closes or takes back gets a short email from the template
   `msp_no_longer_needed`, in the handoff's subject form so their system files it on their ticket. The previews of
   resolve, close and take back show it with a tick box, ticked by default (`compose_release`); the action's `tell_msp`
   is what was ticked. How "the MSP resolved it itself" is known: it is not, so the agent unticks the box; the
   unticking is recorded too, so the same ticket is not offered again until it goes to the MSP again.
   Nothing is offered when MAIL_SENDING is off or there is no usable MSP address.

3. Take back from MSP (`take_back`): With MSP moves to In progress (assigned) or Open, with an event and an audit entry.

Follow-through for linked tickets: when a parent is resolved, closed or taken back, the linked tickets still With MSP
through it (`linked_with_msp_through`) are offered back to Open or In progress, ticked by default. Resolving them stays
what it already was: the existing bulk resolve of a parent and its linked tickets (app/bulk_resolve.py).
"""

import hashlib
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app.audit import log_action
from app.email_templates import render_template, ticket_values
from app.linking import PARENT_TYPES
from app.messages import mail_subject, record_notice
from app.models import FINISHED_STATUSES, OutboundEmail, Ticket, TicketEvent, TicketStatus, User
from app.msp import (
    EVENT_MSP_NOT_TOLD,
    EVENT_MSP_TOLD,
    EVENT_SENT_TO_MSP,
    SCOPE_THREAD,
    MspError,
    MspPreview,
    hold_on_take_back,
    msp_cc,
    send_to_msp,
    usable_msp_address,
)
from app.outbox import MSP_HANDOFF, is_on, queue_msp_release
from app.splitting import SplitError, check_manual_status, has_sub_tickets, lock_ticket
from app.ticket_service import record_event, set_status
from app.visibility import Access, access_context, ticket_access

EVENT_WITH_MSP_THROUGH = "with_msp_through"  # on a linked ticket: to_value is the parent's reference
EVENT_TAKEN_BACK = "taken_back_from_msp"  # to_value: the status it went to; detail says why (a parent's, say)
EVENT_LINKED_TAKEN_BACK = "linked_taken_back_from_msp"  # on the parent: to_value is how many

AUDIT_TAKEN_BACK = "msp_taken_back"
AUDIT_MSP_TOLD = "msp_told_no_longer_needed"

OUTCOME_RESOLVED = "resolved"
OUTCOME_CLOSED = "closed"
OUTCOME_TAKEN_BACK = "taken_back"
OUTCOME_TEXT = {
    OUTCOME_RESOLVED: "resolved by IT",
    OUTCOME_CLOSED: "closed by IT",
    OUTCOME_TAKEN_BACK: "taken back by IT",
}


# ---- Linked tickets go With MSP with their parent ----------------------------------------------------------


@dataclass
class LinkedPlan:
    tickets: list[Ticket]  # what would be marked With MSP, in ticket order
    left_alone: int = 0  # linked, not finished and not With MSP, but ones the agent cannot work (or that cannot take it)

    @property
    def ids(self) -> list[int]:
        return [t.id for t in self.tickets]

    @property
    def digest(self) -> str:
        return ids_digest(self.ids)


def ids_digest(ids: list[int]) -> str:
    """A short fingerprint of the linked tickets a preview showed (a parent can have hundreds), carried by the confirm form."""
    return hashlib.sha256(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()


def _lock_originals(session: Session, tickets: list[Ticket]) -> None:
    """The original of every split part first, in id order, as resolving does, so two requests cannot wait on each other."""
    for original_id in sorted({t.split_from_id for t in tickets if t.split_from_id is not None}):
        lock_ticket(session, original_id)


def _can_take_msp(session: Session, child: Ticket) -> bool:
    return (
        child.triaged_at is not None
        and child.status not in FINISHED_STATUSES
        and child.status not in (TicketStatus.WITH_MSP, TicketStatus.AWAITING_APPROVAL)
        and not has_sub_tickets(session, child)  # a split original's status follows its parts
    )


def _linked_children(session: Session, parent: Ticket, lock: bool = False) -> list[Ticket]:
    query = select(Ticket).where(Ticket.linked_parent_id == parent.id).order_by(Ticket.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(session.exec(query).all())


def linked_to_mark(session: Session, parent: Ticket, actor: User, lock: bool = False) -> LinkedPlan:
    """The linked tickets sending `parent` to the MSP would also mark With MSP. Empty unless the parent is a Major
    Incident or Problem. Access is the agent's own (app/visibility.py): a ticket they cannot work is counted, not marked."""
    if parent.type not in PARENT_TYPES:
        return LinkedPlan([])
    levels = access_context(session, actor)
    plan = LinkedPlan([])
    for child in _linked_children(session, parent, lock):
        if child.status in FINISHED_STATUSES or child.status == TicketStatus.WITH_MSP:
            continue
        if _can_take_msp(session, child) and ticket_access(actor, child, levels) == Access.WORK:
            plan.tickets.append(child)
        else:
            plan.left_alone += 1
    return plan


def mark_linked(
    session: Session, parent: Ticket, actor: User, *, tell_requesters: bool, expect_digest: Optional[str] = None
) -> list[Ticket]:
    """Marks the linked tickets With MSP through the parent (call it with the parent already sent). Each changes status only:
    nothing is emailed to the MSP for it. `expect_digest` is the fingerprint of the list the preview showed: if it is not the list now, nothing is
    changed and MspError says to preview again."""
    _lock_originals(session, _linked_children(session, parent))
    plan = linked_to_mark(session, parent, actor, lock=True)
    if expect_digest is not None and plan.digest != expect_digest:
        raise MspError("The linked tickets changed since the preview. Preview it again to see which will be marked.")
    for child in plan.tickets:
        set_status(session, child, TicketStatus.WITH_MSP, actor.email, detail=f"with the MSP through {parent.reference}")
        record_event(session, child, EVENT_WITH_MSP_THROUGH, actor.email, to_value=parent.reference)
        log_action(session, actor.email, "ticket", child.id, "with_msp_through", parent.reference)
        if tell_requesters:
            record_notice(session, child, render_template(session, "msp_sent_notice", ticket_values(child)).body)
    return plan.tickets


def send_with_linked(
    session: Session, ticket: Ticket, actor: User, reason: str, scope: str = SCOPE_THREAD, expect_digest: Optional[str] = None,
    *, mark_linked_too: bool = False, tell_requesters: bool = False, expect_linked_digest: Optional[str] = None,
) -> MspPreview:
    """The Send to MSP button: `send_to_msp`, and as ticked in the panel the linked tickets of a Major Incident or Problem
    marked With MSP with it. All or nothing: a refusal raises MspError before anything is changed."""
    preview = send_to_msp(session, ticket, actor, reason, scope, expect_digest=expect_digest)
    if mark_linked_too:
        mark_linked(session, ticket, actor, tell_requesters=tell_requesters, expect_digest=expect_linked_digest)
    return preview


# ---- Taking back --------------------------------------------------------------------------------------------


def _status_after_take_back(ticket: Ticket) -> TicketStatus:
    return TicketStatus.IN_PROGRESS if ticket.assigned_agent_id is not None else TicketStatus.OPEN


def linked_with_msp_through(session: Session, parent: Ticket, actor: User) -> list[Ticket]:
    """The tickets linked under `parent` that are still With MSP because the parent was sent (their latest MSP event is
    "with the MSP through"), and that the agent can work. Never a ticket sent to the MSP by itself."""
    if parent.type not in PARENT_TYPES:
        return []
    children = [c for c in _linked_children(session, parent) if c.status == TicketStatus.WITH_MSP]
    if not children:
        return []
    kinds = (EVENT_WITH_MSP_THROUGH, EVENT_SENT_TO_MSP, EVENT_TAKEN_BACK)
    latest: dict[int, str] = {}
    for event in session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id.in_([c.id for c in children]), TicketEvent.event_type.in_(kinds))
        .order_by(TicketEvent.id)
    ).all():
        latest[event.ticket_id] = event.event_type
    levels = access_context(session, actor)
    return [
        c for c in children
        if latest.get(c.id) == EVENT_WITH_MSP_THROUGH and ticket_access(actor, c, levels) == Access.WORK
        and not has_sub_tickets(session, c)
    ]


def _take_back_one(session: Session, ticket: Ticket, actor: User, why: str) -> None:
    status = _status_after_take_back(ticket)
    set_status(session, ticket, status, actor.email, detail=why)
    record_event(session, ticket, EVENT_TAKEN_BACK, actor.email, to_value=status.value, detail=why)
    hold_on_take_back(session, ticket, actor, why)  # so the automatic handoff does not send it straight back
    log_action(session, actor.email, "ticket", ticket.id, AUDIT_TAKEN_BACK, why[:200])


def take_back_linked(session: Session, parent: Ticket, actor: User, why: str) -> list[Ticket]:
    """Takes the linked tickets that are With MSP through the parent back to In progress or Open."""
    candidates = linked_with_msp_through(session, parent, actor)
    taken: list[Ticket] = []
    if candidates:
        _lock_originals(session, candidates)
        session.exec(select(Ticket.id).where(Ticket.id.in_([t.id for t in candidates])).order_by(Ticket.id).with_for_update()).all()
        for child in candidates:
            session.refresh(child)
            if child.status == TicketStatus.WITH_MSP:
                _take_back_one(session, child, actor, f"{why} ({parent.reference})")
                taken.append(child)
        if taken:
            record_event(session, parent, EVENT_LINKED_TAKEN_BACK, actor.email, to_value=str(len(taken)))
    return taken


def check_can_take_back(session: Session, ticket: Ticket) -> None:
    """Raises MspError saying why the ticket cannot be taken back from the MSP."""
    if ticket.status != TicketStatus.WITH_MSP:
        raise MspError("This ticket is not with the MSP")
    try:
        check_manual_status(session, ticket, _status_after_take_back(ticket))
    except SplitError as error:
        raise MspError(str(error))


def take_back(
    session: Session, ticket: Ticket, actor: User, *, tell_msp: Optional[bool] = None, take_back_linked_too: bool = False
) -> None:
    """Moves a With MSP ticket to In progress (assigned) or Open, recording an event and an audit entry, and (as ticked in
    the preview) tells the MSP it is no longer needed and takes back the linked tickets that went with it."""
    session.flush()
    if ticket.split_from_id is not None:
        lock_ticket(session, ticket.split_from_id)  # the original first, like everything in a family
    session.refresh(ticket, with_for_update=True)
    check_can_take_back(session, ticket)
    release_msp(session, ticket, actor, OUTCOME_TAKEN_BACK, tell_msp)
    if take_back_linked_too:
        take_back_linked(session, ticket, actor, "Parent taken back from the MSP")
    _take_back_one(session, ticket, actor, "taken back from the MSP")


# ---- Telling the MSP -----------------------------------------------------------------------------------------


@dataclass
class ReleasePreview:
    to: str
    cc: list[str]
    subject: str
    body: str
    outcome: str


def _settled(session: Session, ticket: Ticket) -> bool:
    """Whether someone already decided about telling the MSP since the ticket last went to them."""
    if ticket.msp_sent_at is None:
        return False
    latest = session.exec(
        select(TicketEvent.id).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.event_type.in_((EVENT_MSP_TOLD, EVENT_MSP_NOT_TOLD)),
            TicketEvent.created_at >= ticket.msp_sent_at,
        ).limit(1)
    ).first()
    return latest is not None


def _last_handoff(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    return session.exec(
        select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == MSP_HANDOFF).order_by(OutboundEmail.id.desc()).limit(1)
    ).first()


def msp_may_hold(session: Session, ticket: Ticket) -> bool:
    """Whether the MSP may still be working on this ticket as far as we know: its handoff email was actually sent (a ticket merely
    set to With MSP by hand, marked through its parent, or whose handoff has not gone yet or was never queued, is not one they
    know of) and nobody has settled it since."""
    if ticket.msp_sent_at is None or ticket.status in FINISHED_STATUSES or _settled(session, ticket):
        return False
    handoff = _last_handoff(session, ticket)
    return handoff is not None and handoff.sent_at is not None


def compose_release(session: Session, ticket: Ticket, outcome: str, actor: User) -> Optional[ReleasePreview]:
    """The "no longer needed" email as it would go, or None when there is nothing to offer: the MSP may not hold the ticket,
    there is no usable MSP address, or mail is switched off. Changes nothing. The subject is the handoff's, so replies and
    the MSP's own system find the ticket."""
    if outcome not in OUTCOME_TEXT or not is_on() or not msp_may_hold(session, ticket):
        return None
    handoff = _last_handoff(session, ticket)
    address = (handoff.to_address.strip().lower() if handoff is not None else "") or usable_msp_address(session)
    if not address or not usable_msp_address(session):
        return None  # the MSP they were sent to is told even if the setting was changed since; no MSP set at all, nothing is offered
    number = ticket.msp_ticket_number
    body = render_template(session, "msp_no_longer_needed", {
        **ticket_values(ticket), "outcome": OUTCOME_TEXT[outcome], "msp_ticket_text": f" (MSP ticket #{number})" if number else "",
    }).body
    return ReleasePreview(to=address, cc=msp_cc(session, ticket, address, actor), subject=mail_subject(session, ticket), body=body, outcome=outcome)


def release_msp(session: Session, ticket: Ticket, actor: User, outcome: str, tell: Optional[bool]) -> bool:
    """The agent's answer to the tick box, for a ticket they are resolving, closing or taking back (call it before the status
    changes). `tell` True is a ticked box: queues the email and records it. False is a box that was shown and unticked: records that
    the MSP was not told, so the ticket is not offered again. None is a caller that never showed the box: nothing happens. Nothing
    either when there was nothing to offer. True when the MSP was told."""
    if tell is None:
        return False
    preview = compose_release(session, ticket, outcome, actor)
    if preview is None:
        return False
    if not tell:
        record_event(session, ticket, EVENT_MSP_NOT_TOLD, actor.email, to_value=outcome)
        return False
    queue_msp_release(session, ticket, preview.to, preview.cc, preview.subject, preview.body)
    record_event(session, ticket, EVENT_MSP_TOLD, actor.email, to_value=outcome, detail=preview.to)
    log_action(session, actor.email, "ticket", ticket.id, AUDIT_MSP_TOLD, outcome)
    return True


def finish_with_msp(
    session: Session, ticket: Ticket, actor: Optional[User], outcome: str, *, tell_msp: Optional[bool], take_back_linked_too: bool
) -> None:
    """What resolving or closing does about the MSP, as ticked in the preview (call it before the status changes). No
    agent (an automatic resolve) does nothing: the MSP is told by people, never by a job."""
    if actor is None:
        return
    release_msp(session, ticket, actor, outcome, tell_msp)
    if take_back_linked_too:
        take_back_linked(session, ticket, actor, f"Parent {outcome}")

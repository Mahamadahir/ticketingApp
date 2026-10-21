"""Reopening, reclosing and replying after a ticket is finished (phase 4E-1). Nothing here commits.

What happens to a finished ticket:

- Resolved: it can be reopened. `reopen_ticket` is the one place that does it: the agent's Reopen
  action, the status buttons on a Resolved ticket, the requester's Reopen button in the portal and a
  reply (portal or mail, through `app.splitting.apply_requester_reply`). The ticket goes back to In
  progress when someone is assigned and Open otherwise (or the status the agent picked), `reopen_count`
  goes up, a "reopened" event records the reason, and the assigned agent is told (`notify_reopened`).
  A reply that reopens does not send that second notification: the agent has already been told of the
  reply (`record_inbound`).
- Closed: it stays closed. A reply after closure opens a new ticket by the existing paths (source
  email or portal) with a related-ticket link to the old one (`open_follow_on_ticket`), shown on both
  ticket pages (`related_tickets`). It is a plain link, not a parent and child (that is
  app/linking.py). Later mail in the same thread follows the link to the newest ticket
  (`latest_follow_on`), so each further reply does not open yet another ticket.
- Reclose: an agent closes a reopened ticket again with a reason (`reclose_ticket`): the user only
  said thanks, a duplicate, the new issue was raised separately, or it was reopened in error. 4E-2
  adds the thank-you review list for the first of these.

A split original follows its parts (app/splitting.py): reopening a resolved original reopens each of its
Resolved parts, and reopening a Resolved part also reopens a Resolved original, whose status is then
derived from its parts again.
"""

from typing import Optional

from sqlmodel import Session, select

from app.audit import log_action
from app.followers import InvalidEmail, NeverFollowListed, TooManyFollowers, add_follower
from app.messages import STATUS_CHOICES
from app.models import FINISHED_STATUSES, RelatedTicket, Ticket, TicketSource, TicketStatus
from app.notifications import notify_reopened, notify_reply
from app.ticket_service import create_ticket, record_event, set_status

EVENT_REOPENED = "reopened"  # detail: the reason
EVENT_RECLOSED = "reclosed"  # to_value: the reason, detail: its wording
EVENT_FOLLOWS = "follows"  # on the new ticket: to_value is the earlier (closed) ticket
EVENT_FOLLOWED_BY = "followed_by"  # on the closed ticket: to_value is the new ticket

AUDIT_REOPENED = "reopened"
AUDIT_RECLOSED = "reclosed"

# The reasons an agent gives for closing a reopened ticket again, in the order the form offers them.
RECLOSE_REASONS: dict[str, str] = {
    "said_thanks": "User said thanks",
    "duplicate": "Duplicate",
    "raised_separately": "New issue raised separately",
    "reopened_in_error": "Reopened in error",
}

MAX_REASON_LENGTH = 1000
MAX_RELATED = 50
MAX_CHAIN = 25  # how many follow-on tickets latest_follow_on will step through


class ReopenError(ValueError):
    """A reason the ticket cannot be reopened or reclosed, worded for whoever asked."""


def _default_status(ticket: Ticket) -> TicketStatus:
    return TicketStatus.IN_PROGRESS if ticket.assigned_agent_id is not None else TicketStatus.OPEN


def _clean_reason(reason: Optional[str]) -> str:
    reason = (reason or "").replace("\r\n", "\n").strip()
    if not reason:
        raise ReopenError("Say why the ticket is being reopened")
    if len(reason) > MAX_REASON_LENGTH:
        raise ReopenError(f"The reason can be at most {MAX_REASON_LENGTH} characters")
    return reason


def check_can_reopen(ticket: Ticket) -> None:
    if ticket.status == TicketStatus.CLOSED:
        raise ReopenError("A closed ticket cannot be reopened: a reply to it opens a new ticket linked to this one")
    if ticket.status != TicketStatus.RESOLVED:
        raise ReopenError("Only a resolved ticket can be reopened")
    if ticket.triaged_at is None:
        raise ReopenError("Triage the ticket first")


def _reopen_one(
    session: Session, ticket: Ticket, actor_email: Optional[str], reason: str, target: TicketStatus,
    tell_assignee: bool, already_told: Optional[int],
) -> None:
    set_status(session, ticket, target, actor_email, detail="reopened")
    ticket.reopen_count += 1
    session.add(ticket)
    record_event(session, ticket, EVENT_REOPENED, actor_email, detail=reason)
    log_action(session, actor_email or "system", "ticket", ticket.id, AUDIT_REOPENED, reason[:200])
    if tell_assignee and ticket.id != already_told:
        notify_reopened(session, ticket, actor_email, reason)


def reopen_ticket(
    session: Session,
    ticket: Ticket,
    actor_email: Optional[str],
    reason: str,
    *,
    status: Optional[TicketStatus] = None,
    tell_assignee: bool = True,
    already_told: Optional[int] = None,
) -> TicketStatus:
    """Reopens a Resolved ticket and returns the status it went to. `status` is the status an agent picked
    (In progress when someone is assigned and Open otherwise, if not given); it is not used for a split
    original, whose status is derived from its parts. tell_assignee is False to tell nobody; already_told is
    the id of a ticket whose assignee a reply has just been recorded on (they were told of the reply, so not
    again), while the other tickets reopened with it are still announced. Raises ReopenError before anything changes."""
    # Imported here because app.splitting calls this module for a requester's reply.
    from app.splitting import derived_status, has_sub_tickets, lock_ticket

    reason = _clean_reason(reason)
    if ticket.split_from_id is not None:
        lock_ticket(session, ticket.split_from_id)  # the original first, like everything in a family
    session.flush()
    session.refresh(ticket, with_for_update=True)  # two reopens at once: the second finds it open and is refused
    check_can_reopen(ticket)
    if status is not None and status not in STATUS_CHOICES:
        raise ReopenError("A reopened ticket goes back to an open status")

    def derived_or_default(original: Ticket) -> TicketStatus:
        statuses = list(session.exec(select(Ticket.status).where(Ticket.split_from_id == original.id)).all())
        return derived_status(statuses) or _default_status(original)

    if has_sub_tickets(session, ticket):
        parts = session.exec(
            select(Ticket).where(Ticket.split_from_id == ticket.id, Ticket.status == TicketStatus.RESOLVED).order_by(Ticket.id)
        ).all()
        for part in parts:
            _reopen_one(session, part, actor_email, reason, _default_status(part), tell_assignee, already_told)
        target = derived_or_default(ticket)
        _reopen_one(session, ticket, actor_email, reason, target, tell_assignee, already_told)
        return target

    target = status or _default_status(ticket)
    _reopen_one(session, ticket, actor_email, reason, target, tell_assignee, already_told)
    if ticket.split_from_id is not None:
        original = session.get(Ticket, ticket.split_from_id)
        if original is not None and original.status == TicketStatus.RESOLVED:
            # A finished original does not follow its parts, so it is reopened with the part.
            _reopen_one(session, original, actor_email, reason, derived_or_default(original), tell_assignee, already_told)
    return target


def check_can_reclose(session: Session, ticket: Ticket) -> None:
    from app.splitting import SplitError, check_manual_status

    if ticket.reopen_count < 1:
        raise ReopenError("Only a ticket that was reopened can be reclosed")
    if ticket.status in FINISHED_STATUSES:
        raise ReopenError("This ticket is already resolved or closed")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise ReopenError("This request is waiting for its approvers")
    try:
        check_manual_status(session, ticket, TicketStatus.CLOSED)
    except SplitError as error:
        raise ReopenError(str(error))


def reclose_ticket(session: Session, ticket: Ticket, actor_email: str, reason_key: str) -> None:
    """Closes a reopened ticket again, with one of RECLOSE_REASONS. No email goes to the requester: it was
    already told it was resolved, and the reason is for the agents' own records (4E-2 reviews "said thanks")."""
    from app.splitting import lock_ticket

    label = RECLOSE_REASONS.get(reason_key)
    if label is None:
        raise ReopenError("Choose a reason from the list")
    if ticket.split_from_id is not None:
        lock_ticket(session, ticket.split_from_id)
    session.flush()
    session.refresh(ticket, with_for_update=True)
    check_can_reclose(session, ticket)
    set_status(session, ticket, TicketStatus.CLOSED, actor_email, detail=f"reclosed: {label}")
    record_event(session, ticket, EVENT_RECLOSED, actor_email, to_value=reason_key, detail=label)
    log_action(session, actor_email, "ticket", ticket.id, AUDIT_RECLOSED, label)


# ---- A reply after closure --------------------------------------------------------------------------


def open_follow_on_ticket(
    session: Session,
    closed: Ticket,
    *,
    subject: str,
    sender_email: str,
    source: TicketSource,
    actor_email: Optional[str],
) -> Ticket:
    """Opens the new ticket for a reply to a Closed one and links the two. Only the portal and mail paths
    call this (source portal or email; an agent-raised ticket is app/raising.py's alone). The requester is
    the closed ticket's, as the reply is part of their conversation; a follower (or a colleague in the
    thread) who wrote it follows the new ticket instead of becoming its requester. The caller records the
    message that started it."""
    if source == TicketSource.AGENT:
        raise ValueError("A follow-on ticket is raised by the requester, never by an agent")
    sender_email = sender_email.strip().lower()
    new = create_ticket(
        session, subject=subject, requester_email=closed.requester_email, source=source, actor_email=actor_email
    )
    if sender_email != new.requester_email:
        try:
            add_follower(session, new, sender_email, sender_email)
        except (InvalidEmail, TooManyFollowers, NeverFollowListed):
            pass
    session.add(RelatedTicket(ticket_id=new.id, related_ticket_id=closed.id))
    record_event(session, new, EVENT_FOLLOWS, actor_email, to_value=closed.tag, detail="Replied after the earlier ticket was closed")
    record_event(session, closed, EVENT_FOLLOWED_BY, actor_email, to_value=new.tag, detail="Replied after it was closed, so a new ticket was opened")
    log_action(session, actor_email or "system", "ticket", new.id, "follows_closed", closed.tag)
    notify_reply(session, closed, sender_email)  # the old ticket's assignee hears of the reply, as for any reply
    return new


def related_tickets(session: Session, ticket: Ticket) -> list[tuple[Ticket, str]]:
    """The tickets linked to this one by a reply after closure, oldest first, each with "earlier" (this
    ticket follows on from it) or "later" (it follows on from this one). Not filtered by who may open
    them: callers show a link only to someone who could open the other ticket (visibility, portal)."""
    earlier = session.exec(
        select(Ticket).join(RelatedTicket, RelatedTicket.related_ticket_id == Ticket.id)
        .where(RelatedTicket.ticket_id == ticket.id).order_by(Ticket.id).limit(MAX_RELATED)
    ).all()
    later = session.exec(
        select(Ticket).join(RelatedTicket, RelatedTicket.ticket_id == Ticket.id)
        .where(RelatedTicket.related_ticket_id == ticket.id).order_by(Ticket.id).limit(MAX_RELATED)
    ).all()
    return [(t, "earlier") for t in earlier] + [(t, "later") for t in later]


def latest_follow_on(session: Session, ticket: Ticket) -> Ticket:
    """The ticket a new message about this one belongs to. A Closed ticket's thread carries on in the
    ticket opened by a reply after closure (the newest, if there were several), and so on down the chain,
    so a conversation id or Message-ID that matches the old ticket does not open another ticket for each
    further reply. Any other ticket is returned as it is."""
    seen = {ticket.id}
    for _ in range(MAX_CHAIN):
        if ticket.status != TicketStatus.CLOSED:
            break
        newer = session.exec(
            select(Ticket).join(RelatedTicket, RelatedTicket.ticket_id == Ticket.id)
            .where(RelatedTicket.related_ticket_id == ticket.id).order_by(Ticket.id.desc()).limit(1)
        ).first()
        if newer is None or newer.id in seen:
            break
        seen.add(newer.id)
        ticket = newer
    return ticket

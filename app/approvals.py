"""Approvals (phase 1H): a ticket that needs sign-off before the work goes ahead.

Built for service requests from the catalogue, but nothing here knows about request types
beyond where the approvers and the rule come from, so another kind of ticket (change control
later) can use it: give it approvers and a rule, call start_approvals, and let decide settle it.

The rule is two numbers, copied onto the ticket when approvals start: how many approvers must
approve, and how many must decline. Both default to one, so any single approver decides.
  * approved: approvals reach approvals_required.
  * declined: declines reach declines_required, or so many have declined that the approvals
    can no longer be reached.
Whichever happens first settles it; approvers who had not answered are marked not needed.

When it starts: at triage, once the ticket's real type and team are known (an agent who retypes
a catalogue request to an incident ends it). The ticket waits in Awaiting approval, which only
the outcome (or a super admin waiving it) moves on: approved goes to Open, declined closes it
and tells the requester. The SLA clock keeps running meanwhile.

The requester never approves their own request: if they are among the approvers they are left
out, and with no one else left the request needs no approval.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.audit import log_action
from app.messages import record_notice
from app.outbox import queue_approval
from app.models import (
    RequestType,
    RequestTypeApprover,
    Ticket,
    TicketApproval,
    TicketStatus,
    TicketType,
    User,
)
from app.request_types import approvers_of
from app.ticket_service import (
    EVENT_APPROVAL_DECIDED,
    EVENT_APPROVAL_OUTCOME,
    EVENT_APPROVAL_REQUESTED,
    record_event,
    set_status,
)

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
NOT_NEEDED = "not_needed"
MAX_COMMENT_LENGTH = 1000


class ApprovalError(ValueError):
    """A reason a decision cannot be recorded, worded for the approver."""


@dataclass
class Tally:
    approved: int
    declined: int
    pending: int


def eligible_approvers(session: Session, ticket: Ticket) -> list[User]:
    """The approvers of the ticket's request type, leaving out the requester."""
    if ticket.request_type_id is None or ticket.type != TicketType.SERVICE_REQUEST:
        return []
    request_type = session.get(RequestType, ticket.request_type_id)
    if request_type is None:
        return []
    requester = ticket.requester_email.lower()
    return [a for a in approvers_of(session, request_type) if a.email.lower() != requester]


def needs_approval(session: Session, ticket: Ticket) -> bool:
    already = session.exec(select(TicketApproval.id).where(TicketApproval.ticket_id == ticket.id).limit(1)).first()
    return already is None and bool(eligible_approvers(session, ticket))


def status_after_triage(session: Session, ticket: Ticket) -> TicketStatus:
    """Where triage puts the ticket: waiting for approval, or straight to Open."""
    return TicketStatus.AWAITING_APPROVAL if needs_approval(session, ticket) else TicketStatus.OPEN


def start_approvals(session: Session, ticket: Ticket, actor_email: str) -> list[TicketApproval]:
    """Asks the approvers. The ticket is already in Awaiting approval (status_after_triage)."""
    approvers = eligible_approvers(session, ticket)
    request_type = session.get(RequestType, ticket.request_type_id)
    count = len(approvers)
    ticket.approvals_required = min(max(1, request_type.approvals_required), count)
    ticket.declines_required = min(max(1, request_type.declines_required), count)
    session.add(ticket)
    rows = [TicketApproval(ticket_id=ticket.id, approver_id=a.id) for a in approvers]
    session.add_all(rows)
    session.flush()
    for row, approver in zip(rows, approvers):
        queue_approval(session, row, approver)
    record_event(
        session, ticket, EVENT_APPROVAL_REQUESTED, actor_email,
        to_value=", ".join(a.email for a in approvers),
        detail=f"{ticket.approvals_required} of {count} must approve",
    )
    return rows


def approvals_of(session: Session, ticket: Ticket) -> list[tuple[TicketApproval, User]]:
    rows = session.exec(
        select(TicketApproval, User)
        .join(User, User.id == TicketApproval.approver_id)
        .where(TicketApproval.ticket_id == ticket.id)
        .order_by(TicketApproval.id)
    ).all()
    return [(approval, user) for approval, user in rows]


def tally(approvals: list[TicketApproval]) -> Tally:
    return Tally(
        approved=sum(1 for a in approvals if a.decision == APPROVED),
        declined=sum(1 for a in approvals if a.decision == DECLINED),
        pending=sum(1 for a in approvals if a.decision == PENDING),
    )


def outcome(ticket: Ticket, counts: Tally) -> Optional[str]:
    """APPROVED, DECLINED, or None while it is still open."""
    needed = ticket.approvals_required or 1
    if counts.approved >= needed:
        return APPROVED
    if counts.declined >= (ticket.declines_required or 1) or counts.approved + counts.pending < needed:
        return DECLINED
    return None


def describe_rule(ticket: Ticket, total: int) -> str:
    return f"{ticket.approvals_required or 1} of {total} must approve"


def approval_for(session: Session, ticket: Ticket, user: User) -> Optional[TicketApproval]:
    return session.exec(
        select(TicketApproval).where(TicketApproval.ticket_id == ticket.id, TicketApproval.approver_id == user.id)
    ).first()


def pending_for(session: Session, user: User) -> list[tuple[TicketApproval, Ticket]]:
    """Requests waiting on this person, oldest first."""
    rows = session.exec(
        select(TicketApproval, Ticket)
        .join(Ticket, Ticket.id == TicketApproval.ticket_id)
        .where(
            TicketApproval.approver_id == user.id,
            TicketApproval.decision == PENDING,
            Ticket.status == TicketStatus.AWAITING_APPROVAL,
        )
        .order_by(TicketApproval.created_at, TicketApproval.id)
    ).all()
    return [(approval, ticket) for approval, ticket in rows]


def count_pending_for(session: Session, user: User) -> int:
    return session.exec(
        select(func.count())
        .select_from(TicketApproval)
        .join(Ticket, Ticket.id == TicketApproval.ticket_id)
        .where(
            TicketApproval.approver_id == user.id,
            TicketApproval.decision == PENDING,
            Ticket.status == TicketStatus.AWAITING_APPROVAL,
        )
    ).one()


def _settle(session: Session, ticket: Ticket, result: str, approvals: list[TicketApproval], who: User) -> None:
    now = datetime.now(timezone.utc)
    for approval in approvals:
        if approval.decision == PENDING:
            approval.decision, approval.decided_at = NOT_NEEDED, now
            session.add(approval)
    record_event(session, ticket, EVENT_APPROVAL_OUTCOME, who.email, to_value=result)
    if result == APPROVED:
        set_status(session, ticket, TicketStatus.OPEN, who.email, detail="approved", by_approval=True)
        record_notice(session, ticket, "Your request has been approved. IT will pick it up.")
    else:
        set_status(session, ticket, TicketStatus.CLOSED, who.email, detail="approval declined", by_approval=True)
        reasons = [a.comment for a in approvals if a.decision == DECLINED and a.comment]
        record_notice(
            session, ticket,
            "Your request was not approved, so it has been closed."
            + (f" Reason: {' / '.join(reasons)}" if reasons else ""),
        )


def _still_an_approver(session: Session, ticket: Ticket, approval: TicketApproval) -> bool:
    """Whether the person is still named as an approver of the request's type. An admin who
    removes someone (they have left) takes their right to answer with them, including by an
    old emailed link; a request that can then never be approved is for a super admin to waive."""
    if ticket.request_type_id is None:
        return False
    return session.exec(
        select(RequestTypeApprover.id).where(
            RequestTypeApprover.request_type_id == ticket.request_type_id,
            RequestTypeApprover.user_id == approval.approver_id,
        )
    ).first() is not None


def record_decision(session: Session, approval: TicketApproval, decision: str, comment: str) -> Optional[str]:
    """decide() plus its audit entry: the one way a person's answer is recorded, whether it came
    from the portal or the emailed link. Does not commit."""
    result = decide(session, approval, decision, comment)
    approver = session.get(User, approval.approver_id)
    log_action(session, approver.email, "ticket", approval.ticket_id, f"approval_{decision}", (comment or "").strip()[:200])
    return result


def decide(session: Session, approval: TicketApproval, decision: str, comment: str) -> Optional[str]:
    """Records an approver's decision and settles the ticket if that decides it. Returns the
    outcome (APPROVED, DECLINED) when this settled the ticket, else None. Does not commit."""
    if decision not in (APPROVED, DECLINED):
        raise ApprovalError("Choose approve or decline")
    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ApprovalError(f"The comment can be at most {MAX_COMMENT_LENGTH} characters")

    ticket = session.get(Ticket, approval.ticket_id)
    # Lock the ticket and re-read, so two approvers answering at once are counted one after the other.
    session.refresh(ticket, with_for_update=True)
    session.refresh(approval)
    if approval.decision == NOT_NEEDED:
        raise ApprovalError("This request was settled without your answer")
    if approval.decision != PENDING:
        raise ApprovalError("You have already answered this request")
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise ApprovalError("This request is no longer waiting for approval")
    if not _still_an_approver(session, ticket, approval):
        raise ApprovalError("You are no longer an approver for this kind of request")

    approver = session.get(User, approval.approver_id)
    approval.decision, approval.comment, approval.decided_at = decision, comment, datetime.now(timezone.utc)
    session.add(approval)
    session.flush()
    record_event(session, ticket, EVENT_APPROVAL_DECIDED, approver.email, to_value=decision, detail=comment[:200] or None)

    approvals = [a for a, _ in approvals_of(session, ticket)]
    result = outcome(ticket, tally(approvals))
    if result is not None:
        _settle(session, ticket, result, approvals, approver)
    return result


def waive(session: Session, ticket: Ticket, actor: User) -> None:
    """A super admin lets a request through without waiting for the rest (an approver has left,
    say). Everyone still pending is marked not needed. Does not commit."""
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise ApprovalError("This request is not waiting for approval")
    now = datetime.now(timezone.utc)
    for approval, _ in approvals_of(session, ticket):
        if approval.decision == PENDING:
            approval.decision, approval.decided_at = NOT_NEEDED, now
            session.add(approval)
    record_event(session, ticket, EVENT_APPROVAL_OUTCOME, actor.email, to_value="waived")
    set_status(session, ticket, TicketStatus.OPEN, actor.email, detail="approval waived", by_approval=True)
    record_notice(session, ticket, "Your request is going ahead: IT has let it through without waiting for further approval.")
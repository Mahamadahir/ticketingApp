"""Approvals (phase 1H, groups in 4C-1): a ticket that needs sign-off before the work goes ahead.

Built for service requests from the catalogue, but nothing here knows about request types
beyond where the group and the rule come from, so another kind of ticket (change control
later) can use it: give it a group and a pass percentage, call start_approvals, and let decide settle it.

The rule, set per request type by a super admin and copied onto the approval when it starts:
  * voters: the members of the type's approval group at that moment, minus the requester. The number
    is fixed when the approval starts, so changing the group mid-request cannot flip a result.
  * votes_needed = ceil(pass percent x voters / 100), so 5 voters at 60% need 3 and 4 at 60% need 3;
  * approved as soon as the approvals reach votes_needed (equal counts as met);
  * declined as soon as that can no longer be reached (declines > voters - votes_needed), so a complete
    vote always has an outcome;
  * the pass percentage is 1 to 100 (check_percentages, on save); there is no fail percentage.
Whichever settles it first wins; voters who had not answered are marked not needed.

The requester may withdraw while it waits (`withdraw`): the approval ends as "withdrawn", the ticket closes
with the reason "withdrawn" and the voters who had not answered are told. A decision and a withdrawal lock the
ticket and then the approval in the same order, so one racing the other gives exactly one outcome.

Data: one TicketApproval per ask (at most one pending per ticket), one ApprovalVote per voter, made
when it starts. Each voter gets one emailed link naming their vote (app/approval_links.py).

When it starts: at triage, once the ticket's real type and team are known (an agent who retypes
a catalogue request to an incident ends it). The ticket waits in Awaiting approval, which only
the outcome (or a super admin waiving it) moves on: approved goes to Open, declined closes it
and tells the requester. The SLA clock keeps running meanwhile.

The requester never votes on their own request: if they are in the group they are left out, unless the request
type allows self-approval (`approvers_may_self_approve`, off by default, #167). Then an active requester who is in
the group is a voter whose approve vote is recorded when the round starts (comment SELF_APPROVED_COMMENT, event
and audit entry `approval_self_approved`), the outcome is worked out at once (a sole approver, or 1 of 3 at 30%,
settles it; otherwise the others vote as usual), and the vote cannot be changed. The requester can still withdraw
while it waits, as the rules above say. A request type
with no group at all needs no approval. One that has a group but no active member to ask (everyone left, or
only the requester is in it) does NOT skip approval (#161): the ticket waits in Awaiting approval with no
approval round ("waiting for approvers"), the super admins are told in the bell, and the round starts by
itself once an active member is added or reactivated (`start_waiting_rounds`, called at once from those
admin actions and from the reminder job). A super admin can waive it meanwhile. Someone taken out of the
group after the approval started stays counted as a voter but can no longer answer (a super admin waives a
request that can then never be decided).

A voter who becomes inactive (Admin > Users or the nightly Entra check, app/entra_check.py) and had not yet
voted is dropped (`drop_votes_of`): the vote is marked not needed (as waive and withdraw mark theirs), the
voter count falls by one, and the outcome is worked out again on the rest with the same rule, so the
request may be approved or declined at once. With no voters left the round ends as "superseded" and the
ticket waits for approvers as above. Votes already cast still count. The same check runs at vote time and in
the reminder job.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.approval_groups import is_member, members_of
from app.audit import log_action
from app.email_templates import render_template, ticket_values
from app.messages import record_notice
from app.notifications import (
    notify_approval_outcome,
    notify_approval_waiting,
    notify_approval_withdrawn,
    notify_no_active_approvers,
)
from app.outbox import queue_approval, queue_approval_withdrawn
from app.identity import addresses_of, is_requester
from app.models import (
    ApprovalGroup,
    ApprovalGroupMember,
    ApprovalVote,
    RequestType,
    Setting,
    Ticket,
    TicketApproval,
    TicketSource,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.ticket_service import (
    EVENT_APPROVAL_DECIDED,
    EVENT_APPROVAL_OUTCOME,
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_SELF_APPROVED,
    EVENT_APPROVAL_VOTE_REMOVED,
    EVENT_APPROVAL_WAITING,
    EVENT_APPROVAL_WITHDRAWN,
    record_event,
    set_status,
)

# A vote's decision.
PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
NOT_NEEDED = "not_needed"
# An approval's status: pending, then approved or declined, or waived by a super admin, or withdrawn
# by the requester. A plain string column, so a new status needs no migration; the partial unique index
# only looks at 'pending', so a withdrawn approval frees the ticket's one pending slot.
WAIVED = "waived"
WITHDRAWN = "withdrawn"
# A round whose voters all became inactive before anyone voted: it ends and the ticket waits for approvers (#161).
SUPERSEDED = "superseded"
AUDIT_ACTOR = "system"
MAX_COMMENT_LENGTH = 1000
SELF_APPROVED_COMMENT = "Self-approved (allowed for this request type)"


class ApprovalError(ValueError):
    """A reason a decision cannot be recorded, worded for the voter."""


class PercentError(ValueError):
    """A reason the pass percentage cannot be saved, worded for the admin."""


def check_percentages(pass_percent: int) -> None:
    """The pass percentage is a whole number from 1 to 100."""
    if not 1 <= pass_percent <= 100:
        raise PercentError("The pass percentage must be a whole number from 1 to 100")


def votes_needed(percent: int, voters: int) -> int:
    """The smallest number of votes that reaches the percentage."""
    return -(-percent * voters // 100)


def outcome(approved: int, declined: int, voters: int, pass_percent: int) -> Optional[str]:
    """APPROVED as soon as the approvals reach the pass percentage, DECLINED as soon as it can no longer
    be reached (more declines than voters - votes_needed), else None while it is still open. A complete
    vote always reaches one of the two."""
    if voters <= 0:
        return None
    needed = votes_needed(pass_percent, voters)
    if approved >= needed:
        return APPROVED
    if declined > voters - needed:
        return DECLINED
    return None


def may_self_approve(session: Session, ticket: Ticket) -> bool:
    """Only a request the requester raised themselves in the portal (signed in as them): an agent raising on someone's
    behalf, a mail From address or a Teams message is not proof the approver asked, so those are asked as usual."""
    if ticket.source != TicketSource.PORTAL:
        return False
    request_type = session.get(RequestType, ticket.request_type_id) if ticket.request_type_id is not None else None
    return request_type is not None and request_type.approvers_may_self_approve


def eligible_voters(session: Session, ticket: Ticket) -> list[User]:
    """The active members of the ticket's request type's group, leaving out the requester unless the request
    type lets approvers approve their own requests (#167; an inactive requester is never a member here)."""
    group = approval_group_of(session, ticket)
    if group is None:
        return []
    keep_requester = may_self_approve(session, ticket)
    requester = addresses_of(session, ticket.requester_email)  # the requester's person, once
    return [u for u in members_of(session, group.id, active_only=True) if keep_requester or u.email.lower() not in requester]


def approval_group_of(session: Session, ticket: Ticket) -> Optional[ApprovalGroup]:
    """The approval group of the ticket's request type, or None when it has none (or is not a catalogue request)."""
    if ticket.request_type_id is None or ticket.type != TicketType.SERVICE_REQUEST:
        return None
    request_type = session.get(RequestType, ticket.request_type_id)
    if request_type is None or request_type.approval_group_id is None:
        return None
    return session.get(ApprovalGroup, request_type.approval_group_id)


def needs_approval(session: Session, ticket: Ticket) -> bool:
    """True for a request type with an approval group, even when nobody active can answer it yet: it then
    waits for approvers instead of skipping approval (#161). No group at all still skips."""
    already = session.exec(select(TicketApproval.id).where(TicketApproval.ticket_id == ticket.id).limit(1)).first()
    return already is None and approval_group_of(session, ticket) is not None


def status_after_triage(session: Session, ticket: Ticket) -> TicketStatus:
    """Where triage puts the ticket: waiting for approval, or straight to Open."""
    return TicketStatus.AWAITING_APPROVAL if needs_approval(session, ticket) else TicketStatus.OPEN


def is_waiting_for_approvers(session: Session, ticket: Ticket) -> bool:
    """In Awaiting approval with no approval round going: nobody active could be asked (#161)."""
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        return False
    approval = current_approval(session, ticket)
    return approval is None or approval.status != PENDING


def _blocked_key(group_id: int) -> str:
    return f"approval_group_no_active_approvers:{group_id}"


def _enter_waiting(session: Session, ticket: Ticket, actor_email: str) -> None:
    """The ticket waits for approvers: an event, an audit entry, and (once per group until it has an active member
    again, `_blocked_key`) a bell entry for every active super admin. The ticket page shows the wait."""
    group = approval_group_of(session, ticket)
    name = group.name if group is not None else "the approval group"
    record_event(session, ticket, EVENT_APPROVAL_WAITING, actor_email, detail=f"no active approvers in {name}")
    log_action(session, actor_email, "ticket", ticket.id, "approval_waiting_for_approvers", name)
    if group is None or session.get(Setting, _blocked_key(group.id)) is not None:
        return
    request_type = session.get(RequestType, ticket.request_type_id)
    admins = session.exec(select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active)).all()
    told = [a for a in admins if notify_no_active_approvers(session, a, ticket, request_type.name, name) is not None]
    if told:  # with no super admin to tell, it is tried again for the next ticket
        # Two requests triaged at once must not collide on the key.
        session.execute(
            pg_insert(Setting).values(key=_blocked_key(group.id), value=datetime.now(timezone.utc).isoformat()).on_conflict_do_nothing()
        )


def start_approvals(session: Session, ticket: Ticket, actor_email: str) -> Optional[TicketApproval]:
    """Asks the group. The ticket is already in Awaiting approval (status_after_triage). With no active voter
    there is no round: the ticket waits for approvers (returns None) until `start_waiting_rounds`."""
    voters = eligible_voters(session, ticket)
    if not voters:
        _enter_waiting(session, ticket, actor_email)
        return None
    request_type = session.get(RequestType, ticket.request_type_id)
    approval = TicketApproval(
        ticket_id=ticket.id, group_id=request_type.approval_group_id, voters=len(voters),
        pass_percent=request_type.pass_percent,
    )
    session.add(approval)
    session.flush()
    requester = addresses_of(session, ticket.requester_email)
    now = datetime.now(timezone.utc)
    # The requester is only in `voters` when the type allows self-approval (eligible_voters): their vote is made now.
    votes = [
        ApprovalVote(approval_id=approval.id, voter_id=u.id, decision=APPROVED, comment=SELF_APPROVED_COMMENT, decided_at=now)
        if u.email.lower() in requester else ApprovalVote(approval_id=approval.id, voter_id=u.id)
        for u in voters
    ]
    session.add_all(votes)
    session.flush()
    approved, declined, _ = tally(votes)
    result = outcome(approved, declined, approval.voters, approval.pass_percent)
    for vote, voter in zip(votes, voters):
        if vote.decision == PENDING and result is None:  # a request settled by the self-approval asks nobody
            queue_approval(session, vote, voter)
            notify_approval_waiting(session, ticket, voter)
    record_event(
        session, ticket, EVENT_APPROVAL_REQUESTED, actor_email,
        to_value=", ".join(u.email for u in voters), detail=describe_rule(approval),
    )
    if approved:
        record_event(session, ticket, EVENT_APPROVAL_SELF_APPROVED, ticket.requester_email, to_value=APPROVED, detail=SELF_APPROVED_COMMENT)
        log_action(session, ticket.requester_email, "ticket", ticket.id, "approval_self_approved", request_type.name)
    if result is not None:
        _settle(session, ticket, approval, result, votes, actor_email)
    return approval


def _lock_ticket(session: Session, ticket_id: int, skip_locked: bool) -> bool:
    """Locks the ticket row and re-reads it. With `skip_locked`, False when someone else holds it."""
    query = select(Ticket).where(Ticket.id == ticket_id).with_for_update(skip_locked=skip_locked).execution_options(populate_existing=True)
    return session.exec(query).first() is not None


def start_waiting_rounds(session: Session, actor_email: str = AUDIT_ACTOR, skip_locked: bool = False) -> int:
    """Starts the round of every ticket waiting for approvers whose group now has an active member (one was
    added or reactivated, or the request type got another group). Called at once from those admin actions and
    from the reminder job as the safety net. Also forgets the "told the super admins" mark of a group that has
    an active member again. `skip_locked` (the reminder job) leaves a ticket someone is working on to the next run.
    Returns how many rounds started. Does not commit."""
    for mark in session.exec(select(Setting).where(Setting.key.like("approval_group_no_active_approvers:%"))).all():
        group_id = int(mark.key.rsplit(":", 1)[1])
        if members_of(session, group_id, active_only=True):
            session.delete(mark)
    started = 0
    pending_round = select(TicketApproval.id).where(TicketApproval.ticket_id == Ticket.id, TicketApproval.status == PENDING).exists()
    ids = session.exec(
        select(Ticket.id)
        .where(Ticket.status == TicketStatus.AWAITING_APPROVAL, Ticket.type == TicketType.SERVICE_REQUEST, ~pending_round)
        .order_by(Ticket.id)
    ).all()
    for ticket_id in ids:
        ticket = session.get(Ticket, ticket_id)
        # Looked at without a lock first.
        if not eligible_voters(session, ticket):
            continue
        if not _lock_ticket(session, ticket_id, skip_locked) or not is_waiting_for_approvers(session, session.get(Ticket, ticket_id)):
            continue
        ticket = session.get(Ticket, ticket_id)
        start_approvals(session, ticket, actor_email)
        log_action(session, actor_email, "ticket", ticket.id, "approval_round_started", "an active approver is available")
        started += 1
    return started


def restart_after_reopen(session: Session, ticket: Ticket, actor_email: Optional[str]) -> bool:
    """A ticket closed by a decline or a withdrawal that an agent has set back to an open status goes back to
    asking its approvers: a new approval round, votes requested again (decided 3 Nov 2026, #122). Called by
    `set_status` after such a move. Does nothing for a ticket whose latest approval was approved or waived, or
    that has no approval group any more. With no active approver it waits for approvers (#161). Returns whether it restarted."""
    approval = current_approval(session, ticket)
    if approval is None or approval.status not in (DECLINED, WITHDRAWN) or approval_group_of(session, ticket) is None:
        return False
    set_status(session, ticket, TicketStatus.AWAITING_APPROVAL, actor_email, detail="approval restarted", by_approval=True)
    start_approvals(session, ticket, actor_email or "system")
    log_action(session, actor_email or "system", "ticket", ticket.id, "approval_restarted", f"after {approval.status}")
    return True


def describe_rule(approval: TicketApproval) -> str:
    n = approval.voters
    return f"{votes_needed(approval.pass_percent, n)} of {n} must approve ({approval.pass_percent}%)"


def current_approval(session: Session, ticket: Ticket) -> Optional[TicketApproval]:
    """The ticket's pending approval, else its latest one, else None."""
    return session.exec(
        select(TicketApproval)
        .where(TicketApproval.ticket_id == ticket.id)
        .order_by((TicketApproval.status == PENDING).desc(), TicketApproval.id.desc())
        .limit(1)
    ).first()


def votes_of(session: Session, approval: Optional[TicketApproval]) -> list[tuple[ApprovalVote, User]]:
    if approval is None:
        return []
    rows = session.exec(
        select(ApprovalVote, User)
        .join(User, User.id == ApprovalVote.voter_id)
        .where(ApprovalVote.approval_id == approval.id)
        .order_by(ApprovalVote.id)
    ).all()
    return [(vote, user) for vote, user in rows]


def voters_yet_to_vote(session: Session, approval: TicketApproval) -> list[tuple[ApprovalVote, User]]:
    """The voters who have not answered an approval that is still pending (the people a reminder
    or an email-preference check would go to). Empty once the approval is settled."""
    if approval.status != PENDING:
        return []
    return [(vote, user) for vote, user in votes_of(session, approval) if vote.decision == PENDING]


def tally(votes: list[ApprovalVote]) -> tuple[int, int, int]:
    """(approved, declined, pending)."""
    return (
        sum(1 for v in votes if v.decision == APPROVED),
        sum(1 for v in votes if v.decision == DECLINED),
        sum(1 for v in votes if v.decision == PENDING),
    )


def vote_for(session: Session, ticket: Ticket, user: User) -> Optional[ApprovalVote]:
    """This person's vote on the ticket's current approval."""
    approval = current_approval(session, ticket)
    if approval is None:
        return None
    return session.exec(
        select(ApprovalVote).where(ApprovalVote.approval_id == approval.id, ApprovalVote.voter_id == user.id)
    ).first()


def _waiting_votes(user: User):
    """Votes of this person that are unanswered on a request still waiting, from a group they are
    still in (a member taken out can no longer answer, so it is not offered to them)."""
    return (
        select(ApprovalVote, Ticket)
        .join(TicketApproval, TicketApproval.id == ApprovalVote.approval_id)
        .join(Ticket, Ticket.id == TicketApproval.ticket_id)
        .join(
            ApprovalGroupMember,
            (ApprovalGroupMember.group_id == TicketApproval.group_id) & (ApprovalGroupMember.user_id == user.id),
        )
        .where(
            ApprovalVote.voter_id == user.id,
            ApprovalVote.decision == PENDING,
            TicketApproval.status == PENDING,
            Ticket.status == TicketStatus.AWAITING_APPROVAL,
        )
    )


def pending_for(session: Session, user: User) -> list[tuple[ApprovalVote, Ticket]]:
    """Requests waiting on this person, oldest first."""
    rows = session.exec(_waiting_votes(user).order_by(ApprovalVote.created_at, ApprovalVote.id)).all()
    return [(vote, ticket) for vote, ticket in rows]


def count_pending_for(session: Session, user: User) -> int:
    return session.exec(select(func.count()).select_from(_waiting_votes(user).subquery())).one()


def _notice(session: Session, ticket: Ticket, key: str, **extra: str) -> str:
    """The wording of an automatic notice, from the admin-managed template (app/email_templates.py)."""
    return render_template(session, key, {**ticket_values(ticket), **extra}).body


def _close_pending_votes(session: Session, votes: list[ApprovalVote]) -> None:
    now = datetime.now(timezone.utc)
    for vote in votes:
        if vote.decision == PENDING:
            vote.decision, vote.decided_at = NOT_NEEDED, now
            session.add(vote)


def _make_subrequests(session: Session, ticket: Ticket) -> None:
    """Approval passed: the sub-requests recorded at triage are made now (app/subrequests.py). Imported here because
    app.subrequests builds on the splitting machinery, which builds on this module."""
    from app.subrequests import after_approval

    after_approval(session, ticket)


def _settle(session: Session, ticket: Ticket, approval: TicketApproval, result: str, votes: list[ApprovalVote], actor_email: str) -> None:
    approval.status, approval.settled_at = result, datetime.now(timezone.utc)
    session.add(approval)
    _close_pending_votes(session, votes)
    record_event(session, ticket, EVENT_APPROVAL_OUTCOME, actor_email, to_value=result)
    if result == APPROVED:
        set_status(session, ticket, TicketStatus.OPEN, actor_email, detail="approved", by_approval=True)
        record_notice(session, ticket, _notice(session, ticket, "approval_approved"))
        notify_approval_outcome(session, ticket, APPROVED, actor_email)
        _make_subrequests(session, ticket)
    else:
        set_status(session, ticket, TicketStatus.CLOSED, actor_email, detail="approval declined", by_approval=True)
        reasons = [v.comment for v in votes if v.decision == DECLINED and v.comment]
        reason_text = f" Reason: {' / '.join(reasons)}" if reasons else ""
        record_notice(session, ticket, _notice(session, ticket, "approval_declined", reason_text=reason_text))
        notify_approval_outcome(session, ticket, DECLINED, actor_email)


def record_decision(session: Session, vote: ApprovalVote, decision: str, comment: str) -> Optional[str]:
    """decide() plus its audit entry: the one way a person's answer is recorded, whether it came
    from the portal or the emailed link. Does not commit."""
    result = decide(session, vote, decision, comment)
    voter = session.get(User, vote.voter_id)
    approval = session.get(TicketApproval, vote.approval_id)
    log_action(session, voter.email, "ticket", approval.ticket_id, f"approval_{decision}", (comment or "").strip()[:200])
    return result


def decide(session: Session, vote: ApprovalVote, decision: str, comment: str) -> Optional[str]:
    """Records a voter's decision and settles the ticket if that decides it. Returns the outcome
    (APPROVED, DECLINED) when this settled the ticket, else None. Does not commit."""
    if decision not in (APPROVED, DECLINED):
        raise ApprovalError("Choose approve or decline")
    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ApprovalError(f"The comment can be at most {MAX_COMMENT_LENGTH} characters")

    approval = session.get(TicketApproval, vote.approval_id)
    ticket = session.get(Ticket, approval.ticket_id)
    # Lock the ticket (the order everything else in the family locks in), then the approval, and re-read,
    # so two members answering at once are counted one after the other, each seeing the other's vote.
    session.refresh(ticket, with_for_update=True)
    session.refresh(approval, with_for_update=True)
    session.refresh(vote)
    if approval.status == WITHDRAWN:
        raise ApprovalError("This request was withdrawn by the requester, so it no longer needs an answer")
    if vote.decision == NOT_NEEDED:
        raise ApprovalError("This request was settled without your answer")
    if vote.decision != PENDING:
        raise ApprovalError("You have already answered this request")
    if approval.status != PENDING or ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise ApprovalError("This request is no longer waiting for approval")
    voter = session.get(User, vote.voter_id)
    if is_requester(ticket, voter):
        raise ApprovalError("You cannot approve your own request")
    if not voter.is_active:
        raise ApprovalError("Your account is inactive, so you can no longer answer this request")
    if not is_member(session, approval.group_id, vote.voter_id):
        raise ApprovalError("You are no longer an approver for this kind of request")

    vote.decision, vote.comment, vote.decided_at = decision, comment, datetime.now(timezone.utc)
    session.add(vote)
    session.flush()
    record_event(session, ticket, EVENT_APPROVAL_DECIDED, voter.email, to_value=decision, detail=comment[:200] or None)

    votes = [v for v, _ in votes_of(session, approval)]
    approved, declined, _ = tally(votes)
    result = outcome(approved, declined, approval.voters, approval.pass_percent)
    if result is not None:
        _settle(session, ticket, approval, result, votes, voter.email)
    return result


def waive(session: Session, ticket: Ticket, actor: User) -> None:
    """A super admin lets a request through without waiting for the rest (a voter has left,
    say). Everyone still pending is marked not needed. Does not commit."""
    session.refresh(ticket, with_for_update=True)
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise ApprovalError("This request is not waiting for approval")
    approval = current_approval(session, ticket)
    if approval is not None and approval.status == PENDING:
        session.refresh(approval, with_for_update=True)
        approval.status, approval.settled_at = WAIVED, datetime.now(timezone.utc)
        session.add(approval)
        _close_pending_votes(session, [v for v, _ in votes_of(session, approval)])
    record_event(session, ticket, EVENT_APPROVAL_OUTCOME, actor.email, to_value="waived")
    set_status(session, ticket, TicketStatus.OPEN, actor.email, detail="approval waived", by_approval=True)
    record_notice(session, ticket, _notice(session, ticket, "approval_waived"))
    notify_approval_outcome(session, ticket, "waived", actor.email)
    _make_subrequests(session, ticket)


def withdraw(session: Session, ticket: Ticket, requester_email: str) -> None:
    """The requester takes back a request that is still waiting for approval: only the requester, only
    while it waits, never after a decision. The approval ends as withdrawn, voters who had not answered
    are marked not needed and told (bell and email, per their preference), and the ticket closes with the
    reason "withdrawn". Locks like decide() (ticket, then approval) so it and a final vote cannot both win.
    Raises ApprovalError, changing nothing. Does not commit."""
    session.flush()
    session.refresh(ticket, with_for_update=True)
    if (requester_email or "").lower() not in addresses_of(session, ticket.requester_email):
        raise ApprovalError("Only the person who made the request can withdraw it")
    approval = current_approval(session, ticket)
    if approval is not None:
        session.refresh(approval, with_for_update=True)
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise ApprovalError("This request is no longer waiting for approval, so it cannot be withdrawn")
    if approval is None or approval.status != PENDING:
        # Waiting for approvers (#161): no round to end and nobody to tell; the requester can still take it back.
        record_event(session, ticket, EVENT_APPROVAL_WITHDRAWN, requester_email, detail="no approval round had started")
        set_status(session, ticket, TicketStatus.CLOSED, requester_email, detail="withdrawn", by_approval=True)
        log_action(session, requester_email, "ticket", ticket.id, "approval_withdrawn", ticket.tag)
        notify_approval_outcome(session, ticket, "withdrawn", requester_email)
        return
    # Re-read the votes under the lock: one decided since this session loaded them must not be overwritten.
    for vote, _ in votes_of(session, approval):
        session.refresh(vote)
    unanswered = voters_yet_to_vote(session, approval)
    approval.status, approval.settled_at = WITHDRAWN, datetime.now(timezone.utc)
    session.add(approval)
    _close_pending_votes(session, [vote for vote, _ in unanswered])
    record_event(session, ticket, EVENT_APPROVAL_WITHDRAWN, requester_email, detail=f"{len(unanswered)} had not answered")
    set_status(session, ticket, TicketStatus.CLOSED, requester_email, detail="withdrawn", by_approval=True)
    log_action(session, requester_email, "ticket", ticket.id, "approval_withdrawn", ticket.tag)
    notify_approval_outcome(session, ticket, "withdrawn", requester_email)  # the followers; never the requester
    for vote, voter in unanswered:
        notify_approval_withdrawn(session, ticket, voter, requester_email)
        queue_approval_withdrawn(session, vote, voter)


def drop_inactive_votes(session: Session, ticket: Ticket, actor_email: str = AUDIT_ACTOR) -> int:
    """Drops the unanswered votes of voters who are inactive on the ticket's pending approval (#161): each is marked
    not needed (as waive and withdraw mark theirs) with a ticket event and an audit entry, the voter count falls,
    and the outcome is worked out again on the rest with the same rule (the ticket may be approved or declined at
    once). With no voters left the round ends as superseded and the ticket waits for approvers. Answers already
    given count as before. Locks like decide() (ticket, then approval). Returns how many were dropped.
    Does not commit."""
    session.flush()
    session.refresh(ticket, with_for_update=True)
    approval = current_approval(session, ticket)
    if approval is None or approval.status != PENDING or ticket.status != TicketStatus.AWAITING_APPROVAL:
        return 0
    session.refresh(approval, with_for_update=True)
    for vote, _ in votes_of(session, approval):
        session.refresh(vote)
    if approval.status != PENDING:
        return 0
    gone = [(vote, voter) for vote, voter in voters_yet_to_vote(session, approval) if not voter.is_active]
    if not gone:
        return 0
    now = datetime.now(timezone.utc)
    for vote, voter in gone:
        vote.decision, vote.decided_at = NOT_NEEDED, now
        session.add(vote)
        name = voter.display_name or voter.email
        record_event(session, ticket, EVENT_APPROVAL_VOTE_REMOVED, actor_email, to_value=voter.email, detail=f"{name}'s vote removed (account inactive)")
        log_action(session, actor_email, "ticket", ticket.id, "approval_vote_removed", f"{voter.email}: account inactive")
    approval.voters -= len(gone)
    session.add(approval)
    session.flush()
    if approval.voters <= 0:
        approval.status, approval.settled_at = SUPERSEDED, now
        session.add(approval)
        _enter_waiting(session, ticket, actor_email)
        return len(gone)
    votes = [v for v, _ in votes_of(session, approval)]
    approved, declined, _ = tally(votes)
    result = outcome(approved, declined, approval.voters, approval.pass_percent)
    if result is not None:
        _settle(session, ticket, approval, result, votes, actor_email)
    return len(gone)


def drop_votes_of(session: Session, person: User, actor_email: str = AUDIT_ACTOR) -> int:
    """Called wherever a person becomes inactive (Admin > Users, the nightly Entra check): drops their unanswered
    votes on every approval still pending (`drop_inactive_votes`). Returns how many were dropped."""
    session.flush()
    ticket_ids = session.exec(
        select(TicketApproval.ticket_id)
        .join(ApprovalVote, ApprovalVote.approval_id == TicketApproval.id)
        .where(ApprovalVote.voter_id == person.id, ApprovalVote.decision == PENDING, TicketApproval.status == PENDING)
        .order_by(TicketApproval.ticket_id)
    ).all()
    dropped = 0
    for ticket_id in ticket_ids:
        dropped += drop_inactive_votes(session, session.get(Ticket, ticket_id), actor_email)
    return dropped


def reconcile_approvals(session: Session) -> None:
    """The safety net (the reminder job runs it first): drops the votes of anyone inactive on every pending
    approval, then starts the round of every ticket waiting for approvers that now has someone to ask."""
    ticket_ids = session.exec(
        select(TicketApproval.ticket_id)
        .join(ApprovalVote, ApprovalVote.approval_id == TicketApproval.id)
        .join(User, User.id == ApprovalVote.voter_id)
        .where(TicketApproval.status == PENDING, ApprovalVote.decision == PENDING, User.is_active.is_(False))
        .distinct()
        .order_by(TicketApproval.ticket_id)
    ).all()
    for ticket_id in ticket_ids:
        # Never waits for a lock (like the job's own loop): a ticket someone is working on is left to the next run.
        if _lock_ticket(session, ticket_id, True):
            drop_inactive_votes(session, session.get(Ticket, ticket_id))
    start_waiting_rounds(session, skip_locked=True)

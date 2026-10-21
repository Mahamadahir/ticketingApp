"""Approval reminders (phase 4C-3): chase approvers who have not answered. A recurring job on the
database-driven scheduler (app/scheduled_jobs.py), settings-driven (Admin > Settings, "Approval reminders").

For every approval still pending on a ticket that is still Awaiting approval, each voter who has not
answered is emailed a reminder (the `approval_reminder` template) when:
  * the first one is due: `approval_remind_after_days` after their vote was created (the request reached them);
  * a later one is due: `approval_remind_every_days` after the last, up to `approval_max_reminders` in all.
Voters who answered, were marked not needed, or whose approval was approved, declined, waived or
withdrawn are never reminded (voters_yet_to_vote is empty once the approval is settled). Someone taken out
of the group, or who switched approval emails off, is skipped and not counted; the request stays on their
portal list. The email is built (and checked again) when it is sent, and counts toward the automatic-email
allowance (app/mail.py, per approver).

Idempotent by data, not by "since the last run": what is due is worked out from each vote's
`reminder_count` and `last_reminded_at`, which are advanced in the same transaction that queues the email.
Run late, a vote gets one reminder (not one per missed slot); run twice, the second run finds nothing due.

Escalation: when a pending approval is `approval_escalate_after_days` old, each super admin who can open
the ticket gets one bell notification (`escalated_at` makes it once per approval). Teams reminders to
approvers wait for the phase 8 bot.

Off by default (`approval_reminders_enabled`) until mail is proved on staging.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.approval_groups import is_member
from app.approvals import PENDING, voters_yet_to_vote
from app.audit import log_action
from app.models import ApprovalVote, Ticket, TicketApproval, TicketStatus, User, UserRole
from app.notifications import notify_approval_overdue
from app.outbox import is_on, queue_approval_reminder
from app.settings_store import (
    APPROVAL_ESCALATE_AFTER_DAYS,
    APPROVAL_MAX_REMINDERS,
    APPROVAL_REMIND_AFTER_DAYS,
    APPROVAL_REMIND_EVERY_DAYS,
    APPROVAL_REMINDERS_ENABLED,
    count_setting,
    get_flag,
)

AUDIT_ACTOR = "system"


def _due(vote: ApprovalVote, now: datetime, after: timedelta, every: timedelta, maximum: int) -> bool:
    if vote.reminder_count >= maximum:
        return False
    if vote.reminder_count == 0 or vote.last_reminded_at is None:
        return now >= vote.created_at + after
    return now >= vote.last_reminded_at + every


def send_approval_reminders(session: Session, now: Optional[datetime] = None) -> None:
    """The job. Does not commit (the runner does)."""
    if not get_flag(session, APPROVAL_REMINDERS_ENABLED):
        return
    now = now or datetime.now(timezone.utc)
    after = timedelta(days=count_setting(session, APPROVAL_REMIND_AFTER_DAYS))
    every = timedelta(days=count_setting(session, APPROVAL_REMIND_EVERY_DAYS))
    maximum = count_setting(session, APPROVAL_MAX_REMINDERS)
    escalate_after = timedelta(days=count_setting(session, APPROVAL_ESCALATE_AFTER_DAYS))

    approval_ids = session.exec(
        select(TicketApproval.id)
        .join(Ticket, Ticket.id == TicketApproval.ticket_id)
        .where(TicketApproval.status == PENDING, Ticket.status == TicketStatus.AWAITING_APPROVAL)
        .order_by(TicketApproval.id)
    ).all()
    for approval_id in approval_ids:
        approval = session.get(TicketApproval, approval_id)
        ticket = session.get(Ticket, approval.ticket_id)
        # Lock like decide() and withdraw() (ticket, then approval) and look again: a vote, withdrawal or waiver
        # that landed since the list was read must stop the reminder.
        session.refresh(ticket, with_for_update=True)
        session.refresh(approval, with_for_update=True)
        if approval.status != PENDING or ticket.status != TicketStatus.AWAITING_APPROVAL:
            continue
        for vote, _ in voters_yet_to_vote(session, approval):
            session.refresh(vote)
        waiting = voters_yet_to_vote(session, approval)
        if is_on():
            for vote, voter in waiting:
                if not _due(vote, now, after, every, maximum) or not is_member(session, approval.group_id, voter.id):
                    continue
                if queue_approval_reminder(session, vote, voter) is not None:
                    vote.reminder_count += 1
                    vote.last_reminded_at = now
                    session.add(vote)
        if approval.escalated_at is None and waiting and now >= approval.created_at + escalate_after:
            _escalate(session, ticket, approval, now, len(waiting))


def _escalate(session: Session, ticket: Ticket, approval: TicketApproval, now: datetime, unanswered: int) -> None:
    approval.escalated_at = now
    session.add(approval)
    days = (now - approval.created_at).days
    admins = session.exec(select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active)).all()
    for admin in admins:
        notify_approval_overdue(session, ticket, admin, days, unanswered)
    log_action(session, AUDIT_ACTOR, "ticket", ticket.id, "approval_overdue", f"{unanswered} had not answered after {days} days")

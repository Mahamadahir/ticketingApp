"""Scheduled mailbox changes: what happens on the ticket and to the row (#307; tracker #305).

A person asks for mail forwarding (later Full access, Send As) from a start time to an end time. The ticket is a Service Request
that records it; the `MailboxChange` row is what the worker acts on and the source of truth for what Exchange should hold.
Only `app/mailbox_executor.py` talks to Exchange. Nothing here commits except `run_mailbox_changes` (the worker job), which has to
commit between claiming a row and calling Exchange.

The life of a change
--------------------

1. `create_mailbox_change` (the portal, #309) writes the row and puts the ticket in **Scheduled** with Due = the start date and the
   SLA clock starting at the start time (`schedule_ticket`; the system sets this directly, not through the hand rules in
   `required_by.prepare_scheduled`). The Scheduled morning job (`app/due_today.py`) leaves a ticket with a pending change alone.
2. `run_mailbox_changes` (the recurring job `mailbox-changes`) claims one due row at a time with FOR UPDATE SKIP LOCKED, marks it
   `applying` or `removing` with a lease (`next_attempt_at`) and commits, calls the executor with no lock held, then locks the row
   again and records the answer. A worker that dies in between leaves the row in applying/removing; once the lease passes, the
   next run claims it again, and because apply and remove are idempotent that is safe.
3. Answers (`app.mailbox_executor`): succeeded is success; retryable is retried quietly after `mailbox_change_retry_minutes` times the
   attempts so far until `mailbox_change_max_attempts` is used up; permanent and mismatch are final at once; no_answer is
   settled with a `read` (the change is already there: success; otherwise retry as retryable).
4. Applied: the ticket is resolved with a requester-visible note, the requester and the other person get the "started" emails.
   Removed: an event and a note on the original ticket (even if closed; a note does not reopen it) and the "stopped" emails.
5. A start that fails for good: the ticket goes back to Open, unassigned, in its team's queue with an internal note holding the
   raw Exchange text, a requester-visible note and email WITHOUT it, and a bell for the team. The other person is told nothing.
   A removal that fails for good: a new high-priority incident in the same team, linked to the request (a plain related-ticket
   link, `RelatedTicket`: the Major Incident / Problem parent link only takes those two types), with the same internal note, a bell
   and a Teams message for the team; the requester is told nothing until it is fixed.
6. The change then waits in `failed` for an agent: Try again, Done by hand or Cancel (`try_again`, `done_by_hand`, `cancel`).
   The requester can `cancel_by_requester` before the start and `stop_now` while it is on (the portal, #309).

Someone else's mailbox (#311)
-----------------------------
A request about SOMEONE ELSE'S mailbox (the owner of a change is not the requester) is approved first, through the approval flow of
`app/approvals.py`, by the owner of the mailbox or by an IT approver from the request type's group. Its rows are written before
triage (`create_mailbox_change(before_triage=True)`) so the approval knows whose mailbox it is, and wait unclaimed (the job skips a
ticket in Awaiting approval). `after_approval` then schedules the ticket once, starts the SLA at the later of the start time and the
approval, and cancels a change whose end time has already passed (the requester is told); `drop_unstarted` cancels the rows of a
declined or withdrawn request. A change that is approved after its start time begins at the next run of the job. Whoever asked,
the OWNER is told when it starts and when it ends, with who approved it (`_tell_owner`); the requester's own emails and notes use
the owner's name in place of "your mailbox".

Anonymisation refuses a person who owns or is the other person of a change that has not ended, because removing it needs their
address (`anonymisation.refusal`).
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, text
from sqlmodel import Session, select

from app.approvals import APPROVED, DECLINED, WAIVED, current_approval, votes_of
from app.audit import log_action
from app.config import settings
from app.email_templates import render_template, ticket_values
from app.identity import addresses_of, person_for_address
from app.mailbox_executor import (
    OUTCOME_MISMATCH, OUTCOME_NO_ANSWER, OUTCOME_PERMANENT, OUTCOME_RETRYABLE, OUTCOME_SUCCEEDED, ExecutorResult, get_executor, is_enabled,
)
from app.messages import record_notice, record_system_note
from app.models import (
    FINISHED_STATUSES, MAILBOX_ACTIVE, MAILBOX_APPLYING, MAILBOX_CANCELLED, MAILBOX_ENDED, MAILBOX_FAILED, MAILBOX_KINDS,
    MAILBOX_REMOVING, MAILBOX_SCHEDULED, MailboxChange, RelatedTicket, Ticket, TicketEvent, TicketSource, TicketStatus, TicketType,
    User,
)
from app.notifications import REF, notify_mailbox_change_failed
from app.outbox import queue_mailbox_change_mail
from app.resolution import ResolveError, resolve_ticket
from app.settings_store import (
    MAILBOX_CHANGE_LEASE_MINUTES, MAILBOX_CHANGE_MAX_ATTEMPTS, MAILBOX_CHANGE_RETRY_MINUTES, REGISTRY, count_setting, get_int_setting,
)
from app.sla import Level, SlaStartError, apply_impact_urgency, recalculate_deadlines, start_clock_at
from app.ticket_service import EVENT_TRIAGED, create_ticket, record_event, set_status
from app.time_zones import format_moment, zone_for_email
from app.triage import set_assignee

log = logging.getLogger("app.worker")

AUDIT_ENTITY = "mailbox_change"
AUDIT_ACTOR = "worker"

EVENT_SCHEDULED = "mailbox_change_scheduled"
EVENT_STARTED = "mailbox_change_started"
EVENT_ENDED = "mailbox_change_ended"
EVENT_START_FAILED = "mailbox_change_start_failed"
EVENT_REMOVAL_FAILED = "mailbox_change_removal_failed"
EVENT_CANCELLED = "mailbox_change_cancelled"
EVENT_TRY_AGAIN = "mailbox_change_try_again"
EVENT_DONE_BY_HAND = "mailbox_change_done_by_hand"
EVENT_STOPPED_EARLY = "mailbox_change_stopped_early"
EVENT_INCIDENT = "mailbox_change_incident"  # on the request: to_value is the incident raised for a failed removal
EVENT_RAISED_FOR = "mailbox_change_raised_for"  # on the incident: to_value is the request it is about

MAX_ERROR_LENGTH = 2000
MAX_PER_RUN = 20  # changes one run takes; the rest wait for the next run (the job's lease allows a few slow calls, see scheduled_jobs)
MAX_RUN_SECONDS = 240  # no new change is claimed once a run has been going this long
SYSTEM_REQUESTER_FALLBACK = "it-support@mailbox-changes.invalid"  # the incident's requester when no shared mailbox is configured
INCIDENT_IMPACT, INCIDENT_URGENCY = Level.HIGH, Level.MEDIUM  # P2: high priority, not a major incident


class MailboxChangeError(ValueError):
    """A reason a mailbox change action cannot go ahead, worded for the person."""


# Phrases per kind. {target} and {owner} are people's names; {until} a moment in the reader's zone.
STARTED_OWNER = {
    "forwarding": "Mail sent to you is now going to {target} as well",
    "full_access": "{target} now has Full access to your mailbox",
    "send_as": "{target} can now send email as you",
}
STOPPED_OWNER = {
    "forwarding": "Forwarding your mail to {target} has stopped",
    "full_access": "{target} no longer has Full access to your mailbox",
    "send_as": "{target} can no longer send email as you",
}
STARTED_TARGET = {
    "forwarding": "Mail sent to {owner} is now going to you as well",
    "full_access": "You now have Full access to {owner}'s mailbox",
    "send_as": "You can now send email as {owner}",
}
STOPPED_TARGET = {
    "forwarding": "Mail sent to {owner} is no longer going to you",
    "full_access": "You no longer have Full access to {owner}'s mailbox",
    "send_as": "You can no longer send email as {owner}",
}
RESOLUTION_NOTE = {
    "forwarding": "Forwarding to {target} is on until {until}",
    "full_access": "{target} has Full access to your mailbox until {until}",
    "send_as": "{target} can send email as you until {until}",
}
# The same, for a request about someone else's mailbox: told to the requester as a third person, unless the requester is the one the
# change was made for ("target"), when they are told in the second person. {owner} is the owner of the mailbox.
STARTED_THIRD = {
    "forwarding": "Mail sent to {owner} is now going to {target} as well",
    "full_access": "{target} now has Full access to {owner}'s mailbox",
    "send_as": "{target} can now send email as {owner}",
}
STOPPED_THIRD = {
    "forwarding": "Forwarding {owner}'s mail to {target} has stopped",
    "full_access": "{target} no longer has Full access to {owner}'s mailbox",
    "send_as": "{target} can no longer send email as {owner}",
}
RESOLUTION_NOTE_THIRD = {
    "forwarding": "Forwarding of {owner}'s mail to {target} is on until {until}",
    "full_access": "{target} has Full access to {owner}'s mailbox until {until}",
    "send_as": "{target} can send email as {owner} until {until}",
}
RESOLUTION_NOTE_TARGET = {
    "forwarding": "{owner}'s mail is also coming to you until {until}",
    "full_access": "You have Full access to {owner}'s mailbox until {until}",
    "send_as": "You can send email as {owner} until {until}",
}
STARTED_TARGET_KEY = {"full_access": "mailbox_full_access_started_target"}  # the one kind whose email says more (Outlook can be slow)
KIND_WORDS = {"forwarding": "mail forwarding", "full_access": "Full access", "send_as": "Send As"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _name(session: Session, address: str) -> str:
    person = person_for_address(session, address)
    return (person.display_name if person is not None and person.display_name else None) or address


def _ticket_link(ticket: Ticket) -> str:
    base = settings.ticket_link_base.rstrip("/")
    return f"{base}/portal/{ticket.number}" if base else f"(open request {ticket.reference} in the portal)"


def _lines(change: MailboxChange, ticket: Ticket, session: Session) -> dict[str, str]:
    """The values the sentences and the templates are filled in with, for the mailbox owner (requester) and the other person."""
    owner_zone = zone_for_email(session, change.owner_address)
    target_zone = zone_for_email(session, change.target_address)
    requester_zone = zone_for_email(session, ticket.requester_email)
    owner, target = _name(session, change.owner_address), _name(session, change.target_address)
    owner_copy = (
        ("A copy stays in your mailbox." if change.keep_copy else "Your mailbox is **not** keeping a copy.")
        if change.kind == "forwarding" else ""
    )
    on_behalf = _on_behalf(session, change, ticket)
    return {
        "owner": owner, "target": target, "kind": KIND_WORDS.get(change.kind, change.kind),
        "requester": _name(session, ticket.requester_email),
        "until_owner": format_moment(change.ends_at, owner_zone), "until_target": format_moment(change.ends_at, target_zone),
        "until_requester": format_moment(change.ends_at, requester_zone),
        "link": _ticket_link(ticket),
        "copy_text_owner": owner_copy,
        # What the requester is told: in their own mailbox's words, or (someone else's mailbox) in the owner's.
        "copy_text": (
            (f"A copy stays in {owner}'s mailbox." if change.keep_copy else f"{owner}'s mailbox is **not** keeping a copy.")
            if on_behalf and change.kind == "forwarding" else owner_copy
        ),
    }


def _on_behalf(session: Session, change: MailboxChange, ticket: Ticket) -> bool:
    """Whether this change is on SOMEONE ELSE'S mailbox (#311): its owner is not the requester or one of their linked addresses."""
    return change.owner_address.strip().lower() not in addresses_of(session, ticket.requester_email)


def _target_is_requester(session: Session, change: MailboxChange, ticket: Ticket) -> bool:
    return change.target_address.strip().lower() in addresses_of(session, ticket.requester_email)


def _for_requester(session: Session, change: MailboxChange, ticket: Ticket, own: dict, target: dict, third: dict) -> dict:
    """The phrase table for a message to the requester: `own` (their own mailbox), else `target` (they are the one it was made for)
    or `third` (someone else's mailbox, made for another person)."""
    if not _on_behalf(session, change, ticket):
        return own
    return target if _target_is_requester(session, change, ticket) else third


def _sentence(table: dict[str, str], change: MailboxChange, values: dict[str, str]) -> str:
    return table.get(change.kind, "{target} and {owner}: " + change.kind).format(owner=values["owner"], target=values["target"])


def pending_change_condition():
    """SQL: a change that has not started yet (waiting, retrying or being made now): the Scheduled morning job leaves its ticket alone."""
    return MailboxChange.state.in_([MAILBOX_SCHEDULED, MAILBOX_APPLYING])


def change_for_ticket(session: Session, ticket: Ticket) -> Optional[MailboxChange]:
    """The mailbox change a ticket is about (the request itself, or the incident raised for its failed removal): the newest."""
    return session.exec(
        select(MailboxChange)
        .where(or_(MailboxChange.ticket_id == ticket.id, MailboxChange.incident_ticket_id == ticket.id))
        .order_by(MailboxChange.id.desc())
        .limit(1)
    ).first()


def changes_for_ticket(session: Session, ticket: Ticket) -> list[MailboxChange]:
    """Every mailbox change the request made, oldest first (one for forwarding; Full access and Send As asked together are two on
    one ticket, #310). The incident raised for a failed removal has none of its own."""
    return list(session.exec(select(MailboxChange).where(MailboxChange.ticket_id == ticket.id).order_by(MailboxChange.id)).all())


def failed_change_for_ticket(session: Session, ticket: Ticket) -> Optional[MailboxChange]:
    """The oldest change on the request (or on the incident raised for its failed removal) that is waiting for an agent. An agent
    settles them one at a time: the next one shows once this one is dealt with."""
    return session.exec(
        select(MailboxChange)
        .where(or_(MailboxChange.ticket_id == ticket.id, MailboxChange.incident_ticket_id == ticket.id), MailboxChange.state == MAILBOX_FAILED)
        .order_by(MailboxChange.id)
        .limit(1)
    ).first()


def failed_phase(change: MailboxChange) -> Optional[str]:
    """"start" or "removal" for a change in `failed`, else None. A change that was never applied failed at the start."""
    if change.state != MAILBOX_FAILED:
        return None
    return "start" if change.applied_at is None else "removal"


def _audit(session: Session, actor: str, change: MailboxChange, action: str, detail: Optional[str] = None) -> None:
    log_action(session, actor, AUDIT_ENTITY, change.id, action, detail)


def _setting_minutes(session: Session, key: str) -> int:
    value = get_int_setting(session, key)
    return value if value else int(REGISTRY[key].default)


def max_attempts(session: Session) -> int:
    return count_setting(session, MAILBOX_CHANGE_MAX_ATTEMPTS)


# ---- Creating a change and scheduling its ticket -------------------------------------------------------------------


def schedule_ticket(session: Session, ticket: Ticket, change: MailboxChange, not_before: Optional[datetime] = None) -> None:
    """Puts the request in Scheduled with Due = the start date (in the requester's time zone) and starts its SLA clock at the start
    time. Set by the system, not through `prepare_scheduled` (that is the agent's form, which refuses a day whose morning has
    already begun, as a "Now" start would). Called by `create_mailbox_change`, and by the approval flow once a request for someone
    else's mailbox is approved (#311), with `not_before` = the approval time: the SLA then starts at the later of the start time and
    the approval. Does not commit."""
    if change.state != MAILBOX_SCHEDULED:
        return  # cancelled (or already under way) while it waited for approval
    zone = zone_for_email(session, ticket.requester_email)
    sla_moment = max(_aware(change.starts_at), _aware(not_before)) if not_before is not None else _aware(change.starts_at)
    ticket.due_on = sla_moment.astimezone(ZoneInfo(zone)).date()
    ticket.due_set_by_hand = False
    session.add(ticket)
    set_status(session, ticket, TicketStatus.SCHEDULED, "system", detail="waiting for the mailbox change to start")
    if ticket.sla_starts_at is not None and _aware(ticket.sla_starts_at) == sla_moment:
        pass  # a second change on the same request (Full access and Send As together) starts at the same moment: the clock is set
    elif sla_moment > _aware(ticket.created_at):
        try:
            start_clock_at(session, ticket, sla_moment, "system")
        except SlaStartError:
            recalculate_deadlines(session, ticket)  # cannot start later than it finished; the due date still applies
    else:
        recalculate_deadlines(session, ticket)
    record_event(
        session, ticket, EVENT_SCHEDULED, "system", to_value=change.kind,
        detail=f"starts {_aware(change.starts_at):%Y-%m-%d %H:%M} UTC, ends {_aware(change.ends_at):%Y-%m-%d %H:%M} UTC",
    )


def create_mailbox_change(
    session: Session,
    ticket: Ticket,
    *,
    kind: str,
    owner_address: str,
    target_address: str,
    starts_at: datetime,
    ends_at: datetime,
    created_by: str,
    owner_object_id: Optional[str] = None,
    target_object_id: Optional[str] = None,
    keep_copy: bool = True,
    before_triage: bool = False,
) -> MailboxChange:
    """Writes the change for a request ticket and schedules the ticket. With `before_triage` (a request about someone else's mailbox,
    #311) the ticket is not triaged yet and is not scheduled: the row is written first so the approval that triage starts knows whose
    mailbox it is, and `finish_on_behalf` schedules it after triage (or `after_approval` once approved). The ticket must already be triaged (a portal request of a
    type whose defaults are all set triages itself when raised, `request_types.self_triage`). A request still waiting for approval
    (#311) is left as it is: the row waits, and the job does not touch it, until `schedule_ticket` is called when it is approved.
    Times are UTC; `ends_at` is required and after `starts_at`. Does not commit."""
    if not is_enabled():
        raise MailboxChangeError("Scheduled mailbox changes are not switched on (MAILBOX_EXECUTOR is not set in this environment)")
    if kind not in MAILBOX_KINDS:
        raise MailboxChangeError(f"Unknown kind of mailbox change {kind!r}")
    starts_at, ends_at = _aware(starts_at), _aware(ends_at)
    if ends_at <= starts_at:
        raise MailboxChangeError("The end must be after the start")
    if ticket.triaged_at is None and not before_triage:
        raise MailboxChangeError("The request has not been triaged: its request type needs defaults so that it triages itself")
    change = MailboxChange(
        ticket_id=ticket.id, kind=kind, owner_object_id=(owner_object_id or None) and owner_object_id.lower(),
        owner_address=owner_address.strip().lower(), target_object_id=(target_object_id or None) and target_object_id.lower(),
        target_address=target_address.strip().lower(), keep_copy=keep_copy if kind == "forwarding" else True,
        starts_at=starts_at, ends_at=ends_at, created_by=created_by.strip().lower(),
    )
    session.add(change)
    session.flush()
    _audit(session, created_by, change, "created", f"{kind}, starts {starts_at:%Y-%m-%d %H:%M} UTC, ends {ends_at:%Y-%m-%d %H:%M} UTC")
    if ticket.status != TicketStatus.AWAITING_APPROVAL and not before_triage:
        schedule_ticket(session, ticket, change)
    return change


def finish_on_behalf(session: Session, ticket: Ticket) -> None:
    """After a request for someone else's mailbox has been raised and triaged (#311): a request that waits for approval keeps its rows
    scheduled but unclaimed (the job skips Awaiting approval) and is scheduled by `after_approval`; one that did not need to wait is
    scheduled now. Raises MailboxChangeError when the request was not triaged (its type's defaults are not all set)."""
    if ticket.triaged_at is None:
        raise MailboxChangeError("The request has not been triaged: its request type needs defaults so that it triages itself")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        return
    for change in changes_for_ticket(session, ticket):
        schedule_ticket(session, ticket, change)


def after_approval(session: Session, ticket: Ticket, now: Optional[datetime] = None) -> None:
    """The request for someone else's mailbox was approved (#311, called by the approval flow): each change still waiting is scheduled
    now with `schedule_ticket` (once per change; it is idempotent), the SLA starting at the later of its start time and now. A
    change that starts in the past begins at the job's next run. One whose end time has already passed is cancelled, the request
    is resolved with a note and the requester told. Does nothing for a request with no waiting change. Does not commit."""
    now = now or utcnow()
    waiting = [c for c in changes_for_ticket(session, ticket) if c.state == MAILBOX_SCHEDULED and c.applied_at is None]
    if not waiting:
        return
    live = []
    for change in waiting:
        if _aware(change.ends_at) <= now:
            change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
            session.add(change)
            _audit(session, AUDIT_ACTOR, change, "cancelled", "approved after the end time had passed")
            record_event(session, ticket, EVENT_CANCELLED, "system", to_value=change.kind, detail="approved after its end time")
        else:
            live.append(change)
    for change in live:
        schedule_ticket(session, ticket, change, not_before=now)
    if not live:
        _resolve(session, ticket, "This request was approved after the end time you chose, so nothing was changed.", None, _others(session, ticket, waiting[0]))
        record_notice(session, ticket, render_template(session, "mailbox_expired_requester", ticket_values(ticket)).body)


DECLINED_LINE = "This request was declined. Submit a new request."


def was_declined(session: Session, ticket: Ticket) -> bool:
    """Whether this request for someone else's mailbox was declined (#325): it has changes, every one cancelled before it was ever made,
    and its latest approval was declined. Stays true if an agent reopens the ticket afterwards: the changes are not brought back, so
    the pages say it was declined and offer no controls."""
    changes = changes_for_ticket(session, ticket)
    if not changes or any(c.state != MAILBOX_CANCELLED or c.applied_at is not None for c in changes):
        return False
    approval = current_approval(session, ticket)
    return approval is not None and approval.status == DECLINED


def drop_unstarted(session: Session, ticket: Ticket, reason: str, now: Optional[datetime] = None) -> None:
    """A request for someone else's mailbox will not be approved (declined or withdrawn, #311): the changes it was holding back are
    cancelled with an audit entry, so the job never makes them. The ticket was closed by the approval flow, which told the requester.
    Does not commit."""
    now = now or utcnow()
    for change in changes_for_ticket(session, ticket):
        if change.state == MAILBOX_SCHEDULED and change.applied_at is None:
            change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
            session.add(change)
            _audit(session, AUDIT_ACTOR, change, "cancelled", reason)
            record_event(session, ticket, EVENT_CANCELLED, "system", to_value=change.kind, detail=reason)


# ---- The ticket side of each outcome -------------------------------------------------------------------------------


def lock_owner(session: Session, owner_address: str) -> None:
    """Takes a Postgres advisory lock, held until the transaction ends, keyed on the mailbox's owner. Taken before an overlap check so
    that two submits for one mailbox (a double click, two tabs) take turns: the second waits, then sees the first one's rows and is
    refused. The lock is released by the commit or rollback the route does (#325)."""
    session.exec(text("select pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(key=f"mailbox-change:{owner_address.strip().lower()}"))


def _others(session: Session, ticket: Ticket, change: MailboxChange) -> list[MailboxChange]:
    """The other changes the same request made (Full access and Send As asked together, #310), with this one's state saved first."""
    session.flush()
    return [other for other in changes_for_ticket(session, ticket) if other.id != change.id]


def _unsettled(others: list[MailboxChange]) -> bool:
    """Whether any of these is still to be made or waiting for an agent to make it: the request is not done while one is."""
    return any(o.state in (MAILBOX_SCHEDULED, MAILBOX_APPLYING) or (o.state == MAILBOX_FAILED and o.applied_at is None) for o in others)


def _applied_note(session: Session, change: MailboxChange, others: list[MailboxChange]) -> str:
    """The resolution note: one line for each change that was made, this one and the others, oldest first."""
    made = sorted([change] + [o for o in others if o.applied_at is not None and o.state != MAILBOX_CANCELLED], key=lambda c: c.id)
    lines = []
    for item in made:
        item_ticket = session.get(Ticket, item.ticket_id)
        values = _lines(item, item_ticket, session)
        table = _for_requester(session, item, item_ticket, RESOLUTION_NOTE, RESOLUTION_NOTE_TARGET, RESOLUTION_NOTE_THIRD)
        lines.append(table.get(item.kind, "{target}: {kind}").format(owner=values["owner"], target=values["target"], until=values["until_requester"]))
    return ". ".join(lines)


def _cancelled_part_note(session: Session, change: MailboxChange, others: list[MailboxChange]) -> str:
    """The resolution note for a request with several changes when one was dropped: it says which, and what is in place instead."""
    word = KIND_WORDS.get(change.kind, change.kind)
    made = [o for o in others if o.applied_at is not None and o.state != MAILBOX_CANCELLED]
    text = f"The {word} part of this request was cancelled, so nothing was changed for it."
    return text + (" " + _applied_note(session, made[0], made[1:]) + "." if made else "")


def _resolve(session: Session, ticket: Ticket, note: str, actor: Optional[User], others: Optional[list[MailboxChange]] = None) -> bool:
    """Resolves the request with `note` as its resolution, no resolution email (the started or cancelled notice is the email).
    A ticket that is already finished, or cannot be resolved now, is left as it is, and so is one whose other changes (`others`,
    from `_others`) are not all settled yet: it resolves when the last one has been made."""
    if ticket.status in FINISHED_STATUSES:
        return False
    if others and _unsettled(others):
        return False
    try:
        resolve_ticket(session, ticket, actor, note, send_email=False)
    except ResolveError as error:
        log.info("mailbox change: ticket %s not resolved (%s)", ticket.number, error)
        return False
    return True


def _tell_target(session: Session, change: MailboxChange, ticket: Ticket, key: str, table: dict[str, str], values: dict[str, str]) -> None:
    if _on_behalf(session, change, ticket) and _target_is_requester(session, change, ticket):
        return  # the requester is the other person: their own email and note already say so
    rendered = render_template(
        session, key,
        {
            "owner": values["owner"], "kind": values["kind"], "until": values["until_target"],
            "change": _sentence(table, change, values),
        },
    )
    queue_mailbox_change_mail(session, ticket, change.target_address, rendered.subject or "", rendered.body)


def _approved_by(session: Session, ticket: Ticket) -> str:
    """Who approved a request for someone else's mailbox, as the end of a sentence for the owner: "you approved it", "it was approved by
    Sam Lee (IT Support)" or "it was approved by an administrator" (a waiver)."""
    approval = current_approval(session, ticket)
    if approval is None:
        return "it was approved"
    if approval.status == WAIVED:
        return "it was approved by an administrator"
    for vote, voter in votes_of(session, approval):
        if vote.decision == APPROVED:
            mailbox_owners = {c.owner_address.strip().lower() for c in changes_for_ticket(session, ticket)}
            if mailbox_owners & addresses_of(session, voter.email):
                return "you approved it"
            return f"it was approved by {voter.display_name or voter.email} (IT Support)"
    return "it was approved"


def _tell_owner(session: Session, change: MailboxChange, ticket: Ticket, key: str, table: dict[str, str], values: dict[str, str]) -> None:
    """The owner of the mailbox is always told when a change THEY did not ask for starts or ends (#311), whoever approved it and
    whether or not they voted. Nothing for a request about the person's own mailbox (the requester is told, as before)."""
    if not _on_behalf(session, change, ticket):
        return
    rendered = render_template(
        session, key,
        {
            "change": _sentence(table, change, values), "until": values["until_owner"], "copy_text": values["copy_text_owner"],
            "requested_by": values["requester"], "approved_by": _approved_by(session, ticket),
        },
    )
    queue_mailbox_change_mail(session, ticket, change.owner_address, rendered.subject or "", rendered.body)


def _on_applied(session: Session, change: MailboxChange, ticket: Ticket, now: datetime, actor: Optional[User] = None) -> None:
    """The change is in Exchange: resolve the request with a note the requester sees, and tell both people."""
    change.state, change.applied_at, change.attempts, change.next_attempt_at = MAILBOX_ACTIVE, now, 0, None
    change.last_error_category = change.last_error = None
    change.updated_at = now
    session.add(change)
    values = _lines(change, ticket, session)
    others = _others(session, ticket, change)
    note = _applied_note(session, change, others)
    record_event(session, ticket, EVENT_STARTED, actor.email if actor else "system", to_value=change.kind, detail=("done by hand" if actor else None))
    _resolve(session, ticket, note, actor, others)
    body = render_template(
        session, "mailbox_started_requester",
        {
            **ticket_values(ticket),
            "change": _sentence(_for_requester(session, change, ticket, STARTED_OWNER, STARTED_TARGET, STARTED_THIRD), change, values),
            "target": values["target"], "until": values["until_requester"], "copy_text": values["copy_text"], "link": values["link"],
        },
    ).body
    record_notice(session, ticket, body)
    _tell_target(session, change, ticket, STARTED_TARGET_KEY.get(change.kind, "mailbox_started_target"), STARTED_TARGET, values)
    _tell_owner(session, change, ticket, "mailbox_started_owner", STARTED_OWNER, values)


def _on_removed(session: Session, change: MailboxChange, ticket: Ticket, now: datetime, actor: Optional[User] = None) -> None:
    """The change is out of Exchange: an event and a note on the original ticket, even if it is closed (a note does not reopen
    it), and the stopped emails."""
    change.state, change.removed_at, change.attempts, change.next_attempt_at = MAILBOX_ENDED, now, 0, None
    change.last_error_category = change.last_error = None
    change.updated_at = now
    session.add(change)
    values = _lines(change, ticket, session)
    record_event(session, ticket, EVENT_ENDED, actor.email if actor else "system", to_value=change.kind, detail=("done by hand" if actor else None))
    body = render_template(
        session, "mailbox_stopped_requester",
        {
            **ticket_values(ticket),
            "change": _sentence(_for_requester(session, change, ticket, STOPPED_OWNER, STOPPED_TARGET, STOPPED_THIRD), change, values),
            "target": values["target"],
        },
    ).body
    record_notice(session, ticket, body)
    _tell_target(session, change, ticket, "mailbox_stopped_target", STOPPED_TARGET, values)
    _tell_owner(session, change, ticket, "mailbox_stopped_owner", STOPPED_OWNER, values)
    _settle_incident(session, change, now, actor)


def _settle_incident(session: Session, change: MailboxChange, now: datetime, actor: Optional[User]) -> None:
    """A removal that failed raised a P2 incident (`_on_removal_failed`). Once the change is removed after all (Try again worked, or
    an agent removed it by hand) the incident is noted and resolved, so it is not left open (#325). One already finished is left."""
    if not change.incident_ticket_id:
        return
    incident = session.get(Ticket, change.incident_ticket_id)
    if incident is None or incident.status in FINISHED_STATUSES:
        return
    how = f"by hand, by {actor.email}" if actor else "when the removal was tried again"
    note = (
        f"The mailbox change (change {change.id}, {KIND_WORDS.get(change.kind, change.kind)} on {change.owner_address}) has now been "
        f"removed {how}, at {now:%Y-%m-%d %H:%M} UTC. Nothing is left in Exchange."
    )
    record_system_note(session, incident, note)
    try:
        resolve_ticket(session, incident, actor, note, send_email=False)
    except ResolveError as error:
        log.info("mailbox change: incident %s not resolved (%s)", incident.number, error)


def _internal_note(change: MailboxChange, phase: str, now: datetime) -> str:
    what = "make" if phase == "start" else "remove"
    return (
        f"The mailbox change could not be {'made' if phase == 'start' else 'removed'}. This note is for the team: the requester "
        f"has not been shown the Exchange message.\n"
        f"What was attempted (change {change.id}, {KIND_WORDS.get(change.kind, change.kind)}): {what} {KIND_WORDS.get(change.kind, change.kind)} on {change.owner_address} for {change.target_address}"
        + (f" (keep a copy: {'yes' if change.keep_copy else 'no'})" if change.kind == "forwarding" else "")
        + f"; it was to run from {_aware(change.starts_at):%Y-%m-%d %H:%M} UTC to {_aware(change.ends_at):%Y-%m-%d %H:%M} UTC.\n"
        f"When: {now:%Y-%m-%d %H:%M} UTC. Attempts: {change.attempts}. Error category: {change.last_error_category or 'none'}.\n"
        f"Exchange message: {change.last_error or '(none)'}\n"
        f"Job id: {change.last_job_id or '(none)'}\n"
        f"Read-back: {change.last_found or '(nothing was read)'}\n"
        + (
            "Agents: use Try again, Done by hand or Cancel on the request."
            if phase == "start" else
            "Exchange may still hold the change. Agents: use Try again or Done by hand on the request (Cancel is not offered)."
        )
    )


def _on_start_failed(session: Session, change: MailboxChange, ticket: Ticket, now: datetime) -> None:
    """The start failed for good: the request goes back to the team queue, open and unassigned, with the Exchange text in an
    internal note only, and the requester is told plainly (once) that nothing was changed."""
    change.state, change.next_attempt_at, change.updated_at = MAILBOX_FAILED, None, now
    session.add(change)
    told_before = _event_count(session, ticket, EVENT_START_FAILED, change.kind) > 0
    several = len(_others(session, ticket, change)) > 0
    record_event(session, ticket, EVENT_START_FAILED, "system", to_value=change.kind, detail=f"{change.last_error_category}, {change.attempts} attempts")
    if ticket.status not in FINISHED_STATUSES and ticket.status not in (TicketStatus.OPEN, TicketStatus.AWAITING_APPROVAL):
        # Whatever an agent moved it to meanwhile (On hold, Awaiting user, With MSP): it goes back to the queue the team watches.
        set_status(session, ticket, TicketStatus.OPEN, "system", detail="the mailbox change could not be made")
    if ticket.status not in FINISHED_STATUSES:
        set_assignee(session, ticket, None, None)
    record_system_note(session, ticket, _internal_note(change, "start", now))
    if not told_before:  # a second final failure (after Try again) does not email the requester again
        if several:  # one of two changes on the request: say which, and that the other is not affected by it
            body = render_template(
                session, "mailbox_part_could_not_start_requester", {**ticket_values(ticket), "kind": KIND_WORDS.get(change.kind, change.kind)}
            ).body
        else:
            body = render_template(session, "mailbox_could_not_start_requester", ticket_values(ticket)).body
        record_notice(session, ticket, body)
    notify_mailbox_change_failed(
        session, ticket, f"{REF} {ticket.subject}: the scheduled mailbox change could not be made, so the request is back in the team's queue"
    )


def _event_count(session: Session, ticket: Ticket, event_type: str, kind: Optional[str] = None) -> int:
    """How many events of this type the ticket has, for one kind of change when `kind` is given (the event's to_value)."""
    query = select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == event_type)
    if kind is not None:
        query = query.where(TicketEvent.to_value == kind)
    return session.exec(query).one()


def _incident_requester() -> str:
    return settings.graph_shared_mailbox.strip().lower() or SYSTEM_REQUESTER_FALLBACK


def _on_removal_failed(session: Session, change: MailboxChange, ticket: Ticket, now: datetime) -> Optional[Ticket]:
    """The removal failed for good: Exchange may still hold the change, so a new high-priority incident (P2) is raised in the same
    team, linked to the request with a plain related-ticket link, with the same internal note. The team gets a bell and a Teams
    message; the requester is told nothing until it is fixed."""
    change.state, change.next_attempt_at, change.updated_at = MAILBOX_FAILED, None, now
    session.add(change)
    record_event(session, ticket, EVENT_REMOVAL_FAILED, "system", to_value=change.kind, detail=f"{change.last_error_category}, {change.attempts} attempts")
    earlier = session.get(Ticket, change.incident_ticket_id) if change.incident_ticket_id else None
    if earlier is not None and earlier.status not in FINISHED_STATUSES:
        # A second final failure (after Try again): the incident already open is the place for it, not a second incident.
        record_system_note(session, earlier, "The removal was tried again and failed again.\n\n" + _internal_note(change, "removal", now))
        record_event(session, earlier, EVENT_RAISED_FOR, "system", to_value=ticket.tag, detail="failed again after Try again")
        notify_mailbox_change_failed(
            session, earlier, f"{REF} {earlier.subject}: the scheduled mailbox change could not be removed again. Check Exchange by hand"
        )
        return earlier
    incident = create_ticket(
        session,
        subject=f"Mailbox change could not be removed: {change.kind.replace('_', ' ')} on {change.owner_address}",
        requester_email=_incident_requester(), source=TicketSource.PORTAL, ticket_type=TicketType.INCIDENT,
        team_id=ticket.team_id, actor_email="system",
    )
    incident.category_id = ticket.category_id
    incident.triaged_at, incident.triaged_by_id = now, None
    session.add(incident)
    apply_impact_urgency(session, incident, INCIDENT_IMPACT, INCIDENT_URGENCY)
    record_event(session, incident, EVENT_TRIAGED, "system", detail=f"incident, raised automatically, priority {incident.priority}")
    set_status(session, incident, TicketStatus.OPEN, "system")
    session.add(RelatedTicket(ticket_id=incident.id, related_ticket_id=ticket.id))
    change.incident_ticket_id = incident.id
    session.add(change)
    record_event(session, ticket, EVENT_INCIDENT, "system", to_value=incident.tag)
    record_event(session, incident, EVENT_RAISED_FOR, "system", to_value=ticket.tag)
    record_system_note(
        session, incident,
        f"Raised automatically because a scheduled mailbox change could not be removed: Exchange may still hold it. "
        f"The request is {ticket.reference} ({ticket.subject}); this ticket is linked to it as a related ticket.\n\n"
        + _internal_note(change, "removal", now),
    )
    notify_mailbox_change_failed(
        session, incident, f"{REF} {incident.subject}: a scheduled mailbox change could not be removed. Check Exchange by hand"
    )
    return incident


# ---- The worker job -----------------------------------------------------------------------------------------------


@dataclass
class _Work:
    change_id: int
    operation: str  # "apply" or "remove"


def _due_condition(now: datetime):
    """Rows the job may claim: a start that is due (or retrying), an end that is due (or retrying), or a row left in applying or
    removing whose lease has run out (the worker that held it died)."""
    free = or_(MailboxChange.next_attempt_at.is_(None), MailboxChange.next_attempt_at <= now)
    return or_(
        and_(MailboxChange.state == MAILBOX_SCHEDULED, MailboxChange.starts_at <= now, free),
        and_(MailboxChange.state == MAILBOX_ACTIVE, MailboxChange.ends_at <= now, free),
        and_(MailboxChange.state.in_([MAILBOX_APPLYING, MAILBOX_REMOVING]), MailboxChange.next_attempt_at <= now),
    )


def _claim_next(session: Session, now: datetime, skip: set[int]) -> Optional[_Work]:
    """Locks the next due row (SKIP LOCKED: a second worker takes another), re-checks it under the lock, marks it applying or
    removing with a lease and commits, so the executor is called with no lock held. Returns None when nothing is due. A row the
    job must leave alone for now is added to `skip` (and the loop looks at the next); one that must not be made at all is
    cancelled."""
    while True:
        query = select(MailboxChange).where(_due_condition(now)).order_by(MailboxChange.id).limit(1).with_for_update(skip_locked=True)
        if skip:
            query = query.where(MailboxChange.id.not_in(skip))
        change = session.exec(query.execution_options(populate_existing=True)).first()
        if change is None:
            session.rollback()
            return None
        ticket = session.get(Ticket, change.ticket_id)
        starting = change.state in (MAILBOX_SCHEDULED, MAILBOX_APPLYING)
        if starting and ticket.status in (TicketStatus.AWAITING_APPROVAL, TicketStatus.NEW):
            skip.add(change.id)  # not approved (or not triaged) yet: it waits, whatever the time
            session.rollback()
            continue
        # Only a row that is still waiting is cancelled here. One found in `applying` (the worker died after claiming it) may have
        # been made in Exchange already, so it goes on to be applied again (idempotent) and removed in its turn, never dropped.
        waiting = change.state == MAILBOX_SCHEDULED
        if waiting and ticket.status in FINISHED_STATUSES and change.applied_at is None:
            # Closed or resolved before it began (declined, withdrawn, settled by hand): it is never made.
            change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
            session.add(change)
            _audit(session, AUDIT_ACTOR, change, "cancelled", "the request was finished before the change started")
            session.commit()
            continue
        if waiting and _aware(change.ends_at) <= now and change.applied_at is None:
            change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
            session.add(change)
            _audit(session, AUDIT_ACTOR, change, "cancelled", "the end time passed before the change could start")
            record_event(session, ticket, EVENT_CANCELLED, "system", to_value=change.kind, detail="the end time passed before it could start")
            _resolve(session, ticket, "The time for this change passed before it could be made, so nothing was changed.", None, _others(session, ticket, change))
            session.commit()
            continue
        operation = "apply" if starting else "remove"
        change.state = MAILBOX_APPLYING if operation == "apply" else MAILBOX_REMOVING
        change.attempts += 1
        change.next_attempt_at = now + timedelta(minutes=_setting_minutes(session, MAILBOX_CHANGE_LEASE_MINUTES))
        change.updated_at = now
        session.add(change)
        _audit(session, AUDIT_ACTOR, change, change.state, f"attempt {change.attempts}")
        work = _Work(change.id, operation)
        session.commit()
        return work


def _anything_due(session: Session, now: datetime) -> bool:
    """A cheap look (no lock) so an idle run never builds the executor: the real one is not available everywhere."""
    found = session.exec(select(MailboxChange.id).where(_due_condition(now)).limit(1)).first() is not None
    session.rollback()
    return found


def _snapshot(change: MailboxChange) -> MailboxChange:
    """A detached copy for the executor: it needs the fields, not the session (and must not hold a transaction open while it
    waits for Exchange)."""
    columns = (
        "id", "ticket_id", "kind", "owner_object_id", "owner_address", "target_object_id", "target_address", "keep_copy",
        "starts_at", "ends_at", "attempts",
    )
    return MailboxChange(**{name: getattr(change, name) for name in columns}, created_by=change.created_by)


def _call(operation: str, snapshot: MailboxChange) -> ExecutorResult:
    executor = get_executor()
    try:
        result = getattr(executor, operation)(snapshot)
        if operation == "apply" or operation == "remove":
            return _settle_no_answer(executor, operation, snapshot, result)
        return result
    except Exception as exc:  # an executor that raises is a temporary failure of ours, not an answer about Exchange
        log.exception("mailbox change %s: the executor raised during %s", snapshot.id, operation)
        return ExecutorResult(OUTCOME_RETRYABLE, error=f"The executor failed: {type(exc).__name__}: {exc}")


def _settle_no_answer(executor, operation: str, snapshot: MailboxChange, result: ExecutorResult) -> ExecutorResult:
    """no_answer: the call may or may not have worked, so look. If Exchange already shows what the call was to do, it counts
    as success (nothing is applied twice); otherwise it is retried like any temporary failure."""
    if result.outcome != OUTCOME_NO_ANSWER:
        return result
    try:
        seen = executor.read(snapshot)
    except Exception as exc:
        return ExecutorResult(OUTCOME_RETRYABLE, error=f"{result.error or 'No answer'}; reading it back failed: {type(exc).__name__}: {exc}", job_id=result.job_id)
    wanted = operation == "apply"
    if seen.outcome == OUTCOME_SUCCEEDED and seen.found is wanted:
        return ExecutorResult(OUTCOME_SUCCEEDED, found=seen.found, found_detail=seen.found_detail, job_id=seen.job_id or result.job_id)
    return ExecutorResult(
        OUTCOME_RETRYABLE, found=seen.found, found_detail=seen.found_detail,
        error=result.error or "No answer", job_id=seen.job_id or result.job_id,
    )


def _record_result(session: Session, work: _Work, result: ExecutorResult, now: datetime) -> None:
    change = session.exec(
        select(MailboxChange).where(MailboxChange.id == work.change_id).with_for_update().execution_options(populate_existing=True)
    ).first()
    expected = MAILBOX_APPLYING if work.operation == "apply" else MAILBOX_REMOVING
    if change is None or change.state != expected:
        # An agent or another worker settled it while the executor was working: their record wins.
        log.warning("mailbox change %s: result of %s dropped, the row is no longer %s", work.change_id, work.operation, expected)
        session.rollback()
        return
    ticket = session.get(Ticket, change.ticket_id)
    change.last_job_id = result.job_id or change.last_job_id
    change.last_found = result.found_detail or (None if result.found is None else ("in place" if result.found else "not in place"))
    if result.outcome == OUTCOME_SUCCEEDED:
        _audit(session, AUDIT_ACTOR, change, "applied" if work.operation == "apply" else "removed", f"job {result.job_id or 'none'}")
        if work.operation == "apply":
            _on_applied(session, change, ticket, now)
        else:
            _on_removed(session, change, ticket, now)
        return
    change.last_error_category = result.outcome
    change.last_error = (result.error or "")[:MAX_ERROR_LENGTH] or None
    final = result.outcome in (OUTCOME_PERMANENT, OUTCOME_MISMATCH) or change.attempts >= max_attempts(session)
    if not final:
        # Quietly retried: the state goes back to what it was, due again after a wait that grows with the attempts.
        change.state = MAILBOX_SCHEDULED if work.operation == "apply" else MAILBOX_ACTIVE
        change.next_attempt_at = now + timedelta(minutes=_setting_minutes(session, MAILBOX_CHANGE_RETRY_MINUTES) * change.attempts)
        change.updated_at = now
        session.add(change)
        _audit(session, AUDIT_ACTOR, change, "retry_scheduled", f"{result.outcome}, attempt {change.attempts}")
        return
    _audit(session, AUDIT_ACTOR, change, "start_failed" if work.operation == "apply" else "removal_failed", f"{result.outcome}, attempts {change.attempts}")
    if work.operation == "apply":
        _on_start_failed(session, change, ticket, now)
    else:
        _on_removal_failed(session, change, ticket, now)


def _record_breakdown(session: Session, work: _Work, result: ExecutorResult, error: str, now: datetime) -> None:
    """The result could not be recorded (a template, the ticket or a notification raised). Whatever Exchange now holds is kept in
    the row: a made change counts as applied (so Cancel is not offered and the removal still runs), and the row is handed to the
    team as failed, with the error and a bell, rather than retried for ever. The ticket is left as it was."""
    change = session.exec(
        select(MailboxChange).where(MailboxChange.id == work.change_id).with_for_update().execution_options(populate_existing=True)
    ).first()
    if change is None or change.state not in (MAILBOX_APPLYING, MAILBOX_REMOVING):
        return
    if result.outcome == OUTCOME_SUCCEEDED and change.applied_at is None:
        change.applied_at = now
    change.state, change.next_attempt_at, change.updated_at = MAILBOX_FAILED, None, now
    change.last_error_category, change.last_error, change.last_job_id = "recording_failed", error[:MAX_ERROR_LENGTH], result.job_id
    session.add(change)
    _audit(session, AUDIT_ACTOR, change, "recording_failed", f"{work.operation}, executor said {result.outcome}")
    ticket = session.get(Ticket, change.ticket_id)
    if ticket is not None:
        try:
            with session.begin_nested():
                record_system_note(session, ticket, _internal_note(change, "start" if change.applied_at is None else "removal", now))
                notify_mailbox_change_failed(session, ticket, f"{REF} {ticket.subject}: a scheduled mailbox change needs an agent (it could not be recorded)")
        except Exception:
            log.exception("mailbox change %s: the breakdown note could not be written", change.id)


def run_mailbox_changes(session: Session, now: Optional[datetime] = None) -> int:
    """The recurring job `mailbox-changes` (registered in app/scheduled_jobs.py): starts the changes whose time has come, removes
    those whose end has, and retries failed attempts that are due. Returns how many it handled. One row at a time, each claimed with
    FOR UPDATE SKIP LOCKED and committed before Exchange is called, so two runs at once never take the same change and a run late
    or twice does nothing the second time. Commits after each change (the runner commits at the end as well)."""
    clock = (lambda: now) if now is not None else utcnow  # a long run takes the time afresh for each change
    now = clock()
    handled = 0
    skip: set[int] = set()
    if not is_enabled() or not _anything_due(session, now):
        return 0  # off (MAILBOX_EXECUTOR unset) or nothing due
    began = time.monotonic()
    get_executor()  # raises ExecutorNotAvailable before anything is claimed, so no change loses an attempt to a missing executor
    while True:
        if handled >= MAX_PER_RUN or time.monotonic() - began > MAX_RUN_SECONDS:
            break  # a bounded run: what is left is due, and the next run takes it
        work = _claim_next(session, clock(), skip)
        if work is None:
            break
        change = session.get(MailboxChange, work.change_id)
        snapshot = _snapshot(change)
        session.rollback()  # nothing is held while Exchange is called
        result = _call(work.operation, snapshot)
        try:
            _record_result(session, work, result, clock())
            session.commit()
        except Exception as exc:
            # Recording must not stop the run or leave the row to be re-claimed for ever: it is settled in a fresh transaction,
            # and a change that cannot be recorded goes to the team as a failure.
            log.exception("mailbox change %s: recording the result of %s failed", work.change_id, work.operation)
            session.rollback()
            _record_breakdown(session, work, result, f"{type(exc).__name__}: {exc}", clock())
            session.commit()
        handled += 1
        skip.add(work.change_id)  # not twice in one run, whatever happened
    return handled


# ---- What an agent can do with a failed change ----------------------------------------------------------------------


def _locked(session: Session, change: MailboxChange) -> MailboxChange:
    return session.exec(
        select(MailboxChange).where(MailboxChange.id == change.id).with_for_update().execution_options(populate_existing=True)
    ).one()


def _require_failed(change: MailboxChange) -> str:
    phase = failed_phase(change)
    if phase is None:
        raise MailboxChangeError("This mailbox change is not waiting for an agent")
    return phase


def try_again(session: Session, change: MailboxChange, actor: User, now: Optional[datetime] = None) -> None:
    """One more attempt now: the worker's next run makes (or removes) the change, and if that fails it is final again straight away.
    Records an event and an audit entry. Does not commit; the caller asks the job to run (scheduled_jobs.run_now)."""
    now = now or utcnow()
    change = _locked(session, change)
    phase = _require_failed(change)
    if phase == "start" and _aware(change.ends_at) <= now:
        raise MailboxChangeError("The end time of this change has passed, so it can no longer be made: use Cancel change, or Done by hand if you made it")
    ticket = session.get(Ticket, change.ticket_id)
    if phase == "start" and ticket.status in FINISHED_STATUSES:
        # Made on a finished request it would be done without the request being resolved by it or the team seeing it on the ticket:
        # refuse, and let the agent reopen the request first (#325).
        raise MailboxChangeError("This request is already resolved or closed. Reopen it first, then use Try again, or use Done by hand if you made the change")
    change.state = MAILBOX_SCHEDULED if phase == "start" else MAILBOX_ACTIVE
    change.attempts = max(max_attempts(session) - 1, 0)
    change.next_attempt_at, change.updated_at = None, now
    if phase == "removal":
        change.ends_at = min(_aware(change.ends_at), now)
    session.add(change)
    _audit(session, actor.email, change, "try_again", phase)
    record_event(session, ticket, EVENT_TRY_AGAIN, actor.email, to_value=change.kind, detail=phase)


def done_by_hand(session: Session, change: MailboxChange, actor: User, now: Optional[datetime] = None) -> None:
    """The agent made (start) or removed (removal) the change in the Exchange admin centre. A hand-made start becomes active, so it
    still gets its automatic removal at the end time; the request is resolved and both people are told it is on. A hand-made removal
    ends it and both people are told it stopped. Records an event and an audit entry. Does not commit."""
    now = now or utcnow()
    change = _locked(session, change)
    phase = _require_failed(change)
    ticket = session.get(Ticket, change.ticket_id)
    _audit(session, actor.email, change, "done_by_hand", phase)
    record_event(session, ticket, EVENT_DONE_BY_HAND, actor.email, to_value=change.kind, detail=phase)
    if phase == "start":
        _on_applied(session, change, ticket, now, actor)
    else:
        _on_removed(session, change, ticket, now, actor)


def cancel(session: Session, change: MailboxChange, actor: User, now: Optional[datetime] = None) -> None:
    """Drops a change that could not be started: the request is resolved as cancelled and the requester is told. Only for a failed
    start; after a failed removal Exchange may still hold the change, so it is settled with Try again or Done by hand. Records an
    event and an audit entry. Does not commit."""
    now = now or utcnow()
    change = _locked(session, change)
    phase = _require_failed(change)
    if phase != "start":
        raise MailboxChangeError("Exchange may still hold this change, so it cannot be cancelled: use Try again or Done by hand")
    ticket = session.get(Ticket, change.ticket_id)
    change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
    session.add(change)
    _audit(session, actor.email, change, "cancelled", "by an agent")
    record_event(session, ticket, EVENT_CANCELLED, actor.email, to_value=change.kind, detail="by an agent")
    others = _others(session, ticket, change)
    if others:  # one of several changes on the request: say which was dropped; the others carry on and settle the request
        word = KIND_WORDS.get(change.kind, change.kind)
        _resolve(session, ticket, _cancelled_part_note(session, change, others), actor, others)
        body = render_template(
            session, "mailbox_part_cancelled_requester", {**ticket_values(ticket), "kind": word}
        ).body
    else:
        _resolve(session, ticket, "This request was cancelled by IT Support. Nothing was changed on the mailbox.", actor)
        body = render_template(session, "mailbox_cancelled_requester", ticket_values(ticket)).body
    record_notice(session, ticket, body)


# ---- What the requester can do (the portal, #309) -------------------------------------------------------------------


def cancel_by_requester(session: Session, change: MailboxChange, actor_email: str, now: Optional[datetime] = None) -> None:
    """The requester cancels before the start: the change is dropped and the request resolved with a note. Refused once the start
    time has come (the job may be making it right now: stop it after it starts instead). The caller has checked that the actor is
    the requester. Does not commit."""
    now = now or utcnow()
    change = _locked(session, change)
    if change.state != MAILBOX_SCHEDULED or _aware(change.starts_at) <= now:
        raise MailboxChangeError("This change has already started or is about to: stop it once it is on instead")
    ticket = session.get(Ticket, change.ticket_id)
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise MailboxChangeError("This request is waiting for approval: withdraw the request instead (#311)")
    change.state, change.next_attempt_at, change.updated_at = MAILBOX_CANCELLED, None, now
    session.add(change)
    _audit(session, actor_email, change, "cancelled", "by the requester before the start")
    record_event(session, ticket, EVENT_CANCELLED, actor_email, to_value=change.kind, detail="by the requester")
    others = _others(session, ticket, change)
    note = _cancelled_part_note(session, change, others) if others else "You cancelled this request before it started, so nothing was changed."
    _resolve(session, ticket, note, None, others)


def stop_now(session: Session, change: MailboxChange, actor_email: str, now: Optional[datetime] = None) -> None:
    """The requester stops a change that is on: its end becomes now, and the worker's next run removes it (and tells both people).
    Refused unless it is active. Does not commit."""
    now = now or utcnow()
    change = _locked(session, change)
    if change.state != MAILBOX_ACTIVE:
        raise MailboxChangeError("This change is not on, so there is nothing to stop")
    ticket = session.get(Ticket, change.ticket_id)
    change.ends_at, change.next_attempt_at, change.updated_at = min(_aware(change.ends_at), now), None, now
    session.add(change)
    _audit(session, actor_email, change, "stop_now", "the end time was brought forward to now")
    record_event(session, ticket, EVENT_STOPPED_EARLY, actor_email, to_value=change.kind)

"""Changing a ticket's end user, its requester (issue #279).

An agent who works a ticket can change who it is for: it was raised for the wrong person, a PA wrote in for someone
else, or a shared address sent it. `change_requester` is the one place that does it; the route only calls it.

Who may: `visibility.may_change_requester` (checked again here, so no caller can skip it). The new person is chosen from the
directory (app/people_directory.py, the picker on the ticket page) or is an address typed in. Whether it is the same person as now is asked of
app/identity.py (linked identities), never by comparing two addresses.

What follows, each decided in one place:

- A `requester_changed` ticket event (from and to addresses) and one audit entry. Events are never shown to a
  requester (the portal shows only `messages.requester_visible_messages`).
- The old requester is offered as an email follower (`followers.add_follower`); unticked, they lose the ticket in the
  portal, because that access comes from being the requester or a follower (`identity.my_ticket_condition`).
- `last_requester_channel` is set to email: replies go by email until the new requester writes in some other channel
  (a ticket raised in Teams would otherwise try the new person's Teams chat first).
- The SLA deadline of a Due date is the end of that day in the requester's time zone, so it is worked out again for the
  new person (`sla.recalculate_deadlines`).
- A pending approval round that has the new requester as an approver is not touched: the change is refused, because
  the requester never votes on their own request (app/approvals.py).
- An optional email to the new requester (template `requester_changed`, kind `outbox.REQUESTER_CHANGED`); never to the
  old one.

Nothing here commits.
"""

from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app import people_directory
from app.approvals import PENDING, current_approval
from app.audit import log_action
from app.config import settings
from app.followers import (
    InvalidEmail, NeverFollowListed, TooManyFollowers, add_follower, bare_address, followers_of, normalise_email,
    remove_follower,
)
from app.identity import addresses_of, person_for_address
from app.models import ApprovalVote, Ticket, User
from app.outbox import queue_requester_changed
from app.person_data import is_placeholder_address
from app.reply_channel import EMAIL
from app.sla import recalculate_deadlines
from app.ticket_service import record_event
from app.visibility import access_context, may_change_requester

EVENT_REQUESTER_CHANGED = "requester_changed"  # from_value: the old address; to_value: the new one


class RequesterChangeError(ValueError):
    """A reason the requester cannot be changed, worded for the agent."""


class NotAllowed(RequesterChangeError):
    """The agent may not change this ticket's requester."""


@dataclass
class Outcome:
    old: str
    new: str
    followed: bool = False  # the old requester now follows the ticket
    emailed: bool = False  # the email to the new requester was queued
    notes: tuple[str, ...] = ()  # things that were asked for and could not be done, worded for the agent


def typed_address(text: str) -> Optional[str]:
    """The normalised address in what was typed (a display name around it is dropped), or None when it is not an address."""
    text = (text or "").strip()
    if "@" not in text:
        return None
    try:
        return normalise_email(bare_address(text))
    except InvalidEmail:
        return None


def resolve_address(text: str) -> str:
    """The address the agent typed (the "Use {address}" choice of the picker). Raises RequesterChangeError when it is not one."""
    text = (text or "").strip()
    if not text:
        raise RequesterChangeError("Choose a person from the list, or type an email address and use it")
    address = typed_address(text)
    if address is None:
        raise RequesterChangeError(f"{text} is not a valid email address")
    return address


def _refuse_if_unfit(session: Session, ticket: Ticket, new: str) -> None:
    old = ticket.requester_email.strip().lower()
    if new == old or new in addresses_of(session, old):
        raise RequesterChangeError("That is already the end user of this ticket (it is the same person)")
    if is_placeholder_address(new):
        raise RequesterChangeError("That address belongs to an anonymised person and cannot be used")
    mailbox = settings.graph_shared_mailbox.strip().lower()
    if mailbox and new == mailbox:
        raise RequesterChangeError("That is the IT support mailbox itself, not a person")
    person = person_for_address(session, new)
    if person is not None and not person.is_active:
        raise RequesterChangeError(f"{person.display_name or new} has an account that is switched off")
    # The requester never votes on their own request (app/approvals.py): do not make an approver the requester of a
    # request they are being asked to approve. Someone not in the group, or a decided round, is no problem.
    approval = current_approval(session, ticket)
    if approval is not None and approval.status == PENDING:
        theirs = {a for a in addresses_of(session, new)}
        voters = session.exec(
            select(User.email).join(ApprovalVote, ApprovalVote.voter_id == User.id).where(ApprovalVote.approval_id == approval.id)
        ).all()
        if any(v.lower() in theirs for v in voters):
            raise RequesterChangeError(
                f"{person.display_name if person and person.display_name else new} is one of the approvers of this request, "
                "so cannot be its requester while it waits for approval"
            )


def change_requester(
    session: Session, ticket: Ticket, actor: User, raw_new: str = "", *, object_id: str = "", keep_old_as_follower: bool = True,
    email_new: bool = False,
) -> Outcome:
    """Makes the chosen person the ticket's end user: `object_id` is a directory choice (the server fetches it again and
    refuses anyone not found, switched off or without mail), else `raw_new` is an address that was typed. Raises NotAllowed
    (the rule in visibility) or
    RequesterChangeError (a reason worded for the agent). The caller locks the ticket row first and commits after."""
    if not may_change_requester(actor, ticket, access_context(session, actor)):
        raise NotAllowed("You cannot change the end user of this ticket")
    if (object_id or "").strip():
        try:
            chosen = people_directory.resolve(session, actor, object_id, exclude_self=False, use_blocklist=False)
            new = normalise_email(chosen.mail)
        except people_directory.DirectoryError as error:
            raise RequesterChangeError(str(error))
        except InvalidEmail:
            raise RequesterChangeError("That person has no usable email address")
    else:
        new = resolve_address(raw_new)
    _refuse_if_unfit(session, ticket, new)
    old = ticket.requester_email.strip()

    ticket.requester_email = new
    ticket.last_requester_channel = EMAIL
    session.add(ticket)
    recalculate_deadlines(session, ticket)

    # They are the requester now, so being an email follower as well is redundant (internal followers, who are agents
    # copied for another reason, are left alone).
    for follower in followers_of(session, ticket):
        if not follower.is_internal and follower.email == new:
            remove_follower(session, ticket, follower.id, actor.email)

    outcome = Outcome(old=old, new=new)
    notes: list[str] = []
    if keep_old_as_follower and not is_placeholder_address(old):
        try:
            added = add_follower(session, ticket, old, actor.email) is not None
            outcome.followed = added or any(not f.is_internal and f.email == old.lower() for f in followers_of(session, ticket))
        except NeverFollowListed as error:
            notes.append(f"{error.email} was not kept as a follower: it is on the never-follow list")
        except TooManyFollowers:
            notes.append(f"{old} was not kept as a follower: the ticket already has the most followers allowed")
    detail = "the old end user was kept as a follower" if outcome.followed else "the old end user was not kept as a follower"
    record_event(session, ticket, EVENT_REQUESTER_CHANGED, actor.email, from_value=old, to_value=new, detail=detail)
    log_action(session, actor.email, "ticket", ticket.id, "requester_changed", f"{old} -> {new}")

    if email_new:
        outcome.emailed = queue_requester_changed(session, ticket) is not None
        if not outcome.emailed:
            notes.append("The new end user was not emailed: mail sending is switched off")
    outcome.notes = tuple(notes)
    return outcome

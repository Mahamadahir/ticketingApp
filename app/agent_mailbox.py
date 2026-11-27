"""Set up a mailbox change from the agent ticket page (#364; tracker #305): for a request that arrived by email or Teams.

A request that comes in by email gets no automation (a sign-in proves who asks; an email's From address proves much less, so the
portal is the only place a person asks for a change themselves). An agent is IT, so they may do it for the requester: the card
**Set up mailbox change** on a triaged Service Request writes the same `MailboxChange` rows the portal does, with the same checks
and the same start and end emails, and no approval round. The agent's required note, saying how they confirmed the request (for
example "Confirmed by phone with the owner, 10:05"), is the approval: an event on the ticket (`EVENT_CONFIRMED`) and an entry in the
audit log, as an IT approver's note is on #311's path. The owner is then told, as on every other path, naming who it is for and
that IT set it up (`mailbox_changes._approved_by`).

The checks are the portal's (`app/portal_mailbox.py`; the people, the times and the overlap, loop and lock rules are the same
functions): who the owner and the other person are (picked with the people picker and fetched again, the block list applied), the
owner is not the other person, the times in the agent's own zone, no overlap with a change the mailbox already has, no forwarding
loop, and last the confirmations (keep-a-copy off, and always for Full access and Send As) which the server insists on whatever the
page did. Nothing is committed here: the route does that."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app import identity
from app.approvals import MAX_COMMENT_LENGTH
from app.audit import log_action
from app.config import settings
from app.mailbox_changes import (
    EVENT_AGENT_CONFIRMED as EVENT_CONFIRMED, MailboxChangeError, changes_for_ticket, create_mailbox_change, lock_owner,
)
from app.mailbox_executor import is_enabled
from app.models import (
    FINISHED_STATUSES, MAILBOX_CANCELLED, MAILBOX_ENDED, MailboxChange, RequestType, Ticket, TicketStatus, TicketType, User,
)
from app.people_directory import DirectoryError, Person, resolve
from app.portal_mailbox import (
    ACCESS_LABELS, ConfirmNoCopy, ForwardingForm, MailboxFormError, OwnerRefused, TargetRefused, Times, _forwards_back, _overlapping,
    _owner_words, read_times,
)
from app.ticket_service import EVENT_REQUEST_TYPE, record_event
from app.time_zones import format_moment, zone_for_user

log = logging.getLogger("app.people")

AUDIT_ACTION = "mailbox_change_confirmed"

FORWARDING, FULL_ACCESS, SEND_AS, BOTH = "forwarding", "full_access", "send_as", "both"
KIND_CHOICES = [
    (FORWARDING, "Forward mail"),
    (FULL_ACCESS, "Full access"),
    (SEND_AS, "Send As"),
    (BOTH, "Full access and Send As"),
]
KINDS = {FORWARDING: ["forwarding"], FULL_ACCESS: ["full_access"], SEND_AS: ["send_as"], BOTH: ["full_access", "send_as"]}

NOT_AVAILABLE = "A mailbox change cannot be set up on this ticket."
NOTE_REQUIRED = "Say how you confirmed this request (for example: Confirmed by phone with the owner, 10:05)"


class ConfirmAccess(MailboxFormError):
    """Everything else is fine but the access has not been confirmed: the warning is shown in the card."""


@dataclass
class AgentMailboxForm(ForwardingForm):
    """What the card holds, as typed (strings), so a refusal can show it again. `whose` is unused: the owner is always picked."""

    kind: str = FORWARDING
    note: str = ""  # how the agent confirmed the request: required
    confirmed: bool = False  # the red confirmation of keep-a-copy off, or of access, was given


def live_change(change: MailboxChange) -> bool:
    """Whether a change still counts against offering the card: it is waiting, on, failed or stopping. One that ended or was
    cancelled is over."""
    return change.state not in (MAILBOX_ENDED, MAILBOX_CANCELLED)


def card_offered(session: Session, ticket: Ticket) -> bool:
    """Whether the Set up mailbox change card is offered on this ticket (the chip rule; who may see it is `can_work`, decided by
    the page): a triaged Service Request that is not finished or waiting for approval, with no live mailbox change yet (a
    portal request for a mailbox has its change already). Needs scheduled mailbox changes switched on."""
    return (
        is_enabled()
        and ticket.type == TicketType.SERVICE_REQUEST
        and ticket.triaged_at is not None
        and ticket.status not in FINISHED_STATUSES
        and ticket.status != TicketStatus.AWAITING_APPROVAL
        and not any(live_change(c) for c in changes_for_ticket(session, ticket))
    )


def requester_person(session: Session, agent: User, ticket: Ticket) -> Optional[Person]:
    """The requester as a person of the directory, or None when they do not resolve (an outside address, a guest, no mailbox, on
    the block list). The picker starts with them as the mailbox owner."""
    person = identity.person_for_address(session, ticket.requester_email)
    if person is None:
        return None
    if person.entra_object_id:
        object_id = person.entra_object_id
    elif not settings.graph_configured:
        object_id = f"local-{person.id}"  # the local directory's stand-in id (app/people_directory.py)
    else:
        return None
    try:
        return resolve(session, agent, object_id, exclude_self=False)
    except DirectoryError:
        return None


def _owner(session: Session, agent: User, ticket: Ticket, form: AgentMailboxForm) -> tuple[Person, Optional[Person]]:
    """The owner picked (the requester when none was picked), and the requester as a person (None when they do not resolve)."""
    requester = requester_person(session, agent, ticket) if (not form.owner_object_id.strip() or not form.target_object_id.strip()) else None
    if not form.owner_object_id.strip():
        if requester is None:
            raise OwnerRefused("Choose whose mailbox this is for")
        return requester, requester
    try:
        return resolve(session, agent, form.owner_object_id, exclude_self=False), requester
    except DirectoryError as error:
        raise OwnerRefused(_owner_words(str(error)))


def _other_person(session: Session, agent: User, requester: Optional[Person], owner: Person, form: AgentMailboxForm) -> Person:
    """Who it goes to (forwarding) or is given to (access): the person picked, or when none was picked the requester, if they are not
    the owner. Fetched again by id like every picked person."""
    if form.target_object_id.strip():
        try:
            return resolve(session, agent, form.target_object_id, exclude_self=False)
        except DirectoryError as error:
            raise TargetRefused(str(error))
    if requester is None or requester.mail == owner.mail:
        raise TargetRefused("Choose who it is for")
    return requester


def copy_off_warning(owner: str, name: str, until: str) -> str:
    return (
        f"{owner}'s mail will not be kept. Mail sent to {owner} will go only to {name}. Nothing will arrive in their own mailbox "
        f"until {until}."
    )


def access_warning(owner: str, name: str, kinds: list[str], until: str) -> str:
    parts = []
    if "full_access" in kinds:
        parts.append(f"{name} will be able to read all mail already in {owner}'s mailbox.")
    if "send_as" in kinds:
        parts.append(f"{name} will be able to send email that appears to come from {owner}.")
    return " ".join(parts) + f" This lasts until {until}."


def _overlap_words(owner: Person, person: Person, kind: str, times: Times) -> str:
    if kind == "forwarding":
        return f"{owner.name}'s mail already has forwarding set up for some of that time. Choose times that do not overlap"
    return f"{person.name} already has {ACCESS_LABELS[kind]} on {owner.name}'s mailbox for some of that time. Choose times that do not overlap"


def _matching_type(session: Session, kind: str) -> Optional[RequestType]:
    return session.exec(
        select(RequestType).where(RequestType.mailbox_action == kind, RequestType.is_active.is_(True)).order_by(RequestType.id)
    ).first()


def submit(
    session: Session, agent: User, ticket: Ticket, form: AgentMailboxForm, now: Optional[datetime] = None,
) -> list[MailboxChange]:
    """Writes the change (two for Full access and Send As together, each on its own) on this triaged request, and schedules it. The
    order is the portal's: the people, the note, the times, the locks and the overlap and loop checks, then the confirmations. Raises
    MailboxFormError (ConfirmNoCopy or ConfirmAccess for the warnings) with nothing written. Does not commit; on a refusal the caller
    rolls back."""
    if form.kind not in KINDS:
        raise MailboxFormError("Choose what kind of change to set up")
    session.refresh(ticket, with_for_update=True)  # two clicks, or two agents, take turns: the second sees the first's change
    if not card_offered(session, ticket):
        raise MailboxFormError(NOT_AVAILABLE)
    kinds = KINDS[form.kind]
    owner, requester = _owner(session, agent, ticket, form)
    person = _other_person(session, agent, requester, owner, form)
    if person.mail == owner.mail:
        raise TargetRefused("It cannot go to the person whose mailbox it is. Choose someone else.")
    note = (form.note or "").strip()
    if not note:
        raise MailboxFormError(NOTE_REQUIRED)
    if len(note) > MAX_COMMENT_LENGTH:
        raise MailboxFormError(f"The note can be at most {MAX_COMMENT_LENGTH} characters")
    what = "Forwarding" if kinds == ["forwarding"] else "Access"
    times = read_times(session, agent, form, now, what=what)
    zone = zone_for_user(session, agent)
    until = format_moment(times.ends_at, zone)
    if kinds == ["forwarding"]:
        # Both mailboxes' locks, in a fixed order, before the loop check (as the portal does): two changes at once take turns.
        for address in sorted({owner.mail.strip().lower(), person.mail.strip().lower()}):
            lock_owner(session, address)
        if _forwards_back(session, owner, identity.addresses_of(session, owner.mail), person, times):
            raise MailboxFormError(f"{person.name}'s mail is already forwarded to {owner.name} during that time, so this would create a loop.")
    for kind in kinds:
        if _overlapping(session, owner.mail, times, kind, None if kind == "forwarding" else person.mail):
            raise MailboxFormError(_overlap_words(owner, person, kind, times))
    if kinds == ["forwarding"]:
        if not form.keep_copy and not form.confirmed:
            raise ConfirmNoCopy(copy_off_warning(owner.name, person.name, until))
    elif not form.confirmed:
        raise ConfirmAccess(access_warning(owner.name, person.name, kinds, until))

    try:
        _set_request_type(session, ticket, kinds[0], agent)
        record_event(session, ticket, EVENT_CONFIRMED, agent.email, to_value=", ".join(kinds), detail=note)
        log_action(session, agent.email, "ticket", ticket.id, AUDIT_ACTION, note)
        made = [
            create_mailbox_change(
                session, ticket, kind=kind, owner_address=owner.mail, target_address=person.mail, starts_at=times.starts_at,
                ends_at=times.ends_at, created_by=agent.email, owner_object_id=owner.object_id, target_object_id=person.object_id,
                keep_copy=form.keep_copy,
            )
            for kind in kinds
        ]
    except MailboxChangeError as error:
        log.warning("mailbox change refused on ticket %s for %s: %s", ticket.number, agent.email, error)
        raise MailboxFormError("The change could not be set up right now.")
    return made


def _set_request_type(session: Session, ticket: Ticket, kind: str, agent: User) -> None:
    """A request with no kind of request gets the one that matches (an agent's own choice is left alone)."""
    if ticket.request_type_id is not None:
        return
    request_type = _matching_type(session, kind)
    if request_type is None:
        return
    ticket.request_type_id = request_type.id
    session.add(ticket)
    record_event(session, ticket, EVENT_REQUEST_TYPE, agent.email, to_value=request_type.name, detail="set up as a mailbox change")

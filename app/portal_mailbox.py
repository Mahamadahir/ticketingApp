"""The portal's mailbox forms (#309; tracker #305): ask for your own mail to be forwarded to a colleague, and see and end it.

`submit_forwarding` is the one place the form is read and checked, in this order: who the other person is (the object id the
people picker stored, fetched again: `app.people_directory.resolve`), the times (read in the person's own zone through
`time_zones.parse_local_moment`), and last the keep-a-copy warning (a request that turns the copy off is refused until
`confirm_no_copy` is sent, so a refusal for any other reason is shown first). Then the request is raised through
`app.raising.raise_ticket` (a portal Service Request of a type that triages itself) and the change is written with
`app.mailbox_changes.create_mailbox_change`. Nothing is committed here: the route does that.

The mailbox is always the signed-in person: no function here takes a mailbox from the form. `view` is the plain-words card the
request page shows, with what the requester may still do (Cancel before the start, Stop forwarding now while it is on).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import logging

from sqlmodel import Session, select

from app import identity
from app.mailbox_changes import MailboxChangeError, changes_for_ticket, create_mailbox_change, failed_phase
from app.mailbox_executor import is_enabled
from app.models import (
    MAILBOX_ACTIVE, MAILBOX_APPLYING, MAILBOX_CANCELLED, MAILBOX_ENDED, MAILBOX_FAILED, MAILBOX_REMOVING, MAILBOX_SCHEDULED,
    MailboxChange, RequestType, Ticket, User,
)
from app.people_directory import DirectoryError, Person, resolve
from app.raising import MAX_SUBJECT_LENGTH, RaiseError, raise_ticket
from app.settings_store import MAILBOX_CHANGE_MAX_DAYS, count_setting
from app.time_zones import MomentError, format_moment, parse_local_moment, zone_for_user

log = logging.getLogger("app.people")

QUARTER_HOUR = 15
QUARTER_TIMES = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, QUARTER_HOUR)]
NOW, LATER = "now", "later"

ACCESS_ACTIONS = ("full_access", "send_as")  # a request type with either of these opens the one "give someone access" form

NOT_AVAILABLE = "Forwarding your mail is not available right now. Please contact IT Support."


class MailboxFormError(ValueError):
    """A reason the request cannot be made, worded for the person filling in the form."""


class TargetRefused(MailboxFormError):
    """The chosen person cannot be used: the form forgets them, so the person is asked to choose again."""


class ConfirmAccess(MailboxFormError):
    """Everything else is fine but access to the mailbox has not been confirmed (#310): the warning always comes first, so this
    is the answer to every request that does not carry `confirm_access`."""


class ConfirmNoCopy(MailboxFormError):
    """Everything else is fine but the copy is off and has not been confirmed: the warning is shown (a modal with scripts, a
    confirm block without)."""


@dataclass
class ForwardingForm:
    """What the form holds, as typed (strings), so a refusal can show it again."""

    target_object_id: str = ""
    target_name: str = ""  # shown only; the person is always fetched again by id
    keep_copy: bool = True
    start_mode: str = NOW
    start_date: str = ""
    start_time: str = "09:00"
    end_date: str = ""
    end_time: str = "17:00"
    confirm_no_copy: bool = False


@dataclass
class AccessForm(ForwardingForm):
    """The "give someone access to my mailbox" form (#310): the same person and times as forwarding, and which permissions."""

    full_access: bool = False
    send_as: bool = False
    confirm_access: bool = False


@dataclass(frozen=True)
class Times:
    starts_at: datetime
    ends_at: datetime
    starts_now: bool


def _quarter(text: str, what: str) -> str:
    text = (text or "").strip()
    if text not in QUARTER_TIMES:
        raise MailboxFormError(f"Choose the {what} time in 15-minute steps, such as 09:00, 09:15, 09:30 or 09:45")
    return text


def read_times(session: Session, user: User, form: ForwardingForm, now: Optional[datetime] = None, what: str = "Forwarding") -> Times:
    """The start and end as UTC, from the dates and quarter-hour times typed in the person's own zone. Refuses a time that is not
    a quarter hour or does not exist (the clocks skip it), a start already past (Now is the way to start straight away), an end
    that is not after the start and a span over the `mailbox_change_max_days` setting."""
    now = now or datetime.now(timezone.utc)
    zone = zone_for_user(session, user)
    try:
        if form.start_mode == LATER:
            if not form.start_date.strip():
                raise MailboxFormError("Choose the start date, or choose Now")
            starts_at = parse_local_moment(f"{form.start_date.strip()}T{_quarter(form.start_time, 'start')}", zone)
            if starts_at < now:
                raise MailboxFormError("The start time has already passed. Choose a later time, or choose Now")
        else:
            starts_at = now
        if not form.end_date.strip():
            raise MailboxFormError(f"Choose the end date: {what.lower()} always has an end")
        ends_at = parse_local_moment(f"{form.end_date.strip()}T{_quarter(form.end_time, 'end')}", zone)
    except MomentError as error:
        raise MailboxFormError(str(error))
    if ends_at <= starts_at:
        raise MailboxFormError("The end must be after the start")
    longest = count_setting(session, MAILBOX_CHANGE_MAX_DAYS)
    if ends_at - starts_at > timedelta(days=longest):
        raise MailboxFormError(f"{what} can last at most {longest} days. Choose an earlier end")
    return Times(starts_at, ends_at, form.start_mode != LATER)


def _subject(person: Person) -> str:
    return f"Forward my mail to {person.name}"[:MAX_SUBJECT_LENGTH]


def _description(person: Person, times: Times, zone: str, keep_copy: bool) -> str:
    start = "now" if times.starts_now else f"from {format_moment(times.starts_at, zone)}"
    return (
        f"Please forward the mail sent to me to {person.name} ({person.mail}) {start} until {format_moment(times.ends_at, zone)}. "
        + ("A copy will stay in my mailbox." if keep_copy else "No copy will be kept in my mailbox: it goes only to them.")
    )


def copy_off_warning(name: str, until: str) -> str:
    """The words of the keep-a-copy warning, shown by the modal, the no-script confirm block and the refusal."""
    return f"Your mail will not be kept. Mail sent to you will go only to {name}. Nothing will arrive in your own mailbox until {until}."


def submit_forwarding(
    session: Session, user: User, request_type: RequestType, form: ForwardingForm, now: Optional[datetime] = None,
) -> Ticket:
    """Raises the request and writes the change for the signed-in person's own mailbox. Raises MailboxFormError (ConfirmNoCopy for
    the warning) with nothing written. Does not commit; on any refusal the caller rolls back."""
    if request_type.mailbox_action != "forwarding" or not is_enabled():
        raise MailboxFormError(NOT_AVAILABLE)
    try:
        person = resolve(session, user, form.target_object_id)
    except DirectoryError as error:
        raise TargetRefused(str(error))
    times = read_times(session, user, form, now)
    zone = zone_for_user(session, user)
    if _overlapping(session, user, times, "forwarding"):
        raise MailboxFormError(
            "You already have forwarding set up for some of that time. Cancel or stop it on its request page first, or choose times that do not overlap"
        )
    if not form.keep_copy and not form.confirm_no_copy:
        raise ConfirmNoCopy(copy_off_warning(person.name, format_moment(times.ends_at, zone)))
    try:
        ticket = raise_ticket(
            session, user, subject=_subject(person), description=_description(person, times, zone, form.keep_copy),
            request_type=request_type,
        )
        session.flush()
        create_mailbox_change(
            session, ticket, kind="forwarding", owner_address=user.email, target_address=person.mail, starts_at=times.starts_at,
            ends_at=times.ends_at, created_by=user.email, owner_object_id=user.entra_object_id, target_object_id=person.object_id,
            keep_copy=form.keep_copy,
        )
    except RaiseError as error:
        raise MailboxFormError(str(error))
    except MailboxChangeError as error:
        log.warning("mailbox change refused for %s: %s", user.email, error)  # the detail is for the log, not the requester
        raise MailboxFormError("This request could not be set up right now. Please contact IT Support.")
    return ticket


def _overlapping(session: Session, user: User, times: Times, kind: str, target_address: Optional[str] = None) -> bool:
    """Whether this person already has a change of this kind that is waiting, starting, on or stopping at any time in the same span.
    Exchange holds one forwarding setting per mailbox, so a second would overwrite the first and the first one's removal would
    switch the second off early. For Full access and Send As (`target_address` given) it is the same permission for the same
    other person: the first one's removal would take the second one's away early. (It also stops a double click making two.)"""
    live = (MAILBOX_SCHEDULED, MAILBOX_APPLYING, MAILBOX_ACTIVE, MAILBOX_REMOVING)
    query = select(MailboxChange.id).where(
        MailboxChange.owner_address == user.email.lower(), MailboxChange.kind == kind, MailboxChange.state.in_(live),
        MailboxChange.starts_at < times.ends_at, MailboxChange.ends_at > times.starts_at,
    )
    if target_address is not None:
        query = query.where(MailboxChange.target_address == target_address.strip().lower())
    return session.exec(query).first() is not None


# ---- Giving someone access to my mailbox (#310) ------------------------------------------------------------------

ACCESS_LABELS = {"full_access": "Full access", "send_as": "Send As"}
ACCESS_NOT_AVAILABLE = "Giving someone access to your mailbox is not available right now. Please contact IT Support."


def access_warning(name: str, kinds: list[str], until: str) -> str:
    """The words of the access warning, shown by the modal (the script builds the same sentences), the no-script confirm block
    and the refusal. One sentence for each permission chosen."""
    parts = []
    if "full_access" in kinds:
        parts.append(f"{name} will be able to read all mail already in your mailbox.")
    if "send_as" in kinds:
        parts.append(f"{name} will be able to send email that appears to come from you.")
    return " ".join(parts) + f" This lasts until {until}."


def _access_subject(person: Person) -> str:
    return f"Give {person.name} access to my mailbox"[:MAX_SUBJECT_LENGTH]


def _access_description(person: Person, kinds: list[str], times: Times, zone: str) -> str:
    start = "now" if times.starts_now else f"from {format_moment(times.starts_at, zone)}"
    words = " and ".join(ACCESS_LABELS[k] for k in kinds)
    return f"Please give {person.name} ({person.mail}) {words} on my mailbox {start} until {format_moment(times.ends_at, zone)}."


def submit_access(
    session: Session, user: User, request_type: RequestType, form: AccessForm, now: Optional[datetime] = None,
) -> Ticket:
    """Raises one request and writes one change for each permission chosen (Full access, Send As) on the signed-in person's own
    mailbox, so each is made, fails and is removed on its own. The order is `submit_forwarding`'s: the person, the choice, the times,
    the overlap, then the warning (always: `confirm_access` is required, so every other refusal is shown first), then the ticket
    and the changes. Raises MailboxFormError (ConfirmAccess for the warning) with nothing written. Does not commit."""
    if request_type.mailbox_action not in ACCESS_ACTIONS or not is_enabled():
        raise MailboxFormError(ACCESS_NOT_AVAILABLE)
    try:
        person = resolve(session, user, form.target_object_id)
    except DirectoryError as error:
        raise TargetRefused(str(error))
    kinds = [kind for kind, chosen in (("full_access", form.full_access), ("send_as", form.send_as)) if chosen]
    if not kinds:
        raise MailboxFormError("Choose Full access, Send As, or both")
    times = read_times(session, user, form, now, what="Access")
    zone = zone_for_user(session, user)
    for kind in kinds:
        if _overlapping(session, user, times, kind, person.mail):
            raise MailboxFormError(
                f"{person.name} already has {ACCESS_LABELS[kind]} on your mailbox for some of that time. Cancel or stop it on its "
                "request page first, or choose times that do not overlap"
            )
    if not form.confirm_access:
        raise ConfirmAccess(access_warning(person.name, kinds, format_moment(times.ends_at, zone)))
    try:
        ticket = raise_ticket(
            session, user, subject=_access_subject(person), description=_access_description(person, kinds, times, zone),
            request_type=request_type,
        )
        session.flush()
        for kind in kinds:
            create_mailbox_change(
                session, ticket, kind=kind, owner_address=user.email, target_address=person.mail, starts_at=times.starts_at,
                ends_at=times.ends_at, created_by=user.email, owner_object_id=user.entra_object_id, target_object_id=person.object_id,
            )
    except RaiseError as error:
        raise MailboxFormError(str(error))
    except MailboxChangeError as error:
        log.warning("mailbox change refused for %s: %s", user.email, error)  # the detail is for the log, not the requester
        raise MailboxFormError("This request could not be set up right now. Please contact IT Support.")
    return ticket


# ---- The request page ---------------------------------------------------------------------------------------------

STATE_LABELS = {
    MAILBOX_SCHEDULED: ("Scheduled", "blue"),
    MAILBOX_APPLYING: ("Starting", "blue"),
    MAILBOX_ACTIVE: ("On", "green"),
    MAILBOX_REMOVING: ("Stopping", "blue"),
    MAILBOX_ENDED: ("Stopped", "gray"),
    MAILBOX_CANCELLED: ("Cancelled", "gray"),
}


@dataclass
class ChangeView:
    heading: str
    change_id: int = 0
    rows: list[tuple[str, str]] = field(default_factory=list)
    state: str = ""
    tone: str = "gray"
    line: str = ""
    can_cancel: bool = False
    can_stop: bool = False
    # What the stop step says (it differs per kind: forwarding stops "forwarding", the others remove the access).
    stop_summary: str = "Stop forwarding now"
    stop_text: str = "Stop now? Your mail goes back to arriving only in your own mailbox. IT Support will confirm by email when it has stopped."
    stop_button: str = "Yes, stop forwarding"
    cancel_summary: str = "Cancel this request"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


HEADINGS = {"forwarding": "Forwarding my mail", "full_access": "Full access to my mailbox", "send_as": "Send As on my mailbox"}


def _view_of(session: Session, change: MailboxChange, user: User, now: datetime) -> ChangeView:
    zone = zone_for_user(session, user)
    person = identity.person_for_address(session, change.target_address)
    target = f"{person.display_name} ({change.target_address})" if person is not None and person.display_name else change.target_address
    phase = failed_phase(change)
    if change.state == MAILBOX_FAILED:
        # A failed removal is the team's to fix and the requester is told nothing until it is: the change still reads as stopping.
        label, tone = ("Could not be set up", "red") if phase == "start" else ("Stopping", "blue")
    else:
        label, tone = STATE_LABELS.get(change.state, (change.state.replace("_", " ").capitalize(), "gray"))
    starts, ends = format_moment(change.starts_at, zone), format_moment(change.ends_at, zone)
    result = ChangeView(heading=HEADINGS.get(change.kind, "Mailbox change"), change_id=change.id or 0, state=label, tone=tone)
    if change.kind == "forwarding":
        result.rows = [
            ("Forwarded to", target),
            ("A copy in my mailbox", "Kept" if change.keep_copy else "Not kept: my mail goes only to them"),
            ("From", starts), ("Until", ends),
        ]
    else:
        word = ACCESS_LABELS.get(change.kind, "Access")
        result.rows = [("Given to", target), ("From", starts), ("Until", ends)]
        result.stop_summary = f"Remove {word} now"
        result.stop_text = (
            f"Remove {word} now? {person.display_name if person is not None and person.display_name else change.target_address} "
            "will no longer have it. IT Support will confirm by email when it has been removed."
        )
        result.stop_button = f"Yes, remove {word}"
        result.cancel_summary = f"Cancel {word}"
    noun = "forwarding" if change.kind == "forwarding" else "access"
    if change.state in (MAILBOX_SCHEDULED, MAILBOX_APPLYING):
        result.line = f"This will start at {starts} and end at {ends}."
    elif change.state == MAILBOX_ACTIVE:
        result.line = f"This is on now and ends at {ends}."
    elif change.state == MAILBOX_ENDED:
        result.line = "This has stopped. Your mailbox is back to normal."
    elif change.state == MAILBOX_CANCELLED:
        result.line = "This was cancelled. Nothing was changed on your mailbox."
    elif change.state == MAILBOX_FAILED and phase == "start":
        result.line = "IT Support could not set this up. Nothing was changed on your mailbox, and the team has been told."
    else:
        result.line = f"This {noun} is being stopped."
    result.can_cancel = change.state == MAILBOX_SCHEDULED and _aware(change.starts_at) > now
    result.can_stop = change.state == MAILBOX_ACTIVE
    return result


def views(session: Session, ticket: Ticket, user: User, now: Optional[datetime] = None) -> list[ChangeView]:
    """The mailbox changes this request made, in plain words for its requester, oldest first (one for forwarding; Full access and
    Send As asked together are two, each with its own state and buttons). Empty for any other ticket (and for the incident raised
    for a failed removal, which is the team's, not the requester's). Only the person whose mailbox it is sees them."""
    now = now or datetime.now(timezone.utc)
    mine = identity.requester_emails(user)
    return [_view_of(session, change, user, now) for change in changes_for_ticket(session, ticket) if change.owner_address in mine]


def view(session: Session, ticket: Ticket, user: User, now: Optional[datetime] = None) -> Optional[ChangeView]:
    """The first of `views`, or None."""
    found = views(session, ticket, user, now)
    return found[0] if found else None

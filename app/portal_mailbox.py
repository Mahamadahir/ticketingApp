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

from sqlmodel import Session

from app import identity
from app.mailbox_changes import MailboxChangeError, change_for_ticket, create_mailbox_change, failed_phase
from app.mailbox_executor import is_enabled
from app.models import (
    MAILBOX_ACTIVE, MAILBOX_APPLYING, MAILBOX_CANCELLED, MAILBOX_ENDED, MAILBOX_FAILED, MAILBOX_REMOVING, MAILBOX_SCHEDULED,
    RequestType, Ticket, User,
)
from app.people_directory import DirectoryError, Person, resolve
from app.raising import MAX_SUBJECT_LENGTH, RaiseError, raise_ticket
from app.settings_store import MAILBOX_CHANGE_MAX_DAYS, count_setting
from app.time_zones import MomentError, format_moment, parse_local_moment, zone_for_user

QUARTER_HOUR = 15
QUARTER_TIMES = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, QUARTER_HOUR)]
NOW, LATER = "now", "later"

NOT_AVAILABLE = "Forwarding your mail is not available right now. Please contact IT Support."


class MailboxFormError(ValueError):
    """A reason the request cannot be made, worded for the person filling in the form."""


class TargetRefused(MailboxFormError):
    """The chosen person cannot be used: the form forgets them, so the person is asked to choose again."""


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


def read_times(session: Session, user: User, form: ForwardingForm, now: Optional[datetime] = None) -> Times:
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
            raise MailboxFormError("Choose the end date: forwarding always has an end")
        ends_at = parse_local_moment(f"{form.end_date.strip()}T{_quarter(form.end_time, 'end')}", zone)
    except MomentError as error:
        raise MailboxFormError(str(error))
    if ends_at <= starts_at:
        raise MailboxFormError("The end must be after the start")
    longest = count_setting(session, MAILBOX_CHANGE_MAX_DAYS)
    if ends_at - starts_at > timedelta(days=longest):
        raise MailboxFormError(f"Forwarding can last at most {longest} days. Choose an earlier end")
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
        text = str(error)
        raise MailboxFormError(NOT_AVAILABLE if "not switched on" in text else f"This request could not be set up: {text}")
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
    rows: list[tuple[str, str]] = field(default_factory=list)
    state: str = ""
    tone: str = "gray"
    line: str = ""
    can_cancel: bool = False
    can_stop: bool = False


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def view(session: Session, ticket: Ticket, user: User, now: Optional[datetime] = None) -> Optional[ChangeView]:
    """The mailbox change this request made, in plain words for its requester; None for any other ticket (and for the incident
    raised for a failed removal, which is the team's, not the requester's). Only the person whose mailbox it is sees it."""
    change = change_for_ticket(session, ticket)
    if change is None or change.ticket_id != ticket.id or change.owner_address not in identity.requester_emails(user):
        return None
    now = now or datetime.now(timezone.utc)
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
    result = ChangeView(heading="Forwarding my mail", state=label, tone=tone)
    if change.kind == "forwarding":
        result.rows = [
            ("Forwarded to", target),
            ("A copy in my mailbox", "Kept" if change.keep_copy else "Not kept: my mail goes only to them"),
            ("From", starts), ("Until", ends),
        ]
    else:
        result.heading = "Mailbox change"
        result.rows = [("With", target), ("From", starts), ("Until", ends)]
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
        result.line = "This is being stopped."
    result.can_cancel = change.state == MAILBOX_SCHEDULED and _aware(change.starts_at) > now
    result.can_stop = change.state == MAILBOX_ACTIVE
    return result

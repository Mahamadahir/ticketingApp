"""How an agent sees a ticket status: one label and one tone per status, used everywhere an agent sees one (the queue,
the ticket page, Team activity, the lists of linked tickets, the status pickers). Requesters have their own wording
in `messages.requester_status`; this is not it.

The tone says whose move it is, and the label always carries the meaning (colour is never the only signal):
blue with us, amber waiting on someone outside the team, green done but can still reopen, gray parked.
Red is not used here: it is kept for SLA breaches."""

from typing import Optional, Union

from app.models import FINISHED_STATUSES, TicketStatus

STATUS_LABELS = {
    TicketStatus.NEW: "New",
    TicketStatus.OPEN: "Open",
    TicketStatus.IN_PROGRESS: "In progress",
    TicketStatus.AWAITING_USER: "Awaiting user",
    TicketStatus.AWAITING_APPROVAL: "Awaiting approval",
    TicketStatus.WITH_SUPPLIER: "With supplier",
    TicketStatus.WITH_MSP: "With MSP",
    TicketStatus.SCHEDULED: "Scheduled",
    TicketStatus.RESOLVED: "Resolved",
    TicketStatus.CLOSED: "Closed",
}

STATUS_TONES = {
    TicketStatus.NEW: "blue",
    TicketStatus.OPEN: "blue",
    TicketStatus.IN_PROGRESS: "blue",
    TicketStatus.AWAITING_USER: "amber",
    TicketStatus.AWAITING_APPROVAL: "amber",
    TicketStatus.WITH_SUPPLIER: "amber",
    TicketStatus.WITH_MSP: "amber",
    TicketStatus.RESOLVED: "green",
    TicketStatus.CLOSED: "gray",
    TicketStatus.SCHEDULED: "gray",
}

# The queue's Status filter: "Not finished" is everything except Resolved and Closed.
NOT_FINISHED = "active"


def _status_of(status: Union[TicketStatus, str, None]) -> Optional[TicketStatus]:
    try:
        return status if isinstance(status, TicketStatus) else TicketStatus(str(status).lower())
    except ValueError:
        return None


def status_label(status: Union[TicketStatus, str, None]) -> str:
    """"With MSP", "In progress": the label of a status given as the enum or its stored value."""
    known = _status_of(status)
    return STATUS_LABELS[known] if known else (str(status).replace("_", " ").capitalize() if status else "None")


def status_tone(status: Union[TicketStatus, str, None]) -> str:
    """The 9A chip tone (the `t-` class suffix) of a status; gray for anything unknown."""
    known = _status_of(status)
    return STATUS_TONES[known] if known else "gray"


def status_options() -> list[tuple[str, str]]:
    """(value, label) of the queue's Status filter after "Any status": Not finished, then every status in order."""
    return [(NOT_FINISHED, "Not finished")] + [(s.value, STATUS_LABELS[s]) for s in TicketStatus]


def not_finished() -> tuple[TicketStatus, ...]:
    return tuple(s for s in TicketStatus if s not in FINISHED_STATUSES)

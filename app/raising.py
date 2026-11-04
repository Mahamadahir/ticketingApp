"""Raising a ticket by hand (phase 1G).

Two modes, one form. "For myself" is the normal end-user case (source portal). An agent
can also raise one "for someone else" (source agent): the person came to the desk, or
phoned, and the agent logs it for them. This module is the only place that creates a
`source=agent` ticket, because backdating (app/backdating.py) reads that source to decide
who may be backdated. Splitting copies its original's source and mail will never set it.

The agent can classify and assign the ticket in the same step, so one they already
understand skips the triage queue, and can set when it was reported and, for a fix made
in person, when it was resolved.
"""

from datetime import date, datetime
from typing import Optional

from sqlmodel import Session, select

from app.backdating import BackdateError, backdate_ticket
from app.followers import InvalidEmail, normalise_email
from app.messages import record_inbound
from app.outbox import queue_raised_for
from app.models import (
    Category,
    RequestType,
    Ticket,
    TicketSource,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.notification_settings import EMAIL, RAISED_FOR_YOU, is_enabled
from app.settings_store import TELL_REQUESTER_RAISED_FOR, get_flag
from app.required_by import RequiredByError, set_required_by
from app.sla import Level
from app.resolution import ResolveError, clean_notes, resolve_ticket
from app.ticket_service import EVENT_REQUEST_TYPE, create_ticket, record_event
from app.request_questions import QuestionError, save_answers
from app.request_types import self_triage
from app.triage import triage_ticket

AGENT_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)
MAX_SUBJECT_LENGTH = 200


class RaiseError(ValueError):
    """A reason the ticket cannot be raised, worded for whoever is filling in the form."""


EVENT_REQUESTER_NOTICE = "requester_notice"  # on a ticket raised for someone: whether they are to be told


def tell_requester_decision(session: Session, requester_email: Optional[str] = None) -> tuple[bool, str]:
    """Whether the requester is to be emailed that a ticket was raised for them, and why: the
    Settings switch, and, given their address, their own choice if they have an account. The
    email is sent once mail exists (phase 3).

    The answer is recorded on the ticket when it is raised, as history for the agent. It can go
    stale (an admin flips the switch, the person opts out an hour later), so whatever sends the
    email must ask again at send time and let a "no" win over the recorded "will be told"."""
    if not get_flag(session, TELL_REQUESTER_RAISED_FOR):
        return False, "switched off for everyone in Settings"
    requester = (
        session.exec(select(User).where(User.email == requester_email.strip().lower())).first()
        if requester_email
        else None
    )
    if requester is not None and not is_enabled(session, requester, RAISED_FOR_YOU, EMAIL):
        return False, "they turned this off in their own notification settings"
    return True, "they will be told by email once mail is connected"


def should_tell_requester(session: Session, requester_email: Optional[str] = None) -> bool:
    return tell_requester_decision(session, requester_email)[0]


def is_agent(user: User) -> bool:
    return user.role in AGENT_ROLES


def raise_ticket(
    session: Session,
    creator: User,
    *,
    subject: str,
    description: str,
    for_email: Optional[str] = None,
    ticket_type: TicketType = TicketType.INCIDENT,
    category: Optional[Category] = None,
    impact: Optional[Level] = None,
    urgency: Optional[Level] = None,
    assignee: Optional[User] = None,
    reported_at: Optional[datetime] = None,
    resolved_at: Optional[datetime] = None,
    request_type: Optional[RequestType] = None,
    resolution_notes: Optional[str] = None,
    required_by: Optional[date] = None,
    answers: Optional[dict[int, str]] = None,
) -> Ticket:
    """Raises a ticket. With for_email an agent raises it on that person's behalf; without,
    the creator raises it for themselves. Only the agent mode can classify, assign or
    backdate. A request whose type offers it can carry a Required by date, from either mode; a past
    date is refused unless the agent is backdating (giving reported or resolved times).
    Nothing is committed: the caller does that."""
    subject, description = (subject or "").strip(), (description or "").strip()
    if not subject:
        raise RaiseError("Enter a subject")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise RaiseError(f"The subject can be at most {MAX_SUBJECT_LENGTH} characters")
    if not description:
        raise RaiseError("Describe the problem")

    on_behalf = bool(for_email and for_email.strip())
    if not on_behalf and ticket_type != TicketType.INCIDENT:
        raise RaiseError("Only an agent can choose the type of a ticket")
    if request_type is not None:
        if not request_type.is_active:
            raise RaiseError("That kind of request is no longer available")
        ticket_type = TicketType.SERVICE_REQUEST  # a catalogue request is a service request
    if resolved_at is not None and reported_at is None:
        # Fixed at the desk: it was reported the moment it was fixed, unless the agent says otherwise.
        reported_at = resolved_at
    extras = (category, assignee, reported_at, resolved_at)
    if on_behalf:
        if not is_agent(creator):
            raise RaiseError("Only an agent can raise a ticket for someone else")
        try:
            requester = normalise_email(for_email)
        except InvalidEmail:
            raise RaiseError("Enter a valid email address for the person this is for")
        source = TicketSource.AGENT
    else:
        if any(extra is not None for extra in extras):
            raise RaiseError("Classifying, assigning or backdating is only for a ticket raised for someone else")
        requester, source = creator.email.lower(), TicketSource.PORTAL

    if category is not None and (impact is None or urgency is None):
        raise RaiseError("Choose the impact and urgency to classify the ticket")
    if (impact is not None or urgency is not None) and category is None:
        raise RaiseError("Choose a category to classify the ticket")
    if assignee is not None and category is None:
        raise RaiseError("Classify the ticket (choose a category) to assign it")
    if resolved_at is not None and category is None:
        raise RaiseError("Classify the ticket (choose a category) before saying when it was resolved")
    if resolved_at is None and (resolution_notes or "").strip():
        raise RaiseError("Resolution notes are only for a ticket logged as already resolved: enter the Resolved time too")
    if resolved_at is not None:
        try:
            resolution_notes = clean_notes(resolution_notes)
        except ResolveError as error:
            raise RaiseError(str(error))

    ticket = create_ticket(
        session, subject=subject, requester_email=requester, source=source,
        ticket_type=ticket_type, actor_email=creator.email, created_by_id=creator.id,
        request_type_id=request_type.id if request_type is not None else None,
    )
    if request_type is not None:
        record_event(session, ticket, EVENT_REQUEST_TYPE, creator.email, to_value=request_type.name)
    if request_type is not None:
        # The form questions of the kind of request (app/request_questions.py); the required ones must be answered.
        try:
            save_answers(session, ticket, answers or {}, creator.email, first_time=True, require_all=True)
        except QuestionError as error:
            raise RaiseError(str(error))
    if required_by is not None:
        try:
            set_required_by(
                session, ticket, required_by, creator.email,
                backdating=on_behalf and (reported_at is not None or resolved_at is not None),
            )
        except RequiredByError as error:
            raise RaiseError(str(error))
    if on_behalf:
        # Settled now, while the requester is known, and kept on the ticket for phase 3's mail to read:
        # an agent can see on the ticket whether the person will hear about it, and why not.
        will_tell, why = tell_requester_decision(session, requester)
        record_event(
            session, ticket, EVENT_REQUESTER_NOTICE, creator.email,
            to_value="will be told" if will_tell else "not told", detail=why,
        )
        if will_tell:
            queue_raised_for(session, ticket)
        # Stored as the requester's own first message; who typed it is in the sender.
        record_inbound(
            session, ticket, sender=f"Logged by {creator.display_name} on behalf of {requester}", body=description,
        )
    else:
        record_inbound(session, ticket, sender=creator.display_name, body=description, author_user_id=creator.id)

    if category is not None:
        triage_ticket(
            session, ticket, creator, ticket_type=ticket_type, category=category,
            impact=impact, urgency=urgency, assignee=assignee,
        )
        if resolved_at is None:
            # No list is shown to the agent here, so everything the rules produce is made (now, or once approved);
            # one logged as already resolved makes none. Imported here: app.subrequests builds on app.triage.
            from app.subrequests import after_triage

            after_triage(session, ticket, creator, strict=False)
    elif request_type is not None and not on_behalf:
        # A request for a type with all its defaults needs no triage (#133); with incomplete ones it waits in New.
        self_triage(session, ticket, request_type)
    if resolved_at is not None:
        try:
            # Logged after the fact: the same resolve as at the desk, but nobody is emailed.
            resolve_ticket(session, ticket, creator, resolution_notes, send_email=False)
        except ResolveError as error:
            if ticket.status == TicketStatus.AWAITING_APPROVAL:
                raise RaiseError("A request that needs approval cannot be logged as already resolved")
            raise RaiseError(str(error))
    if reported_at is not None or resolved_at is not None:
        try:
            backdate_ticket(session, ticket, creator.email, reported_at=reported_at, resolved_at=resolved_at)
        except BackdateError as error:
            raise RaiseError(str(error))
    return ticket


def _name_of(session: Session, email: str) -> str:
    user = session.exec(select(User).where(User.email == email.lower())).first()
    return user.display_name if user is not None else email


def source_label(session: Session, ticket: Ticket) -> str:
    """How the ticket came to exist, for the ticket page, the timeline and the requester's
    own view: "Received by email", "Raised in the portal by Jane", "Raised by Agent X for Jane"."""
    who = _name_of(session, ticket.requester_email)
    if ticket.source == TicketSource.EMAIL:
        return "Received by email"
    if ticket.source == TicketSource.PORTAL:
        return f"Raised in the portal by {who}"
    if ticket.source == TicketSource.TEAMS:
        return f"Raised in Teams by {who}"
    creator = session.get(User, ticket.created_by_id) if ticket.created_by_id else None
    return f"Raised by {creator.display_name if creator else 'an agent'} for {who}"

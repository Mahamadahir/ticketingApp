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

from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.backdating import BackdateError, backdate_ticket
from app.followers import InvalidEmail, normalise_email
from app.messages import record_inbound
from app.models import (
    Category,
    Ticket,
    TicketSource,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.settings_store import TELL_REQUESTER_RAISED_FOR, get_flag
from app.sla import Level
from app.ticket_service import create_ticket, set_status
from app.triage import triage_ticket

AGENT_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)
MAX_SUBJECT_LENGTH = 200


class RaiseError(ValueError):
    """A reason the ticket cannot be raised, worded for whoever is filling in the form."""


def should_tell_requester(session: Session) -> bool:
    """Whether the requester is to be emailed that a ticket was raised for them (the Settings
    switch). The email is sent once mail exists (phase 3); the person's own opt-out joins
    this check with the per-person notification settings."""
    return get_flag(session, TELL_REQUESTER_RAISED_FOR)


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
) -> Ticket:
    """Raises a ticket. With for_email an agent raises it on that person's behalf; without,
    the creator raises it for themselves. Only the agent mode can classify, assign or
    backdate. Nothing is committed: the caller does that."""
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

    ticket = create_ticket(
        session, subject=subject, requester_email=requester, source=source,
        ticket_type=ticket_type, actor_email=creator.email, created_by_id=creator.id,
    )
    if on_behalf:
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
    if resolved_at is not None:
        set_status(session, ticket, TicketStatus.RESOLVED, creator.email)
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

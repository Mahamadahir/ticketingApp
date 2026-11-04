"""The team activity feed: everything that happened on the tickets an agent can open,
newest first. Ticket events (status, assignment, triage, escalation, links, splits ...)
and messages (emails, requester replies, internal notes) in one list.

Which tickets: the same ones the agent can see anywhere else, so the feed uses
visibility.visible_ticket_condition and never decides access itself. A ticket moved to
another team drops out of the feed of everyone outside that team, history included.

This is separate from the audit log (admin: who changed settings, users, categories) and
the sign-in activity log (admin: who signed in). Those are system-wide; this one is the
work on tickets, for the people doing it.
"""

from enum import Enum
from typing import Optional

from sqlalchemy import String, case, cast, func, literal, null, union_all
from sqlmodel import select

from app.models import Message, MessageDirection, Ticket, TicketEvent

EXCERPT = 160


class Kind(str, Enum):
    EVENTS = "events"  # what changed on a ticket
    CONVERSATION = "conversation"  # emails to the requester and the requester's replies
    NOTES = "notes"  # internal notes


def feed_query(visible, team_id: Optional[int] = None, kind: Optional[Kind] = None):
    """A select of rows (kind, at, row_id, ticket_id, what, actor, from_value, to_value, detail)
    over the tickets matching `visible` (None means all), newest first."""
    events = (
        select(
            literal("event").label("kind"),
            TicketEvent.created_at.label("at"),
            TicketEvent.id.label("row_id"),
            TicketEvent.ticket_id.label("ticket_id"),
            TicketEvent.event_type.label("what"),
            TicketEvent.actor_email.label("actor"),
            TicketEvent.from_value.label("from_value"),
            TicketEvent.to_value.label("to_value"),
            TicketEvent.detail.label("detail"),
        )
        .join(Ticket, Ticket.id == TicketEvent.ticket_id)
    )
    messages = (
        select(
            literal("message").label("kind"),
            Message.created_at.label("at"),
            Message.id.label("row_id"),
            Message.ticket_id.label("ticket_id"),
            case((Message.msp_forward.is_not(None), "msp"), else_=cast(Message.direction, String)).label("what"),
            Message.sender.label("actor"),
            cast(null(), String).label("from_value"),
            cast(null(), String).label("to_value"),
            case(
                (func.length(Message.body) > EXCERPT, func.substr(Message.body, 1, EXCERPT) + "..."),
                else_=Message.body,
            ).label("detail"),
        )
        .join(Ticket, Ticket.id == Message.ticket_id)
    )
    if kind == Kind.EVENTS:
        branches = [events]
    elif kind == Kind.CONVERSATION:
        branches = [messages.where(Message.direction != MessageDirection.INTERNAL)]
    elif kind == Kind.NOTES:
        branches = [messages.where(Message.direction == MessageDirection.INTERNAL)]
    else:
        branches = [events, messages]

    scoped = []
    for branch in branches:
        if visible is not None:
            branch = branch.where(visible)
        if team_id is not None:
            branch = branch.where(Ticket.team_id == team_id)
        scoped.append(branch)

    combined = union_all(*scoped).subquery()
    return select(*combined.c).order_by(combined.c.at.desc(), combined.c.kind, combined.c.row_id.desc())


KIND_LABELS = {
    Kind.EVENTS: "Changes",
    Kind.CONVERSATION: "Emails and replies",
    Kind.NOTES: "Internal notes",
}

_MESSAGE_WHAT = {
    "msp": "message from the MSP",
    MessageDirection.OUTBOUND.value: "emailed the requester",
    MessageDirection.INBOUND.value: "message from the requester",
    MessageDirection.INTERNAL.value: "added an internal note",
}


def describe(row) -> str:
    """One line for a feed row."""
    if row.kind == "message":
        # The database may hand back the enum's name (OUTBOUND) or its value (outbound).
        text = _MESSAGE_WHAT.get(row.what.lower(), row.what)
        return f"{text}: {row.detail}" if row.detail else text
    text = row.what.replace("_", " ")
    if row.from_value or row.to_value:
        text += f": {row.from_value or 'none'} -> {row.to_value or 'none'}"
    if row.detail:
        text += f" ({row.detail})"
    return text

"""Searching tickets from the agent list (phase 1F).

Search only narrows the list an agent already has: the route applies
visibility.visible_ticket_condition first, so a match in a ticket the agent cannot
open never shows. It looks at the ticket number (1042, #1042, INC-1042), the subject,
the requester and the text of every message, internal notes included, because only
agents search.

Text matching uses Postgres full-text search (English stemming, so "printing" finds
"printer") plus a plain substring match on the subject and requester, which finds
fragments full-text cannot, such as part of an address or a hostname.
"""

from sqlalchemy import and_, exists, func, or_
from sqlmodel import select

from app.models import Message, Ticket
from app.ticket_service import parse_ticket_number

MAX_QUERY_LENGTH = 200
_CONFIG = "english"


def normalise(text: str | None) -> str:
    return (text or "").strip()[:MAX_QUERY_LENGTH]


def _like_pattern(text: str) -> str:
    """The text as a literal substring pattern: % and _ in what was typed mean themselves."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_condition(text: str):
    """The SQL condition matching tickets for the typed text, or None for a blank search."""
    text = normalise(text)
    if not text:
        return None
    query = func.websearch_to_tsquery(_CONFIG, text)
    pattern = _like_pattern(text)
    in_a_message = exists(
        select(Message.id).where(
            and_(
                Message.ticket_id == Ticket.id,
                func.to_tsvector(_CONFIG, Message.body).op("@@")(query),
            )
        )
    )
    clauses = [
        func.to_tsvector(_CONFIG, Ticket.subject).op("@@")(query),
        Ticket.subject.ilike(pattern, escape="\\"),
        Ticket.requester_email.ilike(pattern, escape="\\"),
        in_a_message,
    ]
    number = parse_ticket_number(text)
    if number is not None:
        clauses.append(Ticket.number == number)
    return or_(*clauses)

"""Canned responses (phase 1F-ii): ready-made replies admins manage and agents drop
into the email box. Picking one only fills the box; the agent still reads, edits and
sends it through the normal email action, so every message rule in app/messages.py
still applies.

Placeholders, filled in when an agent picks a response: {reference} (INC-1042),
{subject} and {agent} (the agent's name). Anything else in braces is left as written.
"""

from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from app.forms import MAX_MESSAGE_LENGTH
from app.models import CannedResponse, Ticket, User

MAX_TITLE_LENGTH = 100
PLACEHOLDERS = ("{reference}", "{subject}", "{agent}")


class CannedError(ValueError):
    """A reason a canned response cannot be saved, worded for the admin."""


def _clean(title: str, body: str) -> tuple[str, str]:
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise CannedError("A canned response needs a title")
    if len(title) > MAX_TITLE_LENGTH:
        raise CannedError(f"The title can be at most {MAX_TITLE_LENGTH} characters")
    if not body:
        raise CannedError("A canned response needs some text")
    if len(body) > MAX_MESSAGE_LENGTH:
        raise CannedError(f"The text can be at most {MAX_MESSAGE_LENGTH} characters")
    return title, body


def _title_taken(session: Session, title: str, except_id: int | None) -> bool:
    query = select(CannedResponse.id).where(func.lower(CannedResponse.title) == title.lower())
    if except_id is not None:
        query = query.where(CannedResponse.id != except_id)
    return session.exec(query).first() is not None


def create_canned(session: Session, title: str, body: str) -> CannedResponse:
    title, body = _clean(title, body)
    if _title_taken(session, title, None):
        raise CannedError("There is already a canned response with that title")
    canned = CannedResponse(title=title, body=body)
    session.add(canned)
    session.flush()
    return canned


def update_canned(session: Session, canned: CannedResponse, title: str, body: str, is_active: bool) -> CannedResponse:
    title, body = _clean(title, body)
    if _title_taken(session, title, canned.id):
        raise CannedError("There is already a canned response with that title")
    canned.title, canned.body, canned.is_active = title, body, is_active
    canned.updated_at = datetime.now(timezone.utc)
    session.add(canned)
    return canned


def all_canned(session: Session) -> list[CannedResponse]:
    return list(session.exec(select(CannedResponse).order_by(func.lower(CannedResponse.title))).all())


def active_canned(session: Session) -> list[CannedResponse]:
    return [c for c in all_canned(session) if c.is_active]


def fill(canned: CannedResponse, ticket: Ticket, agent: User) -> str:
    """The response's text with the placeholders for this ticket and agent filled in."""
    values = {"{reference}": ticket.reference, "{subject}": ticket.subject, "{agent}": agent.display_name}
    text = canned.body
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text

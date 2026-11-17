"""Ticket presence (issue #246): who else has this ticket open, and what they are doing on it.

The agent ticket page checks in every `ticket_presence_check_in_seconds` with what the person is doing (the page's script,
app/static/ticket-presence.js). The check-in is one row per person and ticket in `ticket_presence`, in the database because the
app runs several replicas. Someone counts as gone after `ticket_presence_gone_after_seconds` without a check-in; leaving the page
deletes the row at once (a beacon on `pagehide`), and the `presence-cleanup` recurring job deletes the rest (a crashed browser),
so nothing is cleaned up on the request path. Reading never trusts the row alone: it filters on the age, so a row the job has not
reached yet is never shown.

Who is listed is decided here and only from app/visibility.py: someone is shown to the viewer only if they can still open the ticket.
The strip is for agents only; no portal page reads this module."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Session, select

from app.models import Ticket, TicketPresence, User, UserRole
from app.settings_store import PRESENCE_CHECK_IN_SECONDS, PRESENCE_GONE_AFTER_SECONDS, count_setting
from app.visibility import Access, access_context, ticket_access

VIEWING, EMAIL, NOTE, ACTION = "viewing", "email", "note", "action"
ACTIVITIES = (VIEWING, EMAIL, NOTE, ACTION)
AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)
MAX_ACTION_LENGTH = 60

# What each activity reads as after the name. An action says its own (the panel's `data-doing`).
SENTENCES = {
    VIEWING: "is viewing this ticket",
    EMAIL: "is writing an email to the requester",
    NOTE: "is writing an internal note",
}


def check_in_seconds(session: Session) -> int:
    return count_setting(session, PRESENCE_CHECK_IN_SECONDS)


def gone_after_seconds(session: Session) -> int:
    """Never shorter than one check-in interval, or everyone would flicker in and out between two check-ins."""
    return max(count_setting(session, PRESENCE_GONE_AFTER_SECONDS), check_in_seconds(session))


def clean_action(text: Optional[str]) -> str:
    """The name an open panel gave itself ("re-assigning"), made safe to store and show: one line, no markup characters
    that matter (the template escapes it anyway), at most MAX_ACTION_LENGTH characters."""
    return re.sub(r"\s+", " ", text or "").strip()[:MAX_ACTION_LENGTH]


def check_in(
    session: Session, ticket: Ticket, user: User, activity: str, action: Optional[str] = None, now: Optional[datetime] = None
) -> None:
    """Records that `user` has this ticket open and what they are doing. One row per person and ticket (an upsert, so two
    tabs or two replicas never collide). An unknown activity counts as viewing. Does not commit."""
    now = now or datetime.now(timezone.utc)
    if activity not in ACTIVITIES:
        activity = VIEWING
    name = clean_action(action) if activity == ACTION else ""
    if activity == ACTION and not name:
        activity = VIEWING
    values = {
        "ticket_id": ticket.id, "user_id": user.id, "activity": activity, "action_name": name or None, "last_seen_at": now,
    }
    statement = postgresql_insert(TicketPresence).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["ticket_id", "user_id"],
            set_={"activity": activity, "action_name": values["action_name"], "last_seen_at": now},
        )
    )


def leave(session: Session, ticket: Ticket, user: User) -> None:
    """The person closed or left the page: their row goes now. Does not commit."""
    session.execute(delete(TicketPresence).where(TicketPresence.ticket_id == ticket.id, TicketPresence.user_id == user.id))


@dataclass
class Here:
    user: User
    activity: str
    sentence: str  # after the name: "is writing an internal note"


def others_here(session: Session, ticket: Ticket, viewer: User, now: Optional[datetime] = None) -> list[Here]:
    """The other agents on this ticket now, by name. Never the viewer, never anyone past the "gone after" time, never an
    inactive account, and never someone who can no longer open the ticket (access is asked again here, so a ticket that was
    re-assigned away from them stops listing them)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(seconds=gone_after_seconds(session))
    rows = session.exec(
        select(TicketPresence).where(
            TicketPresence.ticket_id == ticket.id, TicketPresence.user_id != viewer.id, TicketPresence.last_seen_at >= since
        )
    ).all()
    found = []
    for row in rows:
        person = session.get(User, row.user_id)
        if person is None or not person.is_active or person.role not in AGENT_ROLES:
            continue
        if ticket_access(person, ticket, access_context(session, person)) < Access.READ:
            continue
        sentence = f"is {row.action_name}" if row.activity == ACTION and row.action_name else SENTENCES.get(row.activity, SENTENCES[VIEWING])
        found.append(Here(person, row.activity, sentence))
    return sorted(found, key=lambda item: (item.user.display_name.lower(), item.user.id))


def digest(others: list[Here]) -> str:
    """A short fingerprint of what the strip says, so a poll that would draw the same strip can be answered with nothing and
    the live region is not touched (the screen reader is not told again)."""
    if not others:
        return ""  # an empty strip is what a page with no `shown` already has
    text = "|".join(f"{item.user.id}:{item.user.display_name}:{item.sentence}" for item in others)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def purge_expired(session: Session) -> None:
    """The `presence-cleanup` recurring job: deletes every row past the "gone after" time. Safe to run late or twice."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=gone_after_seconds(session))
    session.execute(delete(TicketPresence).where(TicketPresence.last_seen_at < cutoff))

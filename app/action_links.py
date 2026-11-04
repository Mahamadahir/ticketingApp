"""The feedback and reopen links in the resolution email (phase 4F).

Each link is a random token, made when the email is sent and never kept: the database holds only its
SHA-256 (`ActionToken`), so reading the database cannot give anyone a working link. The token is the
whole credential, as with the approval links (app/approval_links.py), and the pages are on the public
app (app/routers/action_links.py).

- A GET on a link only shows a confirmation page. A mail scanner or link preview that fetches every
  URL in a message must not reopen a ticket or burn a rating, so only the POST from that page acts.
- A link works once. `claim` marks it used with a single conditional UPDATE inside the same transaction
  as the action, so two POSTs at once give one winner, and a refused action rolls the claim back.
- Feedback is a rating of 1 to 5 and a comment, about the agent who resolved the ticket, and is never
  changed. Reopen goes through `reopen_ticket` (app/reopening.py), so it is exactly the requester's
  portal Reopen button. Only a Resolved ticket reopens; a Closed ticket stays closed (4E-1), and the
  page then says to reply to the email, which opens a new ticket linked to the old one.

The resolution text stored on the ticket holds readable markers where the links go (the timeline
and the outbox row show them); `put_links_in` swaps in the real links as the email is built.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.config import settings
from app.models import ActionToken, Ticket, TicketFeedback, TicketStatus
from app.reopening import ReopenError, check_can_reopen, reopen_ticket
from app.settings_store import AUTO_CLOSE_AFTER_DAYS, FEEDBACK_LINK_MINUTES, REGISTRY, count_setting, get_int_setting

log = logging.getLogger("app.action_links")

FEEDBACK = "feedback"
REOPEN = "reopen"
PATHS = {FEEDBACK: "/feedback/", REOPEN: "/reopen/"}
LIFETIME_SETTINGS = {FEEDBACK: FEEDBACK_LINK_MINUTES}  # the reopen link has none: it follows auto-close (below)

# What stands in for each link in the stored resolution text.
# Worded to read sensibly where the requester sees the stored text (the portal timeline).
FEEDBACK_MARKER = "(your personal feedback link is in the email)"
REOPEN_MARKER = "(your personal reopen link is in the email)"
MARKERS = {FEEDBACK: FEEDBACK_MARKER, REOPEN: REOPEN_MARKER}

MAX_COMMENT_LENGTH = 2000
TOKEN_BYTES = 32


class LinkError(ValueError):
    """A reason the link cannot be acted on, worded for the person holding it."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(session: Session, ticket: Ticket, kind: str, now: Optional[datetime] = None) -> str:
    """A new single-use token for the ticket. Returns the token itself, which is not stored. Unused links of the
    same kind for this ticket are withdrawn: a link from an earlier resolution, or from an email attempt that
    failed, stops working and its row goes, so retries cannot pile up live credentials."""
    now = now or datetime.now(timezone.utc)
    session.execute(delete(ActionToken).where(
        ActionToken.ticket_id == ticket.id, ActionToken.kind == kind, ActionToken.used_at.is_(None)
    ))
    if kind == REOPEN:
        # The reopen link expires when the ticket auto-closes: auto_close_after_days from when it was resolved
        # (#122). One number, so the link never outlives the ticket's Resolved status, whether or not the
        # auto-close switch is on.
        expires_at = (ticket.resolved_at or now) + timedelta(days=count_setting(session, AUTO_CLOSE_AFTER_DAYS))
    else:
        key = LIFETIME_SETTINGS[kind]
        expires_at = now + timedelta(minutes=get_int_setting(session, key) or int(REGISTRY[key].default))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    session.add(ActionToken(
        ticket_id=ticket.id, kind=kind, token_hash=hash_token(token), resolved_by_id=ticket.resolved_by_id,
        created_at=now, expires_at=expires_at,
    ))
    return token


def find(session: Session, kind: str, token: str) -> Optional[ActionToken]:
    """The row for a token of this kind, or None. Whether it is used or expired is the caller's question."""
    row = session.exec(select(ActionToken).where(ActionToken.token_hash == hash_token(token))).first()
    return row if row is not None and row.kind == kind else None


def is_expired(row: ActionToken, now: Optional[datetime] = None) -> bool:
    return (now or datetime.now(timezone.utc)) >= row.expires_at


def claim(session: Session, row: ActionToken, now: Optional[datetime] = None) -> bool:
    """Marks the token used if it is still unused and unexpired. False when someone else got there first.
    The caller commits it together with what the link did, or rolls both back."""
    now = now or datetime.now(timezone.utc)
    result = session.execute(
        update(ActionToken)
        .where(ActionToken.id == row.id, ActionToken.used_at.is_(None), ActionToken.expires_at > now)
        .values(used_at=now)
    )
    session.refresh(row)
    return result.rowcount == 1


def submit_feedback(session: Session, row: ActionToken, rating: int, comment: str) -> TicketFeedback:
    """Records the rating and uses up the link, or raises LinkError and changes nothing."""
    comment = (comment or "").replace("\r\n", "\n").replace("\x00", "").strip()
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        raise LinkError("Choose a rating from 1 to 5")
    if len(comment) > MAX_COMMENT_LENGTH:
        raise LinkError(f"The comment can be at most {MAX_COMMENT_LENGTH} characters")
    if not claim(session, row):
        raise LinkError("This link has already been used or has expired")
    feedback = TicketFeedback(
        ticket_id=row.ticket_id, token_id=row.id, rating=rating, comment=comment, resolved_by_id=row.resolved_by_id
    )
    session.add(feedback)
    return feedback


def can_reopen(ticket: Ticket) -> bool:
    """Whether reopen_ticket would accept this ticket now (its own rule, not a copy of it)."""
    try:
        check_can_reopen(ticket)
    except ReopenError:
        return False
    return True


def reopen_from_link(session: Session, row: ActionToken, ticket: Ticket) -> None:
    """Reopens the ticket as its requester and uses up the link. Raises LinkError and the caller rolls back,
    so a refused reopen leaves the link usable."""
    if not claim(session, row):
        raise LinkError("This link has already been used or has expired")
    try:
        reopen_ticket(session, ticket, ticket.requester_email, "The requester reopened it from the emailed link")
    except ReopenError as error:
        raise LinkError(str(error))


# ---- In the email -----------------------------------------------------------------------------------


def _drop_lines_with(text: str, marker: str) -> str:
    return "\n".join(line for line in text.split("\n") if marker not in line)


def put_links_in(session: Session, ticket: Ticket, body: str) -> str:
    """The email text with each marker replaced by a fresh link. A link that cannot be offered (the ticket is no
    longer Resolved, or for feedback no longer Resolved or Closed, or PUBLIC_BASE_URL is not set) loses its whole line rather than leaving a dead marker."""
    if FEEDBACK_MARKER not in body and REOPEN_MARKER not in body:
        return body
    base = settings.public_base_url.rstrip("/")
    if not base:
        log.warning("PUBLIC_BASE_URL is not set, so the resolution email for %s has no feedback or reopen link", ticket.reference)
    for kind in (FEEDBACK, REOPEN):
        marker = MARKERS[kind]
        if marker not in body:
            continue
        # Reopen needs a Resolved ticket; the feedback link also goes in the closed email (#131).
        offered = ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) if kind == FEEDBACK else ticket.status == TicketStatus.RESOLVED
        if not base or not offered:
            body = _drop_lines_with(body, marker)
        else:
            body = body.replace(marker, f"{base}{PATHS[kind]}{mint(session, ticket, kind)}")
    return body

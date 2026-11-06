"""Server-side sign-in sessions (#157).

The browser keeps a Starlette signed cookie, but it holds only a random token (`sid`); who is signed
in, and whether that is still allowed, is the `user_session` row. That is what lets a session be ended
early: sign out everywhere, a deactivated person, a changed role, a lost laptop. The row's id is the
SHA-256 of the token, so a copy of the table is no use to anyone. Action links (feedback, reopen,
approvals) are signed tokens of their own, not sessions, and never come through here. The public app
has no sign-in and no session cookie.

A session is valid while it is not revoked, is before its absolute expiry (`session_max_days`, fixed
when it starts) and was last seen less than `session_idle_minutes` ago. "Last seen" is written at most
once a minute. Every function that changes rows adds to the caller's session and does not commit,
except `touch`, which uses its own short transaction so a page view never commits half of a request.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request
from sqlalchemy import delete, or_, update
from sqlmodel import Session, select

from app.database import engine
from app.models import User, UserSession
from app.settings_store import SESSION_IDLE_MINUTES, SESSION_KEEP_DAYS, SESSION_MAX_DAYS, count_setting

COOKIE_KEY = "sid"
# last_seen_at moves at most this often: a busy page does not write a row on every request.
TOUCH_EVERY = timedelta(minutes=1)
MAX_USER_AGENT = 200
# The cookie itself lives a little longer than the longest session; the row decides.
MAX_SESSION_DAYS = 365
COOKIE_MAX_AGE_SECONDS = (MAX_SESSION_DAYS + 1) * 24 * 3600

INACTIVE_MESSAGE = "This account has been deactivated. Ask an administrator if you think that is a mistake."
SIGNED_OUT_MESSAGE = "You have been signed out. Please sign in again."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(session: Session, request: Request, user: User) -> UserSession:
    """Signs the person in: a new row and a new cookie token. Whatever the cookie held before (a
    previous person's session, a half-finished sign-in) is dropped, so a token set before sign-in is
    never the one that is trusted afterwards. The caller commits."""
    now = _now()
    token = secrets.token_urlsafe(32)
    max_days = min(count_setting(session, SESSION_MAX_DAYS), MAX_SESSION_DAYS)
    old = request.session.get(COOKIE_KEY)
    if old:
        revoke_token(session, old)
    request.session.clear()
    request.session[COOKIE_KEY] = token
    row = UserSession(
        id=hash_token(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=max_days),
        ip=request.client.host if request.client else None,  # same address the activity log keeps
        user_agent=(request.headers.get("user-agent") or "")[:MAX_USER_AGENT] or None,
    )
    session.add(row)
    return row


def ended_reason(session: Session, row: Optional[UserSession], now: datetime) -> Optional[str]:
    """Why this session no longer counts, or None if it is valid: missing, revoked, expired or idle."""
    if row is None:
        return "missing"
    if row.revoked_at is not None:
        return "revoked"
    if _utc(row.expires_at) <= now:
        return "expired"
    idle = timedelta(minutes=count_setting(session, SESSION_IDLE_MINUTES))
    if _utc(row.last_seen_at) + idle <= now:
        return "idle"
    return None


def current_row(session: Session, request: Request) -> Optional[UserSession]:
    token = request.session.get(COOKIE_KEY)
    if not token or not isinstance(token, str):
        return None
    return session.get(UserSession, hash_token(token))


def touch(row: UserSession, now: datetime) -> None:
    """Moves last_seen_at on, at most once a minute, in its own transaction."""
    if _utc(row.last_seen_at) > now - TOUCH_EVERY:
        return
    with Session(engine) as own:
        own.exec(
            update(UserSession)
            .where(UserSession.id == row.id, UserSession.revoked_at.is_(None), UserSession.last_seen_at <= now - TOUCH_EVERY)
            .values(last_seen_at=now)
        )
        own.commit()


def revoke_token(session: Session, token: str) -> None:
    session.exec(
        update(UserSession)
        .where(UserSession.id == hash_token(token), UserSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


def revoke_user_sessions(session: Session, user_id: int) -> int:
    """Ends every live session of this person, now. Returns how many there were. The caller commits."""
    result = session.exec(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return result.rowcount or 0


def live_session_count(session: Session, user_id: int) -> int:
    now = _now()
    rows = session.exec(
        select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None), UserSession.expires_at > now)
    ).all()
    idle = timedelta(minutes=count_setting(session, SESSION_IDLE_MINUTES))
    return sum(1 for row in rows if _utc(row.last_seen_at) + idle > now)


def purge_ended_sessions(session: Session) -> None:
    """The recurring job (app/scheduled_jobs.py): deletes the record of sessions that expired or were
    revoked more than `session_keep_days` ago. A session that merely went idle is not ended by this job;
    it is deleted once its absolute expiry has passed that long."""
    cutoff = _now() - timedelta(days=count_setting(session, SESSION_KEEP_DAYS))
    session.exec(delete(UserSession).where(or_(UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff)))

"""Emergency passwords (#164): what an agent sets in advance and uses only while outage mode is on
(app/outage.py).

The password is stored as a bcrypt hash, like any local account's, in its own column, so it can never be
used on the ordinary local sign-in. It is refused unless outage mode is on, for end users, for break-glass
accounts (they have their own sign-in) and for anyone without one. Failures count towards the same lockout
as local accounts (app/auth.py). Nothing here logs, prints or audits a password.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from sqlmodel import Session, select

from app import outage, sessions
from app.audit import log_action
from app.auth import BCRYPT_MAX_BYTES, _DUMMY_HASH, _is_locked, _record_failed_login, _verify_password, hash_password
from app.models import User, UserSession
from app.notifications import AGENT_ROLES
from app.settings_store import EMERGENCY_PASSWORD_MIN_LENGTH, count_setting


class PasswordRefused(ValueError):
    """A reason the new password cannot be used, worded for the person setting it."""


def may_hold_one(user: User) -> bool:
    """Agents and super admins who are real Entra people. End users get no fallback; a break-glass account has its own."""
    return user.role in AGENT_ROLES and not user.is_break_glass


def needs_one(user: User) -> bool:
    """Who is prompted at SSO sign-in: may hold one and has none."""
    return may_hold_one(user) and not user.emergency_password_hash


def check_new_password(session: Session, password: str) -> None:
    """Raises PasswordRefused. Length counts characters; bcrypt reads only the first 72 bytes, so a longer one
    is refused rather than quietly cut short. Never the same as a break-glass password."""
    minimum = count_setting(session, EMERGENCY_PASSWORD_MIN_LENGTH)
    if len(password) < minimum:
        raise PasswordRefused(f"The emergency password must be at least {minimum} characters.")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise PasswordRefused(f"The emergency password can be at most {BCRYPT_MAX_BYTES} bytes (about {BCRYPT_MAX_BYTES} plain characters).")
    for account in session.exec(select(User).where(User.is_break_glass, User.password_hash.is_not(None))).all():
        if _verify_password(password, account.password_hash):
            raise PasswordRefused("That password cannot be used. Choose a different one.")  # does not say whose it is


def set_password(session: Session, user: User, password: str) -> bool:
    """Sets or changes the person's emergency password. Returns True if they had one before. Does not commit.
    A new password counts as unused, even during an outage."""
    if not may_hold_one(user):
        raise PasswordRefused("Emergency passwords are for agents.")
    check_new_password(session, password)
    changed = user.emergency_password_hash is not None
    user.emergency_password_hash = hash_password(password)
    user.emergency_password_set_at = datetime.now(timezone.utc)
    user.emergency_password_used_at = None
    session.add(user)
    log_action(session, user.email, "user", user.id, "emergency_password_changed" if changed else "emergency_password_set")
    return changed


@dataclass
class EmergencyResult:
    user: Optional[User] = None
    account: Optional[User] = None  # the real agent the attempt was about, if there is one
    # outage_off, unknown_account, no_emergency_password, account_locked or wrong_password. For the logs only;
    # the person at the form sees one message.
    failure: Optional[str] = None
    locked_until: Optional[datetime] = None


def attempt_emergency_login(session: Session, upn: str, password: str, now: Optional[datetime] = None) -> EmergencyResult:
    """Succeeds only for the right emergency password of an unlocked agent while outage mode is on. Every
    refusal looks the same to the person at the form and costs the same bcrypt time. Does not start a session
    (see `start_emergency_session`) and does not check the account is active (the caller does, after the right
    password, so the message cannot be used to find accounts)."""
    now = now or datetime.now(timezone.utc)
    upn = upn.strip().lower()
    if not outage.is_on(session):
        _verify_password(password, _DUMMY_HASH)
        return EmergencyResult(failure="outage_off")
    user = session.exec(select(User).where(User.email == upn)).first()
    if user is None or not may_hold_one(user):
        _verify_password(password, _DUMMY_HASH)
        return EmergencyResult(failure="unknown_account")
    if not user.emergency_password_hash:
        _verify_password(password, _DUMMY_HASH)
        return EmergencyResult(account=user, failure="no_emergency_password")

    password_ok = _verify_password(password, user.emergency_password_hash)
    if _is_locked(user, now):
        return EmergencyResult(account=user, failure="account_locked")  # attempts during a lock change nothing
    if not password_ok:
        locked_until = _record_failed_login(session, user, now)
        session.commit()
        return EmergencyResult(account=user, failure="wrong_password", locked_until=locked_until)
    user.failed_login_count = 0
    user.locked_until = None
    session.add(user)
    session.commit()
    return EmergencyResult(user=user, account=user)


def start_emergency_session(session: Session, request: Request, user: User) -> Optional[UserSession]:
    """Signs the person in, marked as an emergency session, if outage mode is still on (checked under the lock
    that switching off takes, so a sign-in cannot slip in after the switch). None if it is off now. The caller
    commits."""
    if not outage.lock_on(session):
        return None
    user.emergency_password_used_at = datetime.now(timezone.utc)
    session.add(user)
    row = sessions.start_session(session, request, user)
    row.emergency = True
    return row

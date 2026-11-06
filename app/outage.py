"""Outage mode (#164): the switch that lets agents sign in with an emergency password while Entra is down.

The state is one row in the `setting` table (key `outage_mode`, value JSON with when and by whom), so every
replica sees it at once. It is deliberately not a registered setting: the Admin > Settings page edits only
registered ones, and the switch must go through the two functions here, which audit it and tell the super
admins. Switching on is for a break-glass account only (an ordinary super admin signs in through Entra,
which is the thing that is down; and a stolen super admin session must not be able to open a password
sign-in). Any super admin can switch it off: that only closes the door.

Switching off, in one transaction: every session an emergency sign-in started is ended, every emergency
password that was used during the outage is cleared, and those agents are emailed (admin template
`emergency_password_reset`) and asked again at their next Entra sign-in (app/routers/auth.py). The sign-in
side (the password, the form's checks) is in app/emergency.py.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.models import Setting, User, UserRole, UserSession
from app.notifications import notify_outage_mode
from app.outbox import queue_emergency_reset

OUTAGE_KEY = "outage_mode"
AUDIT_ENTITY = "outage_mode"
ON_ACTION = "outage_mode_on"
OFF_ACTION = "outage_mode_off"
RESET_ACTION = "emergency_password_invalidated"


class OutageError(Exception):
    """A refusal worded for the person who asked."""


@dataclass
class Outage:
    since: Optional[datetime]
    by: str


def status(session: Session) -> Optional[Outage]:
    """None while outage mode is off."""
    row = session.get(Setting, OUTAGE_KEY)
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        since = datetime.fromisoformat(data["since"])
        by = str(data.get("by", ""))
    except (ValueError, KeyError, TypeError):
        since, by = None, ""  # a row that cannot be read still means "on": the safe reading is the one that shows the banner
    return Outage(since=since, by=by)


def is_on(session: Session) -> bool:
    return session.get(Setting, OUTAGE_KEY) is not None


def lock_on(session: Session) -> bool:
    """True if outage mode is on, holding a shared lock on the row until the transaction ends. A sign-in takes
    this before it starts a session, and switching off takes the exclusive lock, so a sign-in either finishes
    before the switch (and its session is ended by it) or sees the mode off and is refused."""
    return session.exec(select(Setting.key).where(Setting.key == OUTAGE_KEY).with_for_update(read=True)).first() is not None


def _super_admins(session: Session) -> list[User]:
    return list(session.exec(select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active)).all())


def turn_on(session: Session, actor: User) -> None:
    """Does not commit. Only a break-glass account may."""
    if not actor.is_break_glass:
        raise OutageError("Only a break-glass account can switch outage mode on.")
    since = datetime.now(timezone.utc)
    row = Setting(key=OUTAGE_KEY, value=json.dumps({"since": since.isoformat(), "by": actor.email}))
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        raise OutageError("Outage mode is already on.")
    log_action(session, actor.email, AUDIT_ENTITY, 0, ON_ACTION, "agents can now sign in with an emergency password")
    for admin in _super_admins(session):
        notify_outage_mode(session, admin, actor, on=True)


@dataclass
class SwitchedOff:
    sessions_ended: int
    passwords_invalidated: int


def turn_off(session: Session, actor: User) -> SwitchedOff:
    """Does not commit. Any super admin may (the caller checks the role)."""
    row = session.exec(select(Setting).where(Setting.key == OUTAGE_KEY).with_for_update()).first()
    if row is None:
        raise OutageError("Outage mode is already off.")
    session.delete(row)
    ended = session.exec(
        update(UserSession).where(UserSession.emergency, UserSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc))
    ).rowcount or 0
    used = list(session.exec(select(User).where(User.emergency_password_used_at.is_not(None)).order_by(User.email)).all())
    for person in used:
        person.emergency_password_hash = None
        person.emergency_password_set_at = None
        person.emergency_password_used_at = None
        session.add(person)
        log_action(session, actor.email, "user", person.id, RESET_ACTION, "used during the outage; a new one is needed")
        if person.is_active:
            queue_emergency_reset(session, person)
    log_action(
        session, actor.email, AUDIT_ENTITY, 0, OFF_ACTION,
        f"{ended} emergency session(s) ended; {len(used)} emergency password(s) invalidated",
    )
    for admin in _super_admins(session):
        notify_outage_mode(session, admin, actor, on=False)
    return SwitchedOff(sessions_ended=ended, passwords_invalidated=len(used))

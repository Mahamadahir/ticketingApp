"""The nightly Entra account check (phase 6B, #158).

Once a night the worker asks Microsoft Graph whether each agent's and super admin's Entra account is
still enabled (`GET /users/{oid}?$select=accountEnabled`, needs User.Read.All, already consented). A
person whose account is disabled, or that Entra no longer has (404), becomes inactive: `is_active` is
cleared, every session of theirs ends (app/sessions.py), the audit log gets `deactivated_by_entra`, and
each active super admin is told once in the bell. Being inactive already means (4B-4, 6A) they cannot sign
in, are never polled for out-of-office and are left out of every picker (`team_members`,
`assignable_agents`, `members_of(active_only=True)`); tickets already assigned to them stay assigned and
Admin > Users can list them (`?inactive=open_tickets`) so they can be re-assigned.

Decisions:
- An account that is enabled again is NOT reactivated here. A super admin reactivates it on Admin > Users.
  That is safer for leavers (a rehire, or an account switched back by mistake, is a deliberate step).
- A transient failure (5xx, 429, a timeout, a refused token) changes nothing for that person; they are
  checked again the next night. If every lookup of a run fails the job raises, so it shows as failed in
  Admin > Recurring jobs and the worker log, like the presence job (the runner then retries it after the
  retry setting, until a run gets at least one answer).
- Skipped: end users, anyone with no Entra object id (the break-glass and local accounts, the dev login),
  anyone with a local password, and anyone already inactive.
- The run time is the `entra_check_time` setting (placeholder 02:00) in the organisation's default time
  zone. The scheduled job wakes hourly and runs once the time has passed and it has not yet succeeded
  since; with the switch off it does nothing (and makes no Graph call).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.audit import log_action
from app.config import settings
from app.models import ScheduledJob, User, UserRole
from app.notifications import notify_account_deactivated
from app.sessions import revoke_user_sessions
from app.settings_store import ENTRA_CHECK_TIME, REGISTRY, get_setting
from app.time_zones import default_zone, parse_clock

logger = logging.getLogger(__name__)

JOB_NAME = "entra-account-check"
AUDIT_ACTOR = "system"
CHECKED_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)


@dataclass
class CheckResult:
    looked_at: int = 0
    deactivated: int = 0
    failed: int = 0
    first_error: str = ""


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def run_time(session: Session) -> time:
    return parse_clock(get_setting(session, ENTRA_CHECK_TIME)) or parse_clock(REGISTRY[ENTRA_CHECK_TIME].default)


def is_due(session: Session, now: datetime) -> bool:
    """True once tonight's run time (in the organisation zone) has passed and the job has not succeeded since."""
    zone = ZoneInfo(default_zone(session))
    local = _aware(now).astimezone(zone)
    due_at = datetime.combine(local.date(), run_time(session), tzinfo=zone)
    if local < due_at:
        return False
    row = session.get(ScheduledJob, JOB_NAME)
    last = row.last_success_at if row is not None else None
    return last is None or _aware(last) < due_at


def check_accounts(
    session: Session, account_enabled: Optional[Callable[[str], Optional[bool]]] = None
) -> CheckResult:
    """Looks up every checked person once and deactivates those Entra disabled or no longer has. Does not
    commit. `account_enabled(oid)` returns True, False, or None for not found, and raises when Graph fails."""
    from app.graph_client import graph_client  # resolved now, so tests can swap the function

    account_enabled = account_enabled or graph_client.account_enabled
    result = CheckResult()
    people = session.exec(
        select(User)
        .where(
            User.role.in_(CHECKED_ROLES), User.is_active, User.entra_object_id.is_not(None), User.password_hash.is_(None)
        )
        .order_by(User.id)
    ).all()
    for person in people:
        result.looked_at += 1
        try:
            enabled = account_enabled(person.entra_object_id)
        except Exception as error:
            result.failed += 1
            result.first_error = result.first_error or type(error).__name__
            logger.warning("entra check: could not look up an account (%s)", type(error).__name__)
            continue
        if enabled is True:
            continue
        deactivate(session, person, "account disabled in Entra" if enabled is False else "account not found in Entra")
        result.deactivated += 1
    return result


def deactivate(session: Session, person: User, reason: str) -> None:
    person.is_active = False
    session.add(person)
    log_action(session, AUDIT_ACTOR, "user", person.id, "deactivated_by_entra", reason)
    ended = revoke_user_sessions(session, person.id)
    if ended:
        log_action(session, AUDIT_ACTOR, "user", person.id, "sessions_revoked", f"deactivated by Entra; {ended} ended")
    admins = session.exec(select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active)).all()
    for admin in admins:
        notify_account_deactivated(session, admin, person, reason)


def run_entra_account_check(session: Session, now: Optional[datetime] = None) -> None:
    """The recurring job (app/scheduled_jobs.py). Does nothing unless ENTRA_ACCOUNT_CHECK_ENABLED is on, Graph is
    configured and tonight's run is due. Raises when every lookup failed, so the failure is visible."""
    if not (settings.entra_account_check_enabled and settings.graph_configured):
        return
    if not is_due(session, now or datetime.now(timezone.utc)):
        return
    result = check_accounts(session)
    if result.looked_at and result.failed == result.looked_at:
        raise RuntimeError(
            f"every one of {result.looked_at} Entra account lookups failed ({result.first_error}); is User.Read.All granted?"
        )

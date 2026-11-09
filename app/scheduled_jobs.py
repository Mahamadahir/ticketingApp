"""Recurring jobs driven by the database (phase 4A): the `scheduled_job` table.

The worker's APScheduler jobs (app/jobs) are in-memory timers: a worker that is down misses its runs
and nothing notices. A job registered here has a row saying when it is next due, so a late or missed
run is found and run, once, and a failure is recorded where an admin can see it.

How it runs. The worker has one APScheduler job, `scheduled-jobs`, every minute. Each tick it makes
sure every registered job has a row, then claims the due ones one at a time: the row is locked
(`FOR UPDATE SKIP LOCKED`, so a second worker skips it) and given a lease (owner and expiry), and
that is committed before the job runs. The job runs in its own session and transaction. The outcome
is then written under the same lease:

- success: `last_success_at` is set and the next run is one interval after this one *started*, so
  however overdue it was it runs once and does not catch up the runs it missed;
- failure: the job's work is rolled back, the error is stored and the job is retried after the retry
  setting (or at its normal time if that is sooner);
- crash: the lease is never cleared, but it expires, and then the next tick claims the job again.

A job that starts more than the "late" setting after it was due writes an audit line saying so.

To add a job, see "How to add a recurring job" in docs/build-plan.md: write a function that takes a
session, then add one `ScheduledJobDef` to SCHEDULED_JOBS below. Nothing else changes (no migration,
no worker edit); the row appears on the next tick. Like every worker job it must be safe to run late
and, after a crash, to run twice.
"""

import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Session, select

from app.anonymisation import run_blob_cleanup
from app.approval_reminders import send_approval_reminders
from app.audit import log_action
from app.auto_close_chase import run_auto_close, run_chase
from app.bank_holidays import run_bank_holiday_fetch
from app.database import engine
from app.due_today import run_scheduled_due
from app.entra_check import JOB_NAME as ENTRA_CHECK_JOB, run_entra_account_check
from app.models import ScheduledJob
from app.msp import run_msp_ack_alerts, run_msp_forwards, run_msp_handoff
from app.reply_files import expire as expire_reply_files
from app.sessions import purge_ended_sessions
from app.teams_pending import expire_pending
from app.settings_store import JOB_LATE_MINUTES, JOB_LEASE_MINUTES, JOB_RETRY_MINUTES, REGISTRY, get_int_setting

log = logging.getLogger("app.worker")

TICK_EVERY_SECONDS = 60
AUDIT_ACTOR = "worker"
AUDIT_ENTITY = "scheduled_job"
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ScheduledJobDef:
    name: str  # unique; the row's key and the name in the heartbeat line
    every_seconds: int
    run: Callable[[Session], None]  # given a session; the runner commits on return, rolls back on a raise


# The registry: add a ScheduledJobDef here to add a recurring job.
SCHEDULED_JOBS: list[ScheduledJobDef] = [
    ScheduledJobDef("approval-reminders", 3600, send_approval_reminders),
    ScheduledJobDef("auto-close", 3600, run_auto_close),
    ScheduledJobDef("bank-holidays", 21600, run_bank_holiday_fetch),  # gov.uk bank holidays, cached in the database; every 6 hours so a failed fetch is retried the same day
    ScheduledJobDef("blob-cleanup", 3600, run_blob_cleanup),  # retries stored-file deletes after a requester was anonymised (app/anonymisation.py)
    ScheduledJobDef("chase", 3600, run_chase),
    ScheduledJobDef(ENTRA_CHECK_JOB, 3600, run_entra_account_check),  # wakes hourly; runs once a night after the entra_check_time setting
    ScheduledJobDef("msp-ack-alerts", 300, run_msp_ack_alerts),  # a ticket sent to the MSP that it has not confirmed in time
    ScheduledJobDef("msp-forwards", 300, run_msp_forwards),  # MSP mail left waiting for an agent goes to the user once business hours end
    ScheduledJobDef("msp-handoff", 300, run_msp_handoff),  # every 5 minutes: the handoff starts within 5 minutes of its hour
    ScheduledJobDef("reply-files", 3600, expire_reply_files),  # drops files held for an email preview that was never confirmed (app/reply_files.py)
    ScheduledJobDef("scheduled-due", 900, run_scheduled_due),
    ScheduledJobDef("session-cleanup", 86400, purge_ended_sessions),  # deletes sign-in sessions that ended `session_keep_days` ago
    ScheduledJobDef("teams-pending", 3600, expire_pending),  # ends the Teams bot's unanswered "which request?" questions and releases their held files (app/teams_pending.py)
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def registry_errors(jobs: Sequence[ScheduledJobDef]) -> list[str]:
    errors = []
    names = [job.name for job in jobs]
    if len(set(names)) != len(names):
        errors.append(f"Scheduled job names must be unique: {names}")
    errors += [f"Scheduled job {job.name} must run at least every second" for job in jobs if job.every_seconds < 1]
    return errors


def timing(session: Session, key: str) -> timedelta:
    """A timing setting in minutes. A blank or invalid value (the settings page allows blank) falls back
    to the setting's default rather than to something tiny."""
    minutes = get_int_setting(session, key)
    return timedelta(minutes=minutes if minutes else int(REGISTRY[key].default))


def ensure_rows(session: Session, jobs: Sequence[ScheduledJobDef], now: datetime) -> None:
    """Gives each registered job its row, due now. Safe when two workers do it at once."""
    for job in jobs:
        session.execute(
            postgresql_insert(ScheduledJob.__table__)
            .values(name=job.name, next_run_at=now, failures_in_a_row=0)
            .on_conflict_do_nothing(index_elements=["name"])
        )


def claim_next_due(session: Session, jobs: Sequence[ScheduledJobDef], owner: str, now: datetime) -> Optional[ScheduledJob]:
    """Locks the most overdue job that is due and not leased, leases it to `owner` and returns it, or
    None. Does not commit: the caller commits to make the lease visible. A job whose lease has expired
    is free again. Rows for jobs no longer registered are ignored."""
    row = session.exec(
        select(ScheduledJob)
        .where(
            ScheduledJob.name.in_([job.name for job in jobs]),
            ScheduledJob.next_run_at <= now,
            or_(ScheduledJob.lease_expires_at.is_(None), ScheduledJob.lease_expires_at <= now),
        )
        .order_by(ScheduledJob.next_run_at, ScheduledJob.name)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        return None
    if row.lease_owner is not None:
        # The previous run never reported back: its worker died (or overran the lease). Count it as a failure.
        row.last_error = "The previous run never finished (the worker stopped mid-run?)"
        row.last_error_at = now
        row.failures_in_a_row += 1
        log_action(
            session, AUDIT_ACTOR, AUDIT_ENTITY, 0, f"Job {row.name} recovered after its lease expired",
            f"the run started by {row.lease_owner} at {row.last_started_at:%Y-%m-%d %H:%M} UTC never finished",
        )
    if now - row.next_run_at > timing(session, JOB_LATE_MINUTES):
        log_action(
            session, AUDIT_ACTOR, AUDIT_ENTITY, 0, f"Job {row.name} ran late",
            f"due {row.next_run_at:%Y-%m-%d %H:%M} UTC, started {now:%Y-%m-%d %H:%M} UTC",
        )
    row.lease_owner = owner
    row.lease_expires_at = now + timing(session, JOB_LEASE_MINUTES)
    row.last_started_at = now
    session.add(row)
    return row


def next_slot(due: datetime, started: datetime, every: timedelta) -> datetime:
    """The first slot after `started` on the grid of `every` that begins at `due`. A job is not run
    again for slots it missed, and a daily job stays at its time of day instead of drifting."""
    return due + every * ((started - due) // every + 1)


def _finish(
    job: ScheduledJobDef, owner: str, started: datetime, due: datetime, error: Optional[str], clock: Callable[[], datetime]
) -> None:
    """Records the outcome and releases the lease, but only if this worker still holds it (after a
    lease expiry another worker may have taken the job over, and its record wins)."""
    with Session(engine) as session:
        row = session.exec(
            select(ScheduledJob).where(ScheduledJob.name == job.name, ScheduledJob.lease_owner == owner).with_for_update()
        ).first()
        if row is None or row.last_started_at != started:
            log.warning("job=%s outcome not recorded: its lease was taken over", job.name)
            return
        now = clock()
        row.lease_owner = None
        row.lease_expires_at = None
        if error is None:
            row.last_success_at = now
            row.failures_in_a_row = 0
            row.next_run_at = next_slot(due, started, timedelta(seconds=job.every_seconds))
        else:
            row.last_error = error[:MAX_ERROR_LENGTH]
            row.last_error_at = now
            row.failures_in_a_row += 1
            retry = min(timing(session, JOB_RETRY_MINUTES), timedelta(seconds=job.every_seconds))
            row.next_run_at = now + retry
        session.add(row)
        session.commit()


def run_claimed(
    job: ScheduledJobDef, owner: str, started: datetime, due: datetime, clock: Callable[[], datetime]
) -> bool:
    """Runs a job that was claimed, in its own session, and records the outcome. Never raises."""
    began = time.monotonic()
    error: Optional[str] = None
    try:
        with Session(engine) as session:
            job.run(session)
            session.commit()
    except Exception as exc:
        log.exception("job=%s status=failed", job.name)
        error = f"{type(exc).__name__}: {exc}"
    else:
        log.info("heartbeat job=%s status=ok duration_ms=%d", job.name, (time.monotonic() - began) * 1000)
    try:
        _finish(job, owner, started, due, error, clock)
    except Exception:
        # The work is done but not recorded; the lease expires and the job is recovered then.
        log.exception("job=%s outcome could not be recorded", job.name)
    return error is None


def run_due_jobs(
    jobs: Optional[Sequence[ScheduledJobDef]] = None,
    owner: Optional[str] = None,
    clock: Callable[[], datetime] = utcnow,
) -> list[str]:
    """One tick: runs every registered job that is due (each at most once) and returns the names run.
    A job that fails does not stop the others."""
    jobs = SCHEDULED_JOBS if jobs is None else jobs
    owner = owner or worker_id()
    by_name = {job.name: job for job in jobs}
    with Session(engine) as session:
        ensure_rows(session, jobs, clock())
        session.commit()
    ran: list[str] = []
    for _ in range(len(jobs)):
        with Session(engine) as session:
            now = clock()
            row = claim_next_due(session, [job for job in jobs if job.name not in ran], owner, now)
            if row is None:
                break
            name, due = row.name, row.next_run_at
            session.commit()  # the lease is now visible to every other worker
        ran.append(name)  # never twice in one tick, even if it is due again at once
        run_claimed(by_name[name], owner, now, due, clock)
    return ran


def scheduled_jobs_tick(session: Session) -> None:
    """The worker job (app/jobs): runs the due recurring jobs. Uses its own sessions; the one it is
    given is unused."""
    run_due_jobs()


def status_line(rows: Sequence[ScheduledJob], now: datetime, late: timedelta, registered: Optional[set[str]] = None) -> str:
    """One line for the admin page: whether the recurring jobs are healthy. A job is overdue when it is
    more than `late` past its due time and nothing holds it (the worker is down or stuck). Rows for jobs
    no longer in `registered` are ignored."""
    if registered is not None:
        rows = [row for row in rows if row.name in registered]
    if not rows:
        return "No recurring jobs have run yet."
    failing = [row.name for row in rows if row.failures_in_a_row]
    stuck = [row.name for row in rows if row.lease_expires_at is not None and row.lease_expires_at <= now]
    overdue = [
        row.name for row in rows
        if row.lease_owner is None and now - row.next_run_at > late and row.name not in failing
    ]
    if not failing and not stuck and not overdue:
        return f"All {len(rows)} recurring job(s) are healthy."
    parts = []
    if failing:
        parts.append(f"failing: {', '.join(sorted(failing))}")
    if stuck:
        parts.append(f"did not finish: {', '.join(sorted(stuck))}")
    if overdue:
        parts.append(f"overdue (is the worker running?): {', '.join(sorted(overdue))}")
    return "Problems with recurring jobs, " + "; ".join(parts) + "."

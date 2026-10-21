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

from app.audit import log_action
from app.database import engine
from app.models import ScheduledJob
from app.settings_store import JOB_LATE_MINUTES, JOB_LEASE_MINUTES, JOB_RETRY_MINUTES, get_int_setting

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
SCHEDULED_JOBS: list[ScheduledJobDef] = []


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


def _minutes(session: Session, key: str) -> timedelta:
    return timedelta(minutes=get_int_setting(session, key) or 1)


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
    notes = []
    if row.lease_owner is not None:
        notes.append(f"the previous run by {row.lease_owner} never finished (lease expired)")
        row.last_error = "The previous run never finished (the worker stopped mid-run?)"
        row.last_error_at = now
    if now - row.next_run_at > _minutes(session, JOB_LATE_MINUTES):
        notes.append(f"due {row.next_run_at:%Y-%m-%d %H:%M} UTC, started {now:%Y-%m-%d %H:%M} UTC")
    if notes:
        log_action(session, AUDIT_ACTOR, AUDIT_ENTITY, 0, f"Job {row.name} ran late", "; ".join(notes))
    row.lease_owner = owner
    row.lease_expires_at = now + _minutes(session, JOB_LEASE_MINUTES)
    row.last_started_at = now
    session.add(row)
    return row


def _finish(job: ScheduledJobDef, owner: str, started: datetime, error: Optional[str], clock: Callable[[], datetime]) -> None:
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
            row.next_run_at = started + timedelta(seconds=job.every_seconds)
        else:
            row.last_error = error[:MAX_ERROR_LENGTH]
            row.last_error_at = now
            row.failures_in_a_row += 1
            retry = min(_minutes(session, JOB_RETRY_MINUTES), timedelta(seconds=job.every_seconds))
            row.next_run_at = now + retry
        session.add(row)
        session.commit()


def run_claimed(job: ScheduledJobDef, owner: str, started: datetime, clock: Callable[[], datetime]) -> bool:
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
    _finish(job, owner, started, error, clock)
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
            row = claim_next_due(session, jobs, owner, now)
            if row is None:
                break
            name = row.name
            session.commit()  # the lease is now visible to every other worker
        run_claimed(by_name[name], owner, now, clock)
        ran.append(name)
    return ran


def scheduled_jobs_tick(session: Session) -> None:
    """The worker job (app/jobs): runs the due recurring jobs. Uses its own sessions; the one it is
    given is unused."""
    run_due_jobs()


def status_line(rows: Sequence[ScheduledJob], now: datetime) -> str:
    """One line for the admin page: whether the recurring jobs are healthy."""
    if not rows:
        return "No recurring jobs have run yet."
    failing = [row.name for row in rows if row.failures_in_a_row]
    stuck = [row.name for row in rows if row.lease_expires_at is not None and row.lease_expires_at <= now]
    if not failing and not stuck:
        return f"All {len(rows)} recurring job(s) are healthy."
    parts = []
    if failing:
        parts.append(f"failing: {', '.join(sorted(failing))}")
    if stuck:
        parts.append(f"did not finish: {', '.join(sorted(stuck))}")
    return "Problems with recurring jobs, " + "; ".join(parts) + "."

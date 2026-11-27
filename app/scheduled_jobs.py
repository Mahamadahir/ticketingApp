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

import functools
import logging
import os
import socket
import threading
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
from app.attachment_retries import attachment_retries_job
from app.email_reactions import email_reactions_job
from app.entra_check import JOB_NAME as ENTRA_CHECK_JOB, run_entra_account_check
from app.job_runs import abandon_running, close_run, open_run, purge_job_runs
from app.mailbox_changes import mailbox_changes_job
from app.models import ScheduledJob
from app.msp import run_msp_ack_alerts, run_msp_forwards, run_msp_handoff
from app.note_purge import purge_deleted_notes
from app.notification_purge import purge_notifications
from app.reply_files import expire as expire_reply_files
from app.run_summary import RunSummary, adapt
from app.ticket_presence import purge_expired as purge_ticket_presence
from app.sessions import purge_ended_sessions
from app.teams_pending import expire_pending
from app.settings_store import EMAIL_REACTIONS_MINUTES, MAILBOX_CHANGES_MINUTES, JOB_LATE_MINUTES, JOB_LEASE_MINUTES, JOB_RETRY_MINUTES, REGISTRY, get_int_setting

log = logging.getLogger("app.worker")

__all__ = ["RunSummary"]  # the summary a job's run may return: app/run_summary.py

TICK_EVERY_SECONDS = 60
AUDIT_ACTOR = "worker"
AUDIT_ENTITY = "scheduled_job"
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ScheduledJobDef:
    name: str  # unique; the row's key and the name in the heartbeat line
    every_seconds: int
    run: Callable[[Session], Optional[RunSummary]]  # given a session; returns what it did (a RunSummary); the runner commits on return, rolls back on a raise
    description: str = ""  # what the job does, in plain words: shown on the Run now confirm page
    warns: tuple[str, ...] = ()  # WARN_EMAIL and/or WARN_MSP: shown as a warning on the Run now confirm page
    min_lease_minutes: int = 0  # a job whose run can take long (calls that wait for Azure) holds its lease at least this long, whatever the setting
    background: bool = False  # run in its own thread, so a run that waits a long time (calls that wait for Azure) never holds up the other jobs of the tick
    interval_setting: Optional[str] = None  # a "minutes" setting that overrides every_seconds, so an admin can tune how often it runs

    def seconds(self, session: Session) -> int:
        """How often the job is due now: its interval setting in minutes when it has one (a blank or invalid value falls back to
        the setting's default), else the fixed every_seconds."""
        if self.interval_setting is None:
            return self.every_seconds
        return int(timing(session, self.interval_setting).total_seconds())


WARN_EMAIL = "email"
WARN_MSP = "msp"
WARNINGS = {
    WARN_EMAIL: "This job can send email to people (requesters, approvers or agents) when it runs.",
    WARN_MSP: "This job can hand tickets to the MSP, or send mail to or from it, when it runs. The job still keeps its own rules (for example the handoff only acts in its window).",
}

def counted(key: str, work: Callable[[Session], Optional[int]]) -> Callable[[Session], RunSummary]:
    """Adapts a job function that returns how many it handled (or None) into one that returns the summary {key: n}."""

    @functools.wraps(work)  # `run.__wrapped__` is the job function itself
    def run(session: Session) -> RunSummary:
        return RunSummary.of(**{key: work(session) or 0})

    return run


# The registry: add a ScheduledJobDef here to add a recurring job.
SCHEDULED_JOBS: list[ScheduledJobDef] = [
    ScheduledJobDef(
        "inbound-attachments", 60, attachment_retries_job,
        "Reads again the attachments of incoming emails that were taken without them after two failed reads, on the same backoff as a "
        "failed email (up to 8 tries in all), and adds them to the ticket's message. After the last try it leaves a note on the ticket.",
    ),
    ScheduledJobDef("approval-reminders", 3600, send_approval_reminders, "Reminds approvers who have not voted on a request, and tells the agent when an approval is overdue.", (WARN_EMAIL,)),
    ScheduledJobDef("auto-close", 3600, run_auto_close, "Closes Resolved tickets that nobody has answered for the auto-close period, and emails the requester that it was closed (only when auto-close is on).", (WARN_EMAIL,)),
    ScheduledJobDef("bank-holidays", 21600, run_bank_holiday_fetch, "Fetches the gov.uk bank holidays and keeps them in the database for SLA and due-date calculations; a failed fetch is retried the same day."),
    ScheduledJobDef("blob-cleanup", 3600, run_blob_cleanup, "Retries deleting stored files for a requester who was anonymised, until every file is gone."),  # app/anonymisation.py
    ScheduledJobDef("chase", 3600, run_chase, "Emails requesters of tickets that have been Awaiting user for a while, asking them to reply (only when chasing is on).", (WARN_EMAIL,)),
    ScheduledJobDef(ENTRA_CHECK_JOB, 3600, run_entra_account_check, "Checks in Entra that each user's account is still enabled and deactivates those who are not. It wakes hourly but only checks once a night, after the Entra check time setting."),
    ScheduledJobDef(
        "email-reactions", 600, email_reactions_job,
        "Reads the shared mailbox's Sent Items for reactions people made in Outlook to the emails the app sent, and records them on the "
        "tickets like Teams reactions. It only reads: it sends nothing. How often it runs is the setting \"Outlook reactions: how often "
        "Sent Items is read\".",
        (), interval_setting=EMAIL_REACTIONS_MINUTES,
    ),
    ScheduledJobDef(
        "mailbox-changes", 300, mailbox_changes_job,
        "Makes the scheduled mailbox changes (mail forwarding, mailbox access) whose start time has come, removes those whose end "
        "has, and retries failed attempts, resolving or handing back the request ticket as it goes. Only the executor talks to "
        "Exchange. How often it runs is the setting \"Mailbox changes: how often the worker looks for due changes\".",
        (WARN_EMAIL,), interval_setting=MAILBOX_CHANGES_MINUTES, min_lease_minutes=20, background=True,
    ),
    ScheduledJobDef("msp-ack-alerts", 300, run_msp_ack_alerts, "Raises an alert for a ticket sent to the MSP that the MSP has not confirmed in time."),
    ScheduledJobDef("msp-forwards", 300, run_msp_forwards, "Forwards mail the MSP sent on a ticket, left waiting for an agent, to the user once business hours end.", (WARN_EMAIL, WARN_MSP)),
    ScheduledJobDef("msp-handoff", 300, run_msp_handoff, "At the handoff time each day, sends the tickets waiting for the MSP to the MSP and emails them. It only acts inside its handoff window.", (WARN_EMAIL, WARN_MSP)),
    ScheduledJobDef("job-run-purge", 86400, purge_job_runs, "Deletes the history of scheduled job runs (Admin > Logs > Job runs) that started more than the days-kept setting ago."),
    ScheduledJobDef("note-purge", 86400, counted("deleted", purge_deleted_notes), "Removes notes deleted at least the keep-deleted-notes number of days ago, with their files."),
    ScheduledJobDef("notification-purge", 86400, counted("deleted", purge_notifications), "Deletes dismissed notifications past the undo window and every notification past its age limit, with their Teams posts."),
    ScheduledJobDef("presence-cleanup", 3600, counted("deleted", purge_ticket_presence), "Deletes the records of who is looking at a ticket once they are past the gone-after time."),
    ScheduledJobDef("reply-files", 3600, counted("deleted", expire_reply_files), "Drops files held for an email preview that was never confirmed."),
    ScheduledJobDef("scheduled-due", 900, run_scheduled_due, "On a Scheduled ticket's Due date, moves it back to Open and tells the assignee in the bell."),
    ScheduledJobDef("session-cleanup", 86400, counted("deleted", purge_ended_sessions), "Deletes sign-in sessions that ended more than the session keep-days setting ago."),
    ScheduledJobDef("teams-pending", 3600, counted("expired", expire_pending), "Ends the Teams bot's unanswered \"which request?\" questions and releases their held files."),
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
    requested_by = row.run_requested_by  # read before the claim clears it: the run row records who asked for a Run now
    if row.lease_owner is not None:
        # The previous run never reported back: its worker died (or overran the lease). Count it as a failure.
        abandon_running(session, row.name)
        row.last_error = "The previous run never finished (the worker stopped mid-run?)"
        row.last_error_at = now
        row.failures_in_a_row += 1
        log_action(
            session, AUDIT_ACTOR, AUDIT_ENTITY, 0, f"Job {row.name} recovered after its lease expired",
            f"the run started by {row.lease_owner} at {row.last_started_at:%Y-%m-%d %H:%M} UTC never finished",
        )
    if row.run_requested_at is not None:
        # An admin asked for this run (Run now): it is not late. next_run_at stays in the past while it runs (so a
        # crashed run is recovered); run_resume_at, the due time the job had, is kept until _finish puts the job back on
        # its own schedule from it.
        row.run_requested_at = row.run_requested_by = None
    elif now - row.next_run_at > timing(session, JOB_LATE_MINUTES):
        log_action(
            session, AUDIT_ACTOR, AUDIT_ENTITY, 0, f"Job {row.name} ran late",
            f"due {row.next_run_at:%Y-%m-%d %H:%M} UTC, started {now:%Y-%m-%d %H:%M} UTC",
        )
    row.lease_owner = owner
    registered = next((job for job in jobs if job.name == row.name), None)
    row.lease_expires_at = now + max(timing(session, JOB_LEASE_MINUTES), timedelta(minutes=registered.min_lease_minutes if registered else 0))
    row.last_started_at = now
    session.add(row)
    open_run(session, row.name, owner, now, requested_by)
    return row


def next_slot(due: datetime, started: datetime, every: timedelta) -> datetime:
    """The first slot after `started` on the grid of `every` that begins at `due`. A job is not run
    again for slots it missed, and a daily job stays at its time of day instead of drifting."""
    return due + every * ((started - due) // every + 1)


def _finish(
    job: ScheduledJobDef, owner: str, started: datetime, due: datetime, error: Optional[str], clock: Callable[[], datetime],
    summary: Optional[RunSummary] = None, duration_ms: Optional[int] = None,
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
        close_run(session, job.name, owner, started, now, error, summary, duration_ms)  # the history; never raises
        row.lease_owner = None
        row.lease_expires_at = None
        resume, row.run_resume_at = row.run_resume_at, None
        if error is None:
            row.last_success_at = now
            row.failures_in_a_row = 0
            row.next_run_at = next_slot(due, started, timedelta(seconds=job.seconds(session)))
        else:
            row.last_error = error[:MAX_ERROR_LENGTH]
            row.last_error_at = now
            row.failures_in_a_row += 1
            retry = min(timing(session, JOB_RETRY_MINUTES), timedelta(seconds=job.seconds(session)))
            row.next_run_at = now + retry
            if resume is not None and now < resume < row.next_run_at:
                row.next_run_at = resume  # a requested run that failed does not push the normal run later
        session.add(row)
        session.commit()


def run_claimed(
    job: ScheduledJobDef, owner: str, started: datetime, due: datetime, clock: Callable[[], datetime]
) -> bool:
    """Runs a job that was claimed, in its own session, and records the outcome. Never raises."""
    began = time.monotonic()
    error: Optional[str] = None
    summary: Optional[RunSummary] = None
    try:
        with Session(engine) as session:
            summary = adapt(job.run(session))
            session.commit()
    except Exception as exc:
        log.exception("job=%s status=failed", job.name)
        error = f"{type(exc).__name__}: {exc}"
        summary = None
    else:
        log.info("heartbeat job=%s status=ok duration_ms=%d", job.name, (time.monotonic() - began) * 1000)
    try:
        _finish(job, owner, started, due, error, clock, summary, int((time.monotonic() - began) * 1000))
    except Exception:
        # The work is done but not recorded; the lease expires and the job is recovered then.
        log.exception("job=%s outcome could not be recorded", job.name)
    return error is None


_background_threads: list[threading.Thread] = []  # the running background jobs (tests join them)


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
            name, due = row.name, row.run_resume_at or row.next_run_at  # a requested run keeps the job's own grid
            session.commit()  # the lease is now visible to every other worker
        ran.append(name)  # never twice in one tick, even if it is due again at once
        if by_name[name].background:
            # Claimed and leased above, so no other tick or worker starts it; the thread records the outcome the same way
            # (`run_claimed` -> `_finish`), so the Scheduled jobs page shows its last run, failure and Run now as for any job.
            thread = threading.Thread(
                target=run_claimed, args=(by_name[name], owner, now, due, clock), name=f"job-{name}", daemon=True,
            )
            _background_threads[:] = [t for t in _background_threads if t.is_alive()] + [thread]
            thread.start()
        else:
            run_claimed(by_name[name], owner, now, due, clock)
    return ran


def scheduled_jobs_tick(session: Session) -> None:
    """The worker job (app/jobs): runs the due recurring jobs. Uses its own sessions; the one it is
    given is unused."""
    run_due_jobs()


@dataclass(frozen=True)
class JobHealth:
    """Which recurring jobs are in trouble (the jobs page's status line and the admin home share this rule)."""

    total: int
    failing: tuple[str, ...]
    stuck: tuple[str, ...]
    overdue: tuple[str, ...]

    @property
    def unhealthy(self) -> int:
        return len(set(self.failing) | set(self.stuck) | set(self.overdue))


def job_health(rows: Sequence[ScheduledJob], now: datetime, late: timedelta, registered: Optional[set[str]] = None) -> JobHealth:
    """A job is failing when its last run raised, stuck when its lease expired (the worker died mid-run), and overdue
    when it is more than `late` past its due time and nothing holds it (the worker is down or stuck). Rows for jobs no
    longer in `registered` are ignored."""
    if registered is not None:
        rows = [row for row in rows if row.name in registered]
    failing = [row.name for row in rows if row.failures_in_a_row]
    stuck = [row.name for row in rows if row.lease_expires_at is not None and row.lease_expires_at <= now]
    overdue = [
        row.name for row in rows
        if row.lease_owner is None and now - row.next_run_at > late and row.name not in failing
    ]
    return JobHealth(len(rows), tuple(sorted(failing)), tuple(sorted(stuck)), tuple(sorted(overdue)))


def status_line(rows: Sequence[ScheduledJob], now: datetime, late: timedelta, registered: Optional[set[str]] = None) -> str:
    """One line for the admin page: whether the recurring jobs are healthy (`job_health`)."""
    health = job_health(rows, now, late, registered)
    if not health.total:
        return "No recurring jobs have run yet."
    failing, stuck, overdue = health.failing, health.stuck, health.overdue
    if not failing and not stuck and not overdue:
        return "The 1 recurring job is healthy." if health.total == 1 else f"All {health.total} recurring jobs are healthy."
    parts = []
    if failing:
        parts.append(f"failing: {', '.join(sorted(failing))}")
    if stuck:
        parts.append(f"did not finish: {', '.join(sorted(stuck))}")
    if overdue:
        parts.append(f"overdue (is the worker running?): {', '.join(sorted(overdue))}")
    return "Problems with recurring jobs, " + "; ".join(parts) + "."


# ---- Run now (#300) -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunNowResult:
    requested: bool  # True when next_run_at was set to now; False means nothing changed and `message` says why
    message: str


def within_a_tick() -> str:
    minutes = max(1, -(-TICK_EVERY_SECONDS // 60))
    return f"within {minutes} minute{'' if minutes == 1 else 's'}"


def run_now_refusal(row: Optional[ScheduledJob], now: datetime) -> Optional[str]:
    """Why a run cannot be requested for this row, or None. Running now (an unexpired lease) comes first: a running job
    is also due, and 'already running' is the more useful answer."""
    if row is None:
        return "The worker has not created this job yet; it runs at its first tick."
    if row.lease_expires_at is not None and row.lease_expires_at > now:
        return f"Already running, started at {row.last_started_at:%Y-%m-%d %H:%M} UTC."
    if row.next_run_at <= now:
        return f"Already due; the worker will run it {within_a_tick()}."
    return None


def run_now(session: Session, job: ScheduledJobDef, actor_email: str, now: Optional[datetime] = None) -> RunNowResult:
    """Asks for a run: sets the job's next_run_at to now and notes who asked. The worker runs it on its next tick
    through the normal path (lease, the job's own guards); this never runs job code. Does not commit. Refuses, changing
    nothing, when the job is running now or is already due."""
    now = now or utcnow()
    row = session.exec(select(ScheduledJob).where(ScheduledJob.name == job.name).with_for_update()).first()
    refusal = run_now_refusal(row, now)
    if refusal:
        return RunNowResult(False, refusal)
    row.run_resume_at = row.next_run_at
    row.run_requested_at = now
    row.run_requested_by = actor_email
    row.next_run_at = now
    session.add(row)
    log_action(session, actor_email, AUDIT_ENTITY, 0, f"Run now requested for job {job.name}", f"was next due {row.run_resume_at:%Y-%m-%d %H:%M} UTC")
    return RunNowResult(True, f"Run requested. The worker will start {job.name} {within_a_tick()}.")

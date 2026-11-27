"""The history of scheduled job runs (#341): one `scheduled_job_run` row per run, what it did in plain words, kept for a number of days.

The runner (`app/scheduled_jobs.py`) calls `open_run` when it claims a job (in the claim's transaction), `abandon_running` when it
finds the previous run's lease expired, and `close_run` when a run ends. Each is wrapped so that a failure to write the history is
logged and never stops the job or its outcome on `scheduled_job`.

What a run did is a `RunSummary` (`app/run_summary.py`): fixed count keys and ticket ids. The sentence an admin reads comes only from
`SENTENCES` below, a fixed wording for each job and count key, so it never names a person or an address. A key with no wording shows
as "{key}: {n}" (a test checks that every key a registered job returns has wording).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from app.models import (
    RUN_DID_NOT_FINISH, RUN_DID_WORK, RUN_FAILED, RUN_NOTHING, RUN_RUNNING, ScheduledJobRun, User,
)
from app.run_summary import NOT_WORK_KEYS, RunSummary
from app.settings_store import JOB_RUN_KEEP_DAYS, count_setting

log = logging.getLogger("app.worker")

MAX_TICKET_IDS = 20
MAX_ERROR_LENGTH = 500  # the same limit as scheduled_job.last_error (app.scheduled_jobs.MAX_ERROR_LENGTH)
PURGE_BATCH = 1000
PURGE_MAX_BATCHES = 100

OUTCOME_LABELS = {
    RUN_RUNNING: "Running",
    RUN_DID_WORK: "Did work",
    RUN_NOTHING: "Nothing to do",
    RUN_FAILED: "Failed",
    RUN_DID_NOT_FINISH: "Did not finish",
}
OUTCOME_TONES = {RUN_RUNNING: "t-blue", RUN_DID_WORK: "t-green", RUN_NOTHING: "t-gray", RUN_FAILED: "t-red", RUN_DID_NOT_FINISH: "t-amber"}


def _s(one: str, many: str) -> tuple[str, str]:
    return one, many


# job name -> count key -> (wording for 1, wording for any other number; {n} is the number)
SENTENCES: dict[str, dict[str, tuple[str, str]]] = {
    "approval-reminders": {
        "reminded": _s("Reminded {n} approver", "Reminded {n} approvers"),
        "overdue_told": _s("Told the super admins about {n} overdue approval", "Told the super admins about {n} overdue approvals"),
    },
    "auto-close": {"closed": _s("Closed {n} ticket", "Closed {n} tickets")},
    "bank-holidays": {
        "fetched": _s("Fetched the bank holiday list", "Fetched the bank holiday list"),
        "added": _s("Added {n} new date", "Added {n} new dates"),
        "fetch_failed": _s("Could not fetch from gov.uk; kept the last list", "Could not fetch from gov.uk; kept the last list"),
    },
    "blob-cleanup": {
        "deleted": _s("Deleted {n} stored file", "Deleted {n} stored files"),
        "failed": _s("{n} stored file could not be deleted", "{n} stored files could not be deleted"),
    },
    "chase": {
        "chased": _s("Chased {n} ticket", "Chased {n} tickets"),
        "resolved": _s("Auto-resolved {n} ticket nobody answered", "Auto-resolved {n} tickets nobody answered"),
        "gave_up": _s("Told the team a chase email could not be sent on {n} ticket", "Told the team a chase email could not be sent on {n} tickets"),
    },
    "entra-account-check": {
        "checked": _s("Checked {n} account in Entra", "Checked {n} accounts in Entra"),
        "deactivated": _s("Deactivated {n} account", "Deactivated {n} accounts"),
        "skipped_not_due": _s("Not due: it checks once a night", "Not due: it checks once a night"),
        "skipped_off": _s("Switched off or Graph is not configured", "Switched off or Graph is not configured"),
    },
    "email-reactions": {
        "read": _s("Read {n} sent email", "Read {n} sent emails"),
        "recorded": _s("Recorded {n} reaction", "Recorded {n} reactions"),
    },
    "job-run-purge": {"deleted": _s("Deleted {n} old run record", "Deleted {n} old run records")},
    "mailbox-changes": {
        "started": _s("Started {n} mailbox change", "Started {n} mailbox changes"),
        "removed": _s("Removed {n} mailbox change", "Removed {n} mailbox changes"),
        "retried": _s("Will retry {n} mailbox change", "Will retry {n} mailbox changes"),
        "failed": _s("{n} mailbox change failed", "{n} mailbox changes failed"),
    },
    "msp-ack-alerts": {"alerted": _s("Alerted about {n} ticket the MSP has not confirmed", "Alerted about {n} tickets the MSP has not confirmed")},
    "msp-forwards": {"forwarded": _s("Forwarded {n} MSP message", "Forwarded {n} MSP messages")},
    "msp-handoff": {
        "sent": _s("Sent {n} ticket to the MSP", "Sent {n} tickets to the MSP"),
        "warned": _s("Warned about {n} ticket due to go to the MSP", "Warned about {n} tickets due to go to the MSP"),
    },
    "note-purge": {"deleted": _s("Deleted {n} note", "Deleted {n} notes")},
    "notification-purge": {"deleted": _s("Deleted {n} notification", "Deleted {n} notifications")},
    "presence-cleanup": {"deleted": _s("Deleted {n} presence record", "Deleted {n} presence records")},
    "reply-files": {"deleted": _s("Dropped {n} held file", "Dropped {n} held files")},
    "scheduled-due": {"reopened": _s("Moved {n} Scheduled ticket back to Open", "Moved {n} Scheduled tickets back to Open")},
    "session-cleanup": {"deleted": _s("Deleted {n} ended session", "Deleted {n} ended sessions")},
    "teams-pending": {"expired": _s("Ended {n} unanswered Teams question", "Ended {n} unanswered Teams questions")},
}


def count_sentence(job_name: str, key: str, number: int) -> str:
    one_many = SENTENCES.get(job_name, {}).get(key)
    if one_many is None:
        return f"{key}: {number}"
    return one_many[0 if number == 1 else 1].format(n=number)


def describe(job_name: str, counts: Optional[dict]) -> str:
    """The sentence for a run's counts: one fixed phrase per non-zero count, joined with ". "; "Nothing to do" when every count is 0."""
    parts = [count_sentence(job_name, key, number) for key, number in (counts or {}).items() if isinstance(number, int) and number]
    return ". ".join(parts) if parts else "Nothing to do"


def outcome_for(summary: Optional[RunSummary]) -> str:
    return RUN_DID_WORK if summary is not None and summary.did_work else RUN_NOTHING


def requested_label(session: Session, run: ScheduledJobRun, names: Optional[dict[str, str]] = None) -> str:
    """"Schedule", or "Run now by {display name}" (the email when that person has no name or no longer exists)."""
    if not run.requested_by:
        return "Schedule"
    if names is None:
        found = session.exec(select(User.display_name).where(User.email == run.requested_by)).first()
        name = found
    else:
        name = names.get(run.requested_by)
    return f"Run now by {name or run.requested_by}"


# ---- Writing the history (never raises) ------------------------------------------------------------------


def open_run(session: Session, job_name: str, owner: str, started: datetime, requested_by: Optional[str]) -> None:
    """Inserts the run's row (state running) in the claim's transaction. A failure is logged and the job still runs."""
    try:
        with session.begin_nested():
            session.add(ScheduledJobRun(job_name=job_name, started_at=started, outcome=RUN_RUNNING, requested_by=requested_by, worker=owner))
            session.flush()
    except Exception:
        log.exception("job=%s run history: could not open the run row", job_name)


def abandon_running(session: Session, job_name: str) -> None:
    """The previous run's lease expired (its worker died or overran): its row, still running, becomes did_not_finish."""
    try:
        with session.begin_nested():
            session.exec(
                update(ScheduledJobRun).where(ScheduledJobRun.job_name == job_name, ScheduledJobRun.outcome == RUN_RUNNING)
                .values(outcome=RUN_DID_NOT_FINISH)
            )
    except Exception:
        log.exception("job=%s run history: could not mark the previous run as not finished", job_name)


def close_run(
    session: Session, job_name: str, owner: str, started: datetime, now: datetime, error: Optional[str],
    summary: Optional[RunSummary], duration_ms: Optional[int],
) -> None:
    """Writes the run's outcome onto its row (found by job, start and worker). The caller has already checked it still holds the
    lease. A failure is logged and does not stop the job's own outcome being recorded."""
    try:
        with session.begin_nested():
            run = session.exec(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.job_name == job_name, ScheduledJobRun.started_at == started,
                    ScheduledJobRun.worker == owner, ScheduledJobRun.outcome == RUN_RUNNING,
                ).with_for_update()
            ).first()
            if run is None:
                return
            run.finished_at = now
            run.duration_ms = duration_ms
            if error is not None:
                run.outcome = RUN_FAILED
                run.error = error[:MAX_ERROR_LENGTH]
            else:
                run.outcome = outcome_for(summary)
            if summary is not None:
                run.summary_counts = {key: int(number) for key, number in summary.counts.items()}
                run.ticket_ids = list(summary.ticket_ids[:MAX_TICKET_IDS])
                run.tickets_more = max(0, len(summary.ticket_ids) - MAX_TICKET_IDS)
            session.add(run)
            session.flush()
    except Exception:
        log.exception("job=%s run history: could not record the outcome", job_name)


# ---- Retention: the job-run-purge job -------------------------------------------------------------------


def purge_job_runs(session: Session, now: Optional[datetime] = None) -> RunSummary:
    """Deletes the runs that started more than `job_run_keep_days` ago. Summary: deleted. Does not commit (the runner does)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=count_setting(session, JOB_RUN_KEEP_DAYS))
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        ids = list(session.exec(select(ScheduledJobRun.id).where(ScheduledJobRun.started_at < cutoff, ScheduledJobRun.outcome != RUN_RUNNING).order_by(ScheduledJobRun.id).limit(PURGE_BATCH)).all())
        if not ids:
            break
        session.exec(delete(ScheduledJobRun).where(ScheduledJobRun.id.in_(ids)))
        session.flush()
        total += len(ids)
    if total:
        log.info("job-run-purge deleted=%d", total)
    return RunSummary.of(deleted=total)


def run_sentence(run: ScheduledJobRun) -> str:
    """One line for a run: what it did, or how it ended when it did not finish cleanly."""
    if run.outcome in (RUN_DID_WORK, RUN_NOTHING):
        return describe(run.job_name, run.summary_counts)
    return OUTCOME_LABELS.get(run.outcome, run.outcome)


def latest_run_sentences(session: Session) -> dict[str, str]:
    """Job name -> the sentence for its most recent run (the Last run column of Scheduled jobs)."""
    newest = select(func.max(ScheduledJobRun.id)).group_by(ScheduledJobRun.job_name)
    return {run.job_name: run_sentence(run) for run in session.exec(select(ScheduledJobRun).where(ScheduledJobRun.id.in_(newest))).all()}

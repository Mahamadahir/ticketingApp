"""The background worker (phase 2C): `python -m app.worker`.

One process runs the scheduled jobs in app/jobs (later: Graph subscription renewal and mail
catch-up, SLA timers, chase and auto-close, delayed Teams notifications, presence refresh). It is a
separate service from the web app because jobs inside the web process would run once per uvicorn
worker: in docker-compose it is the `worker` service, in Azure a Container App fixed at one replica.

It enforces "exactly one" itself: it takes a Postgres advisory lock for its whole life, so a second
worker (a mistaken scale-out, a rolling deploy that overlaps) refuses to start instead of running
every job twice. The lock is released when the process ends, however it ends.

Monitoring: each job that finishes writes one log line

    heartbeat job=<name> status=ok duration_ms=<n>

and a failure writes `job=<name> status=failed` with the traceback. The dead man's switch is a
log-query alert that fires when no `heartbeat job=heartbeat` line has arrived within about three
heartbeat intervals (docs/plan.md, "Health and monitoring"). A job that raises does not stop the
worker or the other jobs; it simply gives no heartbeat.
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Sequence

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session

from app.config import settings, unsafe_setting_errors
from app.telemetry import start_telemetry
from app.database import engine
from app.jobs import JOBS, Job

log = logging.getLogger("app.worker")

# The advisory lock id for "the worker". Any number nothing else in this database uses.
WORKER_LOCK_ID = 726_001
MAX_CONCURRENT_JOBS = 4
LOCK_CHECK_SECONDS = 30  # how often the worker checks it still holds its lock (also keeps that connection busy)


class WorkerState:
    """What the signal handler and the lock watchdog tell main()."""

    stopping = False  # a stop signal arrived
    lost_lock = False  # the single-instance lock was lost


def acquire_single_instance_lock(db: Engine) -> Optional[Connection]:
    """Takes the worker's advisory lock on a connection kept open for the life of the process.
    Returns that connection, or None when another worker already holds the lock."""
    connection = db.connect().execution_options(isolation_level="AUTOCOMMIT")
    got = connection.execute(text("select pg_try_advisory_lock(:id)"), {"id": WORKER_LOCK_ID}).scalar()
    if not got:
        connection.close()
        return None
    return connection


def lock_is_held(connection: Connection) -> bool:
    """Whether this connection still holds the worker lock. A session lock disappears silently if
    its connection is dropped (an idle timeout, a failover), so the worker checks rather than
    assumes. A dead connection counts as not held."""
    try:
        row = connection.execute(
            text(
                "select 1 from pg_locks where locktype = 'advisory' and classid = 0 "
                "and objid = :id and pid = pg_backend_pid() and granted"
            ),
            {"id": WORKER_LOCK_ID},
        ).first()
    except Exception:
        return False
    return row is not None


def make_lock_watchdog(connection: Connection, scheduler: BlockingScheduler, state: WorkerState):
    """A job body that stops the worker when it no longer holds the lock: another worker may be
    running by now, and two workers run every job twice. Exiting lets the supervisor restart it,
    and the restarted worker waits for the lock like any other."""

    def check() -> None:
        if lock_is_held(connection):
            return
        log.error("The single-instance lock was lost; stopping so that two workers never run together")
        state.lost_lock = True
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass

    return check


def job_errors(jobs: Sequence[Job]) -> list[str]:
    errors = []
    names = [job.name for job in jobs]
    if len(set(names)) != len(names):
        errors.append(f"Job names must be unique: {names}")
    errors += [f"Job {job.name} must run at least every second (it is set to {job.every_seconds})" for job in jobs if job.every_seconds < 1]
    return errors


def release_lock(connection: Connection) -> None:
    """Gives the lock up. Closing a pooled connection only returns it to the pool, where it would
    keep the lock, so unlock explicitly and throw the connection away."""
    try:
        connection.execute(text("select pg_advisory_unlock(:id)"), {"id": WORKER_LOCK_ID})
    finally:
        connection.invalidate()
        connection.close()


def run_job(job: Job) -> bool:
    """Runs one job in its own session. Commits and logs the heartbeat on success; on any error
    rolls back, logs the traceback and returns False, never raising into the scheduler."""
    started = time.monotonic()
    try:
        with Session(engine) as session:
            job.run(session)
            session.commit()
    except Exception:
        log.exception("job=%s status=failed", job.name)
        return False
    log.info("heartbeat job=%s status=ok duration_ms=%d", job.name, (time.monotonic() - started) * 1000)
    return True


def build_scheduler(jobs: Optional[Sequence[Job]] = None) -> BlockingScheduler:
    """A scheduler with every job registered, not started. Each job runs at most once at a time
    (a slow run is not overlapped by the next), a pile-up of missed runs collapses into one, and
    each is first due immediately so a restarted worker gives its heartbeat straight away."""
    jobs = JOBS if jobs is None else jobs
    if errors := job_errors(jobs):
        raise ValueError("; ".join(errors))
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(MAX_CONCURRENT_JOBS)},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": None},
        timezone="UTC",
    )
    for job in jobs:
        scheduler.add_job(
            run_job, "interval", seconds=job.every_seconds, args=[job], id=job.name, name=job.name,
            next_run_time=datetime.now(timezone.utc),
        )
    return scheduler


def install_signal_handlers(scheduler: BlockingScheduler, state: Optional[WorkerState] = None) -> None:
    """A stop request (Container Apps sends SIGTERM) lets running jobs finish and then returns.
    Safe when the scheduler has not started yet or is already stopping (a second signal)."""

    def stop(signum, frame):
        log.info("worker stopping (signal %s)", signum)
        if state is not None:
            state.stopping = True
        try:
            scheduler.shutdown(wait=True)
        except SchedulerNotRunningError:
            pass  # not started yet (main() then returns before starting) or already stopping

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    start_telemetry()
    errors = unsafe_setting_errors(settings)
    for error in errors:
        log.error("Refusing to start: %s", error)
    if errors:
        return 2

    errors = job_errors(JOBS)
    for error in errors:
        log.error("Refusing to start: %s", error)
    if errors:
        return 2

    lock = acquire_single_instance_lock(engine)
    if lock is None:
        log.error("Another worker is already running (advisory lock %s is held); exiting", WORKER_LOCK_ID)
        return 1
    state = WorkerState()
    try:
        scheduler = build_scheduler()
        scheduler.add_job(
            make_lock_watchdog(lock, scheduler, state), "interval", seconds=LOCK_CHECK_SECONDS,
            id="lock-watchdog", max_instances=1, coalesce=True,
        )
        install_signal_handlers(scheduler, state)
        if state.stopping:  # a stop signal came in during startup
            return 0
        log.info("worker started: %s", ", ".join(f"{j.name} every {j.every_seconds}s" for j in JOBS))
        scheduler.start()  # blocks until a stop signal or the lock is lost
        if state.lost_lock:
            return 3
        log.info("worker stopped")
        return 0
    finally:
        try:
            release_lock(lock)
        except Exception:  # the connection may be gone; the lock went with it
            log.warning("could not release the worker lock cleanly")


if __name__ == "__main__":
    sys.exit(main())
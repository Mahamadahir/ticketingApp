import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import select

from app import worker
from app.database import engine
from app.jobs import JOBS, Job
from app.models import AuditLog
from app.config import settings


def wrote_an_audit_row(session):
    session.add(AuditLog(actor_email="worker@example.com", entity_type="job", entity_id=0, action="ran"))


def raises(session):
    session.add(AuditLog(actor_email="worker@example.com", entity_type="job", entity_id=0, action="half done"))
    session.flush()
    raise RuntimeError("boom")


def audit_actions(db_session):
    db_session.expire_all()
    return [a.action for a in db_session.exec(select(AuditLog)).all()]


# ---- Running a job ------------------------------------------------------------------------------------


def test_a_job_that_succeeds_is_committed_and_writes_its_heartbeat(db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.worker")

    assert worker.run_job(Job("writer", 60, wrote_an_audit_row)) is True

    assert audit_actions(db_session) == ["ran"]
    lines = [r.getMessage() for r in caplog.records if r.name == "app.worker"]
    assert any(re.fullmatch(r"heartbeat job=writer status=ok duration_ms=\d+", line) for line in lines), lines


def test_a_job_that_fails_is_rolled_back_logged_and_gives_no_heartbeat(db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.worker")

    assert worker.run_job(Job("broken", 60, raises)) is False

    assert audit_actions(db_session) == []  # nothing half done survives
    messages = [r.getMessage() for r in caplog.records]
    assert "job=broken status=failed" in messages
    assert not any(m.startswith("heartbeat job=broken") for m in messages)
    assert any(r.exc_info for r in caplog.records)  # the traceback is in the log


def test_a_failing_job_does_not_stop_the_next_one(db_session):
    worker.run_job(Job("broken", 60, raises))
    assert worker.run_job(Job("writer", 60, wrote_an_audit_row)) is True
    assert audit_actions(db_session) == ["ran"]


def test_the_heartbeat_job_checks_the_database_and_is_registered_by_default(db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.worker")
    [heartbeat] = [j for j in JOBS if j.name == "heartbeat"]

    assert heartbeat.every_seconds == settings.worker_heartbeat_seconds
    assert worker.run_job(heartbeat) is True
    assert any(r.getMessage().startswith("heartbeat job=heartbeat status=ok") for r in caplog.records)


def test_job_names_are_unique():
    names = [job.name for job in JOBS]
    assert len(names) == len(set(names))


# ---- The scheduler ------------------------------------------------------------------------------------


def test_the_scheduler_registers_every_job_without_starting():
    jobs = [Job("a", 30, wrote_an_audit_row), Job("b", 300, wrote_an_audit_row)]

    scheduler = worker.build_scheduler(jobs)

    assert scheduler.running is False
    registered = {j.id: j for j in scheduler.get_jobs()}
    assert set(registered) == {"a", "b"}
    assert registered["a"].trigger.interval.total_seconds() == 30
    assert registered["b"].trigger.interval.total_seconds() == 300


def test_a_job_never_overlaps_itself_and_a_pile_up_of_missed_runs_is_one_run():
    scheduler = worker.build_scheduler([Job("a", 30, wrote_an_audit_row)])
    # Defaults reach a job when the scheduler starts, so they are checked where they are held.
    assert scheduler._job_defaults == {"max_instances": 1, "coalesce": True, "misfire_grace_time": None}
    # (misfire_grace_time None: a late run still runs)


def test_each_job_is_due_straight_away_so_a_restart_gives_its_heartbeat_at_once():
    from datetime import datetime, timedelta, timezone

    [registered] = worker.build_scheduler([Job("a", 3600, wrote_an_audit_row)]).get_jobs()
    # not yet scheduled until start(), but it is created with a first run time of "now"
    assert registered.next_run_time is None or abs(registered.next_run_time - datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_duplicate_names_and_impossible_intervals_are_refused():
    with pytest.raises(ValueError):
        worker.build_scheduler([Job("a", 30, wrote_an_audit_row), Job("a", 60, wrote_an_audit_row)])
    with pytest.raises(ValueError):
        worker.build_scheduler([Job("a", 0, wrote_an_audit_row)])


# ---- Exactly one worker -------------------------------------------------------------------------------


def test_only_one_worker_can_hold_the_lock_and_it_is_released_when_it_ends():
    first = worker.acquire_single_instance_lock(engine)
    assert first is not None
    try:
        assert worker.acquire_single_instance_lock(engine) is None  # a second worker is refused
    finally:
        worker.release_lock(first)

    again = worker.acquire_single_instance_lock(engine)  # the lock was really given up
    assert again is not None
    worker.release_lock(again)


def test_main_refuses_to_start_a_second_worker(caplog, monkeypatch):
    holder = worker.acquire_single_instance_lock(engine)
    started = []
    monkeypatch.setattr(worker, "build_scheduler", lambda: started.append("built") or pytest.fail("second worker started"))
    try:
        assert worker.main() == 1
    finally:
        worker.release_lock(holder)
    assert started == []  # it never got as far as building a scheduler
    assert "Another worker is already running" in caplog.text


def test_main_refuses_unsafe_settings(caplog, monkeypatch):
    monkeypatch.setattr(worker, "unsafe_setting_errors", lambda config: ["SESSION_SECRET_KEY is unset or a default value"])
    # If the refusal were skipped main() would go on to a real scheduler, which blocks forever.
    # Stand in a fake that fails the test at once instead.
    monkeypatch.setattr(worker, "build_scheduler", lambda: pytest.fail("started although the settings are unsafe"))
    assert worker.main() == 2
    assert "Refusing to start: SESSION_SECRET_KEY" in caplog.text
    free = worker.acquire_single_instance_lock(engine)
    assert free is not None  # and it took no lock
    worker.release_lock(free)


def test_main_runs_the_scheduler_then_releases_the_lock(monkeypatch):
    calls = []

    class FakeScheduler:
        def add_job(self, *args, **kwargs):
            calls.append("watchdog")

        def start(self):
            calls.append("start")

        def shutdown(self, wait=True):
            calls.append("shutdown")

    monkeypatch.setattr(worker, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda scheduler, state=None: calls.append("handlers"))

    assert worker.main() == 0

    assert calls == ["watchdog", "handlers", "start"]
    again = worker.acquire_single_instance_lock(engine)  # released on the way out
    assert again is not None
    worker.release_lock(again)


def test_a_stop_signal_shuts_the_scheduler_down_waiting_for_running_jobs(monkeypatch):
    import signal

    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    stopped = []

    class FakeScheduler:
        def shutdown(self, wait=True):
            stopped.append(wait)

    worker.install_signal_handlers(FakeScheduler())

    assert signal.SIGINT in handlers
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert stopped == [True]


def test_the_connection_is_thrown_away_even_if_the_unlock_fails():
    """Closing a pooled connection would keep the lock on it, so a failed unlock must still discard it."""
    calls = []

    class Broken:
        def execute(self, *args, **kwargs):
            calls.append("execute")
            raise RuntimeError("the database went away")

        def invalidate(self):
            calls.append("invalidate")

        def close(self):
            calls.append("close")

    with pytest.raises(RuntimeError):
        worker.release_lock(Broken())

    assert calls == ["execute", "invalidate", "close"]


def test_the_watchdog_sees_whether_the_lock_is_still_held(db_session):
    connection = worker.acquire_single_instance_lock(engine)
    try:
        assert worker.lock_is_held(connection) is True
        connection.execute(text("select pg_advisory_unlock(:id)"), {"id": worker.WORKER_LOCK_ID})
        assert worker.lock_is_held(connection) is False  # the lock is gone though the connection is fine
    finally:
        connection.close()


def test_a_dropped_lock_connection_counts_as_a_lost_lock(db_session):
    connection = worker.acquire_single_instance_lock(engine)
    pid = connection.execute(text("select pg_backend_pid()")).scalar()
    try:
        with engine.connect() as other:  # a failover, an idle timeout or an admin kills the session
            other.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})
            other.commit()
        assert worker.lock_is_held(connection) is False  # no exception, just "not held"
    finally:
        connection.invalidate()
        connection.close()
    free = worker.acquire_single_instance_lock(engine)  # and the lock really is free for a successor
    assert free is not None
    worker.release_lock(free)


def test_a_lost_lock_stops_the_scheduler_and_is_remembered(monkeypatch, caplog):
    stopped = []

    class FakeScheduler:
        def shutdown(self, wait=True):
            stopped.append(wait)

    state = worker.WorkerState()
    monkeypatch.setattr(worker, "lock_is_held", lambda connection: False)

    worker.make_lock_watchdog(object(), FakeScheduler(), state)()

    assert state.lost_lock is True and stopped == [False]
    assert "lock was lost" in caplog.text


def test_a_held_lock_leaves_the_worker_alone(monkeypatch):
    class FakeScheduler:
        def shutdown(self, wait=True):
            raise AssertionError("must not stop")

    state = worker.WorkerState()
    monkeypatch.setattr(worker, "lock_is_held", lambda connection: True)
    worker.make_lock_watchdog(object(), FakeScheduler(), state)()
    assert state.lost_lock is False


def test_the_watchdog_copes_with_a_scheduler_that_is_not_running(monkeypatch):
    from apscheduler.schedulers import SchedulerNotRunningError

    class FakeScheduler:
        def shutdown(self, wait=True):
            raise SchedulerNotRunningError()

    monkeypatch.setattr(worker, "lock_is_held", lambda connection: False)
    worker.make_lock_watchdog(object(), FakeScheduler(), worker.WorkerState())()  # does not raise


def test_main_exits_with_3_when_the_lock_is_lost(monkeypatch):
    watchdog = []

    class FakeScheduler:
        def add_job(self, function, *args, **kwargs):
            watchdog.append(function)

        def start(self):
            watchdog[0]()  # the periodic check runs and finds the lock gone

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(worker, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda scheduler, state=None: None)
    monkeypatch.setattr(worker, "lock_is_held", lambda connection: False)

    assert worker.main() == 3

    free = worker.acquire_single_instance_lock(engine)  # and it still let the lock go
    assert free is not None
    worker.release_lock(free)


def test_main_still_returns_normally_if_the_lock_cannot_be_released(monkeypatch, caplog):
    class FakeScheduler:
        def add_job(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(worker, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda scheduler, state=None: None)
    real_release = worker.release_lock

    def failing_release(connection):
        real_release(connection)  # really let it go, then report the failure
        raise RuntimeError("connection already gone")

    monkeypatch.setattr(worker, "release_lock", failing_release)

    assert worker.main() == 0
    assert "could not release the worker lock cleanly" in caplog.text


def test_the_lock_is_checked_often_enough_that_a_lost_one_is_noticed_quickly(monkeypatch):
    registered = []

    class FakeScheduler:
        def add_job(self, function, trigger, **kwargs):
            registered.append((trigger, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(worker, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda scheduler, state=None: None)

    worker.main()

    [(trigger, kwargs)] = registered
    assert trigger == "interval" and kwargs["seconds"] == worker.LOCK_CHECK_SECONDS
    assert kwargs["id"] == "lock-watchdog" and kwargs["max_instances"] == 1
    assert worker.LOCK_CHECK_SECONDS <= 60  # a lost lock must not go unnoticed for long


def test_a_stop_signal_before_the_scheduler_starts_is_safe(monkeypatch):
    import signal
    from apscheduler.schedulers import SchedulerNotRunningError

    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))

    class NotStarted:
        def shutdown(self, wait=True):
            raise SchedulerNotRunningError()

    state = worker.WorkerState()
    worker.install_signal_handlers(NotStarted(), state)
    handlers[signal.SIGINT](signal.SIGINT, None)  # must not raise
    handlers[signal.SIGINT](signal.SIGINT, None)  # nor on a second signal

    assert state.stopping is True


def test_main_returns_without_starting_when_a_stop_signal_came_during_startup(monkeypatch):
    started = []

    class FakeScheduler:
        def add_job(self, *args, **kwargs):
            pass

        def start(self):
            started.append(True)

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(worker, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(worker, "install_signal_handlers", lambda scheduler, state=None: setattr(state, "stopping", True))

    assert worker.main() == 0
    assert started == []


def test_a_bad_job_interval_is_a_clean_refusal_before_any_lock(monkeypatch, caplog):
    monkeypatch.setattr(worker, "JOBS", [Job("slow", 0, wrote_an_audit_row)])
    monkeypatch.setattr(worker, "build_scheduler", lambda: pytest.fail("started with a bad interval"))

    assert worker.main() == 2

    assert "Refusing to start: Job slow must run at least every second" in caplog.text
    free = worker.acquire_single_instance_lock(engine)
    assert free is not None  # it took no lock
    worker.release_lock(free)


# ---- How it is run ------------------------------------------------------------------------------------


def test_compose_runs_the_worker_from_the_same_image_without_migrating_or_publishing_ports():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    section = compose.split("\n  worker:")[1].split("\nvolumes:")[0]
    assert '["python", "-m", "app.worker"]' in section
    assert "build: ." in section
    assert "ports:" not in section and "docker-entrypoint" not in section
    assert "restart: unless-stopped" in section  # brought back if it exits (a lost lock, a database blip)


def test_the_bicep_starts_the_same_command():
    bicep = Path("infra/main.bicep").read_text(encoding="utf-8")
    assert "command: ['python', '-m', 'app.worker']" in bicep
    assert "{ name: 'APP_ROLE', value: 'worker' }" in bicep


def test_the_scheduler_library_is_pinned():
    assert re.search(r"^APScheduler==\d", Path("requirements.txt").read_text(encoding="utf-8"), re.MULTILINE)
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app import scheduled_jobs
from app.database import engine
from app.jobs import JOBS
from app.models import AuditLog, ScheduledJob
from app.scheduled_jobs import ScheduledJobDef, claim_next_due, registry_errors, run_due_jobs, status_line
from tests.conftest import login_as

START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def counting_job(every=3600, name="count"):
    calls = []

    def run(session):
        calls.append(1)
        session.add(AuditLog(actor_email="job@example.com", entity_type="job", entity_id=0, action=f"{name} work"))

    return ScheduledJobDef(name, every, run), calls


def failing_job(name="boom", every=3600):
    def run(session):
        session.add(AuditLog(actor_email="job@example.com", entity_type="job", entity_id=0, action="half done"))
        session.flush()
        raise RuntimeError("it broke")

    return ScheduledJobDef(name, every, run)


def row(name):
    with Session(engine) as session:
        return session.get(ScheduledJob, name)


def audit_actions():
    with Session(engine) as session:
        return [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.id)).all()]


def late_lines():
    with Session(engine) as session:
        return [a for a in session.exec(select(AuditLog)).all() if a.entity_type == "scheduled_job"]


# ---- Normal running -----------------------------------------------------------------------------------


def test_a_new_job_gets_a_row_and_runs_once_then_waits_its_interval():
    job, calls = counting_job()
    clock = Clock()

    assert run_due_jobs([job], "w1", clock) == ["count"]
    assert run_due_jobs([job], "w1", clock) == []  # not due again yet
    assert len(calls) == 1

    saved = row("count")
    assert saved.last_success_at == START
    assert saved.next_run_at == START + timedelta(hours=1)
    assert saved.lease_owner is None and saved.lease_expires_at is None
    assert saved.failures_in_a_row == 0 and saved.last_error is None
    assert late_lines() == []  # on time: nothing to say

    clock.advance(hours=1)
    assert run_due_jobs([job], "w1", clock) == ["count"]
    assert len(calls) == 2


def test_two_jobs_run_in_one_tick_and_a_failing_one_does_not_stop_the_other():
    good, calls = counting_job(name="good")
    assert sorted(run_due_jobs([failing_job(), good], "w1", Clock())) == ["boom", "good"]
    assert len(calls) == 1


def test_a_job_that_is_no_longer_registered_is_left_alone():
    job, calls = counting_job()
    other, other_calls = counting_job(name="other")
    run_due_jobs([job], "w1", Clock())
    run_due_jobs([other], "w1", Clock(START + timedelta(days=1)))
    assert len(calls) == 1 and len(other_calls) == 1


# ---- Late runs ----------------------------------------------------------------------------------------


def test_a_job_that_is_days_overdue_runs_once_and_says_it_was_late():
    job, calls = counting_job(every=3600)
    clock = Clock()
    run_due_jobs([job], "w1", clock)

    clock.advance(days=3)  # the worker was down for 72 missed runs
    assert run_due_jobs([job], "w1", clock) == ["count"]
    assert run_due_jobs([job], "w1", clock) == []  # no catch-up storm
    assert len(calls) == 2

    saved = row("count")
    assert saved.next_run_at == clock.now + timedelta(hours=1)  # the next run is one interval from now
    lines = late_lines()
    assert len(lines) == 1
    assert lines[0].action == "Job count ran late"
    assert lines[0].actor_email == "worker"
    assert "due 2026-11-02 10:00 UTC" in lines[0].detail and "started 2026-11-05 09:00 UTC" in lines[0].detail


def test_a_job_keeps_its_time_of_day_instead_of_drifting():
    job, _ = counting_job(every=86400)
    clock = Clock()
    run_due_jobs([job], "w1", clock)  # due and started at 09:00
    clock.advance(days=1, seconds=45)  # the tick picks it up 45 seconds after it was due
    run_due_jobs([job], "w1", clock)
    assert row("count").next_run_at == START + timedelta(days=2)


def test_a_job_runs_at_most_once_per_tick_even_if_it_is_due_again_at_once():
    job, calls = counting_job(every=1)

    class RacingClock(Clock):
        def __call__(self):
            self.advance(seconds=2)  # time moves on between the run and the next look
            return self.now

    clock = RacingClock()
    assert run_due_jobs([job], "w1", clock) == ["count"]
    assert len(calls) == 1


def test_a_run_inside_the_late_allowance_is_not_called_late():
    job, _ = counting_job(every=3600)
    clock = Clock()
    run_due_jobs([job], "w1", clock)
    clock.advance(hours=1, minutes=2)  # the allowance is 3 minutes by default
    run_due_jobs([job], "w1", clock)
    assert late_lines() == []


# ---- Two workers --------------------------------------------------------------------------------------


def test_a_second_worker_skips_a_job_the_first_has_locked_or_leased():
    job, _ = counting_job()
    clock = Clock()
    with Session(engine) as session:
        scheduled_jobs.ensure_rows(session, [job], clock())
        session.commit()

    first = Session(engine)
    second = Session(engine)
    try:
        assert claim_next_due(first, [job], "w1", clock()) is not None  # row locked, lease not committed yet
        assert claim_next_due(second, [job], "w2", clock()) is None  # SKIP LOCKED
        first.commit()
        second.rollback()
        assert claim_next_due(second, [job], "w2", clock()) is None  # now it is leased
    finally:
        first.close()
        second.close()


def test_two_workers_racing_run_the_job_once():
    runs = []

    def slow(session):
        runs.append(1)
        time.sleep(0.3)

    job = ScheduledJobDef("slow", 3600, slow)
    clock = Clock()
    barrier = threading.Barrier(2)
    results = {}

    def tick(owner):
        barrier.wait()
        results[owner] = run_due_jobs([job], owner, clock)

    threads = [threading.Thread(target=tick, args=(owner,)) for owner in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(runs) == 1
    assert sorted(results["w1"] + results["w2"]) == ["slow"]


# ---- Failure and retry --------------------------------------------------------------------------------


def test_a_failure_is_recorded_rolled_back_and_retried_after_the_retry_wait():
    clock = Clock()
    assert run_due_jobs([failing_job()], "w1", clock) == ["boom"]

    saved = row("boom")
    assert audit_actions() == []  # the half-done work was rolled back
    assert saved.last_error == "RuntimeError: it broke"
    assert saved.last_error_at == START
    assert saved.failures_in_a_row == 1
    assert saved.last_success_at is None
    assert saved.lease_owner is None
    assert saved.next_run_at == START + timedelta(minutes=5)  # the retry wait, not the hourly interval

    clock.advance(minutes=4)
    assert run_due_jobs([failing_job()], "w1", clock) == []
    clock.advance(minutes=1)
    assert run_due_jobs([failing_job()], "w1", clock) == ["boom"]
    assert row("boom").failures_in_a_row == 2


def test_the_retry_never_waits_longer_than_the_jobs_own_interval():
    run_due_jobs([failing_job(every=120)], "w1", Clock())
    assert row("boom").next_run_at == START + timedelta(seconds=120)


def test_a_job_that_works_again_clears_its_failures():
    clock = Clock()
    state = {"fail": True}

    def flaky(session):
        if state["fail"]:
            raise RuntimeError("down")

    job = ScheduledJobDef("flaky", 3600, flaky)
    run_due_jobs([job], "w1", clock)
    assert row("flaky").failures_in_a_row == 1

    state["fail"] = False
    clock.advance(minutes=5)
    run_due_jobs([job], "w1", clock)
    saved = row("flaky")
    assert saved.failures_in_a_row == 0
    assert saved.last_success_at == clock.now
    assert saved.next_run_at == clock.now + timedelta(hours=1)


def test_a_blank_timing_setting_falls_back_to_its_default():
    from app.settings_store import JOB_LEASE_MINUTES, set_setting

    with Session(engine) as session:
        set_setting(session, JOB_LEASE_MINUTES, "")
        session.commit()
        assert scheduled_jobs.timing(session, JOB_LEASE_MINUTES) == timedelta(minutes=10)


def test_a_failure_to_record_the_outcome_does_not_stop_the_other_jobs(monkeypatch):
    good, calls = counting_job(name="good")
    real = scheduled_jobs._finish

    def broken_finish(job, *args):
        if job.name == "first":
            raise RuntimeError("database went away")
        return real(job, *args)

    monkeypatch.setattr(scheduled_jobs, "_finish", broken_finish)
    first = ScheduledJobDef("first", 3600, lambda session: None)
    assert sorted(run_due_jobs([first, good], "w1", Clock())) == ["first", "good"]
    assert len(calls) == 1


# ---- A crash ------------------------------------------------------------------------------------------


def crash_after_claiming(job, clock, owner="dead-worker"):
    """What a worker that died mid-run leaves behind: a lease that nothing releases."""
    with Session(engine) as session:
        scheduled_jobs.ensure_rows(session, [job], clock())
        session.commit()
    with Session(engine) as session:
        claim_next_due(session, [job], owner, clock())
        session.commit()


def test_a_crashed_runs_lease_blocks_the_job_until_it_expires_and_then_it_runs_again():
    job, calls = counting_job()
    clock = Clock()
    crash_after_claiming(job, clock)

    clock.advance(minutes=9)  # the lease is 10 minutes by default
    assert run_due_jobs([job], "w2", clock) == []
    assert calls == []

    clock.advance(minutes=1)
    assert run_due_jobs([job], "w2", clock) == ["count"]
    assert len(calls) == 1
    saved = row("count")
    assert saved.lease_owner is None
    assert saved.last_success_at == clock.now
    assert saved.last_error.startswith("The previous run never finished")  # kept for the admin to see
    assert saved.failures_in_a_row == 0  # the recovered run then worked
    recovered = [line for line in late_lines() if "recovered" in line.action]
    assert [line.action for line in recovered] == ["Job count recovered after its lease expired"]
    assert "dead-worker" in recovered[0].detail


def test_a_job_that_crashes_the_worker_every_time_shows_as_failing():
    job, _ = counting_job()
    clock = Clock()
    crash_after_claiming(job, clock)
    clock.advance(minutes=11)
    with Session(engine) as session:
        claim_next_due(session, [job], "w2", clock())  # recovers the first crash, then crashes again
        session.commit()
    saved = row("count")
    assert saved.failures_in_a_row == 1
    assert "failing: count" in status_line([saved], clock(), timedelta(minutes=3))


def test_a_worker_whose_lease_was_taken_over_does_not_overwrite_the_new_owners_record():
    job, _ = counting_job()
    clock = Clock()
    crash_after_claiming(job, clock)
    clock.advance(minutes=11)
    with Session(engine) as session:
        taken = claim_next_due(session, [job], "w2", clock())
        started_by_w2 = taken.last_started_at
        session.commit()

    # The first worker comes back to life and reports success for its old run.
    scheduled_jobs._finish(job, "dead-worker", START, START, None, clock)

    saved = row("count")
    assert saved.lease_owner == "w2" and saved.last_started_at == started_by_w2
    assert saved.last_success_at is None


def test_a_stale_run_by_the_same_worker_does_not_overwrite_its_newer_run():
    job, _ = counting_job()
    clock = Clock()
    crash_after_claiming(job, clock, "w1")  # a run by "w1" that overran its lease
    clock.advance(minutes=11)
    with Session(engine) as session:
        claim_next_due(session, [job], "w1", clock())  # the same worker id claims again (a restart reuses it)
        session.commit()

    scheduled_jobs._finish(job, "w1", START, START, "old failure", clock)  # the overrun run reports in

    saved = row("count")
    assert saved.lease_owner == "w1" and saved.last_error.startswith("The previous run never finished")
    assert saved.failures_in_a_row == 1  # only the recovery counted, not the stale report


# ---- Wiring -------------------------------------------------------------------------------------------


def test_the_worker_runs_the_tick_every_minute():
    tick = [job for job in JOBS if job.name == "scheduled-jobs"]
    assert len(tick) == 1 and tick[0].every_seconds == 60


def test_the_registry_refuses_duplicate_names_and_zero_intervals():
    job, _ = counting_job()
    assert registry_errors([job]) == []
    assert registry_errors([job, job])
    assert registry_errors([ScheduledJobDef("x", 0, lambda s: None)])


def test_the_registered_jobs_are_valid():
    assert registry_errors(scheduled_jobs.SCHEDULED_JOBS) == []


# ---- The admin status line ---------------------------------------------------------------------------


def test_status_line_reports_healthy_failing_and_stuck_jobs():
    now = START
    healthy = ScheduledJob(name="a", next_run_at=now)
    failing = ScheduledJob(name="b", next_run_at=now, failures_in_a_row=2)
    stuck = ScheduledJob(name="c", next_run_at=now, lease_owner="w", lease_expires_at=now - timedelta(minutes=1))
    late = timedelta(minutes=3)
    assert status_line([], now, late) == "No recurring jobs have run yet."
    assert status_line([healthy], now, late) == "All 1 recurring job(s) are healthy."
    assert "failing: b" in status_line([healthy, failing], now, late)
    assert "did not finish: c" in status_line([healthy, stuck], now, late)


def test_status_line_reports_a_job_the_worker_has_stopped_running_and_ignores_retired_ones():
    late = timedelta(minutes=3)
    stale = ScheduledJob(name="a", next_run_at=START - timedelta(hours=2))
    just_due = ScheduledJob(name="b", next_run_at=START - timedelta(minutes=2))
    retired = ScheduledJob(name="gone", next_run_at=START, failures_in_a_row=5)
    assert "overdue (is the worker running?): a" in status_line([stale, just_due], START, late)
    assert status_line([just_due, retired], START, late, {"b"}) == "All 1 recurring job(s) are healthy."


def test_the_admin_page_shows_the_status_and_the_last_error(client, monkeypatch):
    monkeypatch.setattr(scheduled_jobs, "SCHEDULED_JOBS", [failing_job()])
    monkeypatch.setattr("app.routers.admin.SCHEDULED_JOBS", scheduled_jobs.SCHEDULED_JOBS)
    run_due_jobs(None, "w1", Clock(datetime.now(timezone.utc)))
    login_as(client, "admin@example.com")

    page = client.get("/admin/scheduled-jobs")
    assert page.status_code == 200
    assert "Problems with recurring jobs, failing: boom." in page.text
    assert "RuntimeError: it broke" in page.text


def test_only_a_super_admin_sees_the_page(client):
    login_as(client, "agent@example.com")
    assert client.get("/admin/scheduled-jobs").status_code == 403

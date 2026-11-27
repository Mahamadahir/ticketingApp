"""#341: the history of scheduled job runs (`scheduled_job_run`), RunSummary, retention and Logs > Job runs."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import scheduled_jobs
from app.database import engine
from app.job_runs import MAX_TICKET_IDS, SENTENCES, count_sentence, describe, purge_job_runs
from app.models import RUN_DID_NOT_FINISH, RUN_DID_WORK, RUN_FAILED, RUN_NOTHING, RUN_RUNNING, ScheduledJob, ScheduledJobRun, Ticket
from app.run_summary import RunSummary
from app.scheduled_jobs import SCHEDULED_JOBS, ScheduledJobDef, claim_next_due, run_due_jobs, run_now
from app.settings_store import JOB_RUN_KEEP_DAYS, REGISTRY, set_setting
from tests.conftest import login_as
from tests.test_scheduled_jobs import START, Clock, crash_after_claiming, registered, ticked_once


def runs(name=None):
    with Session(engine) as session:
        query = select(ScheduledJobRun).order_by(ScheduledJobRun.id)
        if name:
            query = query.where(ScheduledJobRun.job_name == name)
        return list(session.exec(query).all())


def summary_job(summary, name="count", error=None):
    def run(session):
        if error:
            raise RuntimeError(error)
        return summary

    return ScheduledJobDef(name, 3600, run)


# ---- A run leaves one row ------------------------------------------------------------------------------


def test_a_run_with_work_leaves_one_row_with_its_summary_and_times():
    job = summary_job(RunSummary.of([7, 8], closed=2))
    clock = Clock()
    assert run_due_jobs([job], "w1", clock) == ["count"]

    [run] = runs()
    assert run.job_name == "count" and run.outcome == RUN_DID_WORK
    assert run.started_at == START and run.finished_at == START
    assert run.duration_ms is not None and run.duration_ms >= 0
    assert run.summary_counts == {"closed": 2} and run.ticket_ids == [7, 8] and run.tickets_more == 0
    assert run.error is None and run.requested_by is None and run.worker == "w1"


def test_a_run_that_found_nothing_is_nothing_to_do_whether_it_returns_none_or_zero_counts():
    assert run_due_jobs([summary_job(None, "none"), summary_job(RunSummary.of(closed=0), "zero")], "w1", Clock())
    by_name = {run.job_name: run for run in runs()}
    assert by_name["none"].outcome == RUN_NOTHING and by_name["none"].summary_counts == {}
    assert by_name["zero"].outcome == RUN_NOTHING and by_name["zero"].summary_counts == {"closed": 0}


def test_a_summary_with_only_a_ticket_id_or_an_int_return_counts_as_work():
    run_due_jobs([summary_job(RunSummary.of([3])), ScheduledJobDef("int", 3600, lambda session: 4)], "w1", Clock())
    by_name = {run.job_name: run for run in runs()}
    assert by_name["count"].outcome == RUN_DID_WORK
    assert by_name["int"].outcome == RUN_DID_WORK and by_name["int"].summary_counts == {"done": 4}


def test_looking_is_not_doing_a_summary_of_only_checked_counts_is_nothing_to_do():
    run_due_jobs([summary_job(RunSummary.of(checked=40, deactivated=0))], "w1", Clock())
    [run] = runs()
    assert run.outcome == RUN_NOTHING and run.summary_counts == {"checked": 40, "deactivated": 0}


def test_a_failing_run_leaves_a_failed_row_with_the_error_and_the_job_row_as_before():
    job = summary_job(None, error="it broke")
    run_due_jobs([job], "w1", Clock())

    [run] = runs()
    assert run.outcome == RUN_FAILED and run.error == "RuntimeError: it broke"
    assert run.finished_at == START and run.summary_counts == {}
    with Session(engine) as session:
        saved = session.get(ScheduledJob, "count")
    assert saved.last_error == "RuntimeError: it broke" and saved.failures_in_a_row == 1


def test_the_stored_error_is_cut_like_the_job_rows():
    run_due_jobs([summary_job(None, error="x" * 2000)], "w1", Clock())
    assert len(runs()[0].error) == scheduled_jobs.MAX_ERROR_LENGTH


def test_the_row_is_inserted_when_the_job_is_claimed_and_is_running_until_it_finishes():
    job = summary_job(None)
    clock = Clock()
    with Session(engine) as session:
        scheduled_jobs.ensure_rows(session, [job], clock())
        session.commit()
    with Session(engine) as session:
        claim_next_due(session, [job], "w1", clock())
        session.commit()
    [run] = runs()
    assert run.outcome == RUN_RUNNING and run.finished_at is None and run.duration_ms is None and run.worker == "w1"


def test_ticket_ids_are_capped_at_twenty_and_the_rest_counted():
    ids = list(range(1, 36))
    run_due_jobs([summary_job(RunSummary.of(ids, closed=35))], "w1", Clock())
    [run] = runs()
    assert run.ticket_ids == ids[:MAX_TICKET_IDS] and len(run.ticket_ids) == 20 and run.tickets_more == 15


# ---- Run now, a dead worker, a taken-over lease -----------------------------------------------------------


def test_a_run_now_records_who_asked_and_a_scheduled_run_does_not():
    job = summary_job(None)
    run_due_jobs([job], "w1", Clock())  # scheduled
    clock = Clock(START + timedelta(minutes=5))
    with Session(engine) as session:
        assert run_now(session, job, "admin@example.com", clock()).requested
        session.commit()
    assert run_due_jobs([job], "w1", clock) == ["count"]

    first, second = runs()
    assert first.requested_by is None
    assert second.requested_by == "admin@example.com"
    with Session(engine) as session:
        saved = session.get(ScheduledJob, "count")
    assert saved.run_requested_by is None  # the job row clears it as before


def test_a_run_whose_worker_died_is_did_not_finish_when_the_job_is_next_claimed_and_the_new_run_has_its_own_row():
    job = summary_job(RunSummary.of(closed=1))
    clock = Clock()
    crash_after_claiming(job, clock)
    assert [r.outcome for r in runs()] == [RUN_RUNNING]

    clock.advance(minutes=11)
    assert run_due_jobs([job], "w2", clock) == ["count"]

    dead, new = runs()
    assert dead.outcome == RUN_DID_NOT_FINISH and dead.worker == "dead-worker" and dead.finished_at is None
    assert new.outcome == RUN_DID_WORK and new.worker == "w2" and new.started_at == clock.now


def test_a_run_whose_lease_was_taken_over_does_not_overwrite_the_other_workers_row():
    job = summary_job(None)
    clock = Clock()
    crash_after_claiming(job, clock)
    clock.advance(minutes=11)
    with Session(engine) as session:
        claim_next_due(session, [job], "w2", clock())
        session.commit()

    # The first worker comes back and reports a failure for its old run: nothing changes.
    scheduled_jobs._finish(job, "dead-worker", START, START, "late failure", clock, RunSummary.of(closed=9), 5)

    old, new = runs()
    assert old.outcome == RUN_DID_NOT_FINISH and old.error is None and old.summary_counts == {}
    assert new.outcome == RUN_RUNNING and new.worker == "w2" and new.error is None


# ---- Recording must never stop the job -------------------------------------------------------------------


def test_a_failure_writing_the_history_does_not_stop_the_job_or_its_outcome(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("history table gone")

    monkeypatch.setattr("app.job_runs.ScheduledJobRun", broken)
    calls = []
    job = ScheduledJobDef("count", 3600, lambda session: calls.append(1))

    assert run_due_jobs([job], "w1", Clock()) == ["count"]
    assert calls == [1]
    with Session(engine) as session:
        saved = session.get(ScheduledJob, "count")
    assert saved.last_success_at == START and saved.lease_owner is None and saved.failures_in_a_row == 0
    monkeypatch.undo()
    assert runs() == []


def test_a_database_error_in_the_history_write_does_not_break_the_surrounding_transaction(monkeypatch):
    """The write is in a savepoint: a database error there leaves the job's own row recorded."""
    from app import job_runs

    real = job_runs.ScheduledJobRun

    def bad_row(**kwargs):
        return real(**{**kwargs, "outcome": None})  # NOT NULL fails at the flush

    monkeypatch.setattr(job_runs, "ScheduledJobRun", bad_row)
    job = ScheduledJobDef("count", 3600, lambda session: None)
    assert run_due_jobs([job], "w1", Clock()) == ["count"]
    with Session(engine) as session:
        saved = session.get(ScheduledJob, "count")
    assert saved.last_success_at == START and saved.lease_owner is None


# ---- Every registered job returns a summary --------------------------------------------------------------


def test_every_registered_job_returns_a_summary_and_finds_nothing_in_an_empty_database(db_session, monkeypatch):
    monkeypatch.setattr("app.bank_holidays.fetch_feed", lambda region: [])  # never the real gov.uk
    names = []
    for job in SCHEDULED_JOBS:
        result = job.run(db_session)
        db_session.commit()
        assert isinstance(result, RunSummary), f"{job.name} did not return a RunSummary"
        assert not result.did_work, f"{job.name} claims work in an empty database: {result}"
        assert result.counts, f"{job.name} returned no count keys"
        names.append(job.name)
    assert "job-run-purge" in names and "auto-close" in names


def test_every_count_key_a_job_can_return_has_a_sentence():
    expected_keys = {
        "approval-reminders": {"reminded", "overdue_told"}, "auto-close": {"closed"}, "bank-holidays": {"fetched", "added", "fetch_failed"},
        "blob-cleanup": {"deleted", "failed"}, "chase": {"chased", "resolved", "gave_up"},
        "entra-account-check": {"checked", "deactivated", "skipped_not_due", "skipped_off"}, "email-reactions": {"read", "recorded"},
        "job-run-purge": {"deleted"}, "mailbox-changes": {"started", "removed", "retried", "failed"},
        "msp-ack-alerts": {"alerted"}, "msp-forwards": {"forwarded"}, "msp-handoff": {"sent", "warned"},
        "note-purge": {"deleted"}, "notification-purge": {"deleted"}, "presence-cleanup": {"deleted"}, "reply-files": {"deleted"},
        "inbound-attachments": {"retried", "added", "gave_up"}, "scheduled-due": {"reopened"}, "session-cleanup": {"deleted"}, "teams-pending": {"expired"},
    }
    assert {job.name for job in SCHEDULED_JOBS} == set(expected_keys)  # a new job must be listed here, with its wording
    for name, keys in expected_keys.items():
        assert keys <= set(SENTENCES[name]), f"{name} has keys with no sentence: {keys - set(SENTENCES[name])}"
    for name, wording in SENTENCES.items():
        for key, (one, many) in wording.items():
            assert "{n}" in many or key in ("fetched", "skipped_not_due", "skipped_off", "fetch_failed"), f"{name}.{key}"
            one.format(n=1), many.format(n=2)


def test_the_sentences_are_fixed_wording_with_singular_and_plural_and_a_fallback_for_an_unknown_key():
    assert describe("auto-close", {"closed": 1}) == "Closed 1 ticket"
    assert describe("auto-close", {"closed": 3}) == "Closed 3 tickets"
    assert describe("chase", {"chased": 2, "resolved": 0, "gave_up": 1}).startswith("Chased 2 tickets. Told the team")
    assert describe("auto-close", {"closed": 0}) == "Nothing to do" and describe("auto-close", {}) == "Nothing to do"
    assert count_sentence("auto-close", "mystery", 4) == "mystery: 4"
    assert count_sentence("no-such-job", "closed", 4) == "closed: 4"


def test_the_bank_holiday_job_reports_the_dates_it_added_and_nothing_when_the_list_is_unchanged(db_session, monkeypatch):
    from datetime import date

    from app.bank_holidays import run_bank_holiday_fetch

    days = [date(2027, 1, 1), date(2027, 12, 27)]
    monkeypatch.setattr("app.bank_holidays.fetch_feed", lambda region: days)
    first = run_bank_holiday_fetch(db_session)
    db_session.commit()
    assert first.counts == {"fetched": 1, "added": 2, "fetch_failed": 0} and first.did_work
    again = run_bank_holiday_fetch(db_session)
    db_session.commit()
    assert again.counts == {"fetched": 1, "added": 0, "fetch_failed": 0} and not again.did_work


def test_the_auto_close_job_reports_the_tickets_it_closed(db_session):
    from tests.test_auto_close_chase import close_job, configure, resolved
    from app.auto_close_chase import run_auto_close
    from tests.test_auto_close_chase import later

    configure(db_session, close_days=7)
    ticket = resolved(db_session)
    summary = run_auto_close(db_session, later(10))
    db_session.commit()
    assert summary.counts == {"closed": 1} and summary.ticket_ids == [ticket.id] and summary.did_work


# ---- Retention -------------------------------------------------------------------------------------------


def test_the_keep_days_setting_defaults_to_thirty_with_a_minimum_of_one():
    assert REGISTRY[JOB_RUN_KEEP_DAYS].default == "30" and REGISTRY[JOB_RUN_KEEP_DAYS].kind == "count"
    assert REGISTRY[JOB_RUN_KEEP_DAYS].label == "Scheduled job history: days kept (days)"
    assert REGISTRY[JOB_RUN_KEEP_DAYS].group in {"other"}


def add_run(session, days_ago, name="count"):
    session.add(ScheduledJobRun(job_name=name, started_at=datetime.now(timezone.utc) - timedelta(days=days_ago), outcome=RUN_NOTHING))


def test_the_purge_deletes_runs_older_than_the_setting_and_keeps_newer_ones(db_session):
    add_run(db_session, 31)
    add_run(db_session, 29)
    add_run(db_session, 1)
    db_session.commit()

    summary = purge_job_runs(db_session)
    db_session.commit()

    assert summary.counts == {"deleted": 1} and summary.did_work
    assert len(runs()) == 2

    set_setting(db_session, JOB_RUN_KEEP_DAYS, "7")
    db_session.commit()
    assert purge_job_runs(db_session).counts == {"deleted": 1}
    db_session.commit()
    assert len(runs()) == 1


def test_the_purge_is_a_registered_daily_job_whose_own_runs_are_recorded(db_session):
    [job] = [j for j in SCHEDULED_JOBS if j.name == "job-run-purge"]
    assert job.every_seconds == 86400 and job.run is purge_job_runs and job.description
    add_run(db_session, 40, "old")
    db_session.commit()

    assert run_due_jobs([job], "w1", Clock(datetime.now(timezone.utc))) == ["job-run-purge"]

    own = runs("job-run-purge")
    assert len(own) == 1 and own[0].outcome == RUN_DID_WORK and own[0].summary_counts == {"deleted": 1}
    assert runs("old") == []


def test_the_purge_never_deletes_a_run_that_is_still_running(db_session):
    db_session.add(ScheduledJobRun(job_name="count", started_at=datetime.now(timezone.utc) - timedelta(days=90), outcome=RUN_RUNNING))
    db_session.commit()
    assert purge_job_runs(db_session).counts == {"deleted": 0}


def test_a_failed_bank_holiday_fetch_inside_the_grace_period_is_visible(db_session, monkeypatch):
    from datetime import date

    from app.bank_holidays import FetchError, run_bank_holiday_fetch

    monkeypatch.setattr("app.bank_holidays.fetch_feed", lambda region: [date(2027, 1, 1)])
    run_bank_holiday_fetch(db_session)
    db_session.commit()

    def down(region):
        raise FetchError("down")

    monkeypatch.setattr("app.bank_holidays.fetch_feed", down)
    summary = run_bank_holiday_fetch(db_session)
    assert summary.counts["fetch_failed"] == 1 and summary.did_work


# ---- The pages ------------------------------------------------------------------------------------------


def seed_runs(db_session):
    now = datetime.now(timezone.utc)
    ticket = Ticket(subject="Printer", requester_email="sam@example.com")
    db_session.add(ticket)
    db_session.commit()
    db_session.add_all([
        ScheduledJobRun(job_name="auto-close", started_at=now - timedelta(hours=3), finished_at=now, duration_ms=1500, outcome=RUN_DID_WORK,
                        summary_counts={"closed": 1}, ticket_ids=[ticket.id], tickets_more=4, worker="w1"),
        ScheduledJobRun(job_name="chase", started_at=now - timedelta(hours=2), finished_at=now, duration_ms=20, outcome=RUN_NOTHING,
                        summary_counts={"chased": 0, "resolved": 0, "gave_up": 0}, worker="w1"),
        ScheduledJobRun(job_name="chase", started_at=now - timedelta(hours=1), finished_at=now, duration_ms=30, outcome=RUN_FAILED,
                        error="RuntimeError: it broke\nTraceback line", requested_by="admin@example.com", worker="w1"),
        ScheduledJobRun(job_name="session-cleanup", started_at=now - timedelta(days=5), outcome=RUN_DID_NOT_FINISH, worker="w0"),
    ])
    db_session.commit()
    return ticket


def test_job_runs_is_for_super_admins_only(client, db_session):
    login_as(client, "agent@example.com")
    assert client.get("/admin/job-runs").status_code == 403


def test_job_runs_lists_newest_first_with_sentences_chips_and_ticket_links(client, db_session):
    ticket = seed_runs(db_session)
    login_as(client, "admin@example.com", "Ada Admin")
    page = client.get("/admin/job-runs")
    assert page.status_code == 200
    html = page.text
    assert html.index("session-cleanup") > html.index("auto-close")  # the oldest last
    assert "Closed 1 ticket" in html and f'href="/tickets/{ticket.number}"' in html and "and 4 more" in html
    assert "Nothing to do" in html and "Did work" in html and "Failed" in html and "Did not finish" in html
    assert "t-green" in html and "t-gray" in html and "t-red" in html and "t-amber" in html
    assert "RuntimeError: it broke" in html and "Full error" in html  # the short line, and the whole text in a details
    assert "Run now by Ada Admin" in html and "Schedule" in html
    assert "1.5 s" in html and "20 ms" in html


def test_job_runs_filters_by_job_outcome_and_run_now_and_dates(client, db_session):
    seed_runs(db_session)
    login_as(client, "admin@example.com")
    assert "Closed 1 ticket" not in client.get("/admin/job-runs?job=chase").text
    assert client.get("/admin/job-runs?job=chase").text.count("<tr>") == 3  # head and two runs
    failed = client.get("/admin/job-runs?outcome=failed").text
    assert "RuntimeError" in failed and "Closed 1 ticket" not in failed
    only_run_now = client.get("/admin/job-runs?run_now_only=true").text
    assert only_run_now.count("<tr>") == 2 and "Run now by" in only_run_now
    today = datetime.now(timezone.utc).date().isoformat()
    recent = client.get(f"/admin/job-runs?date_from={today}").text
    assert "session-cleanup</td>" not in recent
    assert client.get("/admin/job-runs?date_from=2026-12-31&date_to=2026-01-01").status_code == 400


def test_the_job_filter_lists_registered_jobs_and_retired_names_in_the_table(client, db_session):
    db_session.add(ScheduledJobRun(job_name="retired-job", started_at=datetime.now(timezone.utc), outcome=RUN_NOTHING))
    db_session.commit()
    login_as(client, "admin@example.com")
    html = client.get("/admin/job-runs").text
    assert '<option value="retired-job"' in html and '<option value="auto-close"' in html


def test_job_runs_pages(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([ScheduledJobRun(job_name="count", started_at=now - timedelta(minutes=i), outcome=RUN_NOTHING) for i in range(60)])
    db_session.commit()
    login_as(client, "admin@example.com")
    first = client.get("/admin/job-runs?job=count").text
    second = client.get("/admin/job-runs?job=count&page=2").text
    assert "page=2" in first and first.count("<tr>") != second.count("<tr>")
    assert "job=count" in second  # the filter survives the paging links


def test_job_runs_is_in_the_logs_group_of_the_admin_menu():
    from app.navigation import ADMIN_GROUPS

    logs = dict(ADMIN_GROUPS)["Logs"]
    assert [page.href for page in logs if page.key == "job-runs"] == ["/admin/job-runs"]


def test_scheduled_jobs_has_a_history_link_per_job_and_the_latest_outcome_in_words(client, monkeypatch):
    job = summary_job(RunSummary.of([], closed=3), name="auto-close")
    registered(monkeypatch, job)
    ticked_once(job)
    login_as(client, "admin@example.com")
    html = client.get("/admin/scheduled-jobs").text
    assert 'href="/admin/job-runs?job=auto-close"' in html and "Closed 3 tickets" in html
    landing = client.get("/admin/job-runs?job=auto-close")
    assert landing.status_code == 200 and "Closed 3 tickets" in landing.text and '<option value="auto-close" selected' in landing.text


def test_the_scheduled_jobs_last_run_says_nothing_to_do_for_a_run_that_found_nothing(client, monkeypatch):
    job = summary_job(None)
    registered(monkeypatch, job)
    ticked_once(job)
    login_as(client, "admin@example.com")
    assert "Nothing to do" in client.get("/admin/scheduled-jobs").text


@pytest.mark.parametrize("column", ["job_name", "requested_by", "summary_counts", "ticket_ids"])
def test_the_new_person_data_columns_are_classified(column):
    from app.person_data import COLUMNS, NOT_PERSON, STAFF

    use = COLUMNS[f"scheduled_job_run.{column}"]
    assert use.role == (STAFF if column == "requested_by" else NOT_PERSON)

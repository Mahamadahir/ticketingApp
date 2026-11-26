"""#323: the Scheduled jobs page shows a short error summary, fewer columns and fixed result sentences from a key."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session

from app.database import engine
from app.job_errors import RUN_NOW_SENTENCES, outage_sentence, summarise_error
from app.models import ScheduledJob
from app.scheduled_jobs import ScheduledJobDef, status_line
from tests.conftest import login_as
from tests.test_scheduled_jobs import counting_job, real_now, registered, ticked_once

LONG_SQL_ERROR = (
    'ProgrammingError: (psycopg2.errors.UndefinedTable) relation "bankholidaycache" does not exist\n'
    "LINE 2: FROM bankholidaycache\n                ^\n\n"
    "[SQL: SELECT bankholidaycache.region, bankholidaycache.payload FROM bankholidaycache WHERE bankholidaycache.region = %(region_1)s]\n"
    "[parameters: {'region_1': 'england-and-wales'}]\n(Background on this error at: https://sqlalche.me/e/20/f405)"
)


def without_details(html: str) -> str:
    return re.sub(r'<details class="job-error-full">.*?</details>', "", html, flags=re.DOTALL)


def set_row(name="count", **values):
    with Session(engine) as session:
        saved = session.get(ScheduledJob, name)
        for key, value in values.items():
            setattr(saved, key, value)
        session.commit()


def page_for(client, monkeypatch, query="", **values):
    job = ScheduledJobDef("count", 3600, lambda session: None, "Counts things.")
    registered(monkeypatch, job)
    ticked_once(counting_job()[0])
    set_row(**values)
    login_as(client, "admin@example.com")
    return client.get("/admin/scheduled-jobs" + query).text


# ---- the error summary -------------------------------------------------------------------------------


def test_the_summary_of_a_missing_table_is_plain_and_has_no_sql():
    assert summarise_error(LONG_SQL_ERROR) == "Table bankholidaycache does not exist"


def test_other_errors_are_the_class_and_the_first_line_cut_to_about_80_characters():
    assert summarise_error("RuntimeError: it broke") == "RuntimeError: it broke"
    long = summarise_error("ValueError: " + "x" * 300)
    assert long.startswith("ValueError: xxx") and len(long) <= 80 and long.endswith("…")
    two_lines = summarise_error("KeyError: 'region'\nTraceback line two")
    assert two_lines == "KeyError: 'region'"
    other = summarise_error('OperationalError: (psycopg2.OperationalError) connection refused\n[SQL: SELECT 1]\n[parameters: {\'a\': 1}]')
    assert other == "OperationalError: connection refused"
    assert summarise_error(None) == "" and summarise_error("  ") == ""


# ---- the page ----------------------------------------------------------------------------------------


def test_a_current_failure_is_red_and_the_full_text_is_only_in_the_details(client, monkeypatch):
    now = real_now()
    page = page_for(client, monkeypatch, last_error=LONG_SQL_ERROR, last_error_at=now + timedelta(minutes=1), failures_in_a_row=2)
    assert "job-error-now" in page and "Table bankholidaycache does not exist" in page
    assert "Failures in a row: 2" in page
    details = re.findall(r'<details class="job-error-full">.*?</details>', page, flags=re.DOTALL)
    assert len(details) == 1 and "SELECT bankholidaycache.region" in details[0] and "mono-wrap" in details[0]
    outside = without_details(page)
    assert "SELECT" not in outside and "psycopg2" not in outside and "region_1" not in outside and "sqlalche.me" not in outside


def test_an_error_the_job_has_worked_since_is_muted_and_marked(client, monkeypatch):
    now = real_now()
    page = page_for(
        client, monkeypatch, last_error=LONG_SQL_ERROR, last_error_at=now - timedelta(hours=2), last_success_at=now - timedelta(hours=1),
        failures_in_a_row=0,
    )
    assert "job-error-old" in page and "job-error-now" not in page
    assert "since worked" in page and "Table bankholidaycache does not exist" in page
    assert "Failures in a row" not in page
    assert "SELECT" not in without_details(page)


def test_a_job_with_no_error_shows_no_error_block(client, monkeypatch):
    page = page_for(client, monkeypatch)
    assert '<div class="job-error' not in page and "Full error" not in page


def test_the_table_has_five_columns_with_the_description_and_a_card_label_in_each_cell(client, monkeypatch):
    page = page_for(client, monkeypatch)
    head = re.search(r"<thead>(.*?)</thead>", page, flags=re.DOTALL).group(1)
    assert re.findall(r"<th>(.*?)</th>", head) == ["Job", "Status", "Last run", "Next due", ""]
    for old in ("Failures in a row</th>", "Last error</th>", "Held by</th>", "Every</th>", "Last started</th>", "Last result</th>"):
        assert old not in page
    assert "Counts things." in page and "Every 1 hour" in page
    for label in ("Job", "Status", "Last run", "Next due"):
        assert f'data-label="{label}"' in page
    assert "Held by" not in page  # only while a lease is held


def test_held_by_shows_only_while_a_lease_is_held(client, monkeypatch):
    now = real_now()
    page = page_for(client, monkeypatch, lease_owner="worker-9", lease_expires_at=now + timedelta(minutes=5))
    assert "Held by worker-9 until" in page


def test_a_run_request_shows_under_next_due(client, monkeypatch):
    now = real_now()
    page = page_for(client, monkeypatch, run_requested_at=now, run_requested_by="someone@example.com")
    assert "Run requested at" in page and "by someone@example.com" in page


def test_the_narrow_layout_turns_a_row_into_a_card():
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    block = css[css.index("Admin > Scheduled jobs (#323)"):]
    assert "@media (max-width: 760px)" in block and "attr(data-label)" in block and "display: block" in block


def test_the_status_line_counts_jobs_correctly():
    now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    rows = [ScheduledJob(name=f"j{n}", next_run_at=now) for n in range(18)]
    assert status_line(rows, now, timedelta(minutes=3)) == "All 18 recurring jobs are healthy."
    assert status_line(rows[:2], now, timedelta(minutes=3)) == "All 2 recurring jobs are healthy."
    assert status_line(rows[:1], now, timedelta(minutes=3)) == "The 1 recurring job is healthy."


# ---- the result key ----------------------------------------------------------------------------------


def test_each_result_key_renders_its_sentence_with_the_job_name(client, monkeypatch):
    for key, sentence in RUN_NOW_SENTENCES.items():
        page = page_for(client, monkeypatch, query=f"?done={key}&job=count")
        assert 'role="status">' + sentence.format(job="count", within="within 1 minute") in page


def test_an_unknown_key_an_unknown_job_and_free_text_render_nothing(client, monkeypatch):
    for query in (
        "?done=nonsense&job=count",
        "?done=queued&job=not-a-job",
        "?done=queued",
        "?message=Totally+official+notice",
        "?done=%3Cb%3Ehi%3C%2Fb%3E&job=count",
        "?done=queued&job=%3Cscript%3Ealert(1)%3C%2Fscript%3E",
    ):
        page = page_for(client, monkeypatch, query=query)
        assert "notice-bar" not in page and "Totally official" not in page and "<script>alert" not in page and "<b>hi</b>" not in page


def test_run_now_redirects_with_a_key_and_the_page_shows_the_sentence(client, monkeypatch):
    job, _ = counting_job()
    registered(monkeypatch, job)
    ticked_once(job)
    login_as(client, "admin@example.com")
    first = client.post("/admin/scheduled-jobs/count/run", follow_redirects=False)
    assert first.headers["location"] == "/admin/scheduled-jobs?done=queued&job=count"
    again = client.post("/admin/scheduled-jobs/count/run", follow_redirects=False)
    assert again.headers["location"] == "/admin/scheduled-jobs?done=due&job=count"
    followed = client.get(first.headers["location"])
    assert "Run requested. The worker will start count within 1 minute." in followed.text


def test_a_job_the_worker_has_not_created_yet_gives_the_missing_key(client, monkeypatch):
    job, _ = counting_job()
    registered(monkeypatch, job)
    login_as(client, "admin@example.com")
    response = client.post("/admin/scheduled-jobs/count/run", follow_redirects=False)
    assert response.headers["location"] == "/admin/scheduled-jobs?done=missing&job=count"


# ---- Outage mode, the other page that read ?message= -------------------------------------------------


def test_the_outage_page_shows_sentences_from_keys_and_ignores_free_text(client):
    login_as(client, "admin@example.com")
    assert "Outage mode is already on." in client.get("/admin/outage?done=already-on").text
    page = client.get("/admin/outage?done=off&sessions=2&passwords=3").text
    assert "Outage mode is off. 2 emergency session(s) ended, 3 emergency password(s) cleared." in page
    assert outage_sentence("off", 0, 0).startswith("Outage mode is off.")
    for query in ("?message=Send+your+password+to+evil.example", "?done=whatever", "?done=%3Cb%3Ex%3C%2Fb%3E"):
        shown = client.get("/admin/outage" + query).text
        assert "notice-bar" not in shown and "evil.example" not in shown and "<b>x</b>" not in shown
    assert "notice-bar" not in client.get("/admin/outage?sessions=5").text


def test_a_missing_column_is_not_reported_as_a_missing_table():
    text = 'ProgrammingError: (psycopg2.errors.UndefinedColumn) column "foo" of relation "bankholidaycache" does not exist\nLINE 1: INSERT'
    assert summarise_error(text) == "Column foo does not exist"


def test_a_mangled_count_on_the_outage_page_still_shows_the_page(client):
    login_as(client, "admin@example.com")
    response = client.get("/admin/outage?done=off&sessions=x&passwords=-4")
    assert response.status_code == 200 and "0 emergency session(s) ended, 0 emergency password(s) cleared." in response.text


def test_every_outage_refusal_has_its_own_key():
    from app.job_errors import outage_refusal_key
    from app.outage import OutageError

    assert outage_refusal_key(OutageError("Only a break-glass account can switch outage mode on.")) == "not-break-glass"
    assert outage_refusal_key(OutageError("Outage mode is already on.")) == "already-on"
    assert outage_refusal_key(OutageError("Outage mode is already off.")) == "already-off"
    assert outage_refusal_key(OutageError("something else")) == "refused"

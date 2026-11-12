"""The System and Logs pages of the admin overhaul (9L, part 2): Scheduled jobs, Outage mode, Data protection (and the
anonymise steps), Audit log, Activity log and Feedback on the admin patterns. One smoke test per page; the rules the pages
show are tested where they live."""

import re
from datetime import datetime, timezone
from pathlib import Path

from app import outage, scheduled_jobs
from sqlmodel import select

from app.models import ActivityLog, AuditLog, PendingBlobDelete, User, UserRole
from tests.conftest import login_as, set_role
from tests.test_feedback_views import desk_with_feedback, sign_in  # noqa: F401  (desk_with_feedback is a fixture)
from tests.test_anonymisation import anonymise, preview
from tests.test_scheduled_jobs import Clock, failing_job, run_due_jobs
from tests.test_subject_access import admin_client, storage, world  # noqa: F401  (storage and world are fixtures)

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
PAGES = (
    "admin_scheduled_jobs.html", "admin_outage.html", "admin_data_protection.html", "admin_anonymise.html",
    "admin_audit_log.html", "admin_activity_log.html", "admin_feedback.html",
)


def admin(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)


def test_the_system_and_log_pages_use_tokens_never_raw_colours():
    for name in PAGES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|style=", text), name


def test_scheduled_jobs_show_the_interval_a_result_chip_and_no_invented_run_now(client, db_session, monkeypatch):
    admin(client, db_session)
    assert "empty-state" in client.get("/admin/scheduled-jobs").text
    monkeypatch.setattr(scheduled_jobs, "SCHEDULED_JOBS", [failing_job()])
    monkeypatch.setattr("app.routers.admin.SCHEDULED_JOBS", scheduled_jobs.SCHEDULED_JOBS)
    run_due_jobs(None, "w1", Clock(datetime.now(timezone.utc)))
    page = client.get("/admin/scheduled-jobs").text
    assert "table-box" in page and 'chip t-red">Failing' in page and "<th>Every</th>" in page and "Next due" in page
    assert "Run now" not in page and "RuntimeError: it broke" in page


def test_outage_mode_is_a_status_card_with_the_existing_rules(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/outage").text
    assert 'chip t-green">Off' in page and "Who is ready" in page and "cannot switch it on" in page
    assert 'action="/admin/outage/on"' not in page  # only a break-glass account is offered the switch


def test_outage_mode_on_shows_who_and_when_and_the_off_action(client, db_session):
    admin(client, db_session)
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    boss.is_break_glass = True
    db_session.commit()
    assert 'action="/admin/outage/on"' in client.get("/admin/outage").text
    outage.turn_on(db_session, boss)
    db_session.commit()
    page = client.get("/admin/outage").text
    assert 'chip t-red">On' in page and "boss@example.com" in page and 'action="/admin/outage/off"' in page


def test_data_protection_is_two_cards_then_the_waiting_deletes_table(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/data-protection").text
    assert page.count("card dp-card") == 2 and "Nothing waiting." in page
    assert 'action="/admin/data-protection/subject-access"' in page and 'action="/admin/data-protection/anonymise/preview"' in page
    db_session.add(PendingBlobDelete(blob_key="a/b.pdf", attempts=4, last_error="storage is down"))
    db_session.commit()
    page = client.get("/admin/data-protection").text
    assert "table-box" in page and "a/b.pdf" in page and "storage is down" in page and page.count("could not be deleted yet") == 1


def test_anonymise_keeps_its_preview_and_typed_confirmation(client, db_session, world, storage):
    admin_client(client, db_session)
    page = preview(client)
    assert page.status_code == 200 and "What will change for ann@a.example" in page.text and "Nothing has been changed yet" in page.text
    assert 'action="/admin/data-protection/anonymise"' in page.text and 'name="confirm"' in page.text and 'name="fingerprint"' in page.text
    assert "table-box" in page.text and "Anonymise permanently" in page.text
    wrong = anonymise(client, confirm="other@example.com")
    assert wrong.status_code == 400 and "does not match" in wrong.text and "Nothing was changed" in wrong.text
    done = anonymise(client)
    assert done.status_code == 200 and "Anonymised requester #" in done.text and "chip t-green" in done.text


def test_the_audit_log_is_a_filter_bar_a_table_and_a_pager(client, db_session):
    admin(client, db_session)
    assert "empty-state" in client.get("/admin/audit-log").text
    db_session.add(AuditLog(actor_email="a@example.com", entity_type="team", entity_id=1, action="created", detail="Network"))
    db_session.commit()
    page = client.get("/admin/audit-log").text
    assert "filter-bar" in page and "table-box" in page and 'chip t-gray">created' in page and "Network" in page
    assert 'name="entity_type"' in page and 'name="actor"' in page and 'name="subject_user_id"' in page


def test_the_activity_log_is_a_filter_bar_a_table_with_result_chips(client, db_session):
    admin(client, db_session)
    db_session.add(ActivityLog(event_type="login_failed", actor_email="x@example.com", method="POST", path="/auth/login", status_code=401))
    db_session.commit()
    page = client.get("/admin/activity-log").text
    assert "filter-bar" in page and "table-box" in page and 'chip t-amber">401' in page
    for field in ("event_type", "actor", "date_from", "date_to", "problems"):
        assert f'name="{field}"' in page
    assert "empty-state" in client.get("/admin/activity-log?date_to=2000-01-01").text


def test_feedback_is_filters_and_a_table_with_rating_chips(client, db_session, desk_with_feedback):
    sign_in(client, "admin@example.com", "Admin Alice")
    page = client.get("/admin/feedback").text
    assert "filter-bar" in page and "table-box" in page and "chip t-" in page and 'name="agent"' in page and 'name="rating"' in page
    ann = login_as(client, "agent@example.com", "Agent Ann")
    sign_in(client, "admin@example.com", "Admin Alice")
    assert "empty-state" in client.get("/admin/feedback", params={"agent": ann, "rating": "2"}).text

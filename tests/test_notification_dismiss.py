"""Dismiss, Undo, Clear all read and the purge job (#239)."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import notification_purge
from app.models import Notification, TeamsPost, Ticket, User, UserRole
from app.notification_purge import purge_notifications
from app.notifications import batch_number, due_for_teams, hand_to_teams
from app.scheduled_jobs import SCHEDULED_JOBS, registry_errors, run_due_jobs
from app.settings_store import (
    GROUP_NOTIFICATIONS,
    NOTIFICATION_DELETE_AFTER_DAYS,
    NOTIFICATION_KEEP_DISMISSED_DAYS,
    REGISTRY,
    get_setting,
    set_setting,
)
from tests.conftest import login_as, set_role
from tests.test_bell_dropdown import HX, agent_with
from tests.test_notifications import notifications_for

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def fresh(db_session, text):
    db_session.expire_all()
    return db_session.exec(select(Notification).where(Notification.text == text)).one()


def dismiss(client, notification, **kwargs):
    return client.post(f"/notifications/{notification.id}/dismiss", follow_redirects=False, **kwargs)


def count_label(client):
    return client.get("/notifications/count").text


def make_read(db_session, *texts):
    for text in texts:
        row = fresh(db_session, text)
        row.read_at = datetime.now(timezone.utc)
        db_session.add(row)
        db_session.commit()


def age_dismissal(db_session, text, days):
    row = fresh(db_session, text)
    row.dismissed_at = datetime.now(timezone.utc) - timedelta(days=days)
    db_session.add(row)
    db_session.commit()
    return row


# ---- Dismiss ----------------------------------------------------------------------------------------------------


def test_dismiss_hides_it_everywhere_and_marks_it_read(client, db_session):
    agent_with(client, db_session, 3)
    note = fresh(db_session, "Note 2")
    assert count_label(client) == "Notifications (3)"
    response = dismiss(client, note)
    assert response.status_code == 303 and response.headers["location"] == f"/notifications?dismissed={note.id}"

    row = fresh(db_session, "Note 2")
    assert row.dismissed_at is not None and row.read_at is not None  # dismissing also marks it read
    assert count_label(client) == "Notifications (2)"
    assert "Note 2" not in client.get("/notifications/latest", headers=HX).text
    assert re.findall(r"<td>(?:<b>)?(Note \d)", client.get("/notifications").text) == ["Note 3", "Note 1"]


def test_a_dismissed_row_is_still_in_the_database_until_the_purge(client, db_session):
    agent_with(client, db_session, 1)
    dismiss(client, fresh(db_session, "Note 1"))
    assert len(notifications_for(db_session, "ann@example.com")) == 1


def test_dismissing_a_read_notification_keeps_when_it_was_read(client, db_session):
    agent_with(client, db_session, 1)
    row = fresh(db_session, "Note 1")
    read_at = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    row.read_at = read_at
    db_session.add(row)
    db_session.commit()
    dismiss(client, row)
    assert fresh(db_session, "Note 1").read_at == read_at


def test_dismissing_twice_keeps_the_first_moment_and_is_not_an_error(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    dismiss(client, note)
    first = fresh(db_session, "Note 1").dismissed_at
    assert dismiss(client, note).status_code == 303  # a double click
    assert fresh(db_session, "Note 1").dismissed_at == first


def test_the_page_offers_undo_after_a_dismiss_without_script(client, db_session):
    agent_with(client, db_session, 2)
    note = fresh(db_session, "Note 2")
    assert "Dismissed:" not in client.get(f"/notifications?dismissed={note.id}").text  # nothing dismissed yet: no offer
    dismiss(client, note)
    page = client.get(f"/notifications?dismissed={note.id}").text
    assert f'action="/notifications/{note.id}/undo"' in page and "Dismissed: Note 2" in page


def test_each_row_has_a_dismiss_button_with_a_name_in_the_dropdown_and_on_the_page(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    for html in (client.get("/notifications/latest", headers=HX).text, client.get("/notifications").text):
        assert f'<form method="post" action="/notifications/{note.id}/dismiss"' in html
        assert 'aria-label="Dismiss: Note 1"' in html


def test_with_htmx_a_dismiss_swaps_the_row_for_an_undo_and_asks_the_bell_to_recount(client, db_session):
    agent_with(client, db_session, 2)
    note = fresh(db_session, "Note 2")
    response = client.post(f"/notifications/{note.id}/dismiss", data={"from": "bell"}, headers=HX)
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<li") and f"/notifications/{note.id}/undo" in response.text
    assert response.headers["HX-Trigger"] == "bell-count"
    other = fresh(db_session, "Note 1")
    page = client.post(f"/notifications/{other.id}/dismiss", data={"from": "page"}, headers=HX)
    assert page.text.lstrip().startswith("<tr")  # on the page the row is a table row


def test_the_bell_recounts_when_something_is_dismissed(client, db_session):
    agent_with(client, db_session, 1)
    assert 'hx-trigger="load, every 30s, bell-count from:body"' in client.get("/notifications").text


# ---- Undo -------------------------------------------------------------------------------------------------------


def test_undo_restores_it_and_it_stays_read(client, db_session):
    agent_with(client, db_session, 2)
    note = fresh(db_session, "Note 2")
    dismiss(client, note)
    assert count_label(client) == "Notifications (1)"
    response = client.post(f"/notifications/{note.id}/undo", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/notifications?restored=1"
    row = fresh(db_session, "Note 2")
    assert row.dismissed_at is None and row.read_at is not None
    assert count_label(client) == "Notifications (1)"  # it was marked read by the dismiss and stays read
    assert "Note 2" in client.get("/notifications/latest", headers=HX).text
    assert "1 notification restored" in client.get("/notifications?restored=1").text


def test_with_htmx_undo_puts_the_row_back(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    dismiss(client, note)
    response = client.post(f"/notifications/{note.id}/undo", data={"from": "bell"}, headers=HX)
    assert response.text.lstrip().startswith("<li") and "Note 1" in response.text and f"/notifications/{note.id}/open" in response.text
    assert response.headers["HX-Trigger"] == "bell-count"


def test_undo_after_the_keep_dismissed_window_restores_nothing(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    dismiss(client, note)
    age_dismissal(db_session, "Note 1", 1.01)  # past the one day, and the purge has not run yet
    response = client.post(f"/notifications/{note.id}/undo", follow_redirects=False)
    assert response.headers["location"] == "/notifications?restored=0"
    assert fresh(db_session, "Note 1").dismissed_at is not None
    assert "Nothing to restore" in client.get("/notifications?restored=0").text
    htmx = client.post(f"/notifications/{note.id}/undo", data={"from": "bell"}, headers=HX)
    assert "can no longer be restored" in htmx.text


def test_undo_after_the_purge_has_nothing_to_restore(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    note_id = note.id
    dismiss(client, note)
    age_dismissal(db_session, "Note 1", 2)
    assert purge_notifications(db_session) == 1
    db_session.commit()
    assert client.post(f"/notifications/{note_id}/undo", follow_redirects=False).status_code == 404
    assert notifications_for(db_session, "ann@example.com") == []


def test_the_undo_window_follows_the_setting(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    dismiss(client, note)
    age_dismissal(db_session, "Note 1", 2)
    set_setting(db_session, NOTIFICATION_KEEP_DISMISSED_DAYS, "3")
    db_session.commit()
    client.post(f"/notifications/{note.id}/undo")
    assert fresh(db_session, "Note 1").dismissed_at is None


# ---- Clear all read ---------------------------------------------------------------------------------------------


def test_clear_all_read_dismisses_only_read_ones_and_says_how_many(client, db_session):
    agent_with(client, db_session, 4)
    make_read(db_session, "Note 1", "Note 2")
    assert "Clear all read (2)" in client.get("/notifications").text
    response = client.post("/notifications/clear-read", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/notifications?cleared=")
    assert [fresh(db_session, f"Note {n}").dismissed_at is not None for n in (1, 2, 3, 4)] == [True, True, False, False]
    assert count_label(client) == "Notifications (2)"
    banner = client.get(response.headers["location"]).text
    assert "2 read notifications cleared" in banner and 'action="/notifications/undo-clear"' in banner
    assert "Clear all read" not in banner  # nothing read is left to clear


def test_clear_all_read_is_not_offered_with_nothing_read(client, db_session):
    agent_with(client, db_session, 2)
    assert "Clear all read" not in client.get("/notifications").text
    response = client.post("/notifications/clear-read", follow_redirects=False)
    assert response.headers["location"] == "/notifications?cleared=0"
    assert "no read notifications to clear" in client.get("/notifications?cleared=0").text


def test_undo_of_a_clear_restores_that_batch_only(client, db_session):
    agent_with(client, db_session, 4)
    make_read(db_session, "Note 1", "Note 2", "Note 3")
    dismiss(client, fresh(db_session, "Note 1"))  # dismissed by itself before the batch: not part of it
    location = client.post("/notifications/clear-read", follow_redirects=False).headers["location"]
    batch = int(location.split("=")[1])
    assert fresh(db_session, "Note 2").dismissed_at is not None and fresh(db_session, "Note 3").dismissed_at is not None
    make_read(db_session, "Note 4")
    dismiss(client, fresh(db_session, "Note 4"))  # and one dismissed after it

    response = client.post("/notifications/undo-clear", data={"batch": batch}, follow_redirects=False)
    assert response.headers["location"] == "/notifications?restored=2"
    assert [fresh(db_session, f"Note {n}").dismissed_at is None for n in (1, 2, 3, 4)] == [False, True, True, False]
    again = client.post("/notifications/undo-clear", data={"batch": batch}, follow_redirects=False)
    assert again.headers["location"] == "/notifications?restored=0"  # twice restores nothing more


def test_undo_of_a_clear_after_the_window_restores_nothing(client, db_session):
    agent_with(client, db_session, 1)
    make_read(db_session, "Note 1")
    client.post("/notifications/clear-read")
    old = age_dismissal(db_session, "Note 1", 2)
    client.post("/notifications/undo-clear", data={"batch": batch_number(old.dismissed_at)})
    assert fresh(db_session, "Note 1").dismissed_at is not None


def test_a_batch_of_someone_else_cannot_be_undone(client, db_session):
    agent_with(client, db_session, 1)
    make_read(db_session, "Note 1")
    client.post("/notifications/clear-read")
    batch = batch_number(fresh(db_session, "Note 1").dismissed_at)
    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    response = client.post("/notifications/undo-clear", data={"batch": batch}, follow_redirects=False)
    assert response.headers["location"] == "/notifications?restored=0"
    assert fresh(db_session, "Note 1").dismissed_at is not None


# ---- Who may ----------------------------------------------------------------------------------------------------


def test_the_routes_are_post_only(client, db_session):
    agent_with(client, db_session, 1)
    note = fresh(db_session, "Note 1")
    for path in (f"/notifications/{note.id}/dismiss", f"/notifications/{note.id}/undo", "/notifications/clear-read", "/notifications/undo-clear"):
        assert client.get(path).status_code in (404, 405), path
    assert client.get(f"/notifications/{note.id}/dismiss").status_code == 405
    assert fresh(db_session, "Note 1").dismissed_at is None


def test_the_routes_need_a_signed_in_person(client, db_session):
    for path in ("/notifications/1/dismiss", "/notifications/1/undo", "/notifications/clear-read", "/notifications/undo-clear"):
        response = client.post(path, data={"batch": 1}, follow_redirects=False)
        assert response.status_code in (303, 307, 401, 403), path


def test_a_person_cannot_dismiss_or_undo_someone_elses_notification(client, db_session):
    agent_with(client, db_session, 1)
    mine = fresh(db_session, "Note 1")
    client.post("/auth/logout")
    bob_id = login_as(client, "bob@example.com")
    set_role(db_session, bob_id, UserRole.AGENT)
    assert dismiss(client, mine).status_code == 404
    assert client.post(f"/notifications/{mine.id}/undo").status_code == 404
    assert fresh(db_session, "Note 1").dismissed_at is None
    assert client.post("/notifications/clear-read", follow_redirects=False).status_code == 303
    assert fresh(db_session, "Note 1").dismissed_at is None  # and Clear all read is only their own


def test_a_teams_only_notification_cannot_be_dismissed(client, db_session):
    agent_id = agent_with(client, db_session, 1)
    db_session.add(Notification(user_id=agent_id, kind="assigned", text="Teams only", bell=False))
    db_session.commit()
    assert dismiss(client, fresh(db_session, "Teams only")).status_code == 404


def test_nothing_that_does_not_exist_is_dismissed(client, db_session):
    agent_with(client, db_session, 1)
    assert client.post("/notifications/999999/dismiss").status_code == 404


def test_a_requester_can_dismiss_their_own_portal_notification(client, db_session):
    user_id = login_as(client, "req@example.com", "Req")
    ticket = Ticket(subject="Mine", requester_email="req@example.com", description="d")
    db_session.add(ticket)
    db_session.commit()
    db_session.add(Notification(user_id=user_id, kind="awaiting_you", ticket_id=ticket.id, text="x"))
    db_session.commit()
    assert count_label(client) == "Notifications (1)"
    note = db_session.exec(select(Notification)).one()
    assert dismiss(client, note).status_code == 303
    assert count_label(client) == "Notifications"


# ---- The Teams copy ---------------------------------------------------------------------------------------------


@pytest.fixture
def waiting_copy(client, db_session, no_hold_while_active):
    agent_with(client, db_session, 1)
    row = fresh(db_session, "Note 1")
    row.teams_wanted = True
    row.created_at = NOW - timedelta(hours=1)
    db_session.add(row)
    db_session.commit()
    return row


def test_a_dismissed_notification_never_gets_a_teams_copy(client, db_session, waiting_copy):
    assert [n.id for n in due_for_teams(db_session, NOW)] == [waiting_copy.id]  # due until dismissed: the control
    db_session.rollback()
    dismiss(client, waiting_copy)
    db_session.expire_all()
    assert due_for_teams(db_session, NOW) == []
    sent = []
    assert hand_to_teams(db_session, lambda s, n: sent.append(n.id), NOW) == (0, 0) and sent == []


def test_a_copy_already_queued_is_not_sent_once_dismissed(client, db_session, waiting_copy, monkeypatch):
    from app import teams

    post = teams.queue_notification(db_session, waiting_copy)
    db_session.commit()
    monkeypatch.setattr(teams, "notifications_on", lambda: True)
    dismiss(client, waiting_copy)
    db_session.expire_all()
    with pytest.raises(teams.Unsendable, match="dismissed"):
        teams.notification_post(db_session, db_session.get(TeamsPost, post.id))


# ---- The purge --------------------------------------------------------------------------------------------------


@pytest.fixture
def person(db_session):
    user = User(email="p@example.com", display_name="P")
    db_session.add(user)
    db_session.commit()
    return user


def row(db_session, person, text, *, created_days=0.0, dismissed_days=None):
    n = Notification(
        user_id=person.id, kind="assigned", text=text, created_at=NOW - timedelta(days=created_days),
        dismissed_at=None if dismissed_days is None else NOW - timedelta(days=dismissed_days),
    )
    db_session.add(n)
    db_session.commit()
    return n.id


def remaining(db_session):
    db_session.expire_all()
    return sorted(n.text for n in db_session.exec(select(Notification)).all())


def test_the_purge_deletes_dismissed_rows_past_the_window_and_rows_past_the_age_limit_and_keeps_the_rest(db_session, person):
    row(db_session, person, "dismissed 2 days ago", dismissed_days=2)
    row(db_session, person, "dismissed 12 hours ago", dismissed_days=0.5)
    row(db_session, person, "dismissed just past a day", dismissed_days=1.01)
    row(db_session, person, "181 days old", created_days=181)
    row(db_session, person, "179 days old", created_days=179)
    row(db_session, person, "old and dismissed a minute ago", created_days=181, dismissed_days=0.0007)
    row(db_session, person, "new and unread")
    assert purge_notifications(db_session, NOW) == 4
    assert remaining(db_session) == ["179 days old", "dismissed 12 hours ago", "new and unread"]


def test_the_purge_is_the_same_run_late_or_twice(db_session, person):
    row(db_session, person, "gone", dismissed_days=2)
    row(db_session, person, "kept", created_days=10)
    assert purge_notifications(db_session, NOW + timedelta(days=3)) == 1  # a run three days late: what is due is what the data says
    assert purge_notifications(db_session, NOW + timedelta(days=3)) == 0  # twice
    assert remaining(db_session) == ["kept"]
    assert purge_notifications(db_session, NOW + timedelta(days=171)) == 1  # and later still, the age limit catches the rest
    assert remaining(db_session) == []


def test_the_purge_deletes_the_teams_post_first_sent_or_not(db_session, person):
    gone_a = row(db_session, person, "gone a", dismissed_days=2)
    gone_b = row(db_session, person, "gone b", dismissed_days=2)
    kept = row(db_session, person, "kept")
    db_session.add(TeamsPost(kind="notification", notification_id=gone_a, sent_at=NOW))
    db_session.add(TeamsPost(kind="notification", notification_id=gone_b))  # never sent
    db_session.add(TeamsPost(kind="notification", notification_id=kept))
    db_session.add(TeamsPost(kind="new_ticket"))  # not about a notification
    db_session.commit()
    assert purge_notifications(db_session, NOW) == 2
    db_session.commit()
    db_session.expire_all()
    assert sorted(p.notification_id or 0 for p in db_session.exec(select(TeamsPost)).all()) == [0, kept]


def test_the_purge_works_in_batches(db_session, person, monkeypatch):
    monkeypatch.setattr(notification_purge, "BATCH", 2)
    for n in range(5):
        row(db_session, person, f"gone {n}", dismissed_days=3)
    row(db_session, person, "kept")
    assert purge_notifications(db_session, NOW) == 5
    assert remaining(db_session) == ["kept"]


def test_the_purge_follows_the_two_settings(db_session, person):
    set_setting(db_session, NOTIFICATION_KEEP_DISMISSED_DAYS, "3")
    set_setting(db_session, NOTIFICATION_DELETE_AFTER_DAYS, "30")
    db_session.commit()
    row(db_session, person, "dismissed 2 days ago", dismissed_days=2)
    row(db_session, person, "dismissed 4 days ago", dismissed_days=4)
    row(db_session, person, "40 days old", created_days=40)
    row(db_session, person, "20 days old", created_days=20)
    assert purge_notifications(db_session, NOW) == 2
    assert remaining(db_session) == ["20 days old", "dismissed 2 days ago"]


def test_a_stored_pair_the_page_would_refuse_cannot_delete_more_than_the_dismissed_window(db_session, person):
    set_setting(db_session, NOTIFICATION_KEEP_DISMISSED_DAYS, "30")
    set_setting(db_session, NOTIFICATION_DELETE_AFTER_DAYS, "7")  # written around the form
    db_session.commit()
    row(db_session, person, "20 days old", created_days=20)
    assert purge_notifications(db_session, NOW) == 0  # the age limit reads as 30, not 7


def test_a_bad_setting_falls_back_to_its_default(db_session, person):
    set_setting(db_session, NOTIFICATION_DELETE_AFTER_DAYS, "soon")
    db_session.commit()
    row(db_session, person, "181 days old", created_days=181)
    row(db_session, person, "100 days old", created_days=100)
    assert purge_notifications(db_session, NOW) == 1


def test_the_purge_is_one_daily_line_in_the_registry_and_runs_through_the_runner(db_session, person):
    job = next(j for j in SCHEDULED_JOBS if j.name == "notification-purge")
    assert job.every_seconds == 86400 and job.run is purge_notifications and registry_errors(SCHEDULED_JOBS) == []
    db_session.add(Notification(user_id=person.id, kind="assigned", text="ancient", created_at=datetime.now(timezone.utc) - timedelta(days=400)))
    db_session.add(Notification(user_id=person.id, kind="assigned", text="fresh"))
    db_session.commit()
    assert run_due_jobs([job], "w1") == ["notification-purge"]
    assert remaining(db_session) == ["fresh"]


# ---- The settings -----------------------------------------------------------------------------------------------


def post_group(client, **values):
    data = {"group": GROUP_NOTIFICATIONS, **{f"v_{k}": v for k, v in values.items()}}
    return client.post("/admin/settings/group", data=data, follow_redirects=False)


def test_the_two_settings_are_in_the_notifications_group_with_their_defaults():
    keep, delete = REGISTRY[NOTIFICATION_KEEP_DISMISSED_DAYS], REGISTRY[NOTIFICATION_DELETE_AFTER_DAYS]
    assert (keep.group, keep.default, keep.unit, keep.display_label) == (GROUP_NOTIFICATIONS, "1", "days", "Keep dismissed notifications for")
    assert (delete.group, delete.default, delete.unit, delete.display_label) == (GROUP_NOTIFICATIONS, "180", "days", "Delete notifications after")
    assert keep.description and delete.description


def test_the_settings_page_shows_them(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    page = client.get(f"/admin/settings?group={GROUP_NOTIFICATIONS}").text
    assert "Keep dismissed notifications for" in page and "Delete notifications after" in page


def test_the_settings_are_validated(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    assert post_group(client, **{NOTIFICATION_KEEP_DISMISSED_DAYS: "0"}).status_code == 400  # at least 1 day
    assert post_group(client, **{NOTIFICATION_KEEP_DISMISSED_DAYS: "soon"}).status_code == 400
    assert post_group(client, **{NOTIFICATION_DELETE_AFTER_DAYS: "6"}).status_code == 400  # at least a week
    assert post_group(client, **{NOTIFICATION_DELETE_AFTER_DAYS: "1001"}).status_code == 400
    assert post_group(client, **{NOTIFICATION_KEEP_DISMISSED_DAYS: "200"}).status_code == 400  # longer than the stored 180
    assert post_group(client, **{NOTIFICATION_DELETE_AFTER_DAYS: "10", NOTIFICATION_KEEP_DISMISSED_DAYS: "11"}).status_code == 400  # together
    assert get_setting(db_session, NOTIFICATION_KEEP_DISMISSED_DAYS) == "1" and get_setting(db_session, NOTIFICATION_DELETE_AFTER_DAYS) == "180"
    assert post_group(client, **{NOTIFICATION_DELETE_AFTER_DAYS: "10", NOTIFICATION_KEEP_DISMISSED_DAYS: "10"}).status_code == 303
    db_session.expire_all()
    assert get_setting(db_session, NOTIFICATION_KEEP_DISMISSED_DAYS) == "10" and get_setting(db_session, NOTIFICATION_DELETE_AFTER_DAYS) == "10"
    assert post_group(client, **{NOTIFICATION_DELETE_AFTER_DAYS: "9"}).status_code == 400  # now shorter than the stored 10-day window


# ---- Review follow-ups ------------------------------------------------------------------------------------------


def test_a_post_being_sent_is_never_waited_for_and_its_notification_is_left_for_the_next_run(db_session, person):
    from sqlmodel import Session

    from app.database import engine

    busy = row(db_session, person, "busy", dismissed_days=2)
    free = row(db_session, person, "free", dismissed_days=2)
    db_session.add(TeamsPost(kind="notification", notification_id=busy))
    db_session.commit()
    with Session(engine) as sender:  # the sender holds its post locked while it talks to Teams
        sender.exec(select(TeamsPost).where(TeamsPost.notification_id == busy).with_for_update()).all()
        assert purge_notifications(db_session, NOW) == 1  # returns at once; no lock wait
        db_session.commit()
    assert remaining(db_session) == ["busy"] and free
    assert purge_notifications(db_session, NOW) == 1  # the next run, the sender done
    db_session.commit()
    assert remaining(db_session) == []


def test_made_up_numbers_in_the_banner_and_undo_addresses_do_not_crash(client, db_session):
    agent_with(client, db_session, 1)
    assert client.get("/notifications?cleared=99999999999999999999").status_code == 422
    assert client.get("/notifications?cleared=4611686018427387904").status_code == 200  # in range but no such batch: shows nothing
    assert client.post("/notifications/undo-clear", data={"batch": 99999999999999999999}).status_code == 422
    assert client.post("/notifications/99999999999999999999/dismiss").status_code == 422
    assert client.get("/notifications?dismissed=99999999999999999999").status_code == 422


def test_the_banner_is_not_carried_to_the_next_page(client, db_session):
    agent_with(client, db_session, 60)
    make_read(db_session, "Note 1")
    batch = int(client.post("/notifications/clear-read", follow_redirects=False).headers["location"].split("=")[1])
    page = client.get(f"/notifications?cleared={batch}").text
    assert "cleared." in page
    links = re.findall(r'href="([^"]*page=[^"]*)"', page)
    assert links and not any("cleared" in link for link in links)

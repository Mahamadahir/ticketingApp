"""Per-person time zones: everything is stored in UTC, each person is shown their own zone. Graph is
simulated: the real mailboxSettings read (MailboxSettings.Read) is an Azure-only check."""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app.config import settings
from app.graph_client import GraphClient
from app.jobs import JOBS
from app.models import RequesterTimeZone, Setting, User, UserRole
from app.settings_store import (
    DEFAULT_TIME_ZONE,
    REGISTRY,
    TIME_ZONE_REFRESH_DAYS,
    TIME_ZONE_RETRY_HOURS,
    set_setting,
)
from app.time_zones import (
    clear_manual_zone,
    default_zone,
    end_of_day_for_email,
    end_of_day_for_user,
    end_of_day_utc,
    format_date,
    format_moment,
    note_requester,
    RefreshResult,
    refresh_requester_zones,
    set_browser_zone,
    set_manual_zone,
    valid_zone,
    windows_to_iana,
    zone_for_email,
    zone_for_user,
)
from app.windows_zones import WINDOWS_TO_IANA
from tests.conftest import create_team
from tests.test_presence import graph_with
from tests.test_triage import agent, new_ticket

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
REPORTED = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)  # 17:00 in London (BST ended that morning: GMT)


def a_user(db_session, email="jo@example.com", zone=None, source=None):
    user = User(email=email, display_name="Jo", role=UserRole.END_USER, time_zone=zone, time_zone_source=source)
    db_session.add(user)
    db_session.commit()
    return user


# ---- Names -------------------------------------------------------------------------------------------


def test_only_real_iana_names_are_valid():
    assert valid_zone("Europe/London") == "Europe/London"
    assert valid_zone("  Asia/Tokyo ") == "Asia/Tokyo"
    for bad in ("", None, "GMT Standard Time", "Mars/Olympus", "../etc/passwd", "/etc/localtime", "Europe"):
        assert valid_zone(bad) is None


def test_windows_names_from_graph_become_iana_names():
    assert windows_to_iana("GMT Standard Time") == "Europe/London"
    assert windows_to_iana("Pacific Standard Time") == "America/Los_Angeles"
    assert windows_to_iana("India Standard Time") == "Asia/Kolkata"
    assert windows_to_iana("Europe/Paris") == "Europe/Paris"  # already IANA
    assert windows_to_iana("UTC") == "Etc/UTC"
    assert windows_to_iana("Customized Time Zone") is None
    assert windows_to_iana("") is None and windows_to_iana(None) is None


def test_every_windows_zone_in_the_table_maps_to_a_zone_this_machine_knows():
    assert len(WINDOWS_TO_IANA) > 100
    assert {name for name, iana in WINDOWS_TO_IANA.items() if valid_zone(iana) is None} == set()


# ---- Display -----------------------------------------------------------------------------------------


def test_a_moment_is_shown_in_the_zone_with_the_zone_label():
    assert format_moment(REPORTED, "Europe/London", now=NOW) == "25 Oct 16:00 GMT"
    assert format_moment(REPORTED - timedelta(days=1), "Europe/London", now=NOW) == "24 Oct 17:00 BST"
    assert format_moment(REPORTED, "Asia/Tokyo", now=NOW) == "26 Oct 01:00 JST"
    assert format_moment(REPORTED, "America/New_York", now=NOW) == "25 Oct 12:00 EDT"  # DST ended 1 Nov in the US
    assert format_moment(REPORTED, "UTC", now=NOW) == "25 Oct 16:00 UTC"


def test_seconds_and_a_year_only_when_asked_for_or_not_this_year():
    assert format_moment(REPORTED.replace(second=7), "UTC", seconds=True, now=NOW) == "25 Oct 16:00:07 UTC"
    assert format_moment(REPORTED, "UTC", now=datetime(2027, 1, 5, tzinfo=timezone.utc)) == "25 Oct 2026 16:00 UTC"


def test_a_naive_moment_is_read_as_utc_and_an_unknown_zone_falls_back_to_utc():
    assert format_moment(REPORTED.replace(tzinfo=None), "Asia/Tokyo", now=NOW) == "26 Oct 01:00 JST"
    assert format_moment(REPORTED, "Mars/Olympus", now=NOW) == "25 Oct 16:00 UTC"


def test_a_date_only_shows_the_date_in_that_zone():
    late = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)
    assert format_date(late, "UTC", now=NOW) == "25 Oct"
    assert format_date(late, "Asia/Tokyo", now=NOW) == "26 Oct"  # already tomorrow there


# ---- End of day, as UTC (what the Due date feature builds on) ------------------------------------------------


def test_end_of_day_in_a_zone_as_utc():
    assert end_of_day_utc(date(2026, 7, 1), "Europe/London") == datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc)  # BST
    assert end_of_day_utc(date(2026, 12, 1), "Europe/London") == datetime(2026, 12, 2, 0, 0, tzinfo=timezone.utc)
    assert end_of_day_utc(date(2026, 12, 1), "Asia/Tokyo") == datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
    assert end_of_day_utc(date(2026, 12, 1), "America/Los_Angeles") == datetime(2026, 12, 2, 8, 0, tzinfo=timezone.utc)
    assert end_of_day_utc(date(2026, 12, 1), "Mars/Olympus") == datetime(2026, 12, 2, 0, 0, tzinfo=timezone.utc)


def test_end_of_day_is_right_across_a_clock_change():
    # London clocks went forward on 29 March 2026: that day is 23 hours long.
    start = end_of_day_utc(date(2026, 3, 28), "Europe/London")
    end = end_of_day_utc(date(2026, 3, 29), "Europe/London")
    assert end - start == timedelta(hours=23)
    assert end == datetime(2026, 3, 29, 23, 0, tzinfo=timezone.utc)


# ---- Whose zone ----------------------------------------------------------------------------------------


def test_with_nothing_known_the_default_setting_applies_and_a_bad_setting_falls_back(db_session):
    assert default_zone(db_session) == "Europe/London"
    set_setting(db_session, DEFAULT_TIME_ZONE, "Asia/Tokyo")
    assert default_zone(db_session) == "Asia/Tokyo"
    set_setting(db_session, DEFAULT_TIME_ZONE, "Nowhere/Land")
    assert default_zone(db_session) == "Europe/London"
    assert zone_for_email(db_session, "stranger@example.com") == "Europe/London"


def test_an_accounts_zone_beats_the_mailbox_which_beats_the_default(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "Asia/Tokyo")
    db_session.add(RequesterTimeZone(email="jo@example.com", time_zone="Europe/Paris", source="mailbox", next_check_at=NOW))
    db_session.commit()
    jo = a_user(db_session)
    assert zone_for_email(db_session, "Jo@Example.com") == "Europe/Paris"
    assert zone_for_user(db_session, jo) == "Europe/Paris"
    jo.time_zone, jo.time_zone_source = "America/Chicago", "browser"
    db_session.add(jo)
    db_session.commit()
    assert zone_for_email(db_session, "jo@example.com") == "America/Chicago"
    assert zone_for_user(db_session, jo) == "America/Chicago"
    assert zone_for_email(db_session, "nobody@example.com") == "Asia/Tokyo"


def test_a_stored_zone_that_is_no_longer_valid_is_skipped(db_session):
    jo = a_user(db_session, zone="Old/Name", source="browser")
    assert zone_for_user(db_session, jo) == "Europe/London"


def test_end_of_day_for_a_requester_uses_their_own_zone(db_session):
    db_session.add(RequesterTimeZone(email="sam@example.com", time_zone="Asia/Tokyo", source="mailbox", next_check_at=NOW))
    db_session.commit()
    day = date(2026, 12, 1)
    assert end_of_day_for_email(db_session, day, "sam@example.com") == datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
    assert end_of_day_for_email(db_session, day, "other@example.com") == datetime(2026, 12, 2, 0, 0, tzinfo=timezone.utc)
    jo = a_user(db_session, zone="America/Los_Angeles", source="manual")
    assert end_of_day_for_user(db_session, day, jo) == datetime(2026, 12, 2, 8, 0, tzinfo=timezone.utc)


# ---- The person's own zone -----------------------------------------------------------------------------


def test_the_browser_sets_and_updates_the_zone_and_a_manual_choice_wins_until_cleared(db_session):
    jo = a_user(db_session)
    assert set_browser_zone(jo, "Europe/Paris") is True
    assert (jo.time_zone, jo.time_zone_source) == ("Europe/Paris", "browser")
    assert set_browser_zone(jo, "Europe/Paris") is False  # unchanged
    assert set_browser_zone(jo, "Asia/Tokyo") is True  # they travelled
    assert set_browser_zone(jo, "Nowhere/Land") is False and jo.time_zone == "Asia/Tokyo"

    assert set_manual_zone(jo, "America/Chicago") is True
    assert set_browser_zone(jo, "Asia/Tokyo") is False  # the manual choice stays
    assert (jo.time_zone, jo.time_zone_source) == ("America/Chicago", "manual")
    assert set_manual_zone(jo, "Nowhere/Land") is False and jo.time_zone == "America/Chicago"

    clear_manual_zone(jo)
    assert (jo.time_zone, jo.time_zone_source) == (None, None)
    assert set_browser_zone(jo, "Asia/Tokyo") is True


def test_clearing_does_nothing_to_a_browser_zone(db_session):
    jo = a_user(db_session, zone="Asia/Tokyo", source="browser")
    clear_manual_zone(jo)
    assert jo.time_zone == "Asia/Tokyo"


def test_the_browser_report_route_saves_the_zone(client, db_session):
    agent(client, db_session)
    response = client.post("/settings/time-zone/browser", data={"time_zone": "Asia/Tokyo"})
    assert response.json() == {"time_zone": "Asia/Tokyo", "source": "browser", "changed": True}
    assert client.post("/settings/time-zone/browser", data={"time_zone": "Asia/Tokyo"}).json()["changed"] is False
    bad = client.post("/settings/time-zone/browser", data={"time_zone": "Nowhere/Land"}).json()
    assert bad["changed"] is False and bad["time_zone"] == "Asia/Tokyo"
    db_session.expire_all()
    assert db_session.exec(select(User).where(User.email == "agent@example.com")).one().time_zone == "Asia/Tokyo"


def test_the_browser_report_needs_a_signed_in_user(client):
    response = client.post("/settings/time-zone/browser", data={"time_zone": "Asia/Tokyo"}, follow_redirects=False)
    assert response.status_code == 307 and "/auth/" in response.headers["location"]


def test_a_manual_choice_on_the_settings_page_wins_over_the_browser_until_cleared(client, db_session):
    agent(client, db_session)
    assert client.post("/settings/time-zone", data={"time_zone": "America/Chicago"}, follow_redirects=False).status_code == 303
    assert client.post("/settings/time-zone/browser", data={"time_zone": "Asia/Tokyo"}).json() == {
        "time_zone": "America/Chicago", "source": "manual", "changed": False,
    }
    page = client.get("/settings/notifications").text
    assert "America/Chicago" in page and "which you chose" in page and "Go back to my browser" in page

    assert client.post("/settings/time-zone", data={"clear": "1"}, follow_redirects=False).status_code == 303
    assert client.post("/settings/time-zone/browser", data={"time_zone": "Asia/Tokyo"}).json()["changed"] is True


def test_a_made_up_zone_on_the_settings_page_is_refused(client, db_session):
    agent(client, db_session)
    assert client.post("/settings/time-zone", data={"time_zone": "Nowhere/Land"}).status_code == 400
    assert db_session.exec(select(User).where(User.email == "agent@example.com")).one().time_zone is None


# ---- Pages show the viewer's zone ----------------------------------------------------------------------


def ticket_in_a_team(client, db_session, zone=None, source="browser"):
    team = create_team(db_session, "Support")
    agent_id = agent(client, db_session, member_of=[team])
    ticket = new_ticket(db_session, team=team)
    ticket.reported_at = REPORTED
    ticket.created_at = REPORTED
    db_session.add(ticket)
    if zone:
        user = db_session.get(User, agent_id)
        user.time_zone, user.time_zone_source = zone, source
        db_session.add(user)
    db_session.commit()
    return ticket


def test_the_ticket_page_shows_times_in_the_viewers_zone_with_the_label(client, db_session):
    ticket = ticket_in_a_team(client, db_session, zone="Asia/Tokyo")
    page = client.get(f"/tickets/{ticket.number}").text
    assert "<dt>Reported</dt><dd>26 Oct" in page and "01:00 JST" in page
    assert "UTC" not in page.split("<dt>Reported</dt>")[1].split("</dd>")[0]


def test_someone_with_no_zone_yet_sees_the_default(client, db_session):
    ticket = ticket_in_a_team(client, db_session)
    assert "<dt>Reported</dt><dd>25 Oct" in client.get(f"/tickets/{ticket.number}").text
    assert "16:00 GMT" in client.get(f"/tickets/{ticket.number}").text
    set_setting(db_session, DEFAULT_TIME_ZONE, "Asia/Tokyo")
    db_session.commit()
    assert "01:00 JST" in client.get(f"/tickets/{ticket.number}").text


def test_two_people_see_the_same_moment_in_their_own_zones(client, db_session):
    ticket = ticket_in_a_team(client, db_session, zone="America/New_York")
    assert "12:00 EDT" in client.get(f"/tickets/{ticket.number}").text
    other = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    other.time_zone = "Asia/Tokyo"
    db_session.add(other)
    db_session.commit()
    assert "01:00 JST" in client.get(f"/tickets/{ticket.number}").text


def test_the_page_tells_the_browser_to_report_its_zone_unless_it_was_chosen_by_hand(client, db_session):
    ticket_in_a_team(client, db_session, zone="Asia/Tokyo", source="manual")
    page = client.get("/settings/notifications").text
    assert "/settings/time-zone/browser" in page and 'var source = "manual"' in page


# ---- Requesters' mailbox zones -------------------------------------------------------------------------


def test_a_new_requester_is_noted_once_for_the_worker(db_session):
    note_requester(db_session, "Sam@Example.com", now=NOW)
    note_requester(db_session, "sam@example.com", now=NOW + timedelta(hours=1))
    db_session.commit()
    [row] = db_session.exec(select(RequesterTimeZone)).all()
    assert row.email == "sam@example.com" and row.time_zone is None and row.checked_at is None
    assert row.next_check_at == NOW  # the second call changed nothing


def test_creating_a_ticket_notes_its_requester(db_session):
    new_ticket(db_session, requester="Pat@Example.com")
    assert [r.email for r in db_session.exec(select(RequesterTimeZone)).all()] == ["pat@example.com"]


class FakeMailboxes:
    def __init__(self, zones):
        self.zones = zones  # email -> Graph's name, None (unknown), or an Exception
        self.asked = []

    def __call__(self, email):
        self.asked.append(email)
        answer = self.zones.get(email)
        if isinstance(answer, Exception):
            raise answer
        return answer


def refresh(db_session, mailboxes, now=NOW):
    result = refresh_requester_zones(db_session, mailboxes, now=now)
    db_session.commit()
    db_session.expire_all()
    return result.looked_at


def row_of(db_session, email):
    return db_session.get(RequesterTimeZone, email)


def test_a_windows_zone_from_graph_is_stored_as_iana_and_rechecked_after_the_refresh_interval(db_session):
    note_requester(db_session, "sam@example.com", now=NOW)
    db_session.commit()
    mailboxes = FakeMailboxes({"sam@example.com": "Tokyo Standard Time"})
    assert refresh(db_session, mailboxes) == 1
    row = row_of(db_session, "sam@example.com")
    assert (row.time_zone, row.source, row.checked_at) == ("Asia/Tokyo", "mailbox", NOW)
    assert row.next_check_at == NOW + timedelta(days=30)
    assert zone_for_email(db_session, "sam@example.com") == "Asia/Tokyo"

    assert refresh(db_session, mailboxes, now=NOW + timedelta(days=29)) == 0  # not due yet
    mailboxes.zones["sam@example.com"] = "GMT Standard Time"  # they moved
    assert refresh(db_session, mailboxes, now=NOW + timedelta(days=31)) == 1
    assert row_of(db_session, "sam@example.com").time_zone == "Europe/London"


def test_the_refresh_interval_is_a_setting(db_session):
    set_setting(db_session, TIME_ZONE_REFRESH_DAYS, "7")
    note_requester(db_session, "sam@example.com", now=NOW)
    refresh(db_session, FakeMailboxes({"sam@example.com": "GMT Standard Time"}))
    assert row_of(db_session, "sam@example.com").next_check_at == NOW + timedelta(days=7)


def test_an_unknown_sender_or_a_failed_lookup_keeps_the_default_and_is_retried_later(db_session):
    for email in ("ext@elsewhere.com", "broken@example.com", "odd@example.com"):
        note_requester(db_session, email, now=NOW)
    db_session.commit()
    mailboxes = FakeMailboxes({
        "ext@elsewhere.com": None,  # Graph: no such mailbox
        "broken@example.com": httpx.ConnectError("down"),
        "odd@example.com": "Customized Time Zone",  # cannot be mapped
    })
    assert refresh(db_session, mailboxes) == 3  # one failure does not stop the others
    for email in ("ext@elsewhere.com", "broken@example.com", "odd@example.com"):
        row = row_of(db_session, email)
        assert row.time_zone is None and row.checked_at == NOW
        assert row.next_check_at == NOW + timedelta(hours=24)
        assert zone_for_email(db_session, email) == "Europe/London"
    assert refresh(db_session, mailboxes, now=NOW + timedelta(hours=23)) == 0
    assert refresh(db_session, mailboxes, now=NOW + timedelta(hours=25)) == 3


def test_a_failed_refresh_keeps_the_earlier_reading(db_session):
    note_requester(db_session, "sam@example.com", now=NOW)
    refresh(db_session, FakeMailboxes({"sam@example.com": "Tokyo Standard Time"}))
    refresh(db_session, FakeMailboxes({"sam@example.com": httpx.ConnectError("down")}), now=NOW + timedelta(days=31))
    row = row_of(db_session, "sam@example.com")
    assert row.time_zone == "Asia/Tokyo"
    assert row.next_check_at == NOW + timedelta(days=31, hours=24)


def test_the_retry_interval_is_a_setting(db_session):
    set_setting(db_session, TIME_ZONE_RETRY_HOURS, "2")
    note_requester(db_session, "ext@elsewhere.com", now=NOW)
    refresh(db_session, FakeMailboxes({}))
    assert row_of(db_session, "ext@elsewhere.com").next_check_at == NOW + timedelta(hours=2)


def test_only_a_batch_is_read_per_run_and_the_oldest_first(db_session, monkeypatch):
    from app import time_zones

    monkeypatch.setattr(time_zones, "LOOKUPS_PER_RUN", 2)
    for n, email in enumerate(("c@example.com", "a@example.com", "b@example.com")):
        note_requester(db_session, email, now=NOW - timedelta(minutes=10 - n))
    db_session.commit()
    mailboxes = FakeMailboxes({})
    assert refresh(db_session, mailboxes) == 2
    assert mailboxes.asked == ["c@example.com", "a@example.com"]


# ---- Graph and the worker job -----------------------------------------------------------------------------


def test_the_mailbox_time_zone_is_read_from_mailbox_settings(monkeypatch):
    seen = []

    def handler(method, url, **kw):
        seen.append((url, kw.get("params")))
        return httpx.Response(200, json={"timeZone": "GMT Standard Time"}, request=httpx.Request(method, url))

    assert graph_with(monkeypatch, handler).mailbox_time_zone("jo@example.com") == "GMT Standard Time"
    assert seen == [("https://graph.microsoft.com/v1.0/users/jo%40example.com/mailboxSettings", {"$select": "timeZone"})]


def test_graph_not_knowing_the_mailbox_is_none_and_other_errors_are_raised(monkeypatch):
    def handler_for(status):
        return lambda method, url, **kw: httpx.Response(status, json={}, request=httpx.Request(method, url))

    assert graph_with(monkeypatch, handler_for(404)).mailbox_time_zone("ext@elsewhere.com") is None
    with pytest.raises(httpx.HTTPStatusError):  # for example 403 when MailboxSettings.Read is not granted
        graph_with(monkeypatch, handler_for(403)).mailbox_time_zone("jo@example.com")


def test_a_slash_in_an_email_cannot_change_the_mailbox_route(monkeypatch):
    seen = []

    def handler(method, url, **kw):
        seen.append(url)
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    graph_with(monkeypatch, handler).mailbox_time_zone("a/b@example.com")
    assert seen == ["https://graph.microsoft.com/v1.0/users/a%2Fb%40example.com/mailboxSettings"]


def test_the_worker_runs_the_requester_time_zone_job_only_when_graph_is_configured(db_session, monkeypatch):
    from app import jobs

    assert [j.every_seconds for j in JOBS if j.name == "requester-time-zones"] == [300]
    monkeypatch.setattr(settings, "mailbox_time_zones_enabled", True)
    calls = []
    monkeypatch.setattr(jobs, "refresh_requester_zones", lambda session: calls.append(session) or RefreshResult(0, 0, ""))
    for value in ("", "x"):
        monkeypatch.setattr(settings, "graph_tenant_id", value)
        monkeypatch.setattr(settings, "graph_client_id", value)
        monkeypatch.setattr(settings, "graph_client_secret", value)
        jobs.requester_time_zones(db_session)
    assert len(calls) == 1


def test_the_mailbox_lookup_is_off_by_default_and_then_makes_no_graph_call(db_session, monkeypatch):
    from app import jobs

    assert type(settings).model_fields["mailbox_time_zones_enabled"].default is False
    note_requester(db_session, "newcomer@example.com", now=NOW)  # intake still notes the address
    db_session.commit()
    for name in ("graph_tenant_id", "graph_client_id", "graph_client_secret"):
        monkeypatch.setattr(settings, name, "x")  # Graph is configured; only the switch is off
    monkeypatch.setattr(settings, "mailbox_time_zones_enabled", False)
    calls = []
    monkeypatch.setattr(jobs, "refresh_requester_zones", lambda session: calls.append(session))
    monkeypatch.setattr("app.graph_client.graph_client.mailbox_time_zone", lambda email: calls.append(email))
    jobs.requester_time_zones(db_session)
    assert calls == []
    assert db_session.get(RequesterTimeZone, "newcomer@example.com") is not None
    # Falls back to the default zone: nothing was read.
    assert zone_for_email(db_session, "newcomer@example.com") == default_zone(db_session)


# ---- Admin settings ----------------------------------------------------------------------------------------


def test_the_default_time_zone_is_a_validated_admin_setting(client, db_session):
    from tests.conftest import login_as

    assert REGISTRY[DEFAULT_TIME_ZONE].default == "Europe/London"
    login_as(client, "admin@example.com")
    assert DEFAULT_TIME_ZONE in client.get("/admin/settings?group=other").text
    assert client.post("/admin/settings", data={"key": DEFAULT_TIME_ZONE, "value": "Mars/Olympus"}).status_code == 400
    assert client.post("/admin/settings", data={"key": DEFAULT_TIME_ZONE, "value": "Asia/Tokyo"}, follow_redirects=False).status_code == 303
    assert client.post("/admin/settings", data={"key": DEFAULT_TIME_ZONE, "value": ""}, follow_redirects=False).status_code == 303
    assert default_zone(db_session) == "Europe/London"  # blank means the placeholder again
    assert client.post("/admin/settings", data={"key": DEFAULT_TIME_ZONE, "value": "Asia/Tokyo"}, follow_redirects=False).status_code == 303
    assert db_session.get(Setting, DEFAULT_TIME_ZONE).value == "Asia/Tokyo"


# ---- Failures show, intake and the backdate form --------------------------------------------------------------


def test_a_run_where_every_lookup_failed_fails_the_job_but_keeps_the_retry_times(db_session, monkeypatch):
    from app import jobs, time_zones

    for email in ("a@example.com", "b@example.com"):
        note_requester(db_session, email, now=NOW)
    db_session.commit()
    denied = httpx.HTTPStatusError("denied", request=httpx.Request("GET", "https://x"), response=httpx.Response(403))
    monkeypatch.setattr(settings, "mailbox_time_zones_enabled", True)
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "c")
    monkeypatch.setattr(settings, "graph_client_secret", "s")
    real = time_zones.refresh_requester_zones
    monkeypatch.setattr(jobs, "refresh_requester_zones", lambda session: real(session, FakeMailboxes({"a@example.com": denied, "b@example.com": denied})))

    with pytest.raises(RuntimeError, match="HTTPStatusError 403.*MailboxSettings.Read"):
        jobs.requester_time_zones(db_session)

    db_session.expire_all()
    assert all(row.next_check_at > datetime.now(timezone.utc) for row in db_session.exec(select(RequesterTimeZone)).all())


def test_one_failure_among_good_lookups_does_not_fail_the_job(db_session, monkeypatch):
    from app import jobs, time_zones

    for email in ("a@example.com", "b@example.com"):
        note_requester(db_session, email, now=NOW)
    db_session.commit()
    monkeypatch.setattr(settings, "mailbox_time_zones_enabled", True)
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "c")
    monkeypatch.setattr(settings, "graph_client_secret", "s")
    real = time_zones.refresh_requester_zones
    answers = FakeMailboxes({"a@example.com": "GMT Standard Time", "b@example.com": httpx.ConnectError("down")})
    monkeypatch.setattr(jobs, "refresh_requester_zones", lambda session: real(session, answers))

    jobs.requester_time_zones(db_session)  # no error


def test_mail_from_a_sender_notes_them_for_a_mailbox_lookup_even_when_it_is_a_reply(db_session):
    from app.inbound import ingest_mail
    from tests.test_inbound_mail import mail, mailbox  # noqa: F401  (the helper builds the IncomingMail)

    ingest_mail(db_session, mail(sender="Newcomer@Example.com"))
    db_session.commit()
    assert db_session.get(RequesterTimeZone, "newcomer@example.com") is not None


def test_the_backdate_form_is_shown_and_read_in_the_agents_own_zone(client, db_session):
    from app.backdating import parse_moment
    from app.models import Ticket, TicketSource

    assert parse_moment("2026-10-25T17:00", "Asia/Tokyo") == datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    assert parse_moment("2026-10-25T17:00") == datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
    assert parse_moment("2026-10-25T17:00", "Mars/Olympus") == datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)

    ticket = ticket_in_a_team(client, db_session, zone="Asia/Tokyo")
    ticket.source = TicketSource.AGENT
    db_session.add(ticket)
    db_session.commit()
    page = client.get(f"/tickets/{ticket.number}").text
    assert 'name="reported_at" value="2026-10-26T01:00"' in page  # 16:00 UTC is 01:00 the next day in Tokyo
    assert "times in your time zone (Asia/Tokyo)" in page

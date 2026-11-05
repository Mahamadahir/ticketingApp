"""Handoff bell entries and gov.uk bank holidays (issue #151). Nothing here touches the network: gov.uk is a stub."""

from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlmodel import select

from app import bank_holidays
from app.bank_holidays import FetchError, cached_holidays, parse_feed, refresh_bank_holidays, run_bank_holiday_fetch
from app.config import settings
from app.models import AuditLog, BankHolidayCache, Notification, Ticket, TicketStatus, User, UserRole
from app.msp import hand_off_due_tickets, handoff_window, warn_before_handoff
from app.notification_settings import save_choices
from app.scheduled_jobs import SCHEDULED_JOBS
from app.settings_store import (
    BANK_HOLIDAY_FAIL_DAYS,
    BANK_HOLIDAY_REGION,
    MSP_BANK_HOLIDAYS,
    MSP_HANDOFF_WARN_MINUTES,
    set_setting,
)
from tests.conftest import create_team, login_as, set_role
from tests.test_msp import NOW, START, THURSDAY_MORNING, UTC, at_risk, included_team, mail_on, set_msp  # noqa: F401

WARN_AT = START - timedelta(minutes=20)  # 17:40, inside the default 30 minutes


def assigned_ticket(client, db_session, team, email="pat@example.com", **fields):
    agent_id = login_as(client, email, "Pat")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return at_risk(db_session, team, assigned_agent_id=agent_id, **fields), agent_id


def bells(db_session, kind):
    return db_session.exec(select(Notification).where(Notification.kind == kind).order_by(Notification.id)).all()


# ---- The warning ------------------------------------------------------------------------------------------


def test_the_assignee_is_warned_once_before_the_handoff_and_the_entry_links_to_the_ticket(client, db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    ticket, agent_id = assigned_ticket(client, db_session, team)
    assert warn_before_handoff(db_session, WARN_AT) == 1
    db_session.commit()
    (bell,) = bells(db_session, "msp_handoff_warning")
    assert bell.user_id == agent_id and bell.ticket_id == ticket.id
    assert bell.text.endswith(" will go to the MSP at 18:00. Hold it to keep it.")
    assert bell.actor_email is None
    assert warn_before_handoff(db_session, WARN_AT + timedelta(minutes=5)) == 0  # once per ticket per night
    db_session.commit()
    assert len(bells(db_session, "msp_handoff_warning")) == 1


def test_a_new_night_warns_again(client, db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    ticket, _ = assigned_ticket(client, db_session, team, due=START + timedelta(days=3))
    assert warn_before_handoff(db_session, WARN_AT) == 0  # due after the next morning: not handed off tonight
    ticket.resolution_due_at = START + timedelta(hours=10)
    db_session.add(ticket)
    db_session.commit()
    assert warn_before_handoff(db_session, WARN_AT) == 1
    db_session.commit()
    assert warn_before_handoff(db_session, WARN_AT + timedelta(days=1)) == 1  # Thursday 17:40, due before Friday morning
    db_session.commit()
    assert len(bells(db_session, "msp_handoff_warning")) == 2


def test_the_warning_uses_the_same_rule_as_the_job(client, db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    other = create_team(db_session, "Not included")
    assigned_ticket(client, db_session, team, msp_hold=True)
    assigned_ticket(client, db_session, team, "ann@example.com", status=TicketStatus.AWAITING_USER)
    assigned_ticket(client, db_session, other, "lee@example.com")
    at_risk(db_session, team)  # nobody assigned: nobody to tell
    at_risk(db_session, team, due=THURSDAY_MORNING + timedelta(hours=1))
    assert warn_before_handoff(db_session, WARN_AT) == 0
    assigned_ticket(client, db_session, team, "eve@example.com")
    assert warn_before_handoff(db_session, WARN_AT) == 1  # and the job sends that one (and the unassigned one)
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW) == 2


@pytest.mark.parametrize("now", [START - timedelta(minutes=31), START, START + timedelta(minutes=1)])
def test_no_warning_outside_the_warning_period(client, db_session, mail_on, now):
    set_msp(db_session)
    assigned_ticket(client, db_session, included_team(db_session))
    assert warn_before_handoff(db_session, now) == 0


def test_the_minutes_are_a_setting_and_zero_is_off(client, db_session, mail_on):
    set_msp(db_session)
    assigned_ticket(client, db_session, included_team(db_session))
    set_setting(db_session, MSP_HANDOFF_WARN_MINUTES, "0")
    db_session.commit()
    assert warn_before_handoff(db_session, WARN_AT) == 0
    set_setting(db_session, MSP_HANDOFF_WARN_MINUTES, "10")
    db_session.commit()
    assert warn_before_handoff(db_session, WARN_AT) == 0  # 20 minutes before: outside a 10 minute warning
    assert warn_before_handoff(db_session, START - timedelta(minutes=5)) == 1


def test_nothing_is_warned_when_the_job_could_send_nothing(client, db_session, monkeypatch):
    team = included_team(db_session)
    assigned_ticket(client, db_session, team)
    monkeypatch.setattr(settings, "mail_sending", "dev")
    assert warn_before_handoff(db_session, WARN_AT) == 0  # no MSP address
    set_msp(db_session)
    monkeypatch.setattr(settings, "mail_sending", "off")
    assert warn_before_handoff(db_session, WARN_AT) == 0  # mail off


def test_a_person_who_turned_both_channels_off_is_not_warned(client, db_session, mail_on):
    set_msp(db_session)
    _, agent_id = assigned_ticket(client, db_session, included_team(db_session))
    save_choices(db_session, db_session.get(User, agent_id), set())
    db_session.commit()
    assert warn_before_handoff(db_session, WARN_AT) == 0
    assert bells(db_session, "msp_handoff_warning") == []


# ---- After the handoff --------------------------------------------------------------------------------------


def test_the_assignee_is_told_when_the_job_sends_the_ticket(client, db_session, mail_on):
    set_msp(db_session)
    ticket, agent_id = assigned_ticket(client, db_session, included_team(db_session))
    assert hand_off_due_tickets(db_session, NOW) == 1
    db_session.commit()
    (bell,) = bells(db_session, "msp_handed_off")
    assert bell.user_id == agent_id and bell.ticket_id == ticket.id
    assert bell.text.endswith(" was sent to the MSP at 18:30")
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.WITH_MSP


def test_an_agents_own_send_makes_no_after_entry(client, db_session, mail_on):
    from app.msp import send_to_msp

    set_msp(db_session)
    ticket, agent_id = assigned_ticket(client, db_session, included_team(db_session))
    send_to_msp(db_session, ticket, db_session.get(User, agent_id), "Needs an engineer")
    db_session.commit()
    assert bells(db_session, "msp_handed_off") == []


# ---- gov.uk bank holidays -----------------------------------------------------------------------------------

FEED = {
    "england-and-wales": {"division": "england-and-wales", "events": [{"title": "Boxing Day", "date": "2026-12-28"}, {"title": "Christmas", "date": "2026-12-25"}]},
    "scotland": {"division": "scotland", "events": [{"title": "St Andrew's Day", "date": "2026-11-30"}]},
    "northern-ireland": {"division": "northern-ireland", "events": [{"title": "Battle of the Boyne", "date": "2026-07-13"}]},
}
BOTH = {date(2026, 12, 25), date(2026, 12, 28)}


@pytest.fixture
def gov_uk(monkeypatch):
    """gov.uk answers with FEED (or raises `error`); `calls` counts the requests. httpx.get is the only way out."""
    state = {"calls": 0, "answer": FEED, "error": None}

    def fake_get(url, **kwargs):
        state["calls"] += 1
        assert url == bank_holidays.URL and kwargs.get("timeout")
        if state["error"] is not None:
            raise state["error"]
        return httpx.Response(200, json=state["answer"], request=httpx.Request("GET", url))

    monkeypatch.setattr(bank_holidays.httpx, "get", fake_get)
    return state


def test_parse_feed_reads_one_region_and_refuses_nothing_useful():
    assert parse_feed(FEED, "scotland") == [date(2026, 11, 30)]
    assert parse_feed(FEED, "england-and-wales") == sorted(BOTH)
    for bad in ({}, {"scotland": {}}, {"scotland": {"events": []}}, {"scotland": {"events": [{"date": "x"}]}}, [], None):
        with pytest.raises(FetchError):
            parse_feed(bad, "scotland")


def test_the_job_stores_the_regions_dates_and_the_maths_reads_only_the_cache(db_session, gov_uk):
    assert cached_holidays(db_session) == frozenset()
    run_bank_holiday_fetch(db_session)
    db_session.commit()
    assert gov_uk["calls"] == 1
    assert cached_holidays(db_session) == BOTH
    gov_uk["error"] = AssertionError("the business-day maths must not call the network")
    # Thursday 24 December 2026, 18:00: the next business morning skips the 25th (bank holiday), the weekend and the 28th.
    start = datetime(2026, 12, 24, 18, 0, tzinfo=UTC)
    assert handoff_window(db_session, start + timedelta(minutes=1)) == (start, datetime(2026, 12, 29, 8, 0, tzinfo=UTC))
    assert gov_uk["calls"] == 1


def test_the_region_is_a_setting(db_session, gov_uk):
    set_setting(db_session, BANK_HOLIDAY_REGION, "scotland")
    db_session.commit()
    run_bank_holiday_fetch(db_session)
    db_session.commit()
    assert cached_holidays(db_session) == {date(2026, 11, 30)}
    assert cached_holidays(db_session, "england-and-wales") == frozenset()
    set_setting(db_session, BANK_HOLIDAY_REGION, "nonsense")  # a bad stored value reads as the default region
    db_session.commit()
    assert bank_holidays.region_setting(db_session) == "england-and-wales"


def test_the_typed_list_still_counts_alongside_the_fetched_dates(db_session, gov_uk):
    run_bank_holiday_fetch(db_session)
    set_setting(db_session, MSP_BANK_HOLIDAYS, "2026-12-24")  # a shutdown day
    db_session.commit()
    start = datetime(2026, 12, 23, 18, 0, tzinfo=UTC)  # Wednesday: 24th (typed), 25th and 28th (gov.uk) and the weekend are out
    assert handoff_window(db_session, start + timedelta(minutes=1)) == (start, datetime(2026, 12, 29, 8, 0, tzinfo=UTC))


def test_a_failed_fetch_keeps_the_last_good_list_and_fails_only_after_the_limit(db_session, gov_uk):
    now = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    assert refresh_bank_holidays(db_session, now) is True
    db_session.commit()
    gov_uk["error"] = httpx.ConnectTimeout("slow")
    for days in (1, 2, 3):  # the limit is 3 days by default: three days without a good fetch is still not an alarm
        assert refresh_bank_holidays(db_session, now + timedelta(days=days)) is False
    assert cached_holidays(db_session) == BOTH
    with pytest.raises(FetchError, match="since 2026-11-05"):
        refresh_bank_holidays(db_session, now + timedelta(days=3, hours=1))
    assert cached_holidays(db_session) == BOTH  # never emptied


def test_the_limit_is_a_setting(db_session, gov_uk):
    now = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    refresh_bank_holidays(db_session, now)
    set_setting(db_session, BANK_HOLIDAY_FAIL_DAYS, "1")
    db_session.commit()
    gov_uk["error"] = httpx.ConnectError("down")
    assert refresh_bank_holidays(db_session, now + timedelta(hours=23)) is False
    with pytest.raises(FetchError):
        refresh_bank_holidays(db_session, now + timedelta(days=1, hours=1))


@pytest.mark.parametrize("bad", ["status", "json", "empty"])
def test_a_bad_answer_never_replaces_the_list(db_session, gov_uk, bad):
    now = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    refresh_bank_holidays(db_session, now)
    db_session.commit()
    if bad == "status":
        gov_uk["error"] = httpx.HTTPStatusError("503", request=httpx.Request("GET", "x"), response=httpx.Response(503))
    elif bad == "json":
        gov_uk["error"] = ValueError("not json")
    else:
        gov_uk["answer"] = {"england-and-wales": {"events": []}}
    assert refresh_bank_holidays(db_session, now + timedelta(days=1)) is False
    assert cached_holidays(db_session) == BOTH


def test_with_nothing_cached_a_failure_is_visible_at_once(db_session, gov_uk):
    gov_uk["error"] = httpx.ConnectError("down")
    with pytest.raises(FetchError, match="ever"):
        run_bank_holiday_fetch(db_session)
    assert db_session.exec(select(BankHolidayCache)).all() == []


def test_a_changed_list_replaces_the_dates_and_is_audited(db_session, gov_uk):
    now = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    refresh_bank_holidays(db_session, now)
    gov_uk["answer"] = {"england-and-wales": {"events": [{"date": "2027-01-01"}]}}
    refresh_bank_holidays(db_session, now + timedelta(days=1))
    db_session.commit()
    assert cached_holidays(db_session) == {date(2027, 1, 1)}
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_type == "bank_holidays")).one().detail == "england-and-wales: 1 dates"


def test_the_job_is_registered_daily():
    (job,) = [j for j in SCHEDULED_JOBS if j.name == "bank-holidays"]
    assert job.every_seconds == 86400 and job.run is run_bank_holiday_fetch


def test_the_settings_page_validates_the_region_and_the_new_numbers(client, db_session):
    admin_id = login_as(client, "root@example.com", "Root")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)

    def post(key, value):
        return client.post("/admin/settings", data={"key": key, "value": value}, follow_redirects=False).status_code

    assert post(BANK_HOLIDAY_REGION, "scotland") == 303
    assert post(BANK_HOLIDAY_REGION, "wales") == 400
    assert post(BANK_HOLIDAY_FAIL_DAYS, "0") == 400 and post(BANK_HOLIDAY_FAIL_DAYS, "5") == 303
    assert post(MSP_HANDOFF_WARN_MINUTES, "0") == 303 and post(MSP_HANDOFF_WARN_MINUTES, "abc") == 400

"""7A-i reports (#173): each report against seeded tickets with known answers, the filters, the date range in the
viewer's zone, the visibility limit, the permission, the CSV escaping and the audit entry."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import (
    AuditLog, Category, Ticket, TicketEvent, TicketSource, TicketStatus, TicketType, User, UserPermission, UserRole,
)
from app.permissions import PERMISSIONS, VIEW_REPORTS
from app.report_kit import ReportInputError, csv_cell, csv_text, parse_filters
from app.reports import REPORTS
from app.settings_store import REGISTRY, REPORT_MAX_DAYS, set_setting
from app.visibility import access_context
from tests.conftest import add_membership, create_category, create_team, login_as, set_role

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def at(day: str, hour: int = 10, minute: int = 0) -> datetime:
    """2026-10-05 style date and a time, as UTC."""
    year, month, date = (int(p) for p in day.split("-"))
    return datetime(year, month, date, hour, minute, tzinfo=timezone.utc)


def make_ticket(db, team, created, *, priority=None, source=TicketSource.EMAIL, ticket_type=TicketType.INCIDENT,
                status=TicketStatus.OPEN, category=None, **fields) -> Ticket:
    ticket = Ticket(
        subject=fields.pop("subject", "s"), requester_email="r@example.com", team_id=team.id if team else None, created_at=created,
        priority=priority, source=source, type=ticket_type, status=status,
        category_id=category.id if category else None, triaged_at=created, **fields,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def make_event(db, ticket, event_type, when, to_value=None):
    db.add(TicketEvent(ticket_id=ticket.id, event_type=event_type, created_at=when, to_value=to_value))
    db.commit()


def admin_user(client, db_session, email="admin@example.com", zone="UTC") -> User:
    user_id = login_as(client, email)
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)
    user = db_session.get(User, user_id)
    user.time_zone = zone
    db_session.add(user)
    db_session.commit()
    return user


def run(db, user, key, params=None, now=NOW):
    base = {"from": "2026-10-01", "to": "2026-10-31"}
    filters = parse_filters(db, user, {**base, **(params or {})}, now=now, uses_range=REPORTS[key].uses_range)
    return REPORTS[key].build(db, user, access_context(db, user), filters, now)


def table(report):
    return {tuple(row[:2]): row[2:] for row in report.rows}


def notes(report):
    return dict(report.notes)


@pytest.fixture
def world(client, db_session):
    desk = create_team(db_session, "Desk")
    net = create_team(db_session, "Net")
    return desk, net, admin_user(client, db_session)


# ---- Volume --------------------------------------------------------------------------------------------


def test_volume_counts_created_tickets_four_ways(db_session, world):
    desk, net, admin = world
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, desk, "Laptop", parent=hardware)
    make_ticket(db_session, desk, at("2026-10-05"), category=hardware)
    make_ticket(db_session, desk, at("2026-10-06"), category=laptop, source=TicketSource.PORTAL)
    make_ticket(db_session, net, at("2026-10-07"), ticket_type=TicketType.SERVICE_REQUEST, source=TicketSource.PORTAL)
    make_ticket(db_session, net, at("2026-10-08"), is_backdated=True)  # backdated tickets count towards volume
    make_ticket(db_session, desk, at("2026-09-30", 22))  # outside the range
    make_ticket(db_session, desk, at("2026-11-01"))  # outside the range

    report = run(db_session, admin, "volume")

    assert notes(report)["Tickets created in the range"] == "4"
    counts = {(row[0], row[1]): row[2] for row in report.rows}
    assert counts[("Category", "Hardware")] == 1
    assert counts[("Category", "Hardware / Laptop")] == 1
    assert counts[("Category", "(not categorised)")] == 2
    assert counts[("Team", "Desk")] == 2 and counts[("Team", "Net")] == 2
    assert counts[("Type", "Incident")] == 3 and counts[("Type", "Service request")] == 1
    assert counts[("Source", "Email")] == 2 and counts[("Source", "Portal")] == 2
    for breakdown in ("Category", "Team", "Type", "Source"):
        assert sum(n for (b, _), n in counts.items() if b == breakdown) == 4


def test_volume_keeps_same_named_categories_apart(db_session, world):
    desk, net, admin = world
    for team in (desk, net):
        laptop = create_category(db_session, team, "Laptop", parent=create_category(db_session, team, "Hardware"))
        make_ticket(db_session, team, at("2026-10-05"), category=laptop)
    rows = [row for row in run(db_session, admin, "volume").rows if row[0] == "Category"]
    assert rows == [["Category", "Hardware / Laptop", 1], ["Category", "Hardware / Laptop", 1]]


def test_the_range_is_whole_days_in_the_viewers_zone(client, db_session, world):
    desk, _, _ = world
    london = admin_user(client, db_session, "london@example.com", zone="Europe/London")
    utc = db_session.exec(select(User).where(User.email == "admin@example.com")).one()
    # 23:30 UTC on 30 Sep is 00:30 on 1 Oct in London (BST); 23:30 UTC on 31 Oct is 23:30 on 31 Oct (GMT).
    make_ticket(db_session, desk, at("2026-09-30", 23, 30))
    make_ticket(db_session, desk, at("2026-10-31", 23, 30))
    make_ticket(db_session, desk, at("2026-11-01", 0, 30))  # 00:30 on 1 Nov in London (GMT): outside

    assert notes(run(db_session, london, "volume"))["Tickets created in the range"] == "2"
    assert notes(run(db_session, utc, "volume"))["Tickets created in the range"] == "1"


def test_default_range_is_the_last_30_days_up_to_today_in_the_viewers_zone(client, db_session):
    user = admin_user(client, db_session)
    filters = parse_filters(db_session, user, {}, now=NOW)
    assert (filters.from_day.isoformat(), filters.to_day.isoformat()) == ("2026-10-10", "2026-11-08")
    late = datetime(2026, 11, 8, 23, 30, tzinfo=timezone.utc)
    tokyo = admin_user(client, db_session, "tokyo@example.com", zone="Asia/Tokyo")  # already 9 Nov there
    assert parse_filters(db_session, tokyo, {}, now=late).to_day.isoformat() == "2026-11-09"


def test_filters_team_type_category_priority_source(db_session, world):
    desk, net, admin = world
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, desk, "Laptop", parent=hardware)
    other = create_category(db_session, net, "Network")
    make_ticket(db_session, desk, at("2026-10-05"), category=laptop, priority="p1")
    make_ticket(db_session, desk, at("2026-10-05"), category=hardware, priority="p2", source=TicketSource.PORTAL)
    make_ticket(db_session, net, at("2026-10-05"), category=other, priority="p1", ticket_type=TicketType.PROBLEM)

    def total(**params):
        return notes(run(db_session, admin, "volume", params))["Tickets created in the range"]

    assert total() == "3"
    assert total(team=str(desk.id)) == "2"
    assert total(type="problem") == "1"
    assert total(category=str(hardware.id)) == "2"  # a main category covers its sub-categories
    assert total(category=str(laptop.id)) == "1"
    assert total(priority="p1") == "2"
    assert total(source="portal") == "1"
    assert total(team=str(desk.id), priority="p1", source="email") == "1"


@pytest.mark.parametrize(
    "params",
    [{"from": "0001-01-01", "to": "0001-01-01"}, {"from": "9999-12-31", "to": "9999-12-31"}, {"from": "yesterday"}, {"from": "2026-10-31", "to": "2026-10-01"}, {"team": "x"}, {"type": "nope"},
     {"priority": "p9"}, {"source": "fax"}, {"category": "-1"}, {"from": "2025-01-01", "to": "2026-10-31"}],
)
def test_bad_input_is_refused(client, db_session, params):
    user = admin_user(client, db_session)
    with pytest.raises(ReportInputError):
        parse_filters(db_session, user, params, now=NOW)
    page = client.get("/reports/volume", params=params)
    assert page.status_code == 400
    assert client.get("/reports/volume/csv", params=params).status_code == 400


def test_a_report_that_ignores_the_range_carries_no_dates(client, db_session, world):
    admin = world[2]
    assert client.get("/reports/backlog?from=2020-01-01").status_code == 200  # the range does not apply, so is not checked
    link = [p for p in client.get("/reports/backlog").text.split('href="') if "/csv" in p][0].split('"')[0]
    assert link == "/reports/backlog/csv"
    resp = client.get("/reports/backlog/csv?from=2020-01-01&to=2020-01-02")
    assert resp.headers["content-disposition"] == 'attachment; filename="report-backlog.csv"'
    assert "from=" not in resp.content.decode("utf-8-sig")
    entry = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "report")).one()
    assert entry.detail == "backlog: (UTC)"
    assert not parse_filters(db_session, admin, {}, now=NOW, uses_range=False).as_params()


def test_range_cap_is_a_setting(client, db_session):
    user = admin_user(client, db_session)
    assert REGISTRY[REPORT_MAX_DAYS].default == "366"
    assert parse_filters(db_session, user, {"from": "2025-11-08", "to": "2026-11-08"}, now=NOW)  # 366 days
    with pytest.raises(ReportInputError):
        parse_filters(db_session, user, {"from": "2025-11-07", "to": "2026-11-08"}, now=NOW)  # 367
    set_setting(db_session, REPORT_MAX_DAYS, "10")
    db_session.commit()
    with pytest.raises(ReportInputError):
        parse_filters(db_session, user, {"from": "2026-10-01", "to": "2026-10-11"}, now=NOW)
    assert parse_filters(db_session, user, {"from": "2026-10-01", "to": "2026-10-10"}, now=NOW)


# ---- SLA compliance --------------------------------------------------------------------------------------


def seed_sla(db, desk, net):
    # Desk P1: first response met, resolution met.
    make_ticket(db, desk, at("2026-10-05"), priority="p1", status=TicketStatus.RESOLVED,
                first_response_due_at=at("2026-10-05", 10, 15), first_responded_at=at("2026-10-05", 10, 10),
                resolution_due_at=at("2026-10-05", 14), resolved_at=at("2026-10-05", 13))
    # Desk P1: both late.
    make_ticket(db, desk, at("2026-10-06"), priority="p1", status=TicketStatus.CLOSED, sla_paused_seconds=3600,
                first_response_due_at=at("2026-10-06", 10, 15), first_responded_at=at("2026-10-06", 10, 30),
                resolution_due_at=at("2026-10-06", 15), resolved_at=at("2026-10-06", 16))
    # Desk P2: nobody answered and it is open and overdue: both breached.
    make_ticket(db, desk, at("2026-10-07"), priority="p2",
                first_response_due_at=at("2026-10-07", 12), resolution_due_at=at("2026-10-08"))
    # Net P3: deadlines still in the future: not decided.
    make_ticket(db, net, at("2026-10-08"), priority="p3",
                first_response_due_at=at("2026-11-20"), resolution_due_at=at("2026-11-21"))
    # Net P3, overdue but paused (Awaiting user): not decided either.
    make_ticket(db, net, at("2026-10-08"), priority="p3", status=TicketStatus.AWAITING_USER, sla_paused_at=at("2026-10-09"),
                first_response_due_at=at("2026-10-08", 11), resolution_due_at=at("2026-10-09"))
    # Backdated: left out, counted. No SLA at all: left out, counted.
    make_ticket(db, net, at("2026-10-09"), priority="p1", is_backdated=True)
    make_ticket(db, net, at("2026-10-10"))


def test_sla_compliance_by_team_and_priority(db_session, world):
    desk, net, admin = world
    seed_sla(db_session, desk, net)

    report = run(db_session, admin, "sla")

    rows = table(report)
    assert rows[("Desk", "P1")] == [1, 1, 50.0, 1, 1, 50.0]
    assert rows[("Desk", "P2")] == [0, 1, 0.0, 0, 1, 0.0]
    assert rows[("Net", "P3")] == [0, 0, None, 0, 0, None]
    assert ("Net", "P1") not in rows and ("Net", "None") not in rows
    assert rows[("All", "All")] == [1, 2, 33.3, 1, 2, 33.3]
    found = notes(report)
    assert found["Left out: backdated tickets"] == "1"
    assert found["Left out: tickets without an SLA"] == "1"
    assert found["First response not yet decided (clock running or paused)"] == "2"
    assert found["Resolution not yet decided (clock running or paused)"] == "2"


def test_sla_edge_cases_finished_without_an_email_due_dates_and_pauses(db_session, world):
    desk, _, admin = world
    # Closed with no email to the requester, before the first-response deadline: not late.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p4", status=TicketStatus.CLOSED,
                first_response_due_at=at("2026-10-05", 12), resolved_at=at("2026-10-05", 11))
    # Closed with no email after the deadline: late.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p4", status=TicketStatus.CLOSED,
                first_response_due_at=at("2026-10-05", 12), resolved_at=at("2026-10-06"))
    # Paused, past a Due date: the Due date does not move for the pause, so it is breached.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p4", status=TicketStatus.AWAITING_USER,
                sla_paused_at=at("2026-10-06"), due_on=at("2026-10-07").date(), resolution_due_at=at("2026-10-08"))
    # Paused, past a target (no Due date): undecided.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p4", status=TicketStatus.AWAITING_USER,
                sla_paused_at=at("2026-10-06"), resolution_due_at=at("2026-10-08"))
    report = run(db_session, admin, "sla")
    assert table(report)[("Desk", "P4")] == [1, 1, 50.0, 0, 1, 0.0]
    assert notes(report)["Resolution not yet decided (clock running or paused)"] == "1"


def test_sla_filters_and_range_apply(db_session, world):
    desk, net, admin = world
    seed_sla(db_session, desk, net)
    only_desk = table(run(db_session, admin, "sla", {"team": str(desk.id)}))
    assert ("Net", "P3") not in only_desk and only_desk[("All", "All")][0:2] == [1, 2]
    assert table(run(db_session, admin, "sla", {"priority": "p2"}))[("All", "All")] == [0, 1, 0.0, 0, 1, 0.0]
    empty = run(db_session, admin, "sla", {"from": "2026-01-01", "to": "2026-01-31"})
    assert empty.rows == [["All", "All", 0, 0, None, 0, 0, None]]


# ---- Times -----------------------------------------------------------------------------------------------


def test_mean_and_median_times_in_elapsed_minutes(db_session, world):
    desk, net, admin = world
    # First response after 10 minutes, resolved after 180.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p1", status=TicketStatus.RESOLVED,
                first_responded_at=at("2026-10-05", 10, 10), resolved_at=at("2026-10-05", 13))
    # 30 minutes; 6 hours less an hour in Awaiting user = 300.
    make_ticket(db_session, desk, at("2026-10-06"), priority="p1", status=TicketStatus.CLOSED, sla_paused_seconds=3600,
                first_responded_at=at("2026-10-06", 10, 30), resolved_at=at("2026-10-06", 16))
    # 80 minutes; resolved after 600. With the other two: first 10, 30, 80 (mean 40, median 30); resolution 180, 300, 600.
    make_ticket(db_session, desk, at("2026-10-07"), priority="p1", status=TicketStatus.RESOLVED,
                first_responded_at=at("2026-10-07", 11, 20), resolved_at=at("2026-10-07", 20))
    # Responded, still open: counts for first response only.
    make_ticket(db_session, net, at("2026-10-08"), priority="p3", first_responded_at=at("2026-10-08", 12))
    # Backdated tickets are left out.
    make_ticket(db_session, net, at("2026-10-09"), priority="p3", is_backdated=True, status=TicketStatus.RESOLVED,
                first_responded_at=at("2026-10-12"), resolved_at=at("2026-10-12"))

    report = run(db_session, admin, "times")

    rows = table(report)
    assert rows[("Desk", "P1")] == [3, 40.0, 30.0, 3, 360.0, 300.0]
    assert rows[("Net", "P3")] == [1, 120.0, 120.0, 0, None, None]
    assert rows[("All", "All")] == [4, 60.0, 55.0, 3, 360.0, 300.0]
    assert notes(report)["Left out: backdated tickets"] == "1"
    assert "Elapsed time, not working time" in report.description


def test_times_with_nothing_to_measure(db_session, world):
    _, _, admin = world
    assert run(db_session, admin, "times").rows == [["All", "All", 0, None, None, 0, None, None]]


# ---- Backlog age -----------------------------------------------------------------------------------------


def test_backlog_age_buckets_open_tickets_now_whatever_the_range(db_session, world):
    desk, net, admin = world
    # NOW is 8 Nov 12:00 UTC.
    make_ticket(db_session, desk, at("2026-11-08", 6), priority="p1")  # under a day
    make_ticket(db_session, desk, at("2026-11-06", 12), priority="p1")  # exactly 2 days: 1 to 3
    make_ticket(db_session, desk, at("2026-11-02"), priority="p1", status=TicketStatus.AWAITING_USER)  # 6 days: 3 to 7
    make_ticket(db_session, desk, at("2026-10-20"), priority="p1")  # 19 days: 7 to 30
    make_ticket(db_session, desk, at("2026-08-01"), priority="p1")  # over 30
    make_ticket(db_session, net, at("2026-10-01"), priority="p2", is_backdated=True)  # over 30, backdated still open
    make_ticket(db_session, net, at("2026-10-01"))  # no priority
    make_ticket(db_session, desk, at("2026-10-01"), priority="p1", status=TicketStatus.RESOLVED, resolved_at=at("2026-10-02"))
    make_ticket(db_session, desk, at("2026-10-01"), priority="p1", status=TicketStatus.CLOSED, resolved_at=at("2026-10-02"))

    report = run(db_session, admin, "backlog", {"from": "2026-01-01", "to": "2026-01-02"})  # the range is ignored

    assert report.columns[2:] == ["Under 1 day", "1 to 3 days", "3 to 7 days", "7 to 30 days", "Over 30 days", "Open tickets"]
    rows = table(report)
    assert rows[("Desk", "P1")] == [1, 1, 1, 1, 1, 5]
    assert rows[("Net", "P2")] == [0, 0, 0, 0, 1, 1]
    assert rows[("Net", "None")] == [0, 0, 0, 0, 1, 1]
    assert rows[("All", "All")] == [1, 1, 1, 1, 3, 7]
    assert table(run(db_session, admin, "backlog", {"team": str(net.id)}))[("All", "All")] == [0, 0, 0, 0, 2, 2]
    assert table(run(db_session, admin, "backlog", {"priority": "p2"}))[("All", "All")] == [0, 0, 0, 0, 1, 1]


def test_a_ticket_exactly_on_a_boundary_goes_in_the_older_bucket(db_session, world):
    desk, _, admin = world
    make_ticket(db_session, desk, NOW - timedelta(days=1), priority="p1")
    make_ticket(db_session, desk, NOW - timedelta(days=30), priority="p1")
    assert table(run(db_session, admin, "backlog"))[("Desk", "P1")] == [0, 1, 0, 0, 1, 2]


# ---- Reopen rate -----------------------------------------------------------------------------------------


def test_reopen_rate_and_recloses(db_session, world):
    desk, net, admin = world
    resolved_event = lambda t, when: make_event(db_session, t, "status_changed", when, "resolved")
    a = make_ticket(db_session, desk, at("2026-10-01"))
    resolved_event(a, at("2026-10-05"))
    make_event(db_session, a, "reopened", at("2026-10-06"))
    make_event(db_session, a, "reclosed", at("2026-10-06", 12), "said_thanks")
    b = make_ticket(db_session, desk, at("2026-10-01"))
    resolved_event(b, at("2026-10-05"))
    c = make_ticket(db_session, desk, at("2026-10-01"))
    resolved_event(c, at("2026-10-07"))
    make_event(db_session, c, "reopened", at("2026-10-08"))
    make_event(db_session, c, "reclosed", at("2026-10-09"), "duplicate")
    d = make_ticket(db_session, desk, at("2026-10-01"))  # reopened before it was resolved: not a reopen of this resolution
    make_event(db_session, d, "reopened", at("2026-10-01", 12))
    resolved_event(d, at("2026-10-10"))
    e = make_ticket(db_session, net, at("2026-09-01"))  # resolved before the range, reopened in it: not in the cohort
    resolved_event(e, at("2026-09-20"))
    make_event(db_session, e, "reopened", at("2026-10-05"))
    f = make_ticket(db_session, desk, at("2026-10-01"))  # resolved twice in the range: counted once
    resolved_event(f, at("2026-10-11"))
    resolved_event(f, at("2026-10-12"))
    g = make_ticket(db_session, net, at("2026-10-01"))
    resolved_event(g, at("2026-10-12"))
    make_event(db_session, g, "reopened", at("2026-10-13"))
    original = make_ticket(db_session, net, at("2026-10-01"))  # a split original is left out; its part counts
    part = make_ticket(db_session, net, at("2026-10-01"), split_from_id=original.id)
    resolved_event(original, at("2026-10-14"))
    resolved_event(part, at("2026-10-14"))

    report = run(db_session, admin, "reopen")

    rows = {row[0]: row[1:] for row in report.rows}
    assert rows["Desk"] == [5, 2, 40.0, 2, 1]
    assert rows["Net"] == [2, 1, 50.0, 0, 0]
    assert rows["All"] == [7, 3, 42.9, 2, 1]
    assert run(db_session, admin, "reopen", {"from": "2026-12-01", "to": "2026-12-02"}).rows == [["All", 0, 0, None, 0, 0]]


# ---- Access ----------------------------------------------------------------------------------------------


def test_the_permission_is_registered_and_super_admins_hold_it(client, db_session):
    assert VIEW_REPORTS in PERMISSIONS
    admin_user(client, db_session)
    assert client.get("/reports").status_code == 200
    page = client.get("/reports")
    for definition in REPORTS.values():
        assert f'href="/reports/{definition.key}"' in page.text
    assert '<a href="/reports">Reports</a>' in page.text


def test_agent_needs_the_grant_and_an_end_user_never_gets_in(client, db_session):
    team = create_team(db_session, "Desk")
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    assert client.get("/reports").status_code == 403
    assert client.get("/reports/volume").status_code == 403
    assert client.get("/reports/volume/csv").status_code == 403
    assert "/reports" not in client.get("/tickets").text
    db_session.add(UserPermission(user_id=agent_id, permission=VIEW_REPORTS))
    db_session.commit()
    assert client.get("/reports/volume").status_code == 200
    assert client.get("/reports/volume/csv").status_code == 200
    assert 'href="/reports"' in client.get("/tickets").text
    client.post("/auth/logout")
    end_id = login_as(client, "end@example.com")
    set_role(db_session, end_id, UserRole.END_USER)
    db_session.add(UserPermission(user_id=end_id, permission=VIEW_REPORTS))  # a stray row counts for nothing
    db_session.commit()
    assert client.get("/reports").status_code == 403
    assert client.get("/reports/volume/csv").status_code == 403


def test_unknown_report_is_404(client, db_session):
    admin_user(client, db_session)
    assert client.get("/reports/nonsense").status_code == 404
    assert client.get("/reports/nonsense/csv").status_code == 404


def test_a_team_lead_sees_only_their_teams_and_a_super_admin_sees_all(client, db_session):
    desk = create_team(db_session, "Desk")
    net = create_team(db_session, "Net")
    for team, n in ((desk, 2), (net, 3)):
        for _ in range(n):
            make_ticket(db_session, team, at("2026-10-05"), priority="p1")
    lead_id = login_as(client, "lead@example.com")
    set_role(db_session, lead_id, UserRole.AGENT, desk.id)
    db_session.add(UserPermission(user_id=lead_id, permission=VIEW_REPORTS))
    db_session.commit()
    lead = db_session.get(User, lead_id)
    lead.time_zone = "UTC"
    db_session.add(lead)
    db_session.commit()

    report = run(db_session, lead, "volume")
    assert notes(report)["Tickets created in the range"] == "2"
    assert not any(row[1] == "Net" for row in report.rows)
    # Asking for another team's tickets by filter gets nothing, not their figures.
    assert notes(run(db_session, lead, "volume", {"team": str(net.id)}))["Tickets created in the range"] == "0"
    assert table(run(db_session, lead, "backlog"))[("All", "All")][-1] == 2
    assert "Net" not in client.get("/reports/volume?from=2026-10-01&to=2026-10-31").text
    # The Team filter offers only teams they can see.
    page = client.get("/reports/volume").text
    assert f'<option value="{desk.id}"' in page and f'<option value="{net.id}"' not in page
    csv_body = client.get("/reports/sla/csv?from=2026-10-01&to=2026-10-31").content.decode("utf-8-sig")
    assert "Net" not in csv_body

    admin = admin_user(client, db_session, "boss@example.com")
    assert notes(run(db_session, admin, "volume"))["Tickets created in the range"] == "5"


def test_a_viewer_of_a_team_counts_and_a_parent_teams_member_sees_sub_teams(client, db_session):
    parent = create_team(db_session, "Parent")
    child = create_team(db_session, "Child")
    child.parent_team_id = parent.id
    db_session.add(child)
    db_session.commit()
    make_ticket(db_session, child, at("2026-10-05"))
    user_id = login_as(client, "p@example.com")
    set_role(db_session, user_id, UserRole.AGENT, parent.id)
    user = db_session.get(User, user_id)
    user.time_zone = "UTC"
    db_session.add(user)
    db_session.commit()
    assert notes(run(db_session, user, "volume"))["Tickets created in the range"] == "1"


# ---- CSV -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", '=cmd|" /C calc"!A0'])
def test_cells_that_could_run_as_formulas_get_an_apostrophe(text):
    assert csv_cell(text) == "'" + text


def test_other_cells_are_left_alone():
    assert csv_cell("Desk") == "Desk" and csv_cell("a=b") == "a=b" and csv_cell("") == ""
    assert csv_cell(None) == "" and csv_cell(-3) == -3 and csv_cell(0.5) == 0.5


def test_csv_file_is_utf8_with_a_bom_and_escapes_text(client, db_session):
    admin_user(client, db_session)
    evil = create_team(db_session, '=HYPERLINK("http://x","go")')
    make_ticket(db_session, evil, at("2026-10-05"))
    resp = client.get("/reports/volume/csv?from=2026-10-01&to=2026-10-31&team=" + str(evil.id))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="report-volume-2026-10-01-2026-10-31.csv"'
    assert resp.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0] == ["Breakdown", "Value", "Tickets"]
    assert ["Team", "'" + evil.name, "1"] in rows
    assert not any(cell.startswith("=") for row in rows for cell in row)


def test_csv_has_the_same_numbers_as_the_page_and_the_filters_used(client, db_session):
    desk = create_team(db_session, "Desk")
    net = create_team(db_session, "Net")
    admin_user(client, db_session)
    make_ticket(db_session, desk, at("2026-10-05"), priority="p1")
    make_ticket(db_session, net, at("2026-10-05"), priority="p1")
    page = client.get("/reports/volume?from=2026-10-01&to=2026-10-31&team=" + str(desk.id))
    link = page.text.split('href="')[[i for i, p in enumerate(page.text.split('href="')) if "/csv?" in p][0]].split('"')[0]
    assert "team=" + str(desk.id) in link and "from=2026-10-01" in link
    body = client.get(link.replace("&amp;", "&")).content.decode("utf-8-sig")
    assert "Desk" in body and "Net" not in body
    assert "Tickets created in the range,1" in body
    assert "Filters," in body and "team=" + str(desk.id) in body


def test_every_report_renders_and_downloads(client, db_session):
    desk = create_team(db_session, "Desk")
    admin_user(client, db_session)
    seed_sla(db_session, desk, create_team(db_session, "Net"))
    for key in REPORTS:
        page = client.get(f"/reports/{key}?from=2026-10-01&to=2026-10-31")
        assert page.status_code == 200 and "Download CSV" in page.text, key
        csv_resp = client.get(f"/reports/{key}/csv?from=2026-10-01&to=2026-10-31")
        assert csv_resp.status_code == 200 and csv_resp.content.startswith(b"\xef\xbb\xbf"), key


def test_the_text_of_csv_rows_is_quoted_by_the_csv_writer():
    from app.report_kit import Report

    report = Report("t", ["a", "b"], [['x,"y"', "line\nbreak"]], notes=[("n", "-v")])
    rows = list(csv.reader(io.StringIO(csv_text(report).lstrip("﻿"))))
    assert rows == [["a", "b"], ['x,"y"', "line\nbreak"], [], ["n", "'-v"]]


# ---- Audit -----------------------------------------------------------------------------------------------


def test_each_download_is_audited_with_who_which_report_and_filters_but_a_page_view_is_not(client, db_session):
    desk = create_team(db_session, "Desk")
    admin_user(client, db_session)
    client.get("/reports/volume?from=2026-10-01&to=2026-10-31")
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_type == "report")).all() == []

    client.get(f"/reports/sla/csv?from=2026-10-01&to=2026-10-31&team={desk.id}&priority=p1&source=portal")
    client.get("/reports/volume/csv?from=2026-10-01&to=2026-10-31")
    db_session.expire_all()
    entries = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "report").order_by(AuditLog.id)).all()
    assert [e.action for e in entries] == ["report_csv_download"] * 2
    assert entries[0].actor_email == "admin@example.com"
    for part in ("sla", "from=2026-10-01", "to=2026-10-31", f"team={desk.id}", "priority=p1", "source=portal", "(UTC)"):
        assert part in entries[0].detail
    assert entries[1].detail.startswith("volume:")


def test_a_refused_download_is_not_audited(client, db_session):
    admin_user(client, db_session)
    client.get("/reports/volume/csv?from=bad")
    client.get("/reports/nonsense/csv")
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_type == "report")).all() == []
    client.post("/auth/logout")
    login_as(client, "plain@example.com")
    client.get("/reports/volume/csv")
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_type == "report")).all() == []


def test_the_audit_log_page_can_filter_on_reports(client, db_session):
    admin_user(client, db_session)
    client.get("/reports/volume/csv?from=2026-10-01&to=2026-10-31")
    page = client.get("/admin/audit-log?entity_type=report")
    assert page.status_code == 200 and "report_csv_download" in page.text

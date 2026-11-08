"""7A-ii reports (#174): satisfaction (and the feedback rule), MSP handoffs, Major Incidents and incidents per
Problem, each against seeded data with known answers, plus the CSV."""

import csv
import io
import itertools
from datetime import timedelta

import pytest

from sqlmodel import select

from app.models import (
    ActionToken, TicketFeedback, TicketSource, TicketStatus, TicketType, User, UserPermission, UserRole,
)
from app.msp import BY_AUTOMATIC, EVENT_MSP_ACKNOWLEDGED, EVENT_MSP_TOLD, EVENT_SENT_TO_MSP
from app.msp_actions import EVENT_TAKEN_BACK
from app.permissions import VIEW_REPORTS
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_reports import NOW, admin_user, at, make_event, make_ticket, notes, run

COUNTER = itertools.count(1)


def rate(db, ticket, rating, resolver, when, comment=""):
    n = next(COUNTER)
    token = ActionToken(
        ticket_id=ticket.id, kind="feedback", token_hash=f"h{n}", resolved_by_id=resolver.id if resolver else None,
        expires_at=when + timedelta(days=1), used_at=when,
    )
    db.add(token)
    db.flush()
    db.add(TicketFeedback(
        ticket_id=ticket.id, token_id=token.id, rating=rating, comment=comment,
        resolved_by_id=resolver.id if resolver else None, created_at=when,
    ))
    db.commit()


def make_agent(client, db, email, team, *, grant=True, name=None) -> User:
    user_id = login_as(client, email, name or email)
    set_role(db, user_id, UserRole.AGENT, team.id)
    if grant:
        db.add(UserPermission(user_id=user_id, permission=VIEW_REPORTS))
    user = db.get(User, user_id)
    user.time_zone = "UTC"
    db.add(user)
    db.commit()
    return user


def by_breakdown(report, name):
    return {row[1]: row[2:] for row in report.rows if row[0] == name}


# ---- Satisfaction -------------------------------------------------------------------------------------------


@pytest.fixture
def rated(client, db_session):
    """Ann and Bob (agents, Desk) and an admin; five ratings in October, one automatic, one outside the range."""
    desk = create_team(db_session, "Desk")
    net = create_team(db_session, "Net")
    ann = make_agent(client, db_session, "ann@example.com", desk, name="Ann")
    bob = make_agent(client, db_session, "bob@example.com", desk, name="Bob")
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, desk, "Laptop", parent=hardware)
    t1 = make_ticket(db_session, desk, at("2026-10-01"), category=laptop)
    t2 = make_ticket(db_session, desk, at("2026-10-01"), category=hardware)
    t3 = make_ticket(db_session, net, at("2026-10-01"))
    rate(db_session, t1, 5, ann, at("2026-10-05"), "Ann was brilliant")
    rate(db_session, t2, 4, ann, at("2026-10-06"))
    rate(db_session, t2, 2, bob, at("2026-10-07"), "=Bob was slow")
    rate(db_session, t3, 1, None, at("2026-10-08"), "Nobody answered the robot")
    rate(db_session, t3, 3, bob, at("2026-09-20"), "last month")  # outside the range
    admin = admin_user(client, db_session)
    return desk, net, ann, bob, admin


def test_satisfaction_spread_and_average_by_agent_team_and_category_for_an_admin(db_session, rated):
    desk, net, ann, bob, admin = rated
    report = run(db_session, admin, "satisfaction")
    assert notes(report)["Ratings in the range"] == "4"
    assert report.rows[0][:4] == ["Overall", "All", 4, 3.0]
    assert report.rows[0][4:9] == [1, 1, 0, 1, 1]  # one each of 1, 2, 4 and 5 stars
    agents = by_breakdown(report, "Agent")
    assert agents["Ann"][:2] == [2, 4.5] and agents["Ann"][2:7] == [0, 0, 0, 1, 1]
    assert agents["Bob"][:2] == [1, 2.0]
    assert agents["(automatic resolve)"][:2] == [1, 1.0]  # admins only
    teams = by_breakdown(report, "Team")
    assert teams["Desk"][:2] == [3, pytest.approx(3.67, abs=0.01)] and teams["Net"][:2] == [1, 1.0]
    categories = by_breakdown(report, "Category")
    assert categories["Hardware / Laptop"][:2] == [1, 5.0] and categories["Hardware"][:2] == [2, 3.0]
    assert categories["(not categorised)"][:2] == [1, 1.0]


def test_satisfaction_filters_apply_to_the_rated_ticket(db_session, rated):
    desk, net, ann, bob, admin = rated
    report = run(db_session, admin, "satisfaction", {"team": str(net.id)})
    assert report.rows[0][:4] == ["Overall", "All", 1, 1.0]
    assert "Ann" not in by_breakdown(report, "Agent")
    report = run(db_session, admin, "satisfaction", {"from": "2026-09-01", "to": "2026-09-30"})
    assert report.rows[0][:4] == ["Overall", "All", 1, 3.0]


def test_an_agent_sees_only_feedback_on_tickets_they_resolved_and_never_the_automatic_ones(db_session, rated):
    desk, net, ann, bob, admin = rated
    report = run(db_session, ann, "satisfaction")
    assert report.rows[0][:4] == ["Overall", "All", 2, 4.5]
    assert list(by_breakdown(report, "Agent")) == ["Ann"]
    comments = [row for row in report.rows if row[0] == "Comment"]
    assert [row[-1] for row in comments] == ["Ann was brilliant"]
    flat = " ".join(str(c) for row in report.rows for c in row)
    assert "Bob" not in flat and "robot" not in flat and "slow" not in flat
    report = run(db_session, bob, "satisfaction")
    assert report.rows[0][:4] == ["Overall", "All", 1, 2.0]
    assert "(automatic resolve)" not in " ".join(str(c) for row in report.rows for c in row)


def test_a_colleague_who_can_open_the_tickets_still_reads_no_one_elses_feedback(client, db_session, rated):
    desk, net, ann, bob, admin = rated
    lead = make_agent(client, db_session, "lead@example.com", desk, name="Lead")
    report = run(db_session, lead, "satisfaction")
    assert report.rows[0][:3] == ["Overall", "All", 0]  # they resolved nothing, so they read nothing
    assert [row for row in report.rows if row[0] == "Comment"] == []


def test_satisfaction_never_shows_the_requester_and_escapes_comments_in_the_csv(client, db_session, rated):
    desk, net, ann, bob, admin = rated
    report = run(db_session, admin, "satisfaction")
    assert "r@example.com" not in " ".join(str(c) for row in report.rows for c in row)
    body = client.get("/reports/satisfaction/csv?from=2026-10-01&to=2026-10-31").content.decode("utf-8-sig")
    assert "r@example.com" not in body
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][:4] == ["Breakdown", "Value", "Ratings", "Average rating"]
    assert "'=Bob was slow" in [row[-1] for row in rows if row]
    assert not any(cell.startswith("=") for row in rows for cell in row)


def test_satisfaction_csv_of_an_agent_holds_only_their_rows(client, db_session, rated):
    desk, net, ann, bob, admin = rated
    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    body = client.get("/reports/satisfaction/csv?from=2026-10-01&to=2026-10-31").content.decode("utf-8-sig")
    assert "Bob" in body and "Ann" not in body and "robot" not in body and "brilliant" not in body


def test_satisfaction_lists_only_the_newest_comments_up_to_the_limit(db_session, rated, monkeypatch):
    desk, net, ann, bob, admin = rated
    monkeypatch.setattr("app.reports.MAX_COMMENTS", 2)
    report = run(db_session, admin, "satisfaction")
    comments = [row[-1] for row in report.rows if row[0] == "Comment"]
    assert comments == ["Nobody answered the robot", "=Bob was slow"]
    assert "only the newest 2" in notes(report)["Comments"]


# ---- MSP handoffs -------------------------------------------------------------------------------------------


def sent(db, ticket, when, how="thread"):
    make_event(db, ticket, EVENT_SENT_TO_MSP, when, to_value=how)


def test_msp_handoffs_split_automatic_and_manual_with_acknowledgement_take_back_and_release(db_session):
    desk, net = create_team(db_session, "Desk"), create_team(db_session, "Net")
    a = make_ticket(db_session, desk, at("2026-10-05"))
    b = make_ticket(db_session, desk, at("2026-10-05"))
    c = make_ticket(db_session, desk, at("2026-10-05"))
    d = make_ticket(db_session, net, at("2026-10-05"))
    sent(db_session, a, at("2026-10-06", 18), BY_AUTOMATIC)
    make_event(db_session, a, EVENT_MSP_ACKNOWLEDGED, at("2026-10-06", 18, 30), "ABC1")
    sent(db_session, b, at("2026-10-07", 11), "thread")
    make_event(db_session, b, EVENT_MSP_ACKNOWLEDGED, at("2026-10-07", 12), "ABC2")
    make_event(db_session, b, EVENT_TAKEN_BACK, at("2026-10-08"), "in_progress")
    sent(db_session, c, at("2026-10-09", 9), "original")
    make_event(db_session, c, EVENT_MSP_TOLD, at("2026-10-10"), "resolved")
    sent(db_session, d, at("2026-10-09", 18), BY_AUTOMATIC)
    sent(db_session, d, at("2026-09-30", 18), BY_AUTOMATIC)  # outside the range
    report = run(db_session, _admin(db_session), "msp")
    rows = {row[0]: row[1:] for row in report.rows}
    # sent, automatic, manual, acknowledged, ack %, median minutes, taken back, released
    assert rows["Desk"] == [3, 1, 2, 2, 66.7, 45.0, 1, 1]
    assert rows["Net"] == [1, 1, 0, 0, 0.0, None, 0, 0]
    assert rows["All"] == [4, 2, 2, 2, 50.0, 45.0, 1, 1]


def test_a_second_send_starts_a_new_handoff_and_events_belong_to_the_handoff_before_the_next(db_session):
    desk = create_team(db_session, "Desk")
    a = make_ticket(db_session, desk, at("2026-10-05"))
    sent(db_session, a, at("2026-10-06", 10), "thread")
    make_event(db_session, a, EVENT_MSP_ACKNOWLEDGED, at("2026-10-06", 11), "N1")
    make_event(db_session, a, EVENT_TAKEN_BACK, at("2026-10-07"), "open")
    sent(db_session, a, at("2026-10-08", 10), BY_AUTOMATIC)  # sent again, never acknowledged or taken back this time
    b = make_ticket(db_session, desk, at("2026-10-05"))
    sent(db_session, b, at("2026-10-06", 10), "thread")  # the first send is never acknowledged
    sent(db_session, b, at("2026-10-08", 10), "thread")
    make_event(db_session, b, EVENT_MSP_ACKNOWLEDGED, at("2026-10-08", 10, 20), "N2")  # belongs to the second send only
    admin = _admin(db_session)
    rows = {row[0]: row[1:] for row in run(db_session, admin, "msp").rows}
    assert rows["All"] == [4, 1, 3, 2, 50.0, 40.0, 1, 0]


def test_msp_handoffs_follow_visibility_and_filters(client, db_session):
    desk, net = create_team(db_session, "Desk"), create_team(db_session, "Net")
    mine = make_ticket(db_session, desk, at("2026-10-05"), priority="p1")
    other = make_ticket(db_session, net, at("2026-10-05"), priority="p2")
    sent(db_session, mine, at("2026-10-06", 18), BY_AUTOMATIC)
    sent(db_session, other, at("2026-10-06", 18), BY_AUTOMATIC)
    lead = make_agent(client, db_session, "lead@example.com", desk)
    report = run(db_session, lead, "msp")
    assert [row[0] for row in report.rows] == ["Desk", "All"]
    admin = _admin(db_session)
    assert [r[0] for r in run(db_session, admin, "msp", {"priority": "p2"}).rows] == ["Net", "All"]


def test_msp_with_nothing_sent_has_a_totals_row_of_zeroes(db_session):
    admin = _admin(db_session)
    report = run(db_session, admin, "msp")
    assert report.rows == [["All", 0, 0, 0, 0, None, None, 0, 0]]


def _admin(db):
    user = User(email=f"admin{next(COUNTER)}@example.com", display_name="Admin", role=UserRole.SUPER_ADMIN, time_zone="UTC")
    db.add(user)
    db.commit()
    return user


# ---- Major Incidents ----------------------------------------------------------------------------------------


def test_major_incidents_count_duration_and_linked_tickets(db_session):
    desk = create_team(db_session, "Desk")
    admin = _admin(db_session)
    done = make_ticket(
        db_session, desk, at("2026-10-05", 9), ticket_type=TicketType.MAJOR_INCIDENT, status=TicketStatus.RESOLVED,
        resolved_at=at("2026-10-05", 12), subject="Email down",
    )
    open_one = make_ticket(db_session, desk, at("2026-10-10"), ticket_type=TicketType.MAJOR_INCIDENT, subject="VPN")
    make_ticket(db_session, desk, at("2026-09-01"), ticket_type=TicketType.MAJOR_INCIDENT, subject="Out of range", status=TicketStatus.RESOLVED,
                resolved_at=at("2026-09-02"))
    make_ticket(db_session, desk, at("2026-10-05"), ticket_type=TicketType.INCIDENT)
    for parent, n in ((done, 3), (open_one, 1)):
        for _ in range(n):
            make_ticket(db_session, desk, at("2026-10-06"), linked_parent_id=parent.id)
    report = run(db_session, admin, "major-incidents")
    assert [(r[0], r[1], r[3], r[6], r[7]) for r in report.rows] == [
        (done.reference, "Email down", "Resolved", 180.0, 3),
        (open_one.reference, "VPN", "Open", None, 1),
    ]
    assert report.rows[1][5] is None
    facts = notes(report)
    assert facts["Major Incidents opened in the range"] == "2" and facts["Resolved or closed"] == "1"
    assert facts["Mean duration (minutes)"] == "180.0" and facts["Median duration (minutes)"] == "180.0"
    assert facts["Linked tickets in all"] == "4"


def test_major_incident_mean_and_median_differ(db_session):
    desk = create_team(db_session, "Desk")
    admin = _admin(db_session)
    for hours in (1, 2, 6):
        make_ticket(db_session, desk, at("2026-10-05", 0), ticket_type=TicketType.MAJOR_INCIDENT, status=TicketStatus.CLOSED,
                    resolved_at=at("2026-10-05", 0) + timedelta(hours=hours))
    facts = notes(run(db_session, admin, "major-incidents"))
    assert facts["Mean duration (minutes)"] == "180.0" and facts["Median duration (minutes)"] == "120.0"


def test_major_incidents_with_none_say_so(db_session):
    facts = notes(run(db_session, _admin(db_session), "major-incidents"))
    assert facts["Mean duration (minutes)"] == "none yet"


def test_linked_counts_only_what_the_viewer_may_see(client, db_session):
    desk, net = create_team(db_session, "Desk"), create_team(db_session, "Net")
    parent = make_ticket(db_session, desk, at("2026-10-05"), ticket_type=TicketType.MAJOR_INCIDENT)
    make_ticket(db_session, desk, at("2026-10-06"), linked_parent_id=parent.id)
    make_ticket(db_session, net, at("2026-10-06"), linked_parent_id=parent.id)  # a team the viewer is not in
    lead = make_agent(client, db_session, "lead@example.com", desk)
    assert run(db_session, lead, "major-incidents").rows[0][7] == 1
    assert run(db_session, _admin(db_session), "major-incidents").rows[0][7] == 2


# ---- Incidents per Problem ----------------------------------------------------------------------------------


def test_problems_list_open_first_with_incident_counts_and_old_open_ones(db_session):
    desk = create_team(db_session, "Desk")
    admin = _admin(db_session)
    few = make_ticket(db_session, desk, at("2026-10-02"), ticket_type=TicketType.PROBLEM, subject="Few")
    many = make_ticket(db_session, desk, at("2026-10-03"), ticket_type=TicketType.PROBLEM, subject="Many")
    old_open = make_ticket(db_session, desk, at("2026-01-03"), ticket_type=TicketType.PROBLEM, subject="Old but open")
    closed = make_ticket(db_session, desk, at("2026-10-01"), ticket_type=TicketType.PROBLEM, subject="Fixed",
                         status=TicketStatus.CLOSED, resolved_at=at("2026-10-20"))
    old_closed = make_ticket(db_session, desk, at("2026-01-04"), ticket_type=TicketType.PROBLEM, status=TicketStatus.CLOSED,
                             resolved_at=at("2026-02-01"))
    for parent, n in ((few, 1), (many, 3), (old_open, 2), (closed, 5), (old_closed, 9)):
        for _ in range(n):
            make_ticket(db_session, desk, at("2026-10-06"), linked_parent_id=parent.id)
    make_ticket(db_session, desk, at("2026-10-06"), ticket_type=TicketType.SERVICE_REQUEST, linked_parent_id=many.id)
    report = run(db_session, admin, "problems")
    assert [(r[1], r[3], r[5], r[6]) for r in report.rows] == [
        ("Many", "Open", 3, 4), ("Old but open", "Open", 2, 2), ("Few", "Open", 1, 1), ("Fixed", "Closed", 5, 5),
    ]
    facts = notes(report)
    assert facts["Problems listed"] == "4" and facts["Open"] == "3" and facts["Linked incidents in all"] == "11"


# ---- The pages and the CSV ----------------------------------------------------------------------------------


def test_the_new_reports_are_registered_in_the_menu_and_need_the_permission(client, db_session):
    for key in ("satisfaction", "msp", "major-incidents", "problems"):
        from app.reports import REPORTS

        assert key in REPORTS
    team = create_team(db_session, "Desk")
    agent = make_agent(client, db_session, "a@example.com", team, grant=False)
    for key in ("satisfaction", "msp", "major-incidents", "problems"):
        assert client.get(f"/reports/{key}").status_code == 403 and client.get(f"/reports/{key}/csv").status_code == 403
    db_session.add(UserPermission(user_id=agent.id, permission=VIEW_REPORTS))
    db_session.commit()
    menu = client.get("/reports").text
    for key in ("satisfaction", "msp", "major-incidents", "problems"):
        assert f'/reports/{key}"' in menu
        assert client.get(f"/reports/{key}?from=2026-10-01&to=2026-10-31").status_code == 200
        assert client.get(f"/reports/{key}/csv?from=2026-10-01&to=2026-10-31").status_code == 200


def test_msp_csv_has_the_report_and_escapes_a_team_name(client, db_session):
    evil = create_team(db_session, "=cmd|' /C calc'!A0")
    admin_user(client, db_session)
    ticket = make_ticket(db_session, evil, at("2026-10-05"))
    sent(db_session, ticket, at("2026-10-06", 18), BY_AUTOMATIC)
    body = client.get("/reports/msp/csv?from=2026-10-01&to=2026-10-31").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][:3] == ["Team", "Sent to the MSP", "Automatic (18:00 handoff)"]
    assert rows[1][0] == "'=cmd|' /C calc'!A0" and rows[1][1] == "1"
    assert not any(cell.startswith("=") for row in rows for cell in row)
    assert any(row[:1] == ["Filters"] for row in rows)


def test_problem_subjects_are_escaped_in_the_csv(client, db_session):
    desk = create_team(db_session, "Desk")
    admin_user(client, db_session)
    make_ticket(db_session, desk, at("2026-10-05"), ticket_type=TicketType.PROBLEM, subject="@SUM(A1)")
    body = client.get("/reports/problems/csv?from=2026-10-01&to=2026-10-31").content.decode("utf-8-sig")
    assert "'@SUM(A1)" in body

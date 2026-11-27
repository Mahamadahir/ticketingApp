"""The agent queue's Status filter, its Time to breach sort, and the one mapping of a status to a label and tone (#290).
Visibility is still decided in app/visibility.py; these tests only check that the filter and sort never widen it."""

import re
from datetime import datetime, timedelta, timezone

from app import pagination
from app.routers import tickets as tickets_router
from app.models import FINISHED_STATUSES, TicketStatus
from app.status_display import STATUS_LABELS, STATUS_TONES, status_label, status_options, status_tone
from tests.conftest import add_membership, create_category, create_team
from tests.test_queue_page import triaged
from tests.test_triage import agent, new_ticket

UTC_NOW = datetime.now(timezone.utc)


def subjects(page: str) -> list[str]:
    return re.findall(r'<a class="subj" href="[^"]*">([^<]*)</a>', page)


def desk_agent(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent_id = agent(client, db_session, member_of=[desk])
    return desk, category, agent_id


def one_per_status(db_session, desk, category):
    return {s: triaged(db_session, desk, category, f"T-{s.value}", status=s) for s in TicketStatus}


# ---- The mapping --------------------------------------------------------------------------------------------


def test_the_mapping_covers_every_status_with_the_agreed_tones():
    assert set(STATUS_LABELS) == set(TicketStatus) == set(STATUS_TONES)
    assert {s.value: status_tone(s) for s in TicketStatus} == {
        "new": "blue", "open": "blue", "in_progress": "blue",
        "awaiting_user": "amber", "awaiting_approval": "amber", "with_supplier": "amber", "with_msp": "amber",
        "resolved": "green", "closed": "gray", "scheduled": "gray",
    }
    assert "red" not in STATUS_TONES.values()  # kept for SLA breaches
    assert status_label(TicketStatus.WITH_MSP) == "With MSP" and status_label("in_progress") == "In progress"
    assert status_label("awaiting_approval") == "Awaiting approval"
    assert status_tone("something_else") == "gray"  # an unknown stored value never breaks a page


def test_the_filter_options_are_any_not_finished_then_each_status():
    assert [label for _value, label in status_options()] == [
        "Not finished", "New", "Open", "In progress", "Awaiting user", "Awaiting user · acknowledged", "Awaiting approval", "With supplier",
        "With MSP", "Scheduled", "Resolved", "Closed",
    ]


# ---- Status filter ------------------------------------------------------------------------------------------


def test_each_status_option_returns_only_that_status(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    one_per_status(db_session, desk, category)

    for status in TicketStatus:
        page = client.get("/tickets", params={"status": status.value}).text
        assert subjects(page) == [f"T-{status.value}"]
        assert "Showing 1 of 1" in page


def test_not_finished_excludes_resolved_and_closed_and_any_returns_everything(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    one_per_status(db_session, desk, category)

    active = set(subjects(client.get("/tickets", params={"status": "active"}).text))
    assert active == {f"T-{s.value}" for s in TicketStatus if s not in FINISHED_STATUSES}
    for blank in ({}, {"status": ""}):
        assert len(subjects(client.get("/tickets", params=blank).text)) == len(TicketStatus)


def test_the_status_filter_combines_with_team_search_and_mine(client, db_session):
    desk, category, me = desk_agent(client, db_session)
    other = create_team(db_session, "Networks")
    other_category = create_category(db_session, other, "Wi-Fi")
    add_membership(db_session, me, other.id)
    triaged(db_session, desk, category, "Router at desk", status=TicketStatus.WITH_MSP, assigned_agent_id=me)
    triaged(db_session, desk, category, "Printer at desk", status=TicketStatus.WITH_MSP)
    triaged(db_session, desk, category, "Router open", status=TicketStatus.OPEN)
    triaged(db_session, other, other_category, "Router elsewhere", status=TicketStatus.WITH_MSP)

    def listed(**params):
        return sorted(subjects(client.get("/tickets", params={"status": "with_msp", **params}).text))

    assert listed() == ["Printer at desk", "Router at desk", "Router elsewhere"]
    assert listed(team_id=str(desk.id)) == ["Printer at desk", "Router at desk"]
    assert listed(q="router") == ["Router at desk", "Router elsewhere"]
    assert listed(mine="true") == ["Router at desk"]
    assert listed(team_id=str(other.id), q="router") == ["Router elsewhere"]


def test_the_counts_and_showing_line_follow_the_filter(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    one_per_status(db_session, desk, category)
    page = client.get("/tickets", params={"status": "active"}).text
    assert f"Showing {len(TicketStatus) - 2} of {len(TicketStatus) - 2}" in page


def test_an_unknown_status_or_sort_is_a_422(client, db_session):
    desk_agent(client, db_session)
    assert client.get("/tickets", params={"status": "sleeping"}).status_code == 422
    assert client.get("/tickets", params={"sort": "newest"}).status_code == 422
    assert client.get("/tickets", params={"status": "", "sort": ""}).status_code == 200


def test_the_filter_never_shows_another_teams_ticket(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    other = create_team(db_session, "Networks")
    other_category = create_category(db_session, other, "Wi-Fi")
    triaged(db_session, desk, category, "Mine", status=TicketStatus.WITH_MSP)
    triaged(db_session, other, other_category, "Not mine", status=TicketStatus.WITH_MSP)
    for params in ({"status": "with_msp"}, {"status": "active"}, {"status": "with_msp", "sort": "breach"}, {"status": "with_msp", "team_id": str(other.id)}):
        assert "Not mine" not in client.get("/tickets", params=params).text


def test_the_form_has_the_status_and_sort_controls(client, db_session):
    desk_agent(client, db_session)
    page = client.get("/tickets", params={"status": "with_msp", "sort": "breach"}).text
    assert '<option value="">Any status</option>' in page
    assert '<option value="active">Not finished</option>' in page
    assert '<option value="with_msp" selected>With MSP</option>' in page
    assert '<option value="breach" selected>Time to breach</option>' in page
    assert '<option value="">Queue order</option>' in page


# ---- Sort by time to breach ---------------------------------------------------------------------------------


def clocked(db_session, desk, category, subject, due, **fields):
    return triaged(db_session, desk, category, subject, resolution_due_at=due, first_responded_at=UTC_NOW, created_at=UTC_NOW - timedelta(days=9), **fields)


def make_queue(db_session, desk, category):
    clocked(db_session, desk, category, "E-later", UTC_NOW + timedelta(days=5))
    clocked(db_session, desk, category, "C-overdue-less", UTC_NOW - timedelta(hours=1))
    clocked(db_session, desk, category, "B-overdue-most", UTC_NOW - timedelta(days=3))
    clocked(db_session, desk, category, "D-soon", UTC_NOW + timedelta(hours=1))
    clocked(db_session, desk, category, "F-paused", UTC_NOW + timedelta(minutes=5), sla_paused_at=UTC_NOW - timedelta(hours=1), status=TicketStatus.AWAITING_USER)
    clocked(db_session, desk, category, "H-resolved", UTC_NOW - timedelta(days=8), status=TicketStatus.RESOLVED, resolved_at=UTC_NOW)
    triaged(db_session, desk, category, "G-no-target", priority=None, resolution_due_at=None)
    fresh = new_ticket(db_session, desk)  # untriaged, with a first-response clock running
    fresh.subject, fresh.first_response_due_at = "U-untriaged", UTC_NOW + timedelta(hours=2)
    db_session.add(fresh)
    db_session.commit()


def test_sorted_by_breach_the_most_overdue_comes_first_then_soonest_then_no_clock(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    make_queue(db_session, desk, category)

    order = subjects(client.get("/tickets", params={"sort": "breach"}).text)
    assert order[:5] == ["B-overdue-most", "C-overdue-less", "D-soon", "U-untriaged", "E-later"]  # untriaged is not pinned
    assert set(order[5:]) == {"F-paused", "G-no-target", "H-resolved"}  # no running clock: after every running one


def test_the_default_order_is_unchanged_and_still_pins_untriaged(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    make_queue(db_session, desk, category)

    for params in ({}, {"sort": ""}):
        order = subjects(client.get("/tickets", params=params).text)
        assert order[0] == "U-untriaged"
        assert order[1:5] == ["B-overdue-most", "C-overdue-less", "D-soon", "E-later"]


def test_the_sort_agrees_with_the_chip_on_a_due_date_lead_time(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    # A Due date counts as due some days early (the lead time), as the chip does: this one sorts ahead of a plain
    # deadline that is nearer in calendar terms.
    clocked(db_session, desk, category, "Plain-3d", UTC_NOW + timedelta(days=3))
    # Default lead time is one day, so the dated one counts as due in 2.5 days against the plain one's 3.
    clocked(db_session, desk, category, "Dated-3.5d", UTC_NOW + timedelta(days=3, hours=12), due_on=(UTC_NOW + timedelta(days=3, hours=12)).date())
    order = subjects(client.get("/tickets", params={"sort": "breach"}).text)
    assert order.index("Dated-3.5d") < order.index("Plain-3d")


def test_the_sort_and_status_survive_pagination_and_the_tabs(client, db_session, monkeypatch):
    real = pagination.paginate

    def two_to_a_page(*args):  # Page.size is bound when the class is defined, so set it after
        page = real(*args)
        page.size = 2
        return page

    monkeypatch.setattr(pagination, "PAGE_SIZE", 2)
    monkeypatch.setattr(tickets_router, "paginate", two_to_a_page)
    desk, category, _ = desk_agent(client, db_session)
    for n in range(5):
        clocked(db_session, desk, category, f"P-{n}", UTC_NOW + timedelta(days=n + 1), status=TicketStatus.WITH_MSP)

    page = client.get("/tickets", params={"sort": "breach", "status": "with_msp"}).text
    assert subjects(page) == ["P-0", "P-1"]
    link = re.search(r'<a href="([^"]*)">Next', page).group(1).replace("&amp;", "&")
    assert "sort=breach" in link and "status=with_msp" in link and "page=2" in link
    assert subjects(client.get("/tickets" + link).text) == ["P-2", "P-3"]
    tab = re.search(r'href="([^"]*)"[^>]*>My queue', page).group(1)
    assert "sort=breach" in tab and "status" not in tab  # the tabs keep the sort and clear the status


# ---- Colours and labels -------------------------------------------------------------------------------------


def test_each_status_renders_its_label_and_tone_on_the_queue_and_the_ticket_header(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    tickets = one_per_status(db_session, desk, category)

    queue = client.get("/tickets").text
    for status, ticket in tickets.items():
        chip = f'<span class="chip t-{STATUS_TONES[status]}">{STATUS_LABELS[status]}</span>'
        assert chip in queue
        header = client.get(f"/tickets/{ticket.number}").text.split('class="tp-meta"')[1].split("</div>")[0]
        assert chip in header
    assert "With MSP" in queue and "With msp" not in queue and "Awaiting approval" in queue


def test_the_service_level_box_and_linked_lists_use_the_same_chip(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    parent = triaged(db_session, desk, category, "Parent", status=TicketStatus.IN_PROGRESS)
    triaged(db_session, desk, category, "Child", status=TicketStatus.WITH_SUPPLIER, split_from_id=parent.id)
    page = client.get(f"/tickets/{parent.number}").text
    assert page.count('<span class="chip t-blue">In progress</span>') >= 2  # the header and the Service level box
    assert '<span class="chip t-amber">With supplier</span>' in page


def test_team_activity_uses_the_same_mapping(client, db_session):
    from app.team_activity import status_chip

    assert status_chip("with_msp") == ("With MSP", "amber")
    assert status_chip("in_progress") == ("In progress", "blue")
    assert status_chip(None) == ("None", "gray")

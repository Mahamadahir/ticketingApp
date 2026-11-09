"""The agent queue page (9B): the header and tab counts, the wording of the time-to-breach cell, the Needs triage chip and
its pinning, the empty states and the one search box. Who sees what is still decided in app/visibility.py."""

import re
from datetime import date, datetime, timedelta, timezone

from app.models import Ticket, TicketSource, TicketStatus, UserRole
from app.sla import queue_sla, sla_view
from tests.conftest import create_category, create_team, set_role
from tests.test_triage import agent, new_ticket

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def ticket_with(**fields) -> Ticket:
    """An in-memory ticket for the wording tests; a priority and a creation time unless the test says otherwise."""
    base = dict(
        subject="x", requester_email="r@example.com", source=TicketSource.EMAIL, status=TicketStatus.OPEN,
        priority="p3", triaged_at=NOW - timedelta(hours=5), created_at=NOW - timedelta(hours=4), first_responded_at=NOW,
    )
    return Ticket(**{**base, **fields})


def words(**fields):
    ticket = ticket_with(**fields)
    cell = queue_sla(ticket, sla_view(ticket, NOW, 25), NOW)
    return cell.tone, cell.text


# ---- The wording, one state at a time -------------------------------------------------------------------------


def test_breached_names_how_long_ago():
    assert words(resolution_due_at=NOW - timedelta(minutes=80)) == ("red", "Breached 1h 20m ago")


def test_first_response_counts_down_to_the_first_response():
    ticket = dict(first_responded_at=None, first_response_due_at=NOW + timedelta(minutes=42), created_at=NOW - timedelta(hours=3))
    assert words(**ticket) == ("amber", "42m to first response")  # inside the last 25% of its window
    assert words(**{**ticket, "first_response_due_at": NOW + timedelta(hours=9)}) == ("green", "9h 0m to first response")


def test_resolution_counts_down_and_a_due_date_is_named():
    assert words(resolution_due_at=NOW + timedelta(days=1, hours=4), created_at=NOW - timedelta(hours=1)) == ("green", "1d 4h left")
    tone, text = words(resolution_due_at=NOW + timedelta(hours=30), due_on=date(2026, 11, 10), created_at=NOW - timedelta(hours=1))
    assert (tone, text) == ("green", "1d 6h left · due 2026-11-10")


def test_paused_says_who_is_waited_on():
    assert words(sla_paused_at=NOW - timedelta(hours=1), resolution_due_at=NOW + timedelta(hours=3)) == (
        "blue", "Paused · waiting on requester",
    )


def test_met_and_missed_for_a_finished_ticket():
    done = dict(status=TicketStatus.RESOLVED, resolved_at=NOW - timedelta(hours=1))
    assert words(**done, resolution_due_at=NOW + timedelta(hours=1)) == ("green", "Met · resolved in time")
    assert words(**done, resolution_due_at=NOW - timedelta(hours=2)) == ("red", "Missed · resolved late")


def test_no_sla_untriaged_and_backdated():
    assert words(priority=None) == ("gray", "No SLA")
    assert words(priority=None, triaged_at=None) == ("gray", "No SLA until triaged")
    assert words(is_backdated=True, resolution_due_at=NOW + timedelta(hours=3)) == ("gray", "No SLA · backdated")


# ---- The page -------------------------------------------------------------------------------------------------


def triaged(db_session, team, category, subject, **fields):
    ticket = new_ticket(db_session, team)
    ticket.subject, ticket.triaged_at, ticket.category_id = subject, NOW, category.id
    ticket.status, ticket.priority = TicketStatus.OPEN, "p2"
    for key, value in fields.items():
        setattr(ticket, key, value)
    db_session.add(ticket)
    db_session.commit()
    return ticket


def figures(page: str) -> dict:
    chips = re.findall(r'<span class="chip t-(?:red|amber|blue) queue-figure">(\d+) ([a-z ]+)</span>', page)
    tabs = re.findall(r'class="tab[^"]*"[^>]*>([^<]+?) <span class="chip t-gray">(\d+)</span>', page)
    return {**{name: int(n) for n, name in chips}, **{name: int(n) for name, n in tabs}}


def test_the_counts_include_only_tickets_the_viewer_can_see(client, db_session):
    mine, other = create_team(db_session, "Desk"), create_team(db_session, "Networks")
    category = create_category(db_session, mine, "Hardware")
    elsewhere = create_category(db_session, other, "Wi-Fi")
    me = agent(client, db_session, member_of=[mine])
    late = dict(resolution_due_at=datetime.now(timezone.utc) - timedelta(hours=1), first_responded_at=NOW)
    triaged(db_session, mine, category, "Late here", assigned_agent_id=me, **late)
    triaged(db_session, mine, category, "Not late", resolution_due_at=datetime.now(timezone.utc) + timedelta(days=3), first_responded_at=NOW)
    triaged(db_session, other, elsewhere, "Late elsewhere", **late)
    triaged(db_session, other, elsewhere, "Also elsewhere")

    seen = figures(client.get("/tickets").text)
    assert seen["breached"] == 1  # the other team's late ticket is not counted
    assert seen["All I can see"] == 2 and seen["My queue"] == 1 and seen["untriaged"] == 0


def test_at_risk_counts_a_clock_in_its_last_stretch(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    now = datetime.now(timezone.utc)
    triaged(db_session, desk, category, "Close", created_at=now - timedelta(hours=9), resolution_due_at=now + timedelta(minutes=30), first_responded_at=now)
    assert figures(client.get("/tickets").text)["at risk"] == 1


def test_an_untriaged_ticket_has_the_chip_and_stays_pinned_above_a_late_one(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    triaged(db_session, desk, category, "B-late", resolution_due_at=datetime.now(timezone.utc) - timedelta(days=2), first_responded_at=NOW)
    fresh = new_ticket(db_session, desk)
    fresh.subject = "A-fresh"
    db_session.add(fresh)
    db_session.commit()

    page = client.get("/tickets").text
    assert page.index("A-fresh") < page.index("B-late")
    assert page.count("Needs triage") == 1
    assert figures(page)["untriaged"] == 1
    assert "Breached 2d" in page


def test_the_untriaged_tab_lists_only_untriaged_and_the_sidebar_link_lands_on_it(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    triaged(db_session, desk, category, "B-done-triage")
    fresh = new_ticket(db_session, desk)
    fresh.subject = "A-fresh"
    db_session.add(fresh)
    db_session.commit()

    page = client.get("/tickets", params={"untriaged": "true"}).text
    assert "A-fresh" in page and "B-done-triage" not in page
    assert re.search(r'<a class="tab on" href="[^"]*"\s+aria-current="true">Untriaged', page)


def test_each_view_has_its_own_empty_state(client, db_session):
    agent(client, db_session)
    assert "No tickets yet." in client.get("/tickets").text
    assert "Nothing is assigned to you." in client.get("/tickets", params={"mine": "true"}).text
    assert "Nothing is waiting for triage." in client.get("/tickets", params={"untriaged": "true"}).text
    assert "No tickets match." in client.get("/tickets", params={"q": "nothing"}).text


def test_the_page_has_no_second_search_box_and_the_top_bar_keeps_the_search(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    triaged(db_session, desk, category, "Router down")

    page = client.get("/tickets", params={"q": "router"}).text
    assert page.count('type="search"') == 1
    assert 'name="q" maxlength="200" value="router"' in page
    assert "Router down" in page and "1 ticket you can open match" in page


def test_assignee_and_channel_and_team_activity(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    triaged(db_session, desk, category, "Someone's")
    page = client.get("/tickets").text
    assert '<span class="muted">Unassigned</span>' in page
    assert 'href="/tickets/activity"' in page
    assert "Desk &middot; Hardware" in page or "Desk · Hardware" in page
    assert "Channel" in page and '<option value="email">Email</option>' in page


def test_a_blank_filter_means_any_and_is_not_an_error(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    triaged(db_session, desk, category, "Router down")

    for params in ({"team_id": "", "source": "email"}, {"team_id": str(desk.id), "source": ""}, {"team_id": "", "source": ""}):
        response = client.get("/tickets", params=params)
        assert response.status_code == 200 and "Router down" in response.text
    assert client.get("/tickets", params={"source": "carrier-pigeon"}).status_code == 422


def test_a_super_admins_name_shows_as_assignee_and_a_missing_category_is_not_called_untriaged(client, db_session):
    desk = create_team(db_session, "Desk")
    category = create_category(db_session, desk, "Hardware")
    admin_id = agent(client, db_session, member_of=[desk])
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    triaged(db_session, desk, category, "Mine", assigned_agent_id=admin_id)
    triaged(db_session, desk, category, "No cat", category_id=None)

    page = client.get("/tickets").text
    assert page.count('<span class="muted">Unassigned</span>') == 1  # only "No cat"
    assert "Desk &middot; No category" in page and "Not triaged" not in page

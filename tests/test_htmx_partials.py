"""Forms on the ticket page swap their response into <main>. If the response is the whole page,
the header and nav appear twice. Every action must return only the page content for htmx."""

import pytest
from sqlmodel import select

from app.models import Ticket
from tests.conftest import create_category
from tests.test_sla import triaged_ticket
from tests.test_triage import agent, new_ticket

HX = {"HX-Request": "true"}
LAYOUT = "IT Support Ticketing"


def assert_partial(response):
    assert response.status_code == 200, response.text
    assert LAYOUT not in response.text
    assert "<nav>" not in response.text
    assert "<html" not in response.text.lower()
    assert "back to tickets" in response.text  # still the ticket content


def test_status_change_returns_the_content_only(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    assert_partial(client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}, headers=HX))


def test_a_normal_request_still_gets_the_whole_page(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    page = client.get(f"/tickets/{ticket.number}")
    assert page.text.count("<nav>") == 1 and LAYOUT in page.text
    full = client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"})
    assert full.text.count("<nav>") == 1


def test_the_ticket_page_actions_return_the_content_only(client, db_session):
    ticket, team = triaged_ticket(client, db_session)
    n = ticket.number
    category = create_category(db_session, team, "Software")
    actions = [
        ("status", {"status": "in_progress"}),
        ("note", {"body": "an internal note"}),
        ("priority", {"impact": "low", "urgency": "low"}),
        ("assign", {}),
        ("followers", {"email": "cc@example.com"}),
        ("link", {"parent_number": "9999"}),
        ("reassign", {"team_id": team.id}),
        ("split", {"subject_0": "Part", "category_id_0": category.id}),
    ]
    for path, data in actions:
        response = client.post(f"/tickets/{n}/{path}", data=data, headers=HX)
        if response.status_code == 200:
            assert_partial(response)
        else:
            # A refusal is a message aimed at #flash, never a page.
            assert response.headers.get("HX-Retarget") == "#flash", (path, response.text)
            assert "<nav>" not in response.text


def test_triage_returns_the_content_only(client, db_session):
    team_ticket = new_ticket(db_session)
    from tests.conftest import create_team

    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])
    ticket = db_session.exec(select(Ticket).where(Ticket.id == team_ticket.id)).one()
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "high", "urgency": "high", "category_id": category.id},
        headers=HX,
    )
    assert response.status_code == 200
    assert "<nav>" not in response.text and LAYOUT not in response.text


def test_portal_actions_return_the_content_only(client, db_session):
    from tests.test_portal import create_ticket_for
    from tests.conftest import login_as

    ticket = create_ticket_for(db_session, "me@example.com")
    login_as(client, "me@example.com")
    for path, data in [("comment", {"body": "Still broken"}), ("followers", {"email": "cc@example.com"})]:
        response = client.post(f"/portal/{ticket.number}/{path}", data=data, headers=HX)
        assert response.status_code == 200, (path, response.text)
        assert "<nav>" not in response.text and LAYOUT not in response.text, path

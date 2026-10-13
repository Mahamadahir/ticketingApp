"""Routes and links carry the ticket number, never the internal id. Every test here
gives its ticket a number that differs from its id, so a route that used the wrong one
would fail (in the other tests the two happen to be equal)."""

from datetime import datetime, timezone

from app.models import TicketStatus, UserRole
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_triage import agent, new_ticket

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
NUMBER = 4242


def numbered(db_session, team, **fields):
    """A triaged ticket whose number (4242) is nothing like its id."""
    category = create_category(db_session, team, "Hardware")
    ticket = new_ticket(db_session, team)
    ticket.number = NUMBER
    ticket.triaged_at, ticket.status, ticket.category_id = NOW, TicketStatus.OPEN, category.id
    for key, value in fields.items():
        setattr(ticket, key, value)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.id != ticket.number
    return ticket


def test_the_agent_page_opens_by_number_and_not_by_the_internal_id(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = numbered(db_session, desk)
    agent(client, db_session, member_of=[desk])

    assert client.get(f"/tickets/{NUMBER}").status_code == 200
    assert client.get(f"/tickets/{ticket.id}").status_code == 404  # the id means nothing in a URL


def test_every_agent_action_is_addressed_by_number(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = numbered(db_session, desk)
    me = agent(client, db_session, member_of=[desk])

    assert client.post(f"/tickets/{NUMBER}/note", data={"body": "n"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/assign").status_code == 200
    assert client.post(f"/tickets/{NUMBER}/status", data={"status": "in_progress"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/priority", data={"impact": "low", "urgency": "low"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/email", data={"body": "hello"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/followers", data={"email": "cc@example.com"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/escalate", data={"reason": "needs more"}).status_code == 200
    assert client.post(f"/tickets/{NUMBER}/reassign", data={"team_id": desk.id, "agent_id": me}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == me and ticket.is_escalated

    valid_forms = {
        "note": {"body": "n"}, "assign": {}, "status": {"status": "open"},
        "priority": {"impact": "low", "urgency": "low"}, "email": {"body": "hello"},
        "escalate": {"reason": "x"}, "reassign": {"team_id": desk.id}, "link": {"parent_number": "1"},
        "unlink": {}, "followers": {"email": "x@example.com"},
    }
    for action, form in valid_forms.items():  # the id is not a number of any ticket here
        assert client.post(f"/tickets/{ticket.id}/{action}", data=form).status_code == 404, action


def test_the_pages_link_to_the_number(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = numbered(db_session, desk)
    agent(client, db_session, member_of=[desk])

    listing = client.get("/tickets").text
    detail = client.get(f"/tickets/{NUMBER}").text

    assert f'href="/tickets/{NUMBER}"' in listing and f'href="/tickets/{ticket.id}"' not in listing
    assert f'hx-post="/tickets/{NUMBER}/note"' in detail
    assert f'/tickets/{ticket.id}/' not in detail


def test_an_unknown_or_out_of_range_number_is_a_404_not_a_server_error(client, db_session):
    desk = create_team(db_session, "Service Desk")
    numbered(db_session, desk)
    agent(client, db_session, member_of=[desk])

    for number in (0, 99999, 999_999_999, 1_000_000_000, 99_999_999_999_999):
        assert client.get(f"/tickets/{number}").status_code == 404, number
        assert client.post(f"/tickets/{number}/note", data={"body": "x"}).status_code == 404, number


def test_the_shareable_link_and_the_agent_page_agree(client, db_session):
    desk = create_team(db_session, "Service Desk")
    numbered(db_session, desk)
    agent(client, db_session, member_of=[desk])

    assert client.get(f"/t/{NUMBER}").text == client.get(f"/tickets/{NUMBER}").text


def test_the_portal_is_addressed_by_number_too(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = numbered(db_session, desk, requester_email="jane@example.com")
    login_as(client, "jane@example.com")

    assert client.get(f"/portal/{NUMBER}").status_code == 200
    assert client.get(f"/portal/{ticket.id}").status_code == 404
    assert client.post(f"/portal/{NUMBER}/comment", data={"body": "hi"}).status_code in (200, 303)
    assert client.post(f"/portal/{NUMBER}/followers", data={"email": "f@example.com"}).status_code in (200, 303)

    listing = client.get("/portal").text
    assert f'href="/portal/{NUMBER}"' in listing and f'href="/portal/{ticket.id}"' not in listing
    assert f'/portal/{NUMBER}/comment' in client.get(f"/portal/{NUMBER}").text
    assert client.get("/portal/99999999999").status_code == 404


def test_the_category_review_is_addressed_by_number(client, db_session):
    desk = create_team(db_session, "Service Desk")
    working = create_category(db_session, desk, "Software")
    ticket = numbered(
        db_session, desk, status=TicketStatus.RESOLVED, resolved_at=NOW, needs_category_review=True
    )
    boss = login_as(client, "boss@example.com")
    set_role(db_session, boss, UserRole.SUPER_ADMIN)

    page = client.get("/admin/category-review").text
    assert f'action="/admin/category-review/{NUMBER}"' in page and f'/{ticket.id}"' not in page
    response = client.post(
        f"/admin/category-review/{NUMBER}", data={"category_id": working.id}, follow_redirects=False
    )
    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.needs_category_review is False and ticket.category_id == working.id
    assert client.post(f"/admin/category-review/{ticket.id}", data={"category_id": working.id}).status_code == 404

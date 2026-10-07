from app.forms import MAX_MESSAGE_LENGTH
from app.models import Ticket, UserRole
from app.pagination import PAGE_SIZE
from tests.conftest import create_team, login_as, set_role


def add_tickets(db_session, count: int, **fields) -> None:
    for number in range(count):
        db_session.add(Ticket(subject=f"Ticket {number}", **fields))
    db_session.commit()


def test_agent_queue_is_paginated_and_keeps_filters(client, db_session):
    team = create_team(db_session, "Service Desk")
    login_as(client, "admin@example.com")
    add_tickets(db_session, PAGE_SIZE + 5, requester_email="user@example.com", team_id=team.id)

    first_page = client.get(f"/tickets?team_id={team.id}")
    assert first_page.text.count('href="/tickets/') == PAGE_SIZE
    assert f"team_id={team.id}&amp;page=2" in first_page.text

    second_page = client.get(f"/tickets?team_id={team.id}&page=2")
    assert second_page.text.count('href="/tickets/') == 5


def test_portal_list_is_paginated(client, db_session):
    login_as(client, "requester@example.com")
    add_tickets(db_session, PAGE_SIZE + 1, requester_email="requester@example.com")

    assert client.get("/portal").text.count('class="ticket-card"') == PAGE_SIZE
    assert client.get("/portal?page=2").text.count('class="ticket-card"') == 1


def test_page_number_below_one_shows_first_page(client, db_session):
    login_as(client, "requester@example.com")
    add_tickets(db_session, 3, requester_email="requester@example.com")

    assert client.get("/portal?page=0").text.count('class="ticket-card"') == 3


def test_agent_reply_over_the_length_cap_is_rejected(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    add_tickets(db_session, 1, requester_email="user@example.com", team_id=team.id)

    response = client.post("/tickets/1/reply", data={"body": "x" * (MAX_MESSAGE_LENGTH + 1)})
    assert response.status_code == 422


def test_portal_comment_over_the_length_cap_is_rejected(client, db_session):
    login_as(client, "requester@example.com")
    add_tickets(db_session, 1, requester_email="requester@example.com")

    response = client.post("/portal/1/comment", data={"body": "x" * (MAX_MESSAGE_LENGTH + 1)})
    assert response.status_code == 422

    assert client.post("/portal/1/comment", data={"body": "x" * MAX_MESSAGE_LENGTH}).status_code == 200

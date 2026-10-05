from app.models import Ticket, TicketStatus
from tests.conftest import login_as


def create_ticket_for(db_session, requester_email, subject="Can't print"):
    ticket = Ticket(subject=subject, requester_email=requester_email)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def test_end_user_sees_only_own_tickets(client, db_session):
    create_ticket_for(db_session, "me@example.com", subject="My ticket")
    create_ticket_for(db_session, "someone-else@example.com", subject="Not mine")

    login_as(client, "me@example.com")

    response = client.get("/portal")
    assert "My ticket" in response.text
    assert "Not mine" not in response.text


def test_end_user_cannot_view_others_ticket(client, db_session):
    other_ticket = create_ticket_for(db_session, "someone-else@example.com")

    login_as(client, "me@example.com")

    response = client.get(f"/portal/{other_ticket.id}")
    assert response.status_code == 404


def test_end_user_comment_does_not_change_status(client, db_session):
    ticket = create_ticket_for(db_session, "me@example.com")
    login_as(client, "me@example.com")

    client.post(f"/portal/{ticket.id}/comment", data={"body": "Still broken, please help"})

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN
    assert len(ticket.messages) == 1
    assert ticket.messages[0].body == "Still broken, please help"

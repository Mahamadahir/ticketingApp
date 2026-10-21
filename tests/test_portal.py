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

    response = client.get(f"/portal/{other_ticket.number}")
    assert response.status_code == 404


def test_end_user_comment_does_not_change_status(client, db_session):
    ticket = create_ticket_for(db_session, "me@example.com")
    login_as(client, "me@example.com")

    client.post(f"/portal/{ticket.number}/comment", data={"body": "Still broken, please help"})

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.NEW
    assert len(ticket.messages) == 1
    assert ticket.messages[0].body == "Still broken, please help"


# --- matching the signed-in person to tickets: lower-cased, through app/identity.py ---

from app import identity
from app.models import Message, MessageDirection, MessageVisibility, TicketFollower


def test_a_ticket_stored_with_different_capitalisation_is_listed_and_opens(client, db_session):
    ticket = create_ticket_for(db_session, "Me@Example.COM", subject="Mixed case")
    login_as(client, "me@example.com")

    listing = client.get("/portal").text
    assert "Mixed case" in listing and "(following)" not in listing
    assert client.get(f"/portal/{ticket.number}").status_code == 200


def test_a_follower_sees_the_followed_ticket_in_the_list_but_never_internal_notes(client, db_session):
    own = create_ticket_for(db_session, "boss@example.org", subject="Boss own")
    other = create_ticket_for(db_session, "me@example.com", subject="Followed one")
    db_session.add(TicketFollower(ticket_id=other.id, email="boss@example.org", added_by_email="me@example.com"))
    db_session.add(Message(
        ticket_id=other.id, direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.AGENTS_ONLY,
        sender="agent", body="SECRET internal note",
    ))
    db_session.commit()
    login_as(client, "Boss@Example.org")

    listing = client.get("/portal").text
    assert "Followed one" in listing and "Boss own" in listing
    assert listing.index("Boss own") < listing.index("Followed one")  # their own come first
    assert listing.count("(following)") == 1
    page = client.get(f"/portal/{other.number}")
    assert page.status_code == 200 and "SECRET internal note" not in page.text


def test_someone_else_sees_neither_in_the_list_nor_by_link(client, db_session):
    ticket = create_ticket_for(db_session, "me@example.com", subject="Private matter")
    db_session.add(TicketFollower(ticket_id=ticket.id, email="boss@example.org", added_by_email="me@example.com"))
    db_session.commit()
    login_as(client, "stranger@example.org")

    assert "Private matter" not in client.get("/portal").text
    assert client.get(f"/portal/{ticket.number}").status_code == 404
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "hi"}).status_code == 404


def test_every_portal_path_goes_through_the_requester_emails_helper(client, db_session, monkeypatch):
    first = create_ticket_for(db_session, "me@example.com", subject="Under first address")
    second = create_ticket_for(db_session, "me@othercompany.com", subject="Under second address")
    login_as(client, "me@example.com")
    assert client.get(f"/portal/{second.number}").status_code == 404

    monkeypatch.setattr(identity, "requester_emails", lambda user: {"me@example.com", "me@othercompany.com"})
    listing = client.get("/portal").text
    assert "Under first address" in listing and "Under second address" in listing
    r = client.get(f"/portal/{second.number}")
    assert r.status_code == 200
    assert client.post(f"/portal/{second.number}/comment", data={"body": "via second"}).status_code == 200
    assert client.post(f"/portal/{second.number}/followers", data={"email": "pal@example.org"}).status_code in (200, 303)
    assert client.get(f"/portal/{first.number}").status_code == 200


def test_tickets_are_stored_with_a_lower_case_requester(db_session):
    from app.models import TicketSource
    from app.ticket_service import create_ticket

    ticket = create_ticket(db_session, subject="x", requester_email="  Mixed@Example.COM ", source=TicketSource.PORTAL)
    assert ticket.requester_email == "mixed@example.com"

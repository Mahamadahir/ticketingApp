"""Forms use htmx, which ignores 4xx responses unless told otherwise. A refused
action must come back as a message the page can show, not as nothing."""

from app.models import Ticket, TicketStatus, UserRole
from tests.conftest import create_team, login_as, set_role

HTMX = {"HX-Request": "true"}


def make_ticket(db_session, team_id, status=TicketStatus.OPEN):
    ticket = Ticket(subject="Printer", requester_email="me@example.com", team_id=team_id, status=status)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_for(client, db_session, team):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)


def test_a_refused_form_comes_back_as_a_message_aimed_at_the_flash_area(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status=TicketStatus.NEW)
    agent_for(client, db_session, team)

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"}, headers=HTMX)

    assert response.status_code == 400
    assert response.headers["HX-Retarget"] == "#flash"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert "Triage the ticket before emailing the requester" in response.text
    assert response.text.startswith('<p class="error">')


def test_the_same_refusal_without_htmx_is_still_the_normal_json_error(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status=TicketStatus.NEW)
    agent_for(client, db_session, team)

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Triage the ticket before emailing the requester"}
    assert "HX-Retarget" not in response.headers


def test_a_bad_follower_address_and_a_validation_error_show_messages(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    bad_address = client.post(f"/tickets/{ticket.number}/followers", data={"email": "nope"}, headers=HTMX)
    assert bad_address.status_code == 400 and "not a valid email address" in bad_address.text

    missing = client.post(f"/tickets/{ticket.number}/note", data={}, headers=HTMX)
    assert missing.status_code == 422
    assert missing.headers["HX-Retarget"] == "#flash" and "Check what you entered" in missing.text


def test_no_access_to_a_ticket_is_a_message_for_htmx_and_the_page_otherwise(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    ticket = make_ticket(db_session, other.id)
    agent_for(client, db_session, mine)  # the ticket is Open in another team, so not theirs

    ajax = client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"}, headers=HTMX)
    assert ajax.status_code == 403 and ajax.headers["HX-Retarget"] == "#flash"
    assert "have access to do that" in ajax.text  # the apostrophe in "don't" is escaped

    page = client.get(f"/tickets/{ticket.number}")
    assert page.status_code == 403 and "No access" in page.text and "HX-Retarget" not in page.headers


def test_error_messages_are_escaped(client):
    """The message is put into HTML; whatever it contains must not become markup."""
    from app.main import _htmx_error

    response = _htmx_error('<script>alert("x")</script>', 400)
    assert "<script>" not in response.body.decode()
    assert "&lt;script&gt;" in response.body.decode()


def test_the_page_loads_the_script_that_lets_error_messages_through(client):
    login_as(client, "me@example.com")
    page = client.get("/portal").text
    assert 'id="flash"' in page and "htmx:beforeSwap" in page and "HX-Retarget" in page
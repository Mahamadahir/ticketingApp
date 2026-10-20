"""Status follows the conversation: emailing the user puts the ticket in Awaiting
user by itself, and a reply from the requester or a follower hands it back."""

import pytest
from sqlmodel import select

from app.models import AuditLog, Ticket, TicketEvent, TicketStatus, UserRole
from tests.conftest import create_team, login_as, set_role


def make_ticket(db_session, team_id, status=TicketStatus.OPEN, requester="me@example.com"):
    ticket = Ticket(subject="Printer", requester_email=requester, team_id=team_id, status=status)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_for(client, db_session, team):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)


def status_of(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id).status


def status_events(db_session, ticket):
    db_session.expire_all()
    return [
        (e.from_value, e.to_value, e.actor_email)
        for e in db_session.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "status_changed")
        )
    ]


@pytest.mark.parametrize("start", [TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
def test_emailing_the_user_puts_the_ticket_in_awaiting_user_with_no_choice(client, db_session, start):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, start)
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "After sending" not in page  # there is nothing for the agent to choose or tick

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer is it?"}).status_code == 200

    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER
    assert status_events(db_session, ticket) == [(start.value, "awaiting_user", "agent@example.com")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [a.detail for a in audit] == [f"{start.value} -> awaiting_user"]


@pytest.mark.parametrize("status", [TicketStatus.WITH_SUPPLIER, TicketStatus.WITH_MSP, TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_emailing_leaves_statuses_that_are_not_waiting_on_us_alone(client, db_session, status):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status)
    agent_for(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "An update"}).status_code == 200

    assert status_of(db_session, ticket) == status
    assert status_events(db_session, ticket) == []


def test_a_second_email_while_awaiting_the_user_changes_nothing_more(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "First"})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Second"})

    assert len(status_events(db_session, ticket)) == 1


def test_a_refused_email_does_not_change_the_status(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    refused = client.post(
        f"/tickets/{ticket.number}/email", data={"body": "x", "cc_form": "1", "cc": "stranger@example.org"}
    )

    assert refused.status_code == 400
    assert status_of(db_session, ticket) == TicketStatus.OPEN


def test_the_requester_replying_hands_the_ticket_back(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_USER)
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "It is the one on floor 2"}).status_code == 200

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert status_events(db_session, ticket) == [("awaiting_user", "in_progress", "me@example.com")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [(a.actor_email, a.detail) for a in audit] == [("me@example.com", "awaiting_user -> in_progress")]


def test_a_follower_replying_also_hands_it_back(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_USER)
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    client.post("/auth/logout")
    login_as(client, "boss@example.org")

    client.post(f"/portal/{ticket.number}/comment", data={"body": "Boss here, it is floor 2"})

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert status_events(db_session, ticket) == [("awaiting_user", "in_progress", "boss@example.org")]


@pytest.mark.parametrize(
    "status",
    [TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WITH_SUPPLIER, TicketStatus.WITH_MSP, TicketStatus.RESOLVED, TicketStatus.CLOSED],
)
def test_a_reply_only_changes_a_ticket_that_is_awaiting_the_user(client, db_session, status):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status)
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Hello?"}).status_code == 200

    assert status_of(db_session, ticket) == status
    assert status_events(db_session, ticket) == []


def test_the_whole_conversation_moves_the_status_back_and_forth(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer?"})
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Floor 2"})
    client.post("/auth/logout")
    login_as(client, "agent@example.com")  # the same agent, already in the team
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Thanks, trying a fix"})

    assert [(f, t) for f, t, _ in status_events(db_session, ticket)] == [
        ("open", "awaiting_user"),
        ("awaiting_user", "in_progress"),
        ("in_progress", "awaiting_user"),
    ]
    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER


def test_an_agent_can_still_set_the_status_by_hand(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer?"})

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
"""The Scheduled status: work planned for a later date (SRP-2). It reuses SRP-1's Required by date."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app import sla
from app.messages import STATUS_CHOICES, default_status_after_email, status_choices
from app.models import Message, TicketStatus
from app.splitting import derived_status
from tests.conftest import create_team, login_as
from tests.test_sla import NOW as SLA_NOW, clocked, sla_view
from tests.test_split_family import as_requester, family, set_to, status_of as split_status_of
from tests.test_status_follows_conversation import agent_for, make_ticket, status_events, status_of

SCHEDULED = TicketStatus.SCHEDULED


def triaged(db_session, team, status=TicketStatus.IN_PROGRESS):
    ticket = make_ticket(db_session, team.id, status)
    ticket.triaged_at = SLA_NOW
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_the_value_and_its_label():
    assert SCHEDULED.value == "scheduled"


def test_the_database_enum_accepts_the_value(db_session):
    labels = db_session.execute(text("SELECT unnest(enum_range(NULL::ticketstatus))::text")).scalars().all()
    assert "SCHEDULED" in labels


def test_an_agent_can_reach_and_leave_the_status_by_hand(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, team)
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'value="scheduled"' in page and ">scheduled<" in page
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled"}).status_code == 200
    assert status_of(db_session, ticket) == SCHEDULED
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}).status_code == 200
    assert [(f, t) for f, t, _ in status_events(db_session, ticket)] == [
        ("in_progress", "scheduled"),
        ("scheduled", "in_progress"),
    ]


def test_the_dropdown_offers_it_and_a_note_can_set_it(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, team)
    assert SCHEDULED in STATUS_CHOICES and SCHEDULED in status_choices(db_session, ticket)
    agent_for(client, db_session, team)
    assert client.get(f"/tickets/{ticket.number}").text.count('<option value="scheduled"') >= 1

    note = client.post(f"/tickets/{ticket.number}/note", data={"body": "Booked for Friday", "next_status": "scheduled"})
    assert note.status_code == 200
    assert status_of(db_session, ticket) == SCHEDULED
    email = client.post(f"/tickets/{ticket.number}/email", data={"body": "Booked", "next_status": "in_progress"})
    assert email.status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS


def test_emailing_the_user_leaves_a_scheduled_ticket_alone(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SCHEDULED)
    agent_for(client, db_session, team)
    assert default_status_after_email(ticket) == SCHEDULED
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Still on for Friday"}).status_code == 200
    assert status_of(db_session, ticket) == SCHEDULED


def test_a_requester_reply_changes_nothing(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SCHEDULED)
    login_as(client, "me@example.com")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Any news?"}).status_code == 200
    assert status_of(db_session, ticket) == SCHEDULED
    assert status_events(db_session, ticket) == []
    assert db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).first() is not None


def test_the_portal_shows_the_label(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SCHEDULED)
    login_as(client, "me@example.com")
    for url in ("/portal", f"/portal/{ticket.number}"):
        assert "scheduled" in client.get(url).text


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([SCHEDULED, SCHEDULED], SCHEDULED),
        ([SCHEDULED, TicketStatus.RESOLVED], SCHEDULED),
        ([SCHEDULED, TicketStatus.OPEN], TicketStatus.IN_PROGRESS),
        ([SCHEDULED, TicketStatus.WITH_SUPPLIER], TicketStatus.IN_PROGRESS),
    ],
)
def test_derived_status_with_a_scheduled_part(statuses, expected):
    assert derived_status(statuses) == expected


def test_the_original_follows_a_part_that_is_scheduled(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    set_to(client, parts[0], TicketStatus.RESOLVED)
    set_to(client, parts[1], SCHEDULED)
    assert split_status_of(db_session, original) == SCHEDULED


def test_a_requester_reply_on_a_split_original_leaves_a_scheduled_part_alone(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    set_to(client, parts[0], SCHEDULED)
    as_requester(client)
    client.post(f"/portal/{original.number}/comment", data={"body": "Hello?"})
    assert split_status_of(db_session, parts[0]) == SCHEDULED


def test_the_clock_pauses_while_scheduled_and_resumes_after(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.IN_PROGRESS, SCHEDULED, SLA_NOW)
    ticket.status = SCHEDULED
    assert ticket.sla_paused_at is not None
    view = sla_view(ticket, SLA_NOW + timedelta(days=30), 25)
    assert view.state == "paused" and "scheduled" in view.text
    sla.on_status_change(db_session, ticket, SCHEDULED, TicketStatus.IN_PROGRESS, SLA_NOW + timedelta(hours=5))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 5 * 3600


def test_moving_between_awaiting_user_and_scheduled_stays_paused(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, SLA_NOW)
    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, SCHEDULED, SLA_NOW + timedelta(hours=2))
    assert ticket.sla_paused_at is not None and ticket.sla_paused_seconds == 0
    sla.on_status_change(db_session, ticket, SCHEDULED, TicketStatus.IN_PROGRESS, SLA_NOW + timedelta(hours=3))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 3 * 3600


def test_a_scheduled_ticket_with_a_required_by_date_keeps_running(db_session):
    ticket = clocked()
    ticket.required_by = (SLA_NOW + timedelta(days=3)).date()
    ticket.resolution_due_at = sla.required_by_deadline(ticket.required_by)
    sla.on_status_change(db_session, ticket, TicketStatus.IN_PROGRESS, SCHEDULED, SLA_NOW)
    ticket.status = SCHEDULED
    assert sla_view(ticket, SLA_NOW, 25).state != "paused"
    ticket.first_responded_at = SLA_NOW
    assert sla_view(ticket, SLA_NOW + timedelta(days=10), 25).state == "breached"  # the date still counts

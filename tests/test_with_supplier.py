"""The With supplier status: waiting on an outside supplier or vendor (not the MSP)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import select

from app import sla
from app.models import Message, Ticket, TicketStatus
from app.splitting import derived_status
from tests.conftest import create_team, login_as
from tests.test_sla import NOW as SLA_NOW, clocked, sla_view
from tests.test_split_family import as_requester, family, set_to, status_of as split_status_of
from tests.test_status_follows_conversation import agent_for, make_ticket, status_events, status_of

SUPPLIER = TicketStatus.WITH_SUPPLIER
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def test_the_value_and_its_label():
    assert SUPPLIER.value == "with_supplier"
    assert SUPPLIER.value.replace("_", " ") == "with supplier"


def test_the_database_enum_accepts_the_value(db_session):
    labels = db_session.execute(text("SELECT unnest(enum_range(NULL::ticketstatus))::text")).scalars().all()
    assert "WITH_SUPPLIER" in labels


def test_an_agent_can_reach_and_leave_the_status_by_hand(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.IN_PROGRESS)
    ticket.triaged_at = NOW
    db_session.add(ticket)
    db_session.commit()
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'value="with_supplier"' in page and ">with supplier<" in page

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "with_supplier"}).status_code == 200
    assert status_of(db_session, ticket) == SUPPLIER
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert [(f, t) for f, t, _ in status_events(db_session, ticket)] == [
        ("in_progress", "with_supplier"),
        ("with_supplier", "in_progress"),
    ]


def test_it_is_open_not_finished(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SUPPLIER)
    agent_for(client, db_session, team)
    assert db_session.get(Ticket, ticket.id).resolved_at is None
    assert "With supplier" in client.get("/tickets").text  # the queue capitalises the status chip


def test_emailing_the_user_leaves_a_ticket_with_the_supplier_alone(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SUPPLIER)
    agent_for(client, db_session, team)
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Still waiting on them"}).status_code == 200
    assert status_of(db_session, ticket) == SUPPLIER


def test_a_requester_reply_changes_nothing_and_the_agent_decides(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SUPPLIER)
    login_as(client, "me@example.com")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Any news?"}).status_code == 200
    assert status_of(db_session, ticket) == SUPPLIER
    assert status_events(db_session, ticket) == []
    assert db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).first() is not None


def test_the_portal_shows_the_label_and_never_internal_notes(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, SUPPLIER)
    ticket.triaged_at = NOW
    db_session.add(ticket)
    db_session.commit()
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Vendor case VND-991, secret"})
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    for url in ("/portal", f"/portal/{ticket.number}"):
        page = client.get(url).text
        assert "with supplier" in page
        assert "VND-991" not in page


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([SUPPLIER, SUPPLIER], SUPPLIER),
        ([SUPPLIER, TicketStatus.RESOLVED], SUPPLIER),  # exactly one unfinished: its status
        ([SUPPLIER, TicketStatus.OPEN], TicketStatus.IN_PROGRESS),
        ([SUPPLIER, TicketStatus.WITH_MSP], TicketStatus.IN_PROGRESS),
    ],
)
def test_derived_status_with_a_supplier_part(statuses, expected):
    assert derived_status(statuses) == expected


def test_the_original_follows_a_part_that_goes_to_the_supplier(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    set_to(client, parts[0], TicketStatus.RESOLVED)
    set_to(client, parts[1], SUPPLIER)
    assert split_status_of(db_session, original) == SUPPLIER


def test_a_requester_reply_on_a_split_original_leaves_a_supplier_part_alone(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    set_to(client, parts[0], SUPPLIER)
    as_requester(client)
    client.post(f"/portal/{original.number}/comment", data={"body": "Hello?"})
    assert split_status_of(db_session, parts[0]) == SUPPLIER


def test_the_sla_clock_keeps_running_with_the_supplier(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.IN_PROGRESS, SUPPLIER, SLA_NOW)
    assert ticket.sla_paused_at is None
    assert sla_view(ticket, SLA_NOW + timedelta(days=30), 25).state != "paused"
    sla.on_status_change(db_session, ticket, SUPPLIER, TicketStatus.IN_PROGRESS, SLA_NOW + timedelta(hours=5))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 0


def test_moving_from_awaiting_user_to_supplier_resumes_the_clock(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, SLA_NOW)
    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, SUPPLIER, SLA_NOW + timedelta(hours=2))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 2 * 3600

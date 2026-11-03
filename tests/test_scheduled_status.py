"""The Scheduled status: work planned for a day (SRP-2, issue #121). It needs a Due date, never pauses the SLA
clock, and the morning job (tests/test_due_date.py) takes the ticket back to Open on that day."""

from datetime import datetime, timedelta, timezone

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


def tomorrow():
    return (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()


def schedule(client, ticket, **extra):
    """Sets Scheduled the way the status buttons do, with the date the form asks for."""
    return client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled", "due_on": tomorrow(), **extra})


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
    assert schedule(client, ticket).status_code == 200
    assert status_of(db_session, ticket) == SCHEDULED
    db_session.refresh(ticket)
    assert ticket.due_on.isoformat() == tomorrow()
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

    note = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Booked for Friday", "next_status": "scheduled", "due_on": tomorrow()}
    )
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
    schedule(client, parts[1])
    assert split_status_of(db_session, original) == SCHEDULED


def test_a_requester_reply_on_a_split_original_leaves_a_scheduled_part_alone(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    schedule(client, parts[0])
    as_requester(client)
    client.post(f"/portal/{original.number}/comment", data={"body": "Hello?"})
    assert split_status_of(db_session, parts[0]) == SCHEDULED


def test_scheduled_does_not_pause_the_clock(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.IN_PROGRESS, SCHEDULED, SLA_NOW)
    ticket.status = SCHEDULED
    assert ticket.sla_paused_at is None
    assert sla_view(ticket, SLA_NOW, 25).state != "paused"
    assert sla_view(ticket, SLA_NOW + timedelta(days=30), 25).state == "breached"  # it keeps running


def test_moving_from_awaiting_user_to_scheduled_ends_the_pause(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, SLA_NOW)
    assert ticket.sla_paused_at is not None
    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, SCHEDULED, SLA_NOW + timedelta(hours=2))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 2 * 3600


def test_awaiting_user_pauses_a_dated_ticket_but_its_due_date_does_not_move(db_session):
    ticket = clocked()
    ticket.due_on = (SLA_NOW + timedelta(days=3)).date()
    sla.recalculate_deadlines(db_session, ticket)
    deadline = ticket.resolution_due_at
    sla.on_status_change(db_session, ticket, TicketStatus.IN_PROGRESS, TicketStatus.AWAITING_USER, SLA_NOW)
    ticket.status = TicketStatus.AWAITING_USER
    assert sla_view(ticket, SLA_NOW, 25).state == "paused"
    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, TicketStatus.IN_PROGRESS, SLA_NOW + timedelta(days=1))
    assert ticket.sla_paused_seconds == 86400 and ticket.resolution_due_at == deadline  # the wait did not push the date


def test_the_original_leaves_scheduled_when_every_part_is_finished(client, db_session):
    original, parts, _ = family(client, db_session, parts=2)
    set_to(client, parts[0], TicketStatus.RESOLVED)
    schedule(client, parts[1])
    set_to(client, parts[1], TicketStatus.RESOLVED)
    db_session.expire_all()
    fresh = db_session.get(type(original), original.id)
    assert fresh.status == TicketStatus.IN_PROGRESS and fresh.sla_paused_at is None


def test_the_downgrade_ends_the_pause_and_moves_the_deadlines(db_session):
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.models import Ticket

    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "c4e7a2b9d106_scheduled_status.py"
    spec = importlib.util.spec_from_file_location("scheduled_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    now = datetime.now(timezone.utc)
    due = now + timedelta(days=1)
    plain = Ticket(subject="a", requester_email="a@example.com", status=SCHEDULED, sla_paused_at=now - timedelta(days=10),
                   first_response_due_at=due, resolution_due_at=due)
    dated = Ticket(subject="b", requester_email="a@example.com", status=SCHEDULED, sla_paused_at=now - timedelta(days=10),
                   first_response_due_at=due, resolution_due_at=due, required_by=due.date())
    db_session.add(plain)
    db_session.add(dated)
    db_session.commit()
    ids = (plain.id, dated.id)

    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
    db_session.expire_all()
    plain, dated = (db_session.get(Ticket, i) for i in ids)
    assert plain.status == dated.status == TicketStatus.IN_PROGRESS
    assert plain.sla_paused_at is None and dated.sla_paused_at is None
    assert 10 * 86400 <= plain.sla_paused_seconds < 10 * 86400 + 600
    assert plain.resolution_due_at - due > timedelta(days=9)  # moved out by the wait
    assert dated.resolution_due_at == due and dated.first_response_due_at - due > timedelta(days=9)

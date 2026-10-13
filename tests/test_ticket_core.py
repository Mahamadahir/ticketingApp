from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from app.database import engine
from app.models import (
    AuditLog,
    Ticket,
    NumberCounter,
    TicketEvent,
    TicketSource,
    TicketStatus,
    TicketType,
    UserRole,
)
from app.ticket_service import create_ticket
from tests.conftest import create_team, login_as, set_role


def make_ticket(db_session, ticket_type=TicketType.INCIDENT, team_id=None, subject="Test ticket", triaged=False):
    ticket = create_ticket(
        db_session,
        subject=subject,
        requester_email="requester@example.com",
        source=TicketSource.EMAIL,
        ticket_type=ticket_type,
        team_id=team_id,
    )
    if triaged:
        ticket.triaged_at = datetime.now(timezone.utc)
        ticket.status = TicketStatus.OPEN
        db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def events_for(db_session, ticket, event_type=None):
    query = select(TicketEvent).where(TicketEvent.ticket_id == ticket.id).order_by(TicketEvent.id)
    if event_type:
        query = query.where(TicketEvent.event_type == event_type)
    return db_session.exec(query).all()


def login_agent(client, db_session, team):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


def test_one_counter_is_shared_by_every_type_and_the_prefix_follows_the_type(db_session):
    references = [
        make_ticket(db_session, TicketType.INCIDENT).reference,
        make_ticket(db_session, TicketType.SERVICE_REQUEST).reference,
        make_ticket(db_session, TicketType.INCIDENT).reference,
        make_ticket(db_session, TicketType.MAJOR_INCIDENT).reference,
        make_ticket(db_session, TicketType.PROBLEM).reference,
        make_ticket(db_session, TicketType.SERVICE_REQUEST).reference,
    ]
    assert references == ["INC-1", "SR-2", "INC-3", "MI-4", "PRB-5", "SR-6"]


def test_ticket_created_directly_still_gets_a_reference(db_session):
    ticket = Ticket(subject="Direct", requester_email="a@example.com")
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.reference == "INC-1"
    assert ticket.number == 1


def test_counter_matches_last_issued_number(db_session):
    make_ticket(db_session)
    make_ticket(db_session)
    make_ticket(db_session, TicketType.PROBLEM)
    counter = db_session.get(NumberCounter, "ticket")
    assert counter.last_number == 3


def test_new_ticket_defaults_and_created_event(db_session):
    ticket = make_ticket(db_session)
    assert ticket.status == TicketStatus.NEW
    assert ticket.source == TicketSource.EMAIL
    assert ticket.type == TicketType.INCIDENT
    assert ticket.reported_at is not None
    assert ticket.resolved_at is None

    created = events_for(db_session, ticket, "created")
    assert len(created) == 1
    assert created[0].to_value == ticket.tag == f"#{ticket.number}"


def test_dev_simulate_inbound_returns_reference_and_records_event(client, db_session):
    response = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "jane@example.com", "subject": "VPN", "body": "Timeout"},
    )
    assert response.json()["reference"] == "INC-1"
    ticket = db_session.exec(select(Ticket).where(Ticket.number == response.json()["number"])).one()
    assert "ticket_id" not in response.json()  # the internal id is never handed out
    assert len(events_for(db_session, ticket, "created")) == 1


def test_reference_link_opens_ticket_case_insensitively(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, subject="Shared link ticket")
    login_agent(client, db_session, team)

    for path in (f"/t/{ticket.reference}", f"/t/{ticket.reference.lower()}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Shared link ticket" in response.text
        assert ticket.reference in response.text


def test_the_number_alone_finds_a_ticket_whatever_prefix_is_typed(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, TicketType.SERVICE_REQUEST, team_id=team.id, subject="Find by number")
    login_agent(client, db_session, team)

    for text in (str(ticket.number), f"INC-{ticket.number}", f"sr{ticket.number}", f"PRB-{ticket.number}"):
        response = client.get(f"/t/{text}")
        assert response.status_code == 200 and "Find by number" in response.text, text
    for text in ("abc", "INC-", "12x", "NOPE-1", "99999"):
        assert client.get(f"/t/{text}").status_code == 404, text


def test_the_shareable_link_redirects_to_the_one_canonical_address(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, TicketType.SERVICE_REQUEST, team_id=team.id)
    login_agent(client, db_session, team)

    for text in (str(ticket.number), f"INC-{ticket.number}", f"sr-{ticket.number}"):
        response = client.get(f"/t/{text}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == f"/tickets/{ticket.number}", text


def test_ticket_numbers_parse_only_in_the_accepted_forms():
    from app.ticket_service import parse_ticket_number

    assert [parse_ticket_number(t) for t in ("1042", " #1042 ", "INC-1042", "sr-7", "PRB9")] == [1042, 1042, 1042, 7, 9]
    for bad in ("", "INC-", "1042abc", "MAJOR-1", "1 2", "-5", "1234567890", "٣", "0", "0001042", "INC-007"):
        assert parse_ticket_number(bad) is None, bad


def test_reference_link_respects_team_visibility(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    other = make_ticket(db_session, team_id=team_b.id, triaged=True)  # triaged, so team scoping applies
    login_agent(client, db_session, team_a)

    assert client.get(f"/t/{other.reference}").status_code == 403
    assert client.get("/t/INC-999").status_code == 404


def test_every_status_except_new_can_be_set(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)

    for status in TicketStatus:
        if status == TicketStatus.NEW:
            continue  # a ticket only leaves New by being triaged
        assert client.post(f"/tickets/{ticket.number}/status", data={"status": status.value}).status_code == 200
        db_session.refresh(ticket)
        assert ticket.status == status

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "new"}).status_code == 400


def test_status_change_records_event_and_unchanged_status_does_not(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"})
    client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"})

    changes = events_for(db_session, ticket, "status_changed")
    assert len(changes) == 1
    assert (changes[0].from_value, changes[0].to_value) == ("open", "in_progress")
    assert changes[0].actor_email == "agent@example.com"


def test_resolved_at_set_kept_on_close_and_cleared_on_reopen(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"})
    db_session.refresh(ticket)
    resolved_at = ticket.resolved_at
    assert resolved_at is not None

    client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"})
    db_session.refresh(ticket)
    assert ticket.resolved_at == resolved_at

    client.post(f"/tickets/{ticket.number}/status", data={"status": "open"})
    db_session.refresh(ticket)
    assert ticket.resolved_at is None


def test_emailing_the_user_moves_the_ticket_to_awaiting_user_once(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "On it"})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Still on it"})

    changes = events_for(db_session, ticket, "status_changed")
    assert [(e.from_value, e.to_value) for e in changes] == [("open", "awaiting_user")]  # not again on the second email


def test_assign_and_escalate_record_events(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    ticket = make_ticket(db_session, team_id=team_a.id)
    login_agent(client, db_session, team_a)

    client.post(f"/tickets/{ticket.number}/assign")
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Needs network", "target_team_id": team_b.id})

    assigned = events_for(db_session, ticket, "assigned")
    # Assigned to the agent, then unassigned when the escalation moved teams.
    assert [(e.from_value, e.to_value) for e in assigned] == [
        (None, "agent@example.com"),
        ("agent@example.com", None),
    ]

    escalated = events_for(db_session, ticket, "escalated")
    assert len(escalated) == 1
    assert (escalated[0].from_value, escalated[0].to_value) == ("Service Desk", "Network")
    assert escalated[0].detail == "Needs network"


def test_events_are_shown_in_the_ticket_timeline(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"})

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Timeline" in page
    assert "status changed" in page
    assert "open &rarr; in_progress" in page


def test_concurrent_ticket_creation_never_duplicates_a_number():
    workers, per_worker = 8, 5

    def create_many(_):
        for _ in range(per_worker):
            with Session(engine) as session:
                create_ticket(
                    session,
                    subject="Concurrent",
                    requester_email="r@example.com",
                    source=TicketSource.EMAIL,
                )
                session.commit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(create_many, range(workers)))

    with Session(engine) as session:
        numbers = sorted(session.exec(select(Ticket.number)).all())
    assert numbers == list(range(1, workers * per_worker + 1))


def test_email_does_not_undo_a_deliberate_status(client, db_session):
    team = create_team(db_session, "Service Desk")
    login_agent(client, db_session, team)

    for status in (TicketStatus.AWAITING_USER, TicketStatus.WITH_MSP, TicketStatus.RESOLVED, TicketStatus.CLOSED):
        ticket = make_ticket(db_session, team_id=team.id, triaged=True)
        client.post(f"/tickets/{ticket.number}/status", data={"status": status.value})
        db_session.refresh(ticket)
        resolved_at = ticket.resolved_at

        client.post(f"/tickets/{ticket.number}/email", data={"body": "Late email"})
        db_session.refresh(ticket)
        assert ticket.status == status
        assert ticket.resolved_at == resolved_at


def test_the_status_change_after_an_email_is_audited(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id, triaged=True)
    login_agent(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "On it"})

    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [entry.detail for entry in audit] == ["open -> awaiting_user"]


def test_assigning_to_the_same_agent_twice_records_one_event(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id)
    login_agent(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/assign")
    client.post(f"/tickets/{ticket.number}/assign")

    assert len(events_for(db_session, ticket, "assigned")) == 1
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "assigned")).all()) == 1


def test_escalate_to_missing_team_is_rejected_without_changes(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team_id=team.id)
    login_agent(client, db_session, team)

    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x", "target_team_id": 9999})

    assert response.status_code == 400
    db_session.refresh(ticket)
    assert ticket.is_escalated is False
    assert ticket.team_id == team.id
    assert events_for(db_session, ticket, "escalated") == []


def test_escalating_to_another_team_records_the_unassignment(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    ticket = make_ticket(db_session, team_id=team_a.id)
    login_agent(client, db_session, team_a)
    client.post(f"/tickets/{ticket.number}/assign")

    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Network", "target_team_id": team_b.id})

    assigned = events_for(db_session, ticket, "assigned")
    assert [(e.from_value, e.to_value) for e in assigned] == [(None, "agent@example.com"), ("agent@example.com", None)]


def test_portal_shows_readable_status(client, db_session):
    ticket = make_ticket(db_session)
    ticket.requester_email = "me@example.com"
    ticket.status = TicketStatus.AWAITING_USER
    db_session.add(ticket)
    db_session.commit()
    login_as(client, "me@example.com")

    for path in ("/portal", f"/portal/{ticket.number}"):
        page = client.get(path).text
        assert "awaiting user" in page
        assert "awaiting_user</span>" not in page

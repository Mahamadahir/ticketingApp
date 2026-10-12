from sqlmodel import select

from app.models import (
    AuditLog,
    Category,
    Ticket,
    TicketAlias,
    TicketEvent,
    TicketSource,
    TicketStatus,
    TicketType,
    UserRole,
)
from app.settings_store import DEFAULT_TRIAGE_TEAM, set_setting
from app.ticket_service import create_ticket
from tests.conftest import add_membership, create_category, create_team, login_as, set_role


def new_ticket(db_session, team=None, ticket_type=TicketType.INCIDENT, requester="me@example.com"):
    """An untriaged ticket, created the way real intake creates one."""
    ticket = create_ticket(
        db_session,
        subject="Printer on fire",
        requester_email=requester,
        source=TicketSource.EMAIL,
        ticket_type=ticket_type,
        team_id=team.id if team else None,
    )
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent(client, db_session, member_of=(), email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT)
    for team in member_of:
        add_membership(db_session, agent_id, team.id)
    return agent_id


def triage(client, ticket, category, ticket_type="incident", **extra):
    data = {"ticket_type": ticket_type, "category_id": category.id, **extra}
    return client.post(f"/tickets/{ticket.id}/triage", data=data)


def events(db_session, ticket, event_type):
    db_session.expire_all()
    return db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == event_type)
    ).all()


# ---- Triage ----------------------------------------------------------------------


def test_triage_classifies_moves_to_open_and_picks_the_team_from_the_category(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    hardware = create_category(db_session, network, "Hardware")
    laptop = create_category(db_session, None, "Laptop", parent=hardware)
    agent_id = agent(client, db_session, member_of=[desk])

    response = triage(client, ticket, laptop)

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN
    assert ticket.category_id == laptop.id
    assert ticket.team_id == network.id  # the sub-category inherits Hardware's team
    assert ticket.triaged_by_id == agent_id and ticket.triaged_at is not None
    assert ticket.assigned_agent_id is None  # triage does not assign
    assert [e.to_value for e in events(db_session, ticket, "triaged")] == ["Hardware / Laptop"]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "team_changed")] == [
        ("Service Desk", "Network")
    ]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "status_changed")] == [("new", "open")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "triaged")).all()
    assert [a.detail for a in audit] == ["Hardware / Laptop"]


def test_a_new_ticket_leaves_new_only_by_triage(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])

    assert client.post(f"/tickets/{ticket.id}/status", data={"status": "open"}).status_code == 400
    assert client.post(f"/tickets/{ticket.id}/status", data={"status": "resolved"}).status_code == 400
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.NEW

    triage(client, ticket, create_category(db_session, desk))
    assert client.post(f"/tickets/{ticket.id}/status", data={"status": "in_progress"}).status_code == 200
    assert client.post(f"/tickets/{ticket.id}/status", data={"status": "new"}).status_code == 400


def test_a_ticket_created_past_new_counts_as_triaged(db_session):
    ticket = Ticket(subject="x", requester_email="a@example.com", status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.triaged_at is not None
    untouched = Ticket(subject="y", requester_email="a@example.com")
    db_session.add(untouched)
    db_session.commit()
    db_session.refresh(untouched)
    assert untouched.status == TicketStatus.NEW and untouched.triaged_at is None


def test_changing_the_type_reissues_the_reference_and_keeps_the_old_one_working(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    assert ticket.reference == "INC-1"
    agent(client, db_session, member_of=[desk])

    triage(client, ticket, create_category(db_session, desk), ticket_type="service_request")

    db_session.refresh(ticket)
    assert (ticket.type, ticket.reference, ticket.number) == (TicketType.SERVICE_REQUEST, "SR-1", 1)
    assert [a.ticket_id for a in db_session.exec(select(TicketAlias).where(TicketAlias.reference == "INC-1"))] == [
        ticket.id
    ]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "reference_changed")] == [("INC-1", "SR-1")]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "type_changed")] == [
        ("incident", "service_request")
    ]
    # Old links and email subjects still find it, in either case.
    for link in ("/t/SR-1", "/t/INC-1", "/t/inc-1"):
        page = client.get(link)
        assert page.status_code == 200 and "SR-1" in page.text, link
    # The old number is never handed out again.
    assert new_ticket(db_session, desk).reference == "INC-2"


def test_keeping_the_type_keeps_the_reference(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])

    triage(client, ticket, create_category(db_session, desk))

    db_session.refresh(ticket)
    assert ticket.reference == "INC-1"
    assert db_session.exec(select(TicketAlias)).all() == []
    assert events(db_session, ticket, "reference_changed") == []


def test_triage_assignment_is_optional_and_never_clears_an_existing_one(client, db_session):
    """Regression: leaving the assignee blank used to unassign a ticket that had
    already been handed to the triager."""
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent_id = agent(client, db_session, member_of=[desk])
    client.post(f"/tickets/{ticket.id}/assign")  # claimed, still untriaged

    triage(client, ticket, create_category(db_session, desk))

    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == agent_id


def test_triage_can_assign_a_member_of_the_chosen_team_and_only_a_member(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    member_id = agent(client, db_session, member_of=[network], email="member@example.com")
    client.post("/auth/logout")
    outsider_id = agent(client, db_session, member_of=[desk], email="outsider@example.com")
    category = create_category(db_session, network, "Hardware")

    refused = triage(client, ticket, category, agent_id=outsider_id)
    assert refused.status_code == 400
    db_session.refresh(ticket)
    assert ticket.triaged_at is None and ticket.status == TicketStatus.NEW  # nothing half-done

    assert triage(client, ticket, category, agent_id=member_id).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == member_id
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "assigned")] == [
        (None, "member@example.com")
    ]


def test_triage_refusals(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    good = create_category(db_session, desk)
    switched_off = create_category(db_session, desk, "Old")
    switched_off.is_active = False
    no_team = create_category(db_session, None, "Orphan")
    db_session.add(switched_off)
    db_session.commit()

    assert client.post(f"/tickets/{ticket.id}/triage", data={"ticket_type": "incident"}).status_code == 400
    assert triage(client, ticket, switched_off).status_code == 400
    refused = triage(client, ticket, no_team)
    assert refused.status_code == 400 and "no team" in refused.text.lower()
    db_session.refresh(ticket)
    assert ticket.triaged_at is None

    assert triage(client, ticket, good).status_code == 200
    again = triage(client, ticket, good)
    assert again.status_code == 400 and "already been triaged" in again.text


def test_any_agent_can_triage_and_then_keeps_read_access_but_not_write_access(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session)  # in no team at all
    category = create_category(db_session, network)

    assert triage(client, ticket, category).status_code == 200

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200 and "read-only access" in page.text  # triaging does not make it vanish
    assert client.post(f"/tickets/{ticket.id}/note", data={"body": "x"}).status_code == 403


def test_end_users_cannot_triage(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    login_as(client, "me@example.com")

    assert triage(client, ticket, create_category(db_session, desk)).status_code == 403


def test_the_triage_panel_is_only_offered_on_untriaged_tickets(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    category = create_category(db_session, desk)

    assert "<h3>Triage</h3>" in client.get(f"/tickets/{ticket.id}").text
    triage(client, ticket, category)
    page = client.get(f"/tickets/{ticket.id}").text
    assert "<h3>Triage</h3>" not in page and "Category: Hardware" in page


def test_other_goes_to_the_default_triage_team_and_waits_for_review(client, db_session):
    desk = create_team(db_session, "Service Desk")
    set_setting(db_session, DEFAULT_TRIAGE_TEAM, str(desk.id))
    other_team = create_team(db_session, "Elsewhere")
    ticket = new_ticket(db_session, other_team)
    agent(client, db_session, member_of=[desk])
    other = Category(name="Other", is_other=True)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    assert triage(client, ticket, other).status_code == 200

    db_session.refresh(ticket)
    assert ticket.team_id == desk.id and ticket.needs_category_review is True


def test_other_with_no_team_anywhere_is_refused_with_a_helpful_message(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    other = Category(name="Other", is_other=True)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    response = triage(client, ticket, other)

    assert response.status_code == 400 and "default triage team" in response.text


# ---- Creating a category while triaging ----------------------------------------


def test_a_super_admin_can_create_a_category_while_triaging(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    login_as(client, "admin@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/triage",
        data={"ticket_type": "incident", "new_category_name": "  Wi-Fi   dead zones ", "new_category_team_id": network.id},
    )

    assert response.status_code == 200
    created = db_session.exec(select(Category).where(Category.name == "Wi-Fi dead zones")).one()
    db_session.refresh(ticket)
    assert ticket.category_id == created.id and ticket.team_id == network.id
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "category"))] == [
        "Wi-Fi dead zones"
    ]


def test_a_super_admin_can_create_a_sub_category_while_triaging(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    hardware = create_category(db_session, network, "Hardware")
    login_as(client, "admin@example.com")

    client.post(
        f"/tickets/{ticket.id}/triage",
        data={"ticket_type": "incident", "new_category_name": "Docking stations", "new_category_parent_id": hardware.id},
    )

    created = db_session.exec(select(Category).where(Category.name == "Docking stations")).one()
    assert created.parent_id == hardware.id and created.team_id is None
    db_session.refresh(ticket)
    assert ticket.team_id == network.id


def test_an_agent_cannot_create_a_category_and_bad_new_categories_are_refused(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    existing = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])

    refused = client.post(
        f"/tickets/{ticket.id}/triage",
        data={"ticket_type": "incident", "new_category_name": "Sneaky", "new_category_team_id": desk.id},
    )
    assert refused.status_code == 403
    client.post("/auth/logout")

    login_as(client, "admin@example.com")
    bad = (
        {"new_category_name": "hardware", "new_category_team_id": desk.id},  # a duplicate, any case
        {"new_category_name": "No team given"},
        {"new_category_name": "Bad team", "new_category_team_id": 9999},
        {"new_category_name": "Other", "new_category_team_id": desk.id},  # reserved
        {"new_category_name": "x" * 81, "new_category_team_id": desk.id},
        {"new_category_name": "Grandchild", "new_category_parent_id": existing.id, "new_category_team_id": desk.id},
    )
    for fields in bad[:5]:
        response = client.post(f"/tickets/{ticket.id}/triage", data={"ticket_type": "incident", **fields})
        assert response.status_code == 400, fields
    db_session.refresh(ticket)
    assert ticket.triaged_at is None  # none of the refusals triaged it

    sub = create_category(db_session, None, "Laptop", parent=existing)
    deeper = client.post(
        f"/tickets/{ticket.id}/triage",
        data={"ticket_type": "incident", "new_category_name": "Too deep", "new_category_parent_id": sub.id},
    )
    assert deeper.status_code == 400


# ---- Re-assign --------------------------------------------------------------------


def triaged_ticket(db_session, team, **fields):
    ticket = new_ticket(db_session, team)
    ticket.triaged_at = ticket.created_at
    ticket.status = TicketStatus.OPEN
    for name, value in fields.items():
        setattr(ticket, name, value)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def test_reassign_to_a_team_and_an_agent_in_it(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = triaged_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk, network])
    client.post("/auth/logout")
    helper_id = agent(client, db_session, member_of=[network], email="helper@example.com")
    client.post("/auth/logout")
    login_as(client, "agent@example.com")

    response = client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": network.id, "agent_id": helper_id})

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (network.id, helper_id)
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "team_changed")] == [
        ("Service Desk", "Network")
    ]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "assigned")] == [(None, "helper@example.com")]
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "reassigned"))] == [
        "Network: helper@example.com"
    ]
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "escalated")).all() == []


def test_reassign_to_a_team_and_leave_it_unassigned_there(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    agent_id = agent(client, db_session, member_of=[desk, network])
    ticket = triaged_ticket(db_session, desk, assigned_agent_id=agent_id)

    client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": network.id, "agent_id": ""})

    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (network.id, None)
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "assigned")] == [
        ("agent@example.com", None)
    ]


def test_reassign_within_the_team_and_a_no_op_records_nothing(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent_id = agent(client, db_session, member_of=[desk])
    ticket = triaged_ticket(db_session, desk, assigned_agent_id=agent_id)

    client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": desk.id, "agent_id": agent_id})

    assert events(db_session, ticket, "assigned") == [] and events(db_session, ticket, "team_changed") == []
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "reassigned")).all() == []


def test_reassign_refusals(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    outsider_id = agent(client, db_session, member_of=[desk], email="desk-only@example.com")
    client.post("/auth/logout")
    agent_id = agent(client, db_session, member_of=[desk], email="agent@example.com")
    ticket = triaged_ticket(db_session, desk, assigned_agent_id=agent_id)
    untriaged = new_ticket(db_session, desk)

    not_a_member = client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": network.id, "agent_id": outsider_id})
    assert not_a_member.status_code == 400
    assert client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": 9999}).status_code == 400
    assert client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": desk.id, "agent_id": "abc"}).status_code == 400
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (desk.id, agent_id)  # unchanged by every refusal
    # An untriaged ticket's team is decided by triage, not by moving it.
    untriaged_move = client.post(f"/tickets/{untriaged.id}/reassign", data={"team_id": desk.id})
    assert untriaged_move.status_code == 400 and "Triage the ticket first" in untriaged_move.text


def test_a_viewer_cannot_reassign(client, db_session):
    from app.models import MembershipLevel

    desk = create_team(db_session, "Service Desk")
    viewer_id = agent(client, db_session)
    add_membership(db_session, viewer_id, desk.id, MembershipLevel.VIEWER)
    ticket = triaged_ticket(db_session, desk)

    assert client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": desk.id}).status_code == 403


def test_moving_a_ticket_to_a_team_you_are_not_in_says_so_instead_of_showing_a_closed_door(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    agent(client, db_session, member_of=[desk])
    ticket = triaged_ticket(db_session, desk)

    response = client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": network.id})

    assert response.status_code == 200
    assert "was moved to Network" in response.text and "no longer have access" in response.text
    assert client.get(f"/tickets/{ticket.id}").status_code == 403


def test_assign_to_me_is_only_offered_while_unassigned_and_reassign_replaces_it(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent_id = agent(client, db_session, member_of=[desk])
    ticket = triaged_ticket(db_session, desk)

    page = client.get(f"/tickets/{ticket.id}").text
    assert "Assign to me" in page and ">Assign</button>" in page

    ticket.assigned_agent_id = agent_id
    db_session.add(ticket)
    db_session.commit()
    page = client.get(f"/tickets/{ticket.id}").text
    assert "Assign to me" not in page and ">Re-assign</button>" in page


def test_the_agent_pickers_return_options_for_a_team_and_for_a_category(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    agent(client, db_session, member_of=[network], email="net@example.com")
    client.post("/auth/logout")
    agent(client, db_session, member_of=[desk], email="desk@example.com")
    category = create_category(db_session, network)

    by_team = client.get(f"/tickets/agents-for-team?team_id={network.id}").text
    assert "Leave unassigned" in by_team and "Test User" in by_team
    by_category = client.get(f"/tickets/agents-for-category?category_id={category.id}").text
    assert "Leave as is" in by_category and "Test User" in by_category
    nothing = client.get("/tickets/agents-for-category").text
    assert "Leave as is" in nothing and "Test User" not in nothing


# ---- Default triage team --------------------------------------------------------


def test_new_tickets_arrive_in_the_default_triage_team_and_are_visible_to_everyone(client, db_session):
    desk = create_team(db_session, "Service Desk")
    set_setting(db_session, DEFAULT_TRIAGE_TEAM, str(desk.id))
    db_session.commit()

    result = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "jane@example.com", "subject": "VPN", "body": "Timeout"},
    ).json()

    ticket = db_session.get(Ticket, result["ticket_id"])
    assert ticket.team_id == desk.id and ticket.status == TicketStatus.NEW
    agent(client, db_session)  # in no team
    assert "VPN" in client.get("/tickets").text

# ---- Review fixes -------------------------------------------------------------------


def test_two_people_triaging_the_same_ticket_at_once_get_one_winner_and_no_errors(db_session):
    """Regression: the already-triaged check was a stale read, so both went ahead,
    which crashed on the alias or silently overwrote the first triage."""
    import threading

    from fastapi.testclient import TestClient

    from app.main import app

    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    category = create_category(db_session, desk)
    agent_ids = []
    for number in range(2):
        with TestClient(app) as setup_client:
            agent_ids.append(agent(setup_client, db_session, member_of=[desk], email=f"agent{number}@example.com"))

    barrier, codes = threading.Barrier(2), []

    def do_triage(number):
        with TestClient(app) as racing_client:
            racing_client.post("/auth/dev-login", data={"email": f"agent{number}@example.com", "display_name": "A"})
            barrier.wait()
            response = racing_client.post(
                f"/tickets/{ticket.id}/triage", data={"ticket_type": "service_request", "category_id": category.id}
            )
            codes.append(response.status_code)

    threads = [threading.Thread(target=do_triage, args=(number,)) for number in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(codes) == [200, 400]  # one winner, one told it is already triaged, no 500
    assert len(events(db_session, ticket, "triaged")) == 1
    assert len(db_session.exec(select(TicketAlias)).all()) == 1
    db_session.refresh(ticket)
    assert ticket.reference == "SR-1" and ticket.triaged_by_id in agent_ids


def test_a_sub_category_of_a_switched_off_category_cannot_be_chosen(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, None, "Laptop", parent=hardware)
    hardware.is_active = False  # a stale form still names its child
    db_session.add(hardware)
    db_session.commit()
    agent(client, db_session, member_of=[desk])

    response = triage(client, ticket, laptop)

    assert response.status_code == 400 and "no longer in use" in response.text
    db_session.refresh(ticket)
    assert ticket.triaged_at is None


def test_a_super_admin_can_assign_an_untriaged_ticket_without_triaging_it(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    member_id = agent(client, db_session, member_of=[desk], email="member@example.com")
    client.post("/auth/logout")
    login_as(client, "admin@example.com")

    page = client.get(f"/tickets/{ticket.id}").text
    assert "Assign to (without triaging)" in page and "<h3>Triage</h3>" in page

    assert client.post(f"/tickets/{ticket.id}/assign", data={"agent_id": member_id}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == member_id and ticket.triaged_at is None

    triage(client, ticket, create_category(db_session, desk))
    assert "Assign to (without triaging)" not in client.get(f"/tickets/{ticket.id}").text

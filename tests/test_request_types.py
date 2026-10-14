import pytest
from sqlmodel import select

from app.models import (
    AuditLog,
    RequestType,
    RequestTypeApprover,
    Ticket,
    TicketEvent,
    TicketType,
    UserRole,
)
from app.request_types import (
    MAX_APPROVERS,
    RequestTypeError,
    approvers_of,
    create_request_type,
    parse_approvers,
)
from tests.conftest import login_as, set_role
from tests.test_raising import desk, raise_it


def admin(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def person(client, db_session, email, name="Some One"):
    """An account that has signed in once."""
    client.post("/auth/logout")
    login_as(client, email, name)
    client.post("/auth/logout")


def make(db_session, name="New starter", description="", approvers=""):
    request_type = create_request_type(db_session, name, description, approvers)
    db_session.commit()
    db_session.refresh(request_type)
    return request_type


def actions(db_session):
    return [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type")).all()]


# ---- Admin ------------------------------------------------------------------------------------------


def test_an_admin_creates_edits_and_switches_one_off_with_audit(client, db_session):
    person(client, db_session, "boss@example.com", "Big Boss")
    person(client, db_session, "lead@example.com", "Team Lead")
    admin(client, db_session)

    created = client.post(
        "/admin/request-types",
        data={"name": " New starter ", "description": "A new colleague", "approvers": "boss@example.com"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    [request_type] = db_session.exec(select(RequestType)).all()
    assert (request_type.name, request_type.description, request_type.is_active) == ("New starter", "A new colleague", True)
    assert [u.email for u in approvers_of(db_session, request_type)] == ["boss@example.com"]

    client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "New starter", "description": "A new colleague", "approvers": "lead@example.com, BOSS@example.com",
              "is_active": "on"},
    )
    db_session.expire_all()
    assert [u.email for u in approvers_of(db_session, request_type)] == ["boss@example.com", "lead@example.com"]

    client.post(f"/admin/request-types/{request_type.id}", data={"name": "New starter", "approvers": "lead@example.com"})
    db_session.expire_all()
    request_type = db_session.get(RequestType, request_type.id)
    assert request_type.is_active is False
    assert [u.email for u in approvers_of(db_session, request_type)] == ["lead@example.com"]  # boss removed
    assert actions(db_session) == ["created", "edited", "edited", "deactivated"]
    assert "New starter" in client.get("/admin/request-types").text  # still listed when off


def test_saving_without_changes_is_not_an_edit(client, db_session):
    admin(client, db_session)
    request_type = make(db_session, "Laptop", "A laptop")
    client.post(f"/admin/request-types/{request_type.id}", data={"name": "Laptop", "description": "A laptop", "is_active": "on"})
    assert actions(db_session) == []


@pytest.mark.parametrize(
    "data",
    [
        {"name": "  "},
        {"name": "x" * 101},
        {"name": "ok", "description": "d" * 1001},
        {"name": "LAPTOP"},  # names are unique whatever the case
        {"name": "ok", "approvers": "nobody@example.com"},  # no account yet
        {"name": "ok", "approvers": "not-an-address"},
    ],
)
def test_bad_input_is_refused_and_changes_nothing(client, db_session, data):
    admin(client, db_session)
    make(db_session, "Laptop")

    assert client.post("/admin/request-types", data={"description": "", "approvers": "", **data}).status_code == 400
    assert [t.name for t in db_session.exec(select(RequestType)).all()] == ["Laptop"]


def test_the_number_of_approvers_is_limited(client, db_session):
    emails = [f"u{i}@example.com" for i in range(MAX_APPROVERS + 1)]
    for email in emails:
        person(client, db_session, email)
    admin(client, db_session)

    over = client.post("/admin/request-types", data={"name": "Big", "approvers": ",".join(emails)})
    assert over.status_code == 400 and "at most" in over.text
    ok = client.post("/admin/request-types", data={"name": "Big", "approvers": ",".join(emails[:MAX_APPROVERS])})
    assert ok.status_code == 200  # redirected to the list
    assert len(approvers_of(db_session, db_session.exec(select(RequestType)).one())) == MAX_APPROVERS


def test_a_failed_edit_leaves_the_approvers_alone(client, db_session):
    person(client, db_session, "boss@example.com")
    admin(client, db_session)
    request_type = make(db_session, "Laptop", approvers="boss@example.com")

    response = client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "Laptop", "approvers": "ghost@example.com", "is_active": "on"},
    )

    assert response.status_code == 400
    db_session.expire_all()
    assert [u.email for u in approvers_of(db_session, db_session.get(RequestType, request_type.id))] == ["boss@example.com"]


def test_approvers_are_deduplicated_and_unknown_ids_are_404(client, db_session):
    person(client, db_session, "boss@example.com")
    admin(client, db_session)
    request_type = make(db_session, "Laptop", approvers="boss@example.com; Boss@Example.com\nboss@example.com")
    assert len(db_session.exec(select(RequestTypeApprover)).all()) == 1
    assert client.post("/admin/request-types/9999", data={"name": "x"}).status_code == 404
    with pytest.raises(RequestTypeError):
        parse_approvers(db_session, "ghost@example.com")


def test_only_super_admins_manage_request_types(client, db_session):
    desk(client, db_session)
    request_type = make(db_session)
    assert client.get("/admin/request-types").status_code == 403
    assert client.post("/admin/request-types", data={"name": "x"}).status_code == 403
    assert client.post(f"/admin/request-types/{request_type.id}", data={"name": "x"}).status_code == 403


# ---- Raising one ------------------------------------------------------------------------------------


def test_the_form_lists_the_active_kinds_of_request(client, db_session):
    make(db_session, "New starter", "A new colleague")
    off = make(db_session, "Old thing")
    off.is_active = False
    db_session.add(off)
    db_session.commit()
    login_as(client, "jo@example.com")

    page = client.get("/portal/new").text

    assert "New starter - A new colleague" in page and "Old thing" not in page
    assert "Something is not working" in page


def test_an_end_user_raises_a_service_request(client, db_session):
    request_type = make(db_session, "New starter")
    login_as(client, "jo@example.com", "Jo Bloggs")

    response = raise_it(client, subject="Starter on Monday", request_type_id=request_type.id)

    assert response.status_code == 204
    ticket = db_session.exec(select(Ticket)).one()
    assert (ticket.type, ticket.request_type_id) == (TicketType.SERVICE_REQUEST, request_type.id)
    assert ticket.reference.startswith("SR-")
    [event] = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "request_type")).all()
    assert (event.ticket_id, event.to_value) == (ticket.id, "New starter")
    assert "Request: New starter" in client.get(f"/portal/{ticket.number}").text


def test_without_a_request_type_it_is_still_an_incident(client, db_session):
    make(db_session, "New starter")
    login_as(client, "jo@example.com")
    raise_it(client)
    ticket = db_session.exec(select(Ticket)).one()
    assert (ticket.type, ticket.request_type_id) == (TicketType.INCIDENT, None)


def test_a_switched_off_or_unknown_kind_is_refused(client, db_session):
    off = make(db_session, "Old thing")
    off.is_active = False
    db_session.add(off)
    db_session.commit()
    login_as(client, "jo@example.com")

    assert raise_it(client, request_type_id=off.id).status_code == 400
    assert raise_it(client, request_type_id=9999).status_code == 400
    assert raise_it(client, request_type_id="abc").status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


def test_an_end_user_still_cannot_pick_a_ticket_type_directly(client, db_session):
    login_as(client, "jo@example.com")
    assert raise_it(client, ticket_type="service_request").status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


def test_an_agent_raises_a_service_request_for_someone_and_the_type_wins(client, db_session):
    team, category, _ = desk(client, db_session)
    request_type = make(db_session, "Software install")

    response = raise_it(
        client, for_email="walkin@example.com", request_type_id=request_type.id, ticket_type="incident",
        category_id=category.id, impact="low", urgency="low",
    )

    assert response.status_code == 204
    ticket = db_session.exec(select(Ticket)).one()
    assert (ticket.type, ticket.request_type_id, ticket.triaged_at is not None) == (
        TicketType.SERVICE_REQUEST, request_type.id, True,
    )


def test_the_agent_page_shows_the_request_and_its_approvers_but_the_requester_does_not(client, db_session):
    person(client, db_session, "boss@example.com", "Big Boss")
    request_type = make(db_session, "New starter", approvers="boss@example.com")
    team, category, _ = desk(client, db_session)
    raise_it(client, for_email="walkin@example.com", request_type_id=request_type.id)
    ticket = db_session.exec(select(Ticket)).one()

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Request: New starter (approvers: Big Boss)" in page

    client.post("/auth/logout")
    login_as(client, "walkin@example.com")
    portal = client.get(f"/portal/{ticket.number}").text
    assert "Request: New starter" in portal and "Big Boss" not in portal

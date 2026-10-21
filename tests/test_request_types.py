import pytest
from sqlmodel import select

from app.approval_groups import add_member, create_group, members_of
from app.models import (
    ApprovalGroup,
    ApprovalGroupMember,
    AuditLog,
    RequestType,
    Ticket,
    TicketEvent,
    TicketType,
    User,
    UserRole,
)
from app.request_types import RequestTypeError, create_request_type, update_request_type
from tests.conftest import login_as, set_role
from tests.test_raising import desk, raise_it


def admin(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def person(client, db_session, email, name="Some One"):
    """An account that has signed in once."""
    client.post("/auth/logout")
    login_as(client, email, name)
    client.post("/auth/logout")


def group_of(db_session, name, members=("boss@example.com",)):
    """An approval group with these members (accounts are made if they do not exist)."""
    group = create_group(db_session, name)
    for email in members:
        if db_session.exec(select(User).where(User.email == email)).first() is None:
            db_session.add(User(email=email, display_name=email))
            db_session.flush()
        add_member(db_session, group, email)
    db_session.commit()
    db_session.refresh(group)
    return group


def make(db_session, name="New starter", description="", members=("boss@example.com",), pass_percent=51, fail_percent=50):
    group = group_of(db_session, f"{name} group", members)
    request_type = create_request_type(db_session, name, description, group.id, pass_percent, fail_percent)
    db_session.commit()
    db_session.refresh(request_type)
    return request_type


def form(group, **extra):
    return {"approval_group_id": group.id if group is not None else "", "pass_percent": 51, "fail_percent": 50, **extra}


def actions(db_session):
    return [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type")).all()]


# ---- Admin ------------------------------------------------------------------------------------------


def test_an_admin_creates_edits_and_switches_one_off_with_audit(client, db_session):
    first = group_of(db_session, "Managers", ("boss@example.com",))
    second = group_of(db_session, "Leads", ("lead@example.com",))
    admin(client, db_session)

    created = client.post(
        "/admin/request-types",
        data={"name": " New starter ", "description": "A new colleague", "is_active": "on", **form(first)},
        follow_redirects=False,
    )
    assert created.status_code == 303
    [request_type] = db_session.exec(select(RequestType)).all()
    assert (request_type.name, request_type.description, request_type.is_active) == ("New starter", "A new colleague", True)
    assert (request_type.approval_group_id, request_type.pass_percent, request_type.fail_percent) == (first.id, 51, 50)
    assert [u.email for u in members_of(db_session, request_type.approval_group_id)] == ["boss@example.com"]

    client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "New starter", "description": "A new colleague", "is_active": "on", **form(second, pass_percent=60, fail_percent=45)},
    )
    db_session.expire_all()
    request_type = db_session.get(RequestType, request_type.id)
    assert (request_type.approval_group_id, request_type.pass_percent, request_type.fail_percent) == (second.id, 60, 45)

    client.post(f"/admin/request-types/{request_type.id}", data={"name": "New starter", "description": "A new colleague", **form(second, pass_percent=60, fail_percent=45)})
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).is_active is False
    assert actions(db_session) == ["created", "edited", "deactivated"]
    details = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "edited")).all()]
    assert details == ["approval group Managers -> Leads; pass/fail 51%/50% -> 60%/45%"]
    assert "New starter" in client.get("/admin/request-types").text  # still listed when off


def test_saving_without_changes_is_not_an_edit(client, db_session):
    admin(client, db_session)
    request_type = make(db_session, "Laptop", "A laptop")
    client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "Laptop", "description": "A laptop", "is_active": "on", **form(db_session.get(ApprovalGroup, request_type.approval_group_id))},
    )
    assert actions(db_session) == []


@pytest.mark.parametrize(
    "data",
    [
        {"name": "  "},
        {"name": "x" * 101},
        {"name": "ok", "description": "d" * 1001},
        {"name": "LAPTOP"},  # names are unique whatever the case
    ],
)
def test_bad_input_is_refused_and_changes_nothing(client, db_session, data):
    admin(client, db_session)
    make(db_session, "Laptop")
    group = group_of(db_session, "Other", ("lead@example.com",))

    assert client.post("/admin/request-types", data={"description": "", "is_active": "on", **form(group), **data}).status_code == 400
    assert [t.name for t in db_session.exec(select(RequestType)).all()] == ["Laptop"]


@pytest.mark.parametrize("pass_percent,fail_percent", [(50, 50), (30, 70), (1, 99)])
def test_pass_and_fail_must_add_up_to_more_than_100(client, db_session, pass_percent, fail_percent):
    admin(client, db_session)
    group = group_of(db_session, "Managers")

    response = client.post(
        "/admin/request-types",
        data={"name": "Access", "is_active": "on", **form(group, pass_percent=pass_percent, fail_percent=fail_percent)},
    )

    assert response.status_code == 400 and "add up to more than 100" in response.text
    assert db_session.exec(select(RequestType)).all() == []


def test_the_edit_form_checks_the_percentages_too(client, db_session):
    admin(client, db_session)
    request_type = make(db_session, "Access")

    response = client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "Access", "is_active": "on", **form(db_session.get(ApprovalGroup, request_type.approval_group_id), pass_percent=40, fail_percent=60)},
    )

    assert response.status_code == 400 and "add up to more than 100" in response.text
    db_session.expire_all()
    assert (db_session.get(RequestType, request_type.id).pass_percent, db_session.get(RequestType, request_type.id).fail_percent) == (51, 50)


@pytest.mark.parametrize("pass_percent,fail_percent", [(0, 100), (101, 5), (50, 0)])
def test_each_percentage_is_between_1_and_100(client, db_session, pass_percent, fail_percent):
    admin(client, db_session)
    group = group_of(db_session, "Managers")
    response = client.post(
        "/admin/request-types",
        data={"name": "Access", "is_active": "on", **form(group, pass_percent=pass_percent, fail_percent=fail_percent)},
    )
    assert response.status_code == 422 and db_session.exec(select(RequestType)).all() == []


def test_an_active_request_type_needs_a_group_with_a_member(client, db_session):
    admin(client, db_session)
    empty = group_of(db_session, "Nobody", ())

    no_group = client.post("/admin/request-types", data={"name": "A", "is_active": "on", **form(None)})
    empty_group = client.post("/admin/request-types", data={"name": "A", "is_active": "on", **form(empty)})
    missing_group = client.post("/admin/request-types", data={"name": "A", "is_active": "on", "approval_group_id": 9999, "pass_percent": 51, "fail_percent": 50})

    for response in (no_group, empty_group, missing_group):
        assert response.status_code == 400
    assert "at least one member" in no_group.text and "at least one member" in empty_group.text
    assert "does not exist" in missing_group.text
    assert db_session.exec(select(RequestType)).all() == []
    # switched off, it can be saved without one
    assert client.post("/admin/request-types", data={"name": "A", **form(None)}, follow_redirects=False).status_code == 303


def test_switching_a_type_on_with_an_empty_group_is_refused(client, db_session):
    admin(client, db_session)
    empty = group_of(db_session, "Nobody", ())
    off = create_request_type(db_session, "Dormant", "", empty.id, 51, 50, is_active=False)
    db_session.commit()

    response = client.post(f"/admin/request-types/{off.id}", data={"name": "Dormant", "is_active": "on", **form(empty)})

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(RequestType, off.id).is_active is False
    with pytest.raises(RequestTypeError):
        update_request_type(db_session, off, "Dormant", "", empty.id, 51, 50, True)


def test_a_failed_edit_leaves_the_type_alone(client, db_session):
    admin(client, db_session)
    request_type = make(db_session, "Laptop")
    group = db_session.get(ApprovalGroup, request_type.approval_group_id)

    response = client.post(
        f"/admin/request-types/{request_type.id}",
        data={"name": "Notebook", "is_active": "on", **form(group, pass_percent=10, fail_percent=10)},
    )

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).name == "Laptop"


def test_unknown_ids_are_404(client, db_session):
    admin(client, db_session)
    group = group_of(db_session, "Managers")
    assert client.post("/admin/request-types/9999", data={"name": "x", **form(group)}).status_code == 404


def test_only_super_admins_manage_request_types(client, db_session):
    desk(client, db_session)
    request_type = make(db_session)
    assert client.get("/admin/request-types").status_code == 403
    assert client.post("/admin/request-types", data={"name": "x"}).status_code == 403
    assert client.post(f"/admin/request-types/{request_type.id}", data={"name": "x"}).status_code == 403


def test_the_new_type_form_is_prefilled_from_the_settings(client, db_session):
    from app.settings_store import APPROVAL_DEFAULT_FAIL_PERCENT, APPROVAL_DEFAULT_PASS_PERCENT, set_setting

    admin(client, db_session)
    set_setting(db_session, APPROVAL_DEFAULT_PASS_PERCENT, "75")
    set_setting(db_session, APPROVAL_DEFAULT_FAIL_PERCENT, "30")
    db_session.commit()

    page = client.get("/admin/request-types").text

    assert 'name="pass_percent" value="75"' in page and 'name="fail_percent" value="30"' in page


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
    request_type = make(db_session, "New starter")
    team, category, _ = desk(client, db_session)
    raise_it(client, for_email="walkin@example.com", request_type_id=request_type.id)
    ticket = db_session.exec(select(Ticket)).one()

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Request: New starter (approval group: New starter group)" in page

    client.post("/auth/logout")
    login_as(client, "walkin@example.com")
    portal = client.get(f"/portal/{ticket.number}").text
    assert "Request: New starter" in portal and "approval group" not in portal


# ---- Review fixes -----------------------------------------------------------------------------------


def test_retyping_a_catalogue_request_at_triage_drops_its_request_type(client, db_session):
    request_type = make(db_session, "New starter")
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)
    ticket = db_session.exec(select(Ticket)).one()
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id},
    )

    assert response.status_code == 200
    db_session.expire_all()
    ticket = db_session.get(Ticket, ticket.id)
    assert (ticket.type, ticket.request_type_id) == (TicketType.INCIDENT, None)
    assert "Request:" not in client.get(f"/tickets/{ticket.number}").text
    event = db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "request_type")
    ).all()[-1]
    assert (event.from_value, event.to_value, event.detail) == ("New starter", None, "retyped")


def test_triaging_a_catalogue_request_as_a_service_request_keeps_its_type(client, db_session):
    request_type = make(db_session, "New starter")
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)
    ticket = db_session.exec(select(Ticket)).one()
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)

    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )

    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).request_type_id == request_type.id


def test_a_name_clash_that_slips_past_the_check_is_a_friendly_error(client, db_session, monkeypatch):
    admin(client, db_session)
    make(db_session, "Taken")
    monkeypatch.setattr("app.request_types._name_taken", lambda *args, **kwargs: False)  # two admins at once

    response = client.post("/admin/request-types", data={"name": "Taken", "is_active": "on", **form(db_session.exec(select(ApprovalGroup)).first())})

    assert response.status_code == 400 and "already a request type" in response.text


def test_the_audit_entry_says_what_changed(client, db_session):
    admin(client, db_session)
    request_type = make(db_session, "Laptop", "Old text")
    group = db_session.get(ApprovalGroup, request_type.approval_group_id)
    other = group_of(db_session, "Leads", ("lead@example.com",))
    base = {"is_active": "on"}

    client.post(f"/admin/request-types/{request_type.id}", data={"name": "Laptop", "description": "New text", **base, **form(group)})
    client.post(f"/admin/request-types/{request_type.id}", data={"name": "Laptop", "description": "New text", **base, **form(other)})
    client.post(f"/admin/request-types/{request_type.id}", data={"name": "Notebook", "description": "New text", **base, **form(other)})

    details = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "edited", AuditLog.entity_type == "request_type")).all()]
    assert details == ["description changed", "approval group Laptop group -> Leads", "name 'Laptop' -> 'Notebook'"]

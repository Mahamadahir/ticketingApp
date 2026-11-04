"""Request type defaults (#133): a portal request of a type with all its defaults triages itself."""

import pytest
from sqlmodel import select

from app.models import (
    AuditLog,
    Notification,
    RequestType,
    Ticket,
    TicketApproval,
    TicketEvent,
    TicketSource,
    TicketStatus,
    TicketType,
    UserRole,
)
from app.request_types import RequestTypeError, TypeDefaults, create_request_type, self_triage, update_request_type
from app.ticket_service import create_ticket
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_raising import raise_it
from tests.test_request_types import admin, form, group_of, make


def typed(db_session, team, category, name="Laptop", impact="medium", urgency="high", group=True, **fields):
    """A request type with the defaults set (and an approval group unless group=False)."""
    request_type = make(db_session, name) if group else make(db_session, name)
    if not group:
        request_type.approval_group_id = None
    request_type.default_team_id = team.id if team else None
    request_type.default_category_id = category.id if category else None
    request_type.default_impact, request_type.default_urgency = impact, urgency
    for key, value in fields.items():
        setattr(request_type, key, value)
    db_session.add(request_type)
    db_session.commit()
    db_session.refresh(request_type)
    return request_type


def world(db_session):
    team = create_team(db_session, "Service Desk")
    return team, create_category(db_session, team, "Hardware")


def only_ticket(db_session):
    db_session.expire_all()
    return db_session.exec(select(Ticket)).one()


def events(db_session, ticket, event_type):
    return db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == event_type)
    ).all()


# ---- Self-triage -------------------------------------------------------------------------------


def test_a_portal_request_with_all_defaults_triages_itself_into_awaiting_approval(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    login_as(client, "jo@example.com", "Jo Bloggs")

    assert raise_it(client, subject="A laptop", request_type_id=request_type.id).status_code == 204

    ticket = only_ticket(db_session)
    assert ticket.type == TicketType.SERVICE_REQUEST and ticket.request_type_id == request_type.id
    assert (ticket.team_id, ticket.category_id) == (team.id, category.id)
    assert (ticket.impact, ticket.urgency, ticket.priority) == ("medium", "high", "p2")
    assert ticket.triaged_at is not None and ticket.triaged_by_id is None
    assert ticket.status == TicketStatus.AWAITING_APPROVAL and ticket.assigned_agent_id is None
    assert ticket.first_response_due_at is not None and ticket.resolution_due_at is not None
    approval = db_session.exec(select(TicketApproval).where(TicketApproval.ticket_id == ticket.id)).one()
    assert approval.status == "pending"


def test_the_event_and_the_audit_entry_name_the_type_and_the_system(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    ticket = only_ticket(db_session)
    [triaged] = events(db_session, ticket, "triaged")
    assert triaged.actor_email == "system"
    assert "triaged automatically from request type Laptop" in triaged.detail
    [audit] = db_session.exec(
        select(AuditLog).where(AuditLog.entity_id == ticket.id, AuditLog.action == "triaged_automatically")
    ).all()
    assert audit.actor_email == "system"
    assert audit.detail.startswith("Laptop: Hardware")
    # The same actions an agent's triage would leave: the status change and the approval request.
    assert [e.to_value for e in events(db_session, ticket, "status_changed")] == ["awaiting_approval"]
    assert len(events(db_session, ticket, "approval_requested")) == 1


def test_without_an_approval_group_it_opens_unassigned_in_the_team_queue(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, group=False)
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    ticket = only_ticket(db_session)
    assert ticket.status == TicketStatus.OPEN and ticket.assigned_agent_id is None and ticket.team_id == team.id
    assert db_session.exec(select(TicketApproval)).all() == []


def test_the_team_queue_sees_it_and_it_is_not_in_the_triage_queue(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, group=False)
    login_as(client, "jo@example.com")
    raise_it(client, subject="Needs a dock", request_type_id=request_type.id)
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_id = login_as(client, "ann@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    page = client.get(f"/tickets/{ticket.number}")

    assert page.status_code == 200
    assert "Triage</h3>" not in page.text


@pytest.mark.parametrize("missing", ["default_team_id", "default_category_id", "default_impact", "default_urgency"])
def test_a_type_with_incomplete_defaults_behaves_as_today(client, db_session, missing):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, **{missing: None})
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    ticket = only_ticket(db_session)
    assert ticket.status == TicketStatus.NEW and ticket.triaged_at is None and ticket.team_id is None
    assert ticket.type == TicketType.SERVICE_REQUEST and ticket.request_type_id == request_type.id


def test_a_category_that_was_switched_off_since_falls_back_to_the_triage_queue(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    category.is_active = False
    db_session.add(category)
    db_session.commit()
    login_as(client, "jo@example.com")

    assert raise_it(client, request_type_id=request_type.id).status_code == 204

    assert only_ticket(db_session).status == TicketStatus.NEW


def test_a_category_that_moved_to_another_team_falls_back_to_the_triage_queue(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    category.team_id = create_team(db_session, "Network").id
    db_session.add(category)
    db_session.commit()
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    assert only_ticket(db_session).status == TicketStatus.NEW


def test_a_request_without_a_type_or_an_incident_is_not_triaged(client, db_session):
    world(db_session)
    login_as(client, "jo@example.com")
    raise_it(client)

    assert only_ticket(db_session).status == TicketStatus.NEW


def test_a_ticket_from_email_is_never_triaged_by_a_type(db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    ticket = create_ticket(
        db_session, subject="By mail", requester_email="jo@example.com", source=TicketSource.EMAIL,
        ticket_type=TicketType.SERVICE_REQUEST, request_type_id=request_type.id,
    )

    assert self_triage(db_session, ticket, request_type) is False
    assert ticket.triaged_at is None and ticket.status == TicketStatus.NEW


def test_an_agent_raising_for_someone_without_a_category_is_not_triaged_by_the_type(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    agent_id = login_as(client, "ann@example.com", "Ann")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    raise_it(client, for_email="jo@example.com", request_type_id=request_type.id)

    assert only_ticket(db_session).status == TicketStatus.NEW


def test_a_typed_request_that_is_triaged_cannot_be_triaged_again(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, group=False)
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_id = login_as(client, "ann@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id},
    )

    assert response.status_code == 400


def test_self_triage_tells_the_requester_no_more_than_an_agents_triage_does(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, group=False)
    jo = login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    assert db_session.exec(select(Notification).where(Notification.user_id == jo)).all() == []


def test_the_approver_gets_a_bell_entry_and_the_requester_none(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    jo = login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)

    kinds = {n.kind for n in db_session.exec(select(Notification)).all()}
    assert "approval_waiting" in kinds
    assert db_session.exec(select(Notification).where(Notification.user_id == jo)).all() == []


# ---- Admin: the defaults are saved with triage's checks ------------------------------------------


def edit(client, request_type, **fields):
    group_id = request_type.approval_group_id
    data = {"name": request_type.name, "approval_group_id": group_id, "pass_percent": 51, "is_active": "on", **fields}
    return client.post(f"/admin/request-types/{request_type.id}", data=data, follow_redirects=False)


def test_an_admin_saves_the_defaults_and_it_is_audited(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session, "Laptop")
    admin(client, db_session)

    response = edit(
        client, request_type, default_team_id=team.id, default_category_id=category.id,
        default_impact="low", default_urgency="medium",
    )

    assert response.status_code == 303
    db_session.refresh(request_type)
    assert (request_type.default_team_id, request_type.default_category_id) == (team.id, category.id)
    assert (request_type.default_impact, request_type.default_urgency) == ("low", "medium")
    entry = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type", AuditLog.action == "edited")).one()
    assert "defaults" in entry.detail and "Service Desk" in entry.detail and "Hardware" in entry.detail
    page = client.get("/admin/request-types").text
    assert 'name="default_category_id"' in page and f'value="{category.id}" selected' in page


def test_blank_defaults_clear_them(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    admin(client, db_session)

    edit(client, request_type, default_team_id="", default_category_id="", default_impact="", default_urgency="")

    db_session.refresh(request_type)
    assert (request_type.default_team_id, request_type.default_category_id, request_type.default_impact) == (None, None, None)


def test_a_new_type_can_be_created_with_defaults(client, db_session):
    team, category = world(db_session)
    group = group_of(db_session, "Approvers")
    admin(client, db_session)

    client.post(
        "/admin/request-types",
        data={**form(group), "name": "Dock", "is_active": "on", "default_team_id": team.id,
              "default_category_id": category.id, "default_impact": "low", "default_urgency": "low"},
        follow_redirects=False,
    )

    created = db_session.exec(select(RequestType).where(RequestType.name == "Dock")).one()
    assert created.default_category_id == category.id and created.default_urgency == "low"


@pytest.mark.parametrize(
    "bad",
    ["inactive_category", "other_team", "no_such_team", "bad_impact", "bad_urgency"],
)
def test_defaults_get_the_same_checks_as_triage(client, db_session, bad):
    team, category = world(db_session)
    request_type = make(db_session, "Laptop")
    admin(client, db_session)
    fields = {"default_team_id": team.id, "default_category_id": category.id, "default_impact": "low", "default_urgency": "low"}
    if bad == "inactive_category":
        category.is_active = False
        db_session.add(category)
        db_session.commit()
    elif bad == "other_team":
        fields["default_team_id"] = create_team(db_session, "Network").id
    elif bad == "no_such_team":
        fields["default_team_id"] = 9999
    elif bad == "bad_impact":
        fields["default_impact"] = "huge"
    else:
        fields["default_urgency"] = "huge"

    response = edit(client, request_type, **fields)

    assert response.status_code == 400
    db_session.refresh(request_type)
    assert request_type.default_category_id is None


def test_a_category_with_no_team_is_refused(db_session):
    request_type = make(db_session, "Laptop")
    orphan = create_category(db_session, None, "Orphan")

    with pytest.raises(RequestTypeError):
        update_request_type(
            db_session, request_type, "Laptop", "", request_type.approval_group_id, 51, True,
            defaults=TypeDefaults(None, orphan.id, None, None),
        )


def test_updating_without_defaults_leaves_them_alone(db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)

    update_request_type(db_session, request_type, "Laptop", "new text", request_type.approval_group_id, 51, True)

    assert request_type.default_category_id == category.id


def test_only_those_who_manage_request_types_can_save_defaults(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session, "Laptop")
    agent_id = login_as(client, "ann@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    response = edit(client, request_type, default_team_id=team.id, default_category_id=category.id)

    assert response.status_code == 403
    db_session.refresh(request_type)
    assert request_type.default_category_id is None


# ---- The triage form is prefilled -----------------------------------------------------------------


def agent_in(client, db_session, team):
    agent_id = login_as(client, "ann@example.com", "Ann")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


def test_picking_a_kind_prefills_the_types_defaults_out_of_band(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, impact="high", urgency="low")
    agent_in(client, db_session, team)

    html = client.get(f"/tickets/triage-dates?request_type_id={request_type.id}").text

    assert 'id="triage-dates"' in html
    assert 'name="impact" id="triage-impact"' in html and 'value="high" selected' in html
    assert 'value="low" selected' in html
    assert f'value="{category.id}" selected' in html
    assert 'id="triage-agent" hx-swap-oob="true"' in html and "Ann" in html


def test_a_type_with_some_defaults_prefills_only_those(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, None, None, impact="high", urgency=None)
    agent_in(client, db_session, team)

    html = client.get(f"/tickets/triage-dates?request_type_id={request_type.id}").text

    assert 'id="triage-impact"' in html
    assert 'id="triage-urgency"' not in html and 'id="triage-category"' not in html


def test_a_type_without_defaults_or_none_chosen_prefills_nothing(client, db_session):
    team, category = world(db_session)
    plain = make(db_session, "Plain")
    agent_in(client, db_session, team)

    assert "hx-swap-oob" not in client.get(f"/tickets/triage-dates?request_type_id={plain.id}").text
    assert "hx-swap-oob" not in client.get("/tickets/triage-dates?request_type_id=").text


def test_a_switched_off_category_is_not_prefilled(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category, impact=None, urgency=None)
    category.is_active = False
    db_session.add(category)
    db_session.commit()
    agent_in(client, db_session, team)

    assert "hx-swap-oob" not in client.get(f"/tickets/triage-dates?request_type_id={request_type.id}").text


def test_the_triage_form_still_renders_its_selects(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session, "Plain")
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_in(client, db_session, team)

    html = client.get(f"/tickets/{ticket.number}").text

    assert 'name="impact" id="triage-impact" required' in html
    assert 'name="category_id" id="triage-category"' in html and 'hx-get="/tickets/agents-for-category"' in html
    assert 'hx-swap-oob' not in html


def test_the_agent_can_change_the_prefilled_values_when_triaging(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, None, None, impact="high", urgency="high")
    request_type.default_impact = request_type.default_urgency = None
    db_session.add(request_type)
    db_session.commit()
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id)
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_in(client, db_session, team)

    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )

    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).priority == "p4"

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import AuditLog, RequestType, Ticket, TicketStatus, UserRole
from app.required_by import RequiredByError, parse_required_by
from app.settings_store import REQUIRED_BY_LEAD_DAYS, set_setting
from app.sla import required_by_deadline, sla_view
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_raising import desk, last_ticket, raise_it
from tests.test_request_types import make
from tests.test_triage import agent, events

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def today():
    return datetime.now(timezone.utc).date()


def day(offset):
    return (today() + timedelta(days=offset)).isoformat()


def offering(db_session, name="New starter", offers=True):
    request_type = make(db_session, name)
    request_type.offers_required_by = offers
    db_session.add(request_type)
    db_session.commit()
    return request_type


def as_requester(client, email="jo@example.com"):
    client.post("/auth/logout")
    login_as(client, email, "Jo Bloggs")


def audit(db_session, action="required_by_changed"):
    db_session.expire_all()
    return [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == action)).all()]


# ---- Raising with a date --------------------------------------------------------------------


def test_the_requester_raises_a_request_with_a_date_that_replaces_the_resolution_target(client, db_session):
    request_type = offering(db_session)
    as_requester(client)

    response = raise_it(client, request_type_id=request_type.id, required_by=day(5))

    assert response.status_code == 204
    ticket = last_ticket(db_session)
    assert ticket.required_by == today() + timedelta(days=5)
    assert ticket.resolution_due_at == required_by_deadline(ticket.required_by)  # the end of that day
    [event] = events(db_session, ticket, "required_by_changed")
    assert (event.from_value, event.to_value) == (None, day(5))
    assert audit(db_session) == [f"none -> {day(5)}"]


def test_a_request_without_a_date_keeps_the_normal_sla(client, db_session):
    request_type = offering(db_session)
    as_requester(client)

    raise_it(client, request_type_id=request_type.id)

    ticket = last_ticket(db_session)
    assert ticket.required_by is None and ticket.resolution_due_at is None
    assert events(db_session, ticket, "required_by_changed") == []


def test_triage_keeps_the_date_as_the_resolution_deadline_and_the_priority_sets_first_response(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(20))
    ticket = last_ticket(db_session)
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "high", "urgency": "high", "category_id": category.id},
    )

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.resolution_due_at == required_by_deadline(today() + timedelta(days=20))  # not the P1 4 hours
    assert ticket.first_response_due_at is not None


def test_a_past_date_is_refused_and_no_ticket_is_made(client, db_session):
    request_type = offering(db_session)
    as_requester(client)

    response = raise_it(client, request_type_id=request_type.id, required_by=day(-1))

    assert response.status_code == 400 and "past" in response.text
    assert db_session.exec(select(Ticket)).all() == []


def test_today_is_accepted(client, db_session):
    request_type = offering(db_session)
    as_requester(client)

    assert raise_it(client, request_type_id=request_type.id, required_by=day(0)).status_code == 204


def test_a_bad_date_is_refused():
    with pytest.raises(RequiredByError):
        parse_required_by("30/10/2026")
    assert parse_required_by("  ") is None


def test_a_type_that_does_not_offer_the_field_refuses_it(client, db_session):
    request_type = offering(db_session, offers=False)
    as_requester(client)

    response = raise_it(client, request_type_id=request_type.id, required_by=day(5))

    assert response.status_code == 400 and "does not take" in response.text
    assert db_session.exec(select(Ticket)).all() == []


def test_an_ordinary_problem_report_takes_no_date(client, db_session):
    offering(db_session)
    as_requester(client)

    response = raise_it(client, required_by=day(5))

    assert response.status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


# ---- Backdating ------------------------------------------------------------------------------


def agent_raises(client, category, **extra):
    return raise_it(
        client, for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low", **extra
    )


def test_an_agent_who_is_backdating_may_give_a_past_date(client, db_session):
    request_type = offering(db_session)
    _, category, _ = desk(client, db_session)
    reported = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")

    response = agent_raises(
        client, category, request_type_id=request_type.id, required_by=day(-2), reported_at=reported
    )

    assert response.status_code == 204, response.text
    ticket = last_ticket(db_session)
    assert ticket.required_by == today() - timedelta(days=2) and ticket.is_backdated
    assert ticket.resolution_due_at is None  # a backdated ticket has no SLA


def test_an_agent_who_is_not_backdating_cannot_give_a_past_date(client, db_session):
    request_type = offering(db_session)
    _, category, _ = desk(client, db_session)

    response = agent_raises(client, category, request_type_id=request_type.id, required_by=day(-2))

    assert response.status_code == 400 and "past" in response.text


def test_on_the_ticket_page_a_past_date_needs_a_backdated_ticket(client, db_session):
    request_type = offering(db_session)
    _, category, _ = desk(client, db_session)
    agent_raises(client, category, request_type_id=request_type.id)
    ticket = last_ticket(db_session)

    refused = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(-1)})
    ok = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(1)})
    ticket.is_backdated = True
    db_session.add(ticket)
    db_session.commit()
    past_when_backdated = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(-1)})

    assert (refused.status_code, ok.status_code, past_when_backdated.status_code) == (400, 200, 200)
    db_session.refresh(ticket)
    assert ticket.required_by == today() - timedelta(days=1)


# ---- Who may change it, and the audit ----------------------------------------------------------


def test_the_requester_may_change_it_until_triage_and_every_change_is_audited(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)

    assert client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(7)}, follow_redirects=False).status_code == 303
    assert client.post(f"/portal/{ticket.number}/required-by", data={"required_by": ""}, follow_redirects=False).status_code == 303

    db_session.refresh(ticket)
    assert ticket.required_by is None and ticket.resolution_due_at is None
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "required_by_changed")] == [
        (None, day(5)), (day(5), day(7)), (day(7), None),
    ]
    assert audit(db_session) == [f"none -> {day(5)}", f"{day(5)} -> {day(7)}", f"{day(7)} -> none"]


def test_saving_the_same_date_changes_nothing(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)

    client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(5)}, follow_redirects=False)

    assert len(events(db_session, ticket, "required_by_changed")) == 1


def test_the_requester_cannot_change_it_after_triage(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    ticket.triaged_at, ticket.status = NOW, TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()

    response = client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(9)}, follow_redirects=False)

    assert response.status_code == 403
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=5)
    assert "Save date" not in client.get(f"/portal/{ticket.number}").text


def test_someone_else_cannot_change_a_requesters_date(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    as_requester(client, "other@example.com")

    response = client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(9)}, follow_redirects=False)

    assert response.status_code == 404
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=5)


def test_an_agent_who_works_the_ticket_changes_it_after_triage_and_one_who_does_not_cannot(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    desk_team = create_team(db_session, "Service Desk")
    other_team = create_team(db_session, "Network")
    category = create_category(db_session, desk_team, "Hardware")
    client.post("/auth/logout")
    agent(client, db_session, member_of=[desk_team])
    assert client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    ).status_code == 200

    assert client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(8)}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=8)
    assert events(db_session, ticket, "required_by_changed")[-1].actor_email == "agent@example.com"

    client.post("/auth/logout")
    agent(client, db_session, member_of=[other_team], email="stranger@example.com")
    response = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(9)})
    assert response.status_code in (403, 404)
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=8)


def test_a_ticket_whose_type_does_not_offer_the_field_refuses_a_date_from_an_agent(client, db_session):
    request_type = offering(db_session, offers=False)
    _, category, _ = desk(client, db_session)
    agent_raises(client, category, request_type_id=request_type.id)
    ticket = last_ticket(db_session)

    response = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(3)})

    assert response.status_code == 400


# ---- The per-type switch ---------------------------------------------------------------------------


def test_the_admin_switch_turns_the_field_on_and_off_for_a_type(client, db_session):
    request_type = make(db_session, "Laptop")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    base = {
        "name": "Laptop", "description": "", "approval_group_id": request_type.approval_group_id,
        "pass_percent": 51, "is_active": "on",
    }
    assert request_type.offers_required_by is False
    assert 'name="required_by"' not in client.get("/portal/new").text

    client.post(f"/admin/request-types/{request_type.id}", data={**base, "offers_required_by": "on"})
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).offers_required_by is True
    assert 'name="required_by"' in client.get("/portal/new").text

    client.post(f"/admin/request-types/{request_type.id}", data=base)
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).offers_required_by is False
    assert any("Required by date offered" in (d or "") for d in audit(db_session, "edited"))


# ---- The queue --------------------------------------------------------------------------------------


def listed_order(client):
    page = client.get("/tickets").text
    return [name for _, name in sorted((page.index(n), n) for n in ("A-fixed", "B-required"))]


def queue_pair(client, db_session):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])
    from tests.test_triage import new_ticket

    now = datetime.now(timezone.utc)
    for subject, due, required in (
        ("A-fixed", now + timedelta(days=4), None),
        ("B-required", None, today() + timedelta(days=5)),
    ):
        ticket = new_ticket(db_session, team)
        ticket.subject, ticket.triaged_at, ticket.status = subject, now, TicketStatus.OPEN
        ticket.category_id, ticket.priority, ticket.first_responded_at = category.id, "p3", now
        ticket.required_by = required
        ticket.resolution_due_at = required_by_deadline(required) if required else due
        db_session.add(ticket)
    db_session.commit()


def test_the_queue_ranks_a_dated_request_from_the_lead_time_before_its_date(client, db_session):
    queue_pair(client, db_session)

    set_setting(db_session, REQUIRED_BY_LEAD_DAYS, "1")
    db_session.commit()
    # Due 5 to 6 days out, less 1 day, is still after the other ticket's 4 days.
    assert listed_order(client) == ["A-fixed", "B-required"]

    set_setting(db_session, REQUIRED_BY_LEAD_DAYS, "3")
    db_session.commit()
    # Less 3 days it is 2 to 3 days out: ahead of the other ticket.
    assert listed_order(client) == ["B-required", "A-fixed"]


def test_a_dated_request_with_no_first_response_target_is_ranked_by_its_date(client, db_session):
    queue_pair(client, db_session)
    set_setting(db_session, REQUIRED_BY_LEAD_DAYS, "3")
    db_session.commit()
    for ticket in db_session.exec(select(Ticket)).all():
        ticket.first_responded_at = None  # first response still outstanding, but no target is set for it
        ticket.first_response_due_at = None
        db_session.add(ticket)
    db_session.commit()

    assert listed_order(client) == ["B-required", "A-fixed"]


# ---- What shows ---------------------------------------------------------------------------------------


def test_the_countdown_and_pages_say_required_by(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    assert f"Required by: {day(5)}" in client.get(f"/portal/{ticket.number}").text

    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    client.post("/auth/logout")
    agent(client, db_session, member_of=[team])
    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    db_session.refresh(ticket)
    ticket.first_responded_at = NOW
    db_session.add(ticket)
    db_session.commit()

    detail = client.get(f"/tickets/{ticket.number}").text
    assert f"Required by: {day(5)}" in detail and f"SLA: required by {day(5)}, due in" in detail
    assert f"required by {day(5)}, due in" in client.get("/tickets").text


def test_sla_view_marks_a_resolved_dated_ticket_as_required_by(client, db_session):
    ticket = Ticket(
        subject="x", requester_email="a@example.com", priority="p3", status=TicketStatus.RESOLVED,
        required_by=NOW.date(), resolution_due_at=required_by_deadline(NOW.date()), resolved_at=NOW,
    )

    view = sla_view(ticket, NOW, 25)

    assert view.state == "met" and view.which == "required by" and f"required by {NOW.date()}" in view.text


def test_the_lead_time_is_a_setting_with_a_placeholder():
    from app.settings_store import REGISTRY

    assert REGISTRY[REQUIRED_BY_LEAD_DAYS].default == "2"


# ---- Review follow-ups -----------------------------------------------------------------------------


def test_a_date_far_in_the_future_is_refused_not_a_server_error(client, db_session):
    request_type = offering(db_session)
    as_requester(client)

    response = raise_it(client, request_type_id=request_type.id, required_by="9999-12-31")

    assert response.status_code == 400 and "too far" in response.text


def test_the_date_of_a_resolved_request_cannot_be_changed(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    ticket.triaged_at, ticket.status = NOW, TicketStatus.RESOLVED
    db_session.add(ticket)
    db_session.commit()
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)

    response = client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(9)})

    assert response.status_code == 403
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=5)


def test_an_untriaged_dated_request_shows_its_deadline_and_a_waiting_one_keeps_running():
    ticket = Ticket(
        subject="x", requester_email="a@example.com", status=TicketStatus.NEW, required_by=NOW.date() + timedelta(days=3),
        resolution_due_at=required_by_deadline(NOW.date() + timedelta(days=3)), created_at=NOW,
    )
    assert sla_view(ticket, NOW, 25).state == "ok"  # no priority yet, still a deadline to show

    ticket.priority, ticket.status, ticket.sla_paused_at = "p3", TicketStatus.AWAITING_USER, NOW
    assert sla_view(ticket, NOW, 25).state == "ok"  # not "paused"
    ticket.required_by = None
    assert sla_view(ticket, NOW, 25).state == "paused"


def test_a_date_can_be_cleared_after_the_type_stops_offering_it(client, db_session):
    request_type = offering(db_session)
    as_requester(client)
    raise_it(client, request_type_id=request_type.id, required_by=day(5))
    ticket = last_ticket(db_session)
    request_type.offers_required_by = False
    db_session.add(request_type)
    db_session.commit()

    assert "Save date" in client.get(f"/portal/{ticket.number}").text
    client.post(f"/portal/{ticket.number}/required-by", data={"required_by": ""}, follow_redirects=False)

    db_session.refresh(ticket)
    assert ticket.required_by is None
    assert client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(4)}, follow_redirects=False).status_code == 400

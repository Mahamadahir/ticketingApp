"""Form questions on request types and the answers on tickets (#118 part 1)."""

import pytest
from sqlmodel import select

from app.messages import requester_visible_answers
from app.models import (
    AuditLog,
    MembershipLevel,
    RequestTypeQuestion,
    Ticket,
    TicketAnswer,
    TicketEvent,
    TicketSource,
    TicketStatus,
    TicketType,
    UserRole,
)
from app.request_questions import (
    QuestionError,
    add_question,
    answer_rows,
    delete_question,
    move_question,
    questions_for,
    save_answers,
    update_question,
)
from app.request_types import self_triage
from app.ticket_service import create_ticket
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_raising import raise_it
from tests.test_request_types import admin, make
from tests.test_request_type_defaults import typed, world


def question(db_session, request_type, label="Department", kind="text", options="", required=False):
    added = add_question(db_session, request_type, label, kind, options, required)
    db_session.commit()
    db_session.refresh(added)
    return added


def only_ticket(db_session):
    db_session.expire_all()
    return db_session.exec(select(Ticket)).one()


def answers_by_label(db_session, ticket):
    db_session.expire_all()
    return {row["label"]: row["value"] for row in answer_rows(db_session, ticket)}


def email_ticket(db_session, team, request_type=None):
    ticket = create_ticket(
        db_session, subject="Starter", requester_email="boss@example.com", source=TicketSource.EMAIL, team_id=team.id,
        request_type_id=request_type.id if request_type else None,
    )
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_of(client, db_session, team, email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT)
    add_membership(db_session, agent_id, team.id)
    return agent_id


# ---- Admin: managing questions -------------------------------------------------------------------


def test_an_admin_adds_edits_orders_and_deletes_questions_with_audit(client, db_session):
    request_type = make(db_session)
    admin(client, db_session)

    added = client.post(
        f"/admin/request-types/{request_type.id}/questions",
        data={"label": " Department ", "kind": "choice", "options": "Finance\nSales\n\nIT\n", "is_required": "on"},
        follow_redirects=False,
    )
    assert added.status_code == 303
    client.post(f"/admin/request-types/{request_type.id}/questions", data={"label": "Start date", "kind": "date"})
    first, second = questions_for(db_session, request_type.id)
    assert (first.label, first.kind, first.options, first.is_required) == ("Department", "choice", ["Finance", "Sales", "IT"], True)
    assert (second.label, second.kind, second.is_required) == ("Start date", "date", False)

    assert client.post(f"/admin/request-types/questions/{second.id}/move", data={"direction": "up"}).status_code in (200, 303)
    db_session.expire_all()
    assert [q.label for q in questions_for(db_session, request_type.id)] == ["Start date", "Department"]

    client.post(
        f"/admin/request-types/questions/{first.id}",
        data={"label": "Which department", "options": "Finance\nSales", "is_active": "on"},
    )
    db_session.expire_all()
    first = db_session.get(RequestTypeQuestion, first.id)
    assert (first.label, first.options, first.is_required) == ("Which department", ["Finance", "Sales"], False)

    client.post(f"/admin/request-types/questions/{second.id}/delete")
    db_session.expire_all()
    assert [q.label for q in questions_for(db_session, request_type.id)] == ["Which department"]
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type")).all()]
    assert {"question_added", "question_moved", "question_edited", "question_deleted"} <= set(actions)


def test_moving_the_first_question_up_does_nothing(client, db_session):
    request_type = make(db_session)
    a, b = question(db_session, request_type, "A"), question(db_session, request_type, "B")
    assert move_question(db_session, a, up=True) is False
    assert move_question(db_session, b, up=False) is False
    assert [q.label for q in questions_for(db_session, request_type.id)] == ["A", "B"]


@pytest.mark.parametrize(
    "data",
    [
        {"label": "", "kind": "text"},
        {"label": "x" * 201, "kind": "text"},
        {"label": "Q", "kind": "number"},
        {"label": "Q", "kind": "choice", "options": "Only one"},
        {"label": "Q", "kind": "choice", "options": "Same\nsame"},
    ],
)
def test_a_bad_question_is_refused(client, db_session, data):
    request_type = make(db_session)
    admin(client, db_session)
    assert client.post(f"/admin/request-types/{request_type.id}/questions", data=data).status_code == 400
    assert questions_for(db_session, request_type.id, include_inactive=True) == []


def test_a_question_that_was_answered_is_switched_off_not_deleted(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    asked = question(db_session, request_type)
    ticket = email_ticket(db_session, team, request_type)
    save_answers(db_session, ticket, {asked.id: "Finance"}, "agent@example.com", first_time=True)
    db_session.commit()
    admin(client, db_session)

    assert client.post(f"/admin/request-types/questions/{asked.id}/delete").status_code == 400
    assert db_session.get(RequestTypeQuestion, asked.id) is not None
    client.post(f"/admin/request-types/questions/{asked.id}", data={"label": "Department"})  # no is_active box: off
    db_session.expire_all()
    assert db_session.get(RequestTypeQuestion, asked.id).is_active is False
    assert questions_for(db_session, request_type.id) == []
    # The answer is still on the ticket, as it was given.
    assert answers_by_label(db_session, ticket) == {"Department": "Finance"}


def test_the_kind_of_a_question_cannot_change_and_the_page_lists_the_questions(client, db_session):
    request_type = make(db_session)
    asked = question(db_session, request_type, "Needs a laptop", "yes_no")
    admin(client, db_session)
    page = client.get("/admin/request-types")
    assert page.status_code == 200 and "Needs a laptop" in page.text and "Yes / no" in page.text
    update_question(db_session, asked, "Needs a laptop?", "ignored", False, True)
    assert (asked.kind, asked.options) == ("yes_no", [])


def test_only_a_template_manager_can_manage_questions(client, db_session):
    request_type = make(db_session)
    asked = question(db_session, request_type)
    login_as(client, "agent@example.com")
    assert client.post(f"/admin/request-types/{request_type.id}/questions", data={"label": "Q", "kind": "text"}).status_code == 403
    assert client.post(f"/admin/request-types/questions/{asked.id}", data={"label": "Q"}).status_code == 403
    assert client.post(f"/admin/request-types/questions/{asked.id}/move", data={"direction": "up"}).status_code == 403
    assert client.post(f"/admin/request-types/questions/{asked.id}/delete").status_code == 403


# ---- The portal form ----------------------------------------------------------------------------


def test_the_portal_form_shows_the_chosen_types_questions(client, db_session):
    request_type = make(db_session)
    other = make(db_session, "Software")
    question(db_session, request_type, "Department", "choice", "Finance\nSales", True)
    question(db_session, request_type, "Needs a laptop", "yes_no")
    question(db_session, other, "Which software")
    off = question(db_session, request_type, "Old question")
    update_question(db_session, off, "Old question", "", False, False)
    db_session.commit()
    login_as(client, "jo@example.com")

    fragment = client.get("/portal/questions", params={"request_type_id": request_type.id}).text
    assert 'id="request-questions"' in fragment
    assert "Department (required)" in fragment and "Finance" in fragment and "Needs a laptop" in fragment
    assert "Which software" not in fragment and "Old question" not in fragment
    assert "q_" in client.get("/portal/questions", params={"request_type_id": request_type.id}).text
    assert "Department" not in client.get("/portal/questions").text
    # New request shows the questions of the chosen kind (a plain GET, no script needed), and none for the problem report
    chosen = client.get("/portal/new", params={"request_type_id": request_type.id}).text
    assert "Department (required)" in chosen and "Needs a laptop" in chosen and "Which software" not in chosen
    assert "Department" not in client.get("/portal/new").text


def test_a_portal_request_stores_its_answers_and_shows_them_to_the_requester(client, db_session):
    request_type = make(db_session)
    department = question(db_session, request_type, "Department", "choice", "Finance\nSales", True)
    laptop = question(db_session, request_type, "Needs a laptop", "yes_no")
    start = question(db_session, request_type, "Start date", "date")
    login_as(client, "jo@example.com", "Jo Bloggs")

    response = raise_it(
        client, request_type_id=request_type.id,
        **{f"q_{department.id}": "finance", f"q_{laptop.id}": "yes", f"q_{start.id}": ""},
    )
    assert response.status_code == 204

    ticket = only_ticket(db_session)
    assert answers_by_label(db_session, ticket) == {"Department": "Finance", "Needs a laptop": "yes"}
    page = client.get(f"/portal/{ticket.number}").text
    assert "Your answers" in page and "Department" in page and "Finance" in page and "Yes" in page
    assert "Start date" not in page
    assert [(a["label"], a["display"]) for a in requester_visible_answers(db_session, ticket)] == [
        ("Department", "Finance"), ("Needs a laptop", "Yes"),
    ]
    assert [e.to_value for e in db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "answer_changed")).all()] == [
        "Finance", "Yes",
    ]


def test_a_required_question_must_be_answered_to_raise_the_request(client, db_session):
    request_type = make(db_session)
    department = question(db_session, request_type, "Department", required=True)
    login_as(client, "jo@example.com")

    refused = raise_it(client, request_type_id=request_type.id)
    assert refused.status_code == 400 and "Department" in refused.text
    assert raise_it(client, request_type_id=request_type.id, **{f"q_{department.id}": "   "}).status_code == 400
    assert db_session.exec(select(Ticket)).all() == []
    assert raise_it(client, request_type_id=request_type.id, **{f"q_{department.id}": "Sales"}).status_code == 204


@pytest.mark.parametrize(
    "kind, options, bad",
    [("choice", "A\nB", "C"), ("yes_no", "", "maybe"), ("date", "", "tomorrow"), ("text", "", "x" * 501)],
)
def test_an_answer_that_does_not_fit_its_question_is_refused(client, db_session, kind, options, bad):
    request_type = make(db_session)
    asked = question(db_session, request_type, "Q", kind, options)
    login_as(client, "jo@example.com")
    assert raise_it(client, request_type_id=request_type.id, **{f"q_{asked.id}": bad}).status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


def test_answers_to_other_questions_and_incidents_are_ignored(client, db_session):
    request_type = make(db_session)
    other = make(db_session, "Software")
    mine = question(db_session, request_type, "Mine")
    foreign = question(db_session, other, "Foreign")
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id, **{f"q_{mine.id}": "a", f"q_{foreign.id}": "b", "q_9999": "c"})
    assert answers_by_label(db_session, only_ticket(db_session)) == {"Mine": "a"}


def test_a_followers_portal_view_shows_answers_too_but_not_the_internal_form(client, db_session):
    request_type = make(db_session)
    asked = question(db_session, request_type, "Department")
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id, **{f"q_{asked.id}": "Finance"})
    ticket = only_ticket(db_session)
    page = client.get(f"/portal/{ticket.number}").text
    assert "Finance" in page and "Save answers" not in page


# ---- Self-triage ------------------------------------------------------------------------------


def test_a_portal_request_with_its_required_answers_self_triages(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    asked = question(db_session, request_type, "Department", required=True)
    login_as(client, "jo@example.com")

    assert raise_it(client, request_type_id=request_type.id, **{f"q_{asked.id}": "Sales"}).status_code == 204
    ticket = only_ticket(db_session)
    assert ticket.triaged_at is not None and answers_by_label(db_session, ticket) == {"Department": "Sales"}


def test_a_request_missing_a_required_answer_does_not_self_triage(client, db_session):
    team, category = world(db_session)
    request_type = typed(db_session, team, category)
    ticket = email_ticket(db_session, team, request_type)
    ticket.source = TicketSource.PORTAL
    db_session.add(ticket)
    question(db_session, request_type, "Department", required=True)
    assert self_triage(db_session, ticket, request_type) is False
    assert ticket.triaged_at is None and ticket.status == TicketStatus.NEW


# ---- Triage of an email request ------------------------------------------------------------------


def triage_post(client, ticket, category, request_type, **extra):
    data = {
        "ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id,
        "request_type_id": request_type.id, **extra,
    }
    return client.post(f"/tickets/{ticket.number}/triage", data=data)


def test_the_triage_form_shows_the_questions_of_the_chosen_kind(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    question(db_session, request_type, "Department", required=True)
    ticket = email_ticket(db_session, team)
    agent_of(client, db_session, team)

    fragment = client.get("/tickets/triage-dates", params={"request_type_id": request_type.id, "number": ticket.number}).text
    assert 'id="triage-questions"' in fragment and 'hx-swap-oob="true"' in fragment and "Department (required)" in fragment
    assert "Department" not in client.get("/tickets/triage-dates", params={"number": ticket.number}).text.replace("hx-swap-oob", "")
    assert f"triage-dates?number={ticket.number}" in client.get(f"/tickets/{ticket.number}").text


def test_triage_waits_for_the_required_answers_then_stores_them(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    department = question(db_session, request_type, "Department", required=True)
    optional = question(db_session, request_type, "Notes")
    ticket = email_ticket(db_session, team)
    agent_of(client, db_session, team)

    refused = triage_post(client, ticket, category, request_type, **{f"q_{optional.id}": "hi"})
    assert refused.status_code == 400 and "Department" in refused.text
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).triaged_at is None
    assert db_session.exec(select(TicketAnswer)).all() == []  # nothing half-saved

    assert triage_post(client, ticket, category, request_type, **{f"q_{department.id}": "Finance"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).triaged_at is not None
    assert answers_by_label(db_session, db_session.get(Ticket, ticket.id)) == {"Department": "Finance"}


def test_triage_with_a_kind_that_has_no_required_questions_needs_no_answers(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    question(db_session, request_type, "Notes")
    ticket = email_ticket(db_session, team)
    agent_of(client, db_session, team)
    assert triage_post(client, ticket, category, request_type).status_code == 200


def test_a_ticket_retyped_to_an_incident_at_triage_needs_no_answers(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    question(db_session, request_type, "Department", required=True)
    ticket = email_ticket(db_session, team, request_type)
    ticket.type = TicketType.SERVICE_REQUEST
    db_session.add(ticket)
    db_session.commit()
    agent_of(client, db_session, team)
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    assert response.status_code == 200 and only_ticket(db_session).triaged_at is not None


def test_a_portal_ticket_waiting_in_new_keeps_its_answers_on_the_triage_form(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    asked = question(db_session, request_type, "Department", required=True)
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=request_type.id, **{f"q_{asked.id}": "Sales"})
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_of(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'value="Sales"' in page
    # Triage with "Leave as is" for the kind: the stored answers satisfy the required question.
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    assert response.status_code == 200 and only_ticket(db_session).triaged_at is not None


# ---- Editing answers later ---------------------------------------------------------------------


def triaged_with_answers(client, db_session, team, category):
    request_type = make(db_session)
    department = question(db_session, request_type, "Department", "choice", "Finance\nSales", True)
    notes = question(db_session, request_type, "Notes")
    ticket = email_ticket(db_session, team)
    agent_id = agent_of(client, db_session, team)
    assert triage_post(client, ticket, category, request_type, **{f"q_{department.id}": "Finance"}).status_code == 200
    return ticket, department, notes, agent_id


def test_an_agent_who_works_the_ticket_edits_answers_with_events_audit_and_the_flag(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).answers_changed_at is None  # the first filling in is not a change

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Request details" in page and "Save answers" in page

    response = client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": "Sales", f"q_{notes.id}": "Needs a desk"})
    assert response.status_code == 200
    db_session.expire_all()
    ticket = db_session.get(Ticket, ticket.id)
    assert answers_by_label(db_session, ticket) == {"Department": "Sales", "Notes": "Needs a desk"}
    assert ticket.answers_changed_at is not None
    changes = db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "answer_changed")
    ).all()
    assert [(e.detail, e.from_value, e.to_value) for e in changes][-2:] == [
        ("Department", "Finance", "Sales"), ("Notes", None, "Needs a desk"),
    ]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "answers_changed")).one()
    assert (audit.entity_id, audit.detail) == (ticket.id, "Department; Notes")
    assert "were changed" in client.get(f"/tickets/{ticket.number}").text


def test_editing_without_changing_anything_records_nothing(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": "Finance"})
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).answers_changed_at is None
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "answers_changed")).all() == []


def test_a_required_answer_cannot_be_cleared_or_set_to_something_invalid(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": ""}).status_code == 400
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": "Legal"}).status_code == 400
    assert answers_by_label(db_session, db_session.get(Ticket, ticket.id)) == {"Department": "Finance"}


def test_clearing_an_optional_answer_removes_it(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    client.post(f"/tickets/{ticket.number}/answers", data={f"q_{notes.id}": "x"})
    client.post(f"/tickets/{ticket.number}/answers", data={f"q_{notes.id}": ""})
    assert answers_by_label(db_session, db_session.get(Ticket, ticket.id)) == {"Department": "Finance"}


def test_who_may_edit_answers(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    data = {f"q_{notes.id}": "x"}

    # An agent outside the team cannot even open it; a plain user is not an agent.
    client.post("/auth/logout")
    other_team = create_team(db_session, "Network")
    agent_of(client, db_session, other_team, "other@example.com")
    assert client.post(f"/tickets/{ticket.number}/answers", data=data).status_code in (403, 404)
    # A viewer of the team can read the ticket but not work it, so cannot change the answers.
    client.post("/auth/logout")
    viewer_id = login_as(client, "viewer@example.com")
    set_role(db_session, viewer_id, UserRole.AGENT)
    add_membership(db_session, viewer_id, team.id, MembershipLevel.VIEWER)
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/answers", data=data).status_code == 403
    assert "Save answers" not in client.get(f"/tickets/{ticket.number}").text
    client.post("/auth/logout")
    login_as(client, "boss@example.com")  # the requester
    assert client.post(f"/tickets/{ticket.number}/answers", data=data).status_code == 403
    assert "Save answers" not in client.get(f"/portal/{ticket.number}").text
    assert db_session.exec(select(TicketAnswer).where(TicketAnswer.value == "x")).all() == []


def test_a_finished_ticket_cannot_have_its_answers_edited(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    stored = db_session.get(Ticket, ticket.id)
    stored.status = TicketStatus.CLOSED
    db_session.add(stored)
    db_session.commit()
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{notes.id}": "x"}).status_code == 403


# ---- What part 2 relies on ---------------------------------------------------------------------


def test_an_answer_keeps_the_question_as_it_was_asked_and_a_stable_id(db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    asked = question(db_session, request_type, "Department", "choice", "Finance\nSales")
    ticket = email_ticket(db_session, team, request_type)
    save_answers(db_session, ticket, {asked.id: "sales"}, "a@example.com", first_time=True)
    update_question(db_session, asked, "Which department", "Finance\nSales\nLegal", True, True)
    db_session.commit()

    [row] = db_session.exec(select(TicketAnswer)).all()
    # Canonical text (not the typed case) under the stable question id, with the label as answered:
    # equals / not equals / is one of compare `value`, is answered is the row existing.
    assert (row.question_id, row.value, row.question_label, row.question_kind) == (asked.id, "Sales", "Department", "choice")


def test_save_answers_changes_nothing_when_one_answer_is_invalid(db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    a, b = question(db_session, request_type, "A"), question(db_session, request_type, "B", "date")
    ticket = email_ticket(db_session, team, request_type)
    with pytest.raises(QuestionError):
        save_answers(db_session, ticket, {a.id: "fine", b.id: "not a date"}, "a@example.com", first_time=True)
    assert db_session.exec(select(TicketAnswer)).all() == []


def test_delete_question_without_answers_works_and_with_answers_does_not(db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    a, b = question(db_session, request_type, "A"), question(db_session, request_type, "B")
    ticket = email_ticket(db_session, team, request_type)
    save_answers(db_session, ticket, {a.id: "x"}, "a@example.com", first_time=True)
    with pytest.raises(QuestionError):
        delete_question(db_session, a)
    delete_question(db_session, b)
    assert [q.label for q in questions_for(db_session, request_type.id, include_inactive=True)] == ["A"]


# ---- Found in review ----------------------------------------------------------------------------


def test_an_answer_whose_option_was_removed_survives_saving_other_answers(client, db_session):
    team, category = world(db_session)
    ticket, department, notes, _ = triaged_with_answers(client, db_session, team, category)
    update_question(db_session, db_session.get(RequestTypeQuestion, department.id), "Department", "Sales\nLegal", True, True)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Finance (no longer listed)" in page
    # What the browser posts back: the old value is still selected, so it is kept and the other answer saves.
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": "Finance", f"q_{notes.id}": "x"}).status_code == 200
    assert answers_by_label(db_session, db_session.get(Ticket, ticket.id)) == {"Department": "Finance", "Notes": "x"}
    # A new answer must still come from the current list.
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{department.id}": "Marketing"}).status_code == 400


def test_a_field_name_that_only_looks_like_a_number_is_ignored(client, db_session):
    request_type = make(db_session)
    login_as(client, "jo@example.com")
    assert raise_it(client, request_type_id=request_type.id, **{"q_²": "x", "q_" + "9" * 5000: "y"}).status_code == 204


def test_answers_of_the_old_kind_are_dropped_when_triage_picks_another_kind(client, db_session):
    team, category = world(db_session)
    first, second = make(db_session, "Laptop"), make(db_session, "Phone")
    old = question(db_session, first, "Model")
    new = question(db_session, second, "Number", required=True)
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=first.id, **{f"q_{old.id}": "X1"})
    ticket = only_ticket(db_session)
    client.post("/auth/logout")
    agent_of(client, db_session, team)

    assert triage_post(client, ticket, category, second, **{f"q_{new.id}": "0123"}).status_code == 200
    assert answers_by_label(db_session, db_session.get(Ticket, ticket.id)) == {"Number": "0123"}
    assert not db_session.exec(select(TicketAnswer).where(TicketAnswer.question_id == old.id)).all()


def test_answers_cannot_be_edited_before_triage(client, db_session):
    team, category = world(db_session)
    request_type = make(db_session)
    asked = question(db_session, request_type, "Department")
    ticket = email_ticket(db_session, team, request_type)
    agent_of(client, db_session, team)
    assert client.post(f"/tickets/{ticket.number}/answers", data={f"q_{asked.id}": "x"}).status_code == 403
    assert db_session.get(Ticket, ticket.id).answers_changed_at is None

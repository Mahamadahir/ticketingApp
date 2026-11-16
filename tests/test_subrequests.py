"""Sub-request templates on a request type (#118 part 2): conditions, creation after approval or at triage, the agent's
list, assignment, Due offsets, the shared cap, the per-kind own-approval option and the changed-answers note."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import approvals
from app.models import (
    ApprovalVote,
    AuditLog,
    Notification,
    RequestSubTemplate,
    Ticket,
    TicketApproval,
    TicketEvent,
    TicketSource,
    TicketStatus,
    User,
    UserRole,
)
from app.request_questions import QuestionError, add_question, delete_question, save_answers
from app.settings_store import MAX_SUB_TICKETS, set_setting
from app.splitting import SplitError, split_ticket
from app.subrequests import (
    TemplateError,
    after_triage,
    condition_holds,
    format_conditions,
    parse_conditions,
    render_subject,
    save_template,
    template_applies,
)
from app.ticket_service import create_ticket
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_raising import raise_it
from tests.test_request_questions import agent_of, question, triage_post
from tests.test_request_type_defaults import events, typed, world
from tests.test_request_types import admin, group_of, make
from tests.test_approvals import vote_of


def all_tickets(db_session):
    db_session.expire_all()
    return list(db_session.exec(select(Ticket).order_by(Ticket.id)).all())


def parent_of(db_session):
    """The request a test raised (the others are what it made, or tickets the test made for other reasons)."""
    return next(t for t in all_tickets(db_session) if t.split_from_id is None and t.subject in ("A new starter", "Starter"))


def subs_of(db_session, parent):
    db_session.expire_all()
    return list(db_session.exec(select(Ticket).where(Ticket.split_from_id == parent.id).order_by(Ticket.id)).all())


def it_team(db_session):
    team = create_team(db_session, "IT Support")
    return team, create_category(db_session, team, "Laptops")


def template(db_session, request_type, team, category, pattern="Laptop for {Name}", conditions="", **kw):
    made = save_template(
        db_session, request_type, None, subject_pattern=pattern, team_id=team.id, category_id=category.id,
        conditions_text=conditions, **kw,
    )
    db_session.commit()
    db_session.refresh(made)
    return made


def starter(db_session, approval=False):
    """A request type "New starter" (self-triaging, with a Name question and a Department pick list) and the IT team."""
    team, category = world(db_session)
    request_type = typed(db_session, team, category, name="New starter", group=approval)
    name = question(db_session, request_type, "Name", "text", required=True)
    department = question(db_session, request_type, "Department", "choice", "Finance\nSales\nIT")
    laptop = question(db_session, request_type, "Needs a laptop", "yes_no")
    it, laptops = it_team(db_session)
    return request_type, name, department, laptop, it, laptops


def raise_starter(client, request_type, name, department=None, laptop=None, **extra):
    data = {f"q_{name.id}": "Ann Lee", **extra}
    if department is not None:
        data[f"q_{department.id}"] = "Finance"
    if laptop is not None:
        data[f"q_{laptop.id}"] = "yes"
    return raise_it(client, subject="A new starter", request_type_id=request_type.id, **data)


# ---- Conditions ----------------------------------------------------------------------------------


def condition(question_id, operator, value=""):
    return {"question_id": question_id, "operator": operator, "value": value}


def test_each_operator_compares_the_canonical_answer():
    answers = {1: "Finance", 2: "yes"}
    assert condition_holds(condition(1, "equals", "finance"), answers)
    assert not condition_holds(condition(1, "equals", "Sales"), answers)
    assert not condition_holds(condition(3, "equals", "Sales"), answers)
    assert condition_holds(condition(1, "not_equals", "Sales"), answers)
    assert not condition_holds(condition(1, "not_equals", "Finance"), answers)
    assert condition_holds(condition(3, "not_equals", "Sales"), answers)  # never answered: does not equal it
    assert condition_holds(condition(1, "one_of", "Sales\nFinance"), answers)
    assert not condition_holds(condition(1, "one_of", "Sales\nIT"), answers)
    assert not condition_holds(condition(3, "one_of", "Sales\nIT"), answers)
    assert condition_holds(condition(2, "answered"), answers) and not condition_holds(condition(3, "answered"), answers)
    assert not condition_holds(condition(1, "something else", "x"), answers)


def test_conditions_in_a_group_are_anded_and_groups_are_ored_and_none_means_always():
    answers = {1: "Finance", 2: "yes"}
    one = RequestSubTemplate(condition_groups=[[condition(1, "equals", "Finance"), condition(2, "equals", "yes")]])
    two = RequestSubTemplate(condition_groups=[[condition(1, "equals", "Finance"), condition(2, "equals", "no")]])
    either = RequestSubTemplate(condition_groups=[
        [condition(1, "equals", "Sales")], [condition(2, "equals", "yes")],
    ])
    neither = RequestSubTemplate(condition_groups=[[condition(1, "equals", "Sales")], [condition(2, "equals", "no")]])
    assert template_applies(one, answers) and not template_applies(two, answers)
    assert template_applies(either, answers) and not template_applies(neither, answers)
    assert template_applies(RequestSubTemplate(condition_groups=[]), answers)


def test_conditions_are_typed_one_per_line_with_groups_and_read_back(db_session):
    request_type, name, department, laptop, _, _ = starter(db_session)
    text = "Department = finance\nNeeds a laptop != no\n---\nDepartment in Sales | IT\n---\nName is answered"
    groups = parse_conditions(db_session, request_type.id, text)
    assert groups == [
        [condition(department.id, "equals", "Finance"), condition(laptop.id, "not_equals", "no")],
        [condition(department.id, "one_of", "Sales\nIT")],
        [condition(name.id, "answered")],
    ]
    assert format_conditions(db_session, groups) == (
        "Department = Finance\nNeeds a laptop != no\n---\nDepartment in Sales | IT\n---\nName is answered"
    )
    assert parse_conditions(db_session, request_type.id, "  \n") == []


@pytest.mark.parametrize("text", [
    "Shoe size = 9",  # no such question
    "Department = Marketing",  # not an option of the pick list
    "Needs a laptop = maybe",
    "Department in",
    "Department sort of Finance",
])
def test_an_unreadable_or_invalid_condition_is_refused(db_session, text):
    request_type, *_ = starter(db_session)
    with pytest.raises(TemplateError):
        parse_conditions(db_session, request_type.id, text)


def test_the_subject_uses_answers_and_braces_in_an_answer_stay_literal():
    answers = {"name": "{Department} {x}", "department": "Finance"}
    assert render_subject("Laptop for {Name}", answers, "fallback") == "Laptop for {Department} {x}"
    assert render_subject("{name} / {DEPARTMENT} {Unknown} {", answers, "f") == "{Department} {x} / Finance {Unknown} {"
    assert render_subject("Laptop for {Name}", {"name": ""}, "fallback") == "Laptop for"
    assert render_subject("{Name}", {"name": ""}, "fallback") == "fallback"
    assert len(render_subject("x" * 500, {}, "f")) == 200


# ---- Managing templates ----------------------------------------------------------------------------


def post_template(client, request_type, team, category, **fields):
    data = {"subject_pattern": "Laptop for {Name}", "team_id": team.id, "category_id": category.id, **fields}
    return client.post(f"/admin/request-types/{request_type.id}/subrequest-templates", data=data, follow_redirects=False)


def test_an_admin_adds_edits_orders_and_deletes_templates_with_audit(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session)
    admin(client, db_session)

    added = post_template(
        client, request_type, it, laptops, impact="low", urgency="medium", due_days_before="5",
        conditions="Department = Finance\nNeeds a laptop = yes",
    )
    assert added.status_code == 303
    [made] = db_session.exec(select(RequestSubTemplate)).all()
    assert (made.subject_pattern, made.team_id, made.category_id, made.impact, made.urgency, made.due_days_before) == (
        "Laptop for {Name}", it.id, laptops.id, "low", "medium", 5,
    )
    assert made.condition_groups == [[condition(department.id, "equals", "Finance"), condition(laptop.id, "equals", "yes")]]
    second = post_template(client, request_type, it, laptops, subject_pattern="Phone for {Name}")
    assert second.status_code == 303 and len(db_session.exec(select(RequestSubTemplate)).all()) == 2
    page = client.get(f"/admin/request-types?type={request_type.id}").text  # the editor (9K)
    assert "Sub-request templates for New starter" in page and "Laptop for {Name}" in page and "Department = Finance" in page

    edited = client.post(
        f"/admin/request-types/subrequest-templates/{made.id}",
        data={"subject_pattern": "Laptop for {name}", "team_id": it.id, "category_id": laptops.id, "assignee": "parent", "is_active": "on"},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    db_session.expire_all()
    made = db_session.get(RequestSubTemplate, made.id)
    assert made.assign_to_parent_assignee and made.assignee_id is None and made.condition_groups == []

    client.post(f"/admin/request-types/subrequest-templates/{made.id}/move", data={"direction": "down"})
    db_session.expire_all()
    assert [t.subject_pattern for t in db_session.exec(select(RequestSubTemplate).order_by(RequestSubTemplate.position)).all()] == [
        "Phone for {Name}", "Laptop for {name}",
    ]
    client.post(f"/admin/request-types/subrequest-templates/{made.id}/delete")
    assert [t.subject_pattern for t in db_session.exec(select(RequestSubTemplate)).all()] == ["Phone for {Name}"]
    assert [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type")).all()
            if a.action.startswith("subrequest_template")] == [
        "subrequest_template_added", "subrequest_template_added", "subrequest_template_edited",
        "subrequest_template_moved", "subrequest_template_deleted",
    ]


def test_only_someone_who_may_manage_request_templates_manages_the_templates(client, db_session):
    request_type, _, _, _, it, laptops = starter(db_session)
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, it.id)
    assert post_template(client, request_type, it, laptops).status_code == 403
    assert db_session.exec(select(RequestSubTemplate)).all() == []


@pytest.mark.parametrize("fields, message", [
    ({"subject_pattern": "  "}, "needs a subject"),
    ({"subject_pattern": "Laptop for {Nmae}"}, "not one of this request type's questions"),
    ({"team_id": ""}, "Choose the team"),
    ({"assignee": "parent", "conditions": "Shoe size = 9"}, "Cannot read the condition"),
    ({"due_days_before": "400"}, "from 0 to 365"),
    ({"impact": "enormous"}, "Choose the impact"),
])
def test_a_template_that_does_not_hold_together_is_refused(client, db_session, fields, message):
    request_type, _, _, _, it, laptops = starter(db_session)
    admin(client, db_session)
    response = post_template(client, request_type, it, laptops, **fields)
    assert response.status_code == 400 and message in response.text
    assert db_session.exec(select(RequestSubTemplate)).all() == []


def test_the_category_must_belong_to_the_team_and_an_agent_must_be_in_it(client, db_session):
    request_type, _, _, _, it, laptops = starter(db_session)
    desk = create_team(db_session, "Desk")
    admin(client, db_session)
    assert "different team" in post_template(client, request_type, desk, laptops).text
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT)
    admin(client, db_session)
    assert "member of IT Support" in post_template(client, request_type, it, laptops, assignee=outsider).text
    member = login_as(client, "member@example.com")
    set_role(db_session, member, UserRole.AGENT, it.id)
    admin(client, db_session)
    assert post_template(client, request_type, it, laptops, assignee=member).status_code == 303
    with pytest.raises(TemplateError, match="not both"):  # a named agent and "the parent's assignee" make no sense together
        save_template(
            db_session, request_type, None, subject_pattern="x", team_id=it.id, category_id=laptops.id,
             assignee_id=member, assign_to_parent_assignee=True)


def test_a_question_a_condition_uses_cannot_be_deleted(db_session):
    request_type, _, department, _, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops, conditions="Department = Sales")
    with pytest.raises(QuestionError, match="condition"):
        delete_question(db_session, department)


def test_the_own_approval_option_is_set_on_the_request_type_with_audit(client, db_session):
    request_type, *_ = starter(db_session)
    admin(client, db_session)
    other = make(db_session, "Finance access")
    form = {
        "name": other.name, "approval_group_id": other.approval_group_id, "pass_percent": 51, "is_active": "on",
        "subrequests_need_own_approval": "on",
    }
    assert client.post(f"/admin/request-types/{other.id}", data=form, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(type(other), other.id).subrequests_need_own_approval is True
    assert any("need their own approval" in (a.detail or "") for a in db_session.exec(select(AuditLog)).all())


# ---- Creation: a portal request that triages itself ---------------------------------------------------


def test_a_request_needing_no_approval_makes_its_sub_requests_at_triage(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops, "Laptop for {Name}", "Needs a laptop = yes")
    template(db_session, request_type, it, laptops, "Finance system access for {Name}", "Department = Finance")
    template(db_session, request_type, it, laptops, "Sales kit for {Name}", "Department = Sales")
    template(db_session, request_type, it, laptops, "Account for {Name}")
    login_as(client, "jo@example.com", "Jo Bloggs")

    assert raise_starter(client, request_type, name, department, laptop).status_code == 204

    parent = parent_of(db_session)
    subs = subs_of(db_session, parent)
    assert [s.subject for s in subs] == ["Laptop for Ann Lee", "Finance system access for Ann Lee", "Account for Ann Lee"]
    for sub in subs:
        assert sub.split_from_id == parent.id and sub.requester_email == "jo@example.com" and sub.source == parent.source
        assert (sub.team_id, sub.category_id) == (it.id, laptops.id)
        assert sub.triaged_at is not None and sub.status == TicketStatus.OPEN and sub.assigned_agent_id is None
        assert (sub.impact, sub.urgency) == (parent.impact, parent.urgency)  # from the parent when the template has none
    assert [e.to_value for e in events(db_session, parent, "subrequest_created")] == [s.tag for s in subs]
    assert all(len(events(db_session, s, "split_from")) == 1 for s in subs)
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "subrequests_created")).all()
    assert audit.entity_id == parent.id and audit.actor_email == "system" and audit.detail == ", ".join(s.tag for s in subs)
    assert parent.status == TicketStatus.OPEN  # all the same status: the parent follows its parts


def test_what_a_template_sets_is_what_the_sub_request_gets(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    other_type = make(db_session, "Laptop request")
    template(db_session, request_type, it, laptops, "Laptop for {Name}", impact="high", urgency="low", sub_request_type_id=other_type.id)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    [sub] = subs_of(db_session, parent_of(db_session))
    assert (sub.impact, sub.urgency, sub.request_type_id) == ("high", "low", other_type.id)


def test_nothing_is_made_when_no_template_applies_or_there_are_none(client, db_session):
    request_type, name, department, _, it, laptops = starter(db_session)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    assert subs_of(db_session, parent_of(db_session)) == []
    template(db_session, request_type, it, laptops, conditions="Department = Sales")
    raise_starter(client, request_type, name, department)  # Finance
    assert len(all_tickets(db_session)) == 2


def test_the_requester_is_told_in_the_bell_once_and_can_switch_it_off(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops)
    template(db_session, request_type, it, laptops, "Phone for {Name}")
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    entries = db_session.exec(select(Notification)).all()
    assert [(e.kind, e.ticket_id) for e in entries] == [("subrequest_created", parent_of(db_session).id)]
    assert "2 sub-requests were created" in entries[0].text
    assert "Sub-requests are created for your request" in client.get("/settings/notifications").text


# ---- Creation: approval --------------------------------------------------------------------------------


def approve(db_session, parent, email="boss@example.com", decision="approved"):
    vote = vote_of(db_session, parent, email)
    approvals.record_decision(db_session, vote, decision, "")
    db_session.commit()


def test_with_approval_the_list_waits_and_is_made_once_approved(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session, approval=True)
    template(db_session, request_type, it, laptops, "Laptop for {Name}", "Needs a laptop = yes")
    template(db_session, request_type, it, laptops, "Phone for {Name}", "Needs a laptop = no")
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name, department, laptop)

    parent = parent_of(db_session)
    assert parent.status == TicketStatus.AWAITING_APPROVAL and subs_of(db_session, parent) == []
    assert parent.pending_subrequests == [{"template_id": parent.pending_subrequests[0]["template_id"], "subject": "Laptop for Ann Lee"}]
    assert "Laptop for Ann Lee" in events(db_session, parent, "subrequests_planned")[0].detail

    approve(db_session, parent)
    parent = parent_of(db_session)
    assert parent.status == TicketStatus.OPEN and parent.pending_subrequests is None
    assert [s.subject for s in subs_of(db_session, parent)] == ["Laptop for Ann Lee"]
    # asking again (a second call) makes nothing more
    from app.subrequests import after_approval
    assert after_approval(db_session, parent) == []


def test_the_list_confirmed_at_triage_is_made_even_if_the_answers_change_while_it_waits(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session, approval=True)
    template(db_session, request_type, it, laptops, "Laptop for {Name}", "Needs a laptop = yes")
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name, department, laptop)
    parent = parent_of(db_session)
    save_answers(db_session, parent, {laptop.id: "no", name.id: "Someone Else"}, "agent@example.com")
    db_session.commit()
    approve(db_session, parent)
    assert [s.subject for s in subs_of(db_session, parent_of(db_session))] == ["Laptop for Ann Lee"]


def test_a_super_admin_waiving_the_approval_makes_them_too(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session, approval=True)
    template(db_session, request_type, it, laptops)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    approvals.waive(db_session, parent, db_session.get(User, admin_id))
    db_session.commit()
    assert [s.subject for s in subs_of(db_session, parent_of(db_session))] == ["Laptop for Ann Lee"]


def test_a_declined_request_makes_none_and_keeps_the_list_for_a_reopen(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session, approval=True)
    template(db_session, request_type, it, laptops)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    approve(db_session, parent, decision="declined")
    parent = parent_of(db_session)
    assert parent.status == TicketStatus.CLOSED and subs_of(db_session, parent) == []
    assert parent.pending_subrequests is not None


def test_the_parents_approval_covers_its_sub_requests_unless_their_kind_wants_its_own(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    covered = make(db_session, "Laptop request", members=("lead@example.com",))
    own = make(db_session, "Finance access", members=("fin@example.com",))
    own.subrequests_need_own_approval = True
    db_session.add(own)
    db_session.commit()
    template(db_session, request_type, it, laptops, "Laptop for {Name}", sub_request_type_id=covered.id)
    template(db_session, request_type, it, laptops, "Finance for {Name}", sub_request_type_id=own.id)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    first, second = subs_of(db_session, parent_of(db_session))
    assert first.status == TicketStatus.OPEN and approvals.current_approval(db_session, first) is None
    assert second.status == TicketStatus.AWAITING_APPROVAL
    assert approvals.current_approval(db_session, second).status == "pending"
    assert parent_of(db_session).status == TicketStatus.IN_PROGRESS  # one part is waiting, one is open


# ---- The agent's list --------------------------------------------------------------------------------


def email_starter(client, db_session, approval=False):
    request_type, name, department, laptop, it, laptops = starter(db_session, approval)
    from app.models import Category, Team
    desk = db_session.exec(select(Team).where(Team.name == "Service Desk")).one()
    category = db_session.exec(select(Category).where(Category.name == "Hardware")).one()
    agent_of(client, db_session, desk)
    ticket = create_ticket(
        db_session, subject="Starter", requester_email="newhire@example.com", source=TicketSource.EMAIL, team_id=desk.id,
    )
    db_session.commit()
    db_session.refresh(ticket)
    return ticket, request_type, name, department, laptop, it, laptops, category


def test_the_triage_form_lists_what_the_rules_produce_ticked_and_the_others_unticked(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    one = template(db_session, request_type, it, laptops, "Laptop for {Name}", "Needs a laptop = yes")
    two = template(db_session, request_type, it, laptops, "Account for {Name}")
    params = {"number": ticket.number, "request_type_id": request_type.id, f"q_{name.id}": "Ann Lee", f"q_{laptop.id}": "no"}

    page = client.get("/tickets/triage-subrequests", params=params).text
    assert "Account for Ann Lee" in page and "Laptop for Ann Lee" in page
    assert f'value="{two.id}" checked' in page and f'value="{one.id}" checked' not in page
    assert "the rules do not produce this one" in page

    params[f"q_{laptop.id}"] = "yes"  # the list follows the answers
    assert f'value="{one.id}" checked' in client.get("/tickets/triage-subrequests", params=params).text
    # what the agent unticked stays unticked when the form changes again
    params.update({"subrequest_listed": [one.id, two.id], "subrequest_template": [one.id]})
    page = client.get("/tickets/triage-subrequests", params=params).text
    assert f'value="{one.id}" checked' in page and f'value="{two.id}" checked' not in page
    assert client.get("/tickets/triage-subrequests", params={"number": ticket.number}).text.count("checked") == 0
    assert "hx-get=\"/tickets/triage-subrequests" in client.get(f"/tickets/{ticket.number}").text


def test_the_agent_can_untick_a_sub_request_and_add_an_extra(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    one = template(db_session, request_type, it, laptops, "Laptop for {Name}")
    two = template(db_session, request_type, it, laptops, "Phone for {Name}")
    extra = template(db_session, request_type, it, laptops, "Parking for {Name}", "Department = Sales")
    response = triage_post(
        client, ticket, category, request_type, **{f"q_{name.id}": "Ann Lee"},
        subrequest_listed=[one.id, two.id, extra.id], subrequest_template=[one.id, extra.id],
    )
    assert response.status_code == 200
    parent = parent_of(db_session)
    assert [s.subject for s in subs_of(db_session, parent)] == ["Laptop for Ann Lee", "Parking for Ann Lee"]
    assert {e.actor_email for e in events(db_session, parent, "subrequest_created")} == {"agent@example.com"}


def test_a_triage_with_no_list_shown_makes_what_the_rules_produce(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    template(db_session, request_type, it, laptops, "Laptop for {Name}")
    template(db_session, request_type, it, laptops, "Parking for {Name}", "Department = Sales")
    triage_post(client, ticket, category, request_type, **{f"q_{name.id}": "Ann Lee"})
    assert [s.subject for s in subs_of(db_session, parent_of(db_session))] == ["Laptop for Ann Lee"]


def test_one_the_agent_was_never_shown_is_decided_by_the_rules(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    one = template(db_session, request_type, it, laptops, "Laptop for {Name}")
    two = template(db_session, request_type, it, laptops, "Phone for {Name}")
    triage_post(client, ticket, category, request_type, **{f"q_{name.id}": "Ann Lee"}, subrequest_listed=[one.id], subrequest_template=[])
    assert [s.subject for s in subs_of(db_session, parent_of(db_session))] == ["Phone for Ann Lee"]


def test_an_agents_list_over_the_cap_is_refused_and_nothing_is_triaged(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    set_setting(db_session, MAX_SUB_TICKETS, "2")
    db_session.commit()
    for pattern in ("A {Name}", "B {Name}", "C {Name}"):
        template(db_session, request_type, it, laptops, pattern)
    response = triage_post(client, ticket, category, request_type, **{f"q_{name.id}": "Ann Lee"})
    assert response.status_code == 400 and "at most 2 sub-tickets" in response.text and "untick" in response.text
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).triaged_at is None and len(all_tickets(db_session)) == 1


def test_an_agents_triage_with_approval_pending_records_the_list(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session, approval=True)
    one = template(db_session, request_type, it, laptops, "Laptop for {Name}")
    template(db_session, request_type, it, laptops, "Phone for {Name}")
    triage_post(client, ticket, category, request_type, **{f"q_{name.id}": "Ann Lee"}, subrequest_listed=[one.id], subrequest_template=[one.id])
    parent = db_session.get(Ticket, ticket.id)
    db_session.refresh(parent)
    assert parent.status == TicketStatus.AWAITING_APPROVAL and [s["subject"] for s in parent.pending_subrequests] == [
        "Laptop for Ann Lee", "Phone for Ann Lee",  # Phone was never listed, so the rules decide it
    ]
    page = client.get(f"/tickets/{parent.number}").text
    assert "Once this request is approved these sub-requests will be made" in page


# ---- Assignment ----------------------------------------------------------------------------------------


def agent(client, db_session, team, email):
    agent_id = login_as(client, email, email.split("@")[0])
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    client.post("/auth/logout")
    return agent_id


def one_sub(client, db_session, request_type, name):
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    [sub] = subs_of(db_session, parent_of(db_session))
    return sub


def test_a_named_agent_is_assigned_if_available_else_the_team_queue(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    pat = agent(client, db_session, it, "pat@example.com")
    template(db_session, request_type, it, laptops, assignee_id=pat)
    sub = one_sub(client, db_session, request_type, name)
    assert sub.assigned_agent_id == pat
    assert [e.to_value for e in events(db_session, sub, "assigned")] == ["pat@example.com"]
    assert db_session.exec(select(Notification).where(Notification.kind == "assigned")).first().user_id == pat


@pytest.mark.parametrize("change", ["inactive", "left_the_team"])
def test_an_unavailable_named_agent_leaves_the_sub_request_with_the_team(client, db_session, change):
    from app.models import TeamMembership

    request_type, name, _, _, it, laptops = starter(db_session)
    pat = agent(client, db_session, it, "pat@example.com")
    template(db_session, request_type, it, laptops, assignee_id=pat)
    user = db_session.get(User, pat)
    if change == "inactive":
        user.is_active = False
    else:
        db_session.delete(db_session.exec(select(TeamMembership).where(TeamMembership.user_id == pat)).one())
    db_session.add(user)
    db_session.commit()
    sub = one_sub(client, db_session, request_type, name)
    assert sub.assigned_agent_id is None and sub.status == TicketStatus.OPEN and sub.team_id == it.id


def test_an_out_of_office_named_agent_still_gets_the_sub_request(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    pat = agent(client, db_session, it, "pat@example.com")
    template(db_session, request_type, it, laptops, assignee_id=pat)
    user = db_session.get(User, pat)
    user.out_of_office, user.out_of_office_checked_at = True, datetime.now(timezone.utc)
    db_session.add(user)
    db_session.commit()
    sub = one_sub(client, db_session, request_type, name)
    assert sub.assigned_agent_id == pat  # a label, never a block (#151)


def test_the_parents_assignee_takes_the_sub_request_when_they_work_its_team(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    both = agent(client, db_session, it, "both@example.com")
    from app.models import Team
    desk = db_session.exec(select(Team).where(Team.name == "Service Desk")).one()
    add_membership(db_session, both, desk.id)
    template(db_session, request_type, it, laptops, "Mine for {Name}", assign_to_parent_assignee=True)
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    triage_post(client, ticket, category, request_type, agent_id=both, **{f"q_{name.id}": "Ann Lee"})
    [sub] = subs_of(db_session, parent_of(db_session))
    assert sub.assigned_agent_id == both


def test_the_parents_assignee_outside_the_sub_requests_team_is_not_used(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    template(db_session, request_type, it, laptops, "Mine for {Name}", assign_to_parent_assignee=True)
    triage_post(client, ticket, category, request_type, agent_id=login_as(client, "agent@example.com"), **{f"q_{name.id}": "Ann Lee"})
    [sub] = subs_of(db_session, parent_of(db_session))
    assert sub.assigned_agent_id is None


# ---- Due ----------------------------------------------------------------------------------------------


def due_world(client, db_session, days, offset):
    request_type, name, _, _, it, laptops = starter(db_session)  # a fresh database for each test
    request_type.offers_required_by = True
    db_session.add(request_type)
    db_session.commit()
    template(db_session, request_type, it, laptops, due_days_before=days)
    login_as(client, "jo@example.com")
    required = (datetime.now(timezone.utc) + timedelta(days=offset)).date()
    raise_starter(client, request_type, name, required_by=required.isoformat())
    return required, subs_of(db_session, parent_of(db_session))[0]


def test_the_sub_request_is_due_the_templates_days_before_the_parents_required_by(client, db_session):
    required, sub = due_world(client, db_session, 5, 20)
    assert sub.due_on == required - timedelta(days=5) and sub.resolution_due_at is not None
    assert [e.to_value for e in events(db_session, sub, "due_changed")] == [sub.due_on.isoformat()]


def test_it_is_never_due_before_today(client, db_session):
    required, sub = due_world(client, db_session, 30, 3)
    assert datetime.now(timezone.utc).date() - timedelta(days=1) <= sub.due_on <= required


def test_zero_days_means_due_on_the_required_by_day(client, db_session):
    required, sub = due_world(client, db_session, 0, 9)
    assert sub.due_on == required


def test_a_blank_number_of_days_uses_the_due_date_setting_and_no_required_by_means_no_due(client, db_session):
    from app.required_by import sub_request_due

    request_type, name, _, _, it, laptops = starter(db_session)
    ticket = create_ticket(db_session, subject="x", requester_email="jo@example.com", source=TicketSource.PORTAL)
    ticket.required_by = date(2030, 1, 10)
    assert sub_request_due(db_session, ticket, None, today=date(2030, 1, 1)) == date(2030, 1, 9)  # the setting says 1
    assert sub_request_due(db_session, ticket, 4, today=date(2030, 1, 1)) == date(2030, 1, 6)
    assert sub_request_due(db_session, ticket, 40, today=date(2030, 1, 1)) == date(2030, 1, 1)
    ticket.required_by = None
    assert sub_request_due(db_session, ticket, 4) is None
    template(db_session, request_type, it, laptops)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    assert subs_of(db_session, parent_of(db_session))[0].due_on is None


# ---- The shared cap ---------------------------------------------------------------------------------


def test_manual_splitting_uses_the_shared_setting_and_counts_automatic_sub_requests(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops, "A {Name}")
    template(db_session, request_type, it, laptops, "B {Name}")
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    agent_id = agent(client, db_session, it, "pat@example.com")
    actor = db_session.get(User, agent_id)
    set_setting(db_session, MAX_SUB_TICKETS, "3")
    db_session.commit()
    with pytest.raises(SplitError, match="at most 3"):
        split_ticket(db_session, parent, actor, [("C", ""), ("D", "")])
    db_session.rollback()
    assert len(split_ticket(db_session, parent_of(db_session), actor, [("C", "")])) == 1  # the third still fits


def test_an_automatic_list_over_the_cap_makes_the_first_ones_and_records_the_rest(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    set_setting(db_session, MAX_SUB_TICKETS, "2")
    db_session.commit()
    for pattern in ("A {Name}", "B {Name}", "C {Name}"):
        template(db_session, request_type, it, laptops, pattern)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    assert [s.subject for s in subs_of(db_session, parent)] == ["A Ann Lee", "B Ann Lee"]
    [skipped] = events(db_session, parent, "subrequest_skipped")
    assert skipped.to_value == "C Ann Lee" and "at most 2" in skipped.detail


def test_a_template_that_cannot_be_made_is_skipped_and_the_others_are_made(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    broken_category = create_category(db_session, it, "Retired")
    template(db_session, request_type, it, broken_category, "Broken {Name}")
    template(db_session, request_type, it, laptops, "Fine {Name}")
    broken_category.is_active = False
    db_session.add(broken_category)
    db_session.commit()
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    assert [s.subject for s in subs_of(db_session, parent)] == ["Fine Ann Lee"]
    [skipped] = events(db_session, parent, "subrequest_skipped")
    assert skipped.to_value == "Broken Ann Lee" and "no longer in use" in skipped.detail


def test_a_sub_ticket_never_makes_sub_requests_of_its_own(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    other = make(db_session, "Laptop request")
    template(db_session, other, it, laptops, "Nested", )
    template(db_session, request_type, it, laptops, "Laptop for {Name}", sub_request_type_id=other.id)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    [sub] = subs_of(db_session, parent_of(db_session))
    assert subs_of(db_session, sub) == [] and after_triage(db_session, sub, None) == [] and len(all_tickets(db_session)) == 2


# ---- Answers changing later --------------------------------------------------------------------------


def test_answers_changed_after_the_sub_requests_exist_change_nothing_but_agents_see_a_note(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops, "Laptop for {Name}", "Needs a laptop = yes")
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name, department, laptop)
    parent = parent_of(db_session)
    agent_id = agent(client, db_session, it, "pat@example.com")
    desk_agent = login_as(client, "desk@example.com")
    from app.models import Team
    desk = db_session.exec(select(Team).where(Team.name == "Service Desk")).one()
    set_role(db_session, desk_agent, UserRole.AGENT, desk.id)
    assert "nothing was added or removed" not in client.get(f"/tickets/{parent.number}").text

    assert client.post(f"/tickets/{parent.number}/answers", data={f"q_{laptop.id}": "no", f"q_{name.id}": "Ann Lee"}).status_code == 200
    page = client.get(f"/tickets/{parent.number}").text
    assert "Sub-requests were already made from the earlier answers; nothing was added or removed" in page
    assert [s.subject for s in subs_of(db_session, parent_of(db_session))] == ["Laptop for Ann Lee"]  # none removed, none added


def test_answers_changed_with_no_sub_requests_made_show_no_extra_note(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session)
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name, department, laptop)
    parent = parent_of(db_session)
    desk_agent = login_as(client, "desk@example.com")
    from app.models import Team
    desk = db_session.exec(select(Team).where(Team.name == "Service Desk")).one()
    set_role(db_session, desk_agent, UserRole.AGENT, desk.id)
    client.post(f"/tickets/{parent.number}/answers", data={f"q_{laptop.id}": "no", f"q_{name.id}": "Ann Lee"})
    page = client.get(f"/tickets/{parent.number}").text
    assert "after they were first given" in page and "nothing was added or removed" not in page


# ---- Found in review -------------------------------------------------------------------------------


def test_the_sub_requests_list_swaps_itself_and_not_the_page(client, db_session):
    ticket, request_type, name, department, laptop, it, laptops, category = email_starter(client, db_session)
    template(db_session, request_type, it, laptops)
    fragment = client.get("/tickets/triage-subrequests", params={"number": ticket.number, "request_type_id": request_type.id}).text
    assert 'hx-target="this"' in fragment and 'hx-swap="outerHTML"' in fragment  # the form's own target is main


def test_an_agent_raising_a_request_with_a_category_makes_the_sub_requests_too(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops)
    from app.models import Category, Team

    desk = db_session.exec(select(Team).where(Team.name == "Service Desk")).one()
    category = db_session.exec(select(Category).where(Category.name == "Hardware")).one()
    agent_of(client, db_session, desk)
    response = raise_it(
        client, subject="A new starter", for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low",
        request_type_id=request_type.id, **{f"q_{name.id}": "Ann Lee"},
    )
    assert response.status_code == 204
    parent = parent_of(db_session)
    assert parent.triaged_at is not None and [s.subject for s in subs_of(db_session, parent)] == ["Laptop for Ann Lee"]


def test_a_skipped_one_says_what_was_wrong_not_that_the_category_cannot_be_taken(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    other = make(db_session, "Laptop request")
    template(db_session, request_type, it, laptops, "Laptop for {Name}", sub_request_type_id=other.id)
    other.is_active = False
    db_session.add(other)
    db_session.commit()
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    [skipped] = events(db_session, parent_of(db_session), "subrequest_skipped")
    assert "no longer available" in skipped.detail and "category" not in skipped.detail


def test_a_stored_impact_that_is_no_level_skips_that_template_only(client, db_session):
    request_type, name, _, _, it, laptops = starter(db_session)
    bad = template(db_session, request_type, it, laptops, "Bad {Name}")
    template(db_session, request_type, it, laptops, "Good {Name}")
    bad.impact = "enormous"
    db_session.add(bad)
    db_session.commit()
    login_as(client, "jo@example.com")
    raise_starter(client, request_type, name)
    parent = parent_of(db_session)
    assert [s.subject for s in subs_of(db_session, parent)] == ["Good Ann Lee"]
    assert [e.to_value for e in events(db_session, parent, "subrequest_skipped")] == ["Bad Ann Lee"]


def test_a_question_named_in_a_subject_cannot_be_deleted_or_renamed_but_can_be_switched_off(db_session):
    from app.request_questions import update_question

    request_type, name, department, _, it, laptops = starter(db_session)
    template(db_session, request_type, it, laptops, "Laptop for {name}")
    with pytest.raises(QuestionError, match="subject"):
        delete_question(db_session, name)
    with pytest.raises(QuestionError, match="subject"):
        update_question(db_session, name, "Full name", None, True, True)
    update_question(db_session, name, "NAME", None, True, True)  # the same label apart from case
    update_question(db_session, department, "Team", "Finance\nSales\nIT", False, True)  # not named: free to change
    update_question(db_session, name, "NAME", None, True, False)  # switching it off is fine


def test_a_pick_list_option_with_a_bar_is_matched_by_equals_and_cannot_be_typed_into_is_one_of(db_session):
    request_type, *_ = starter(db_session)
    add_question(db_session, request_type, "Kit", "choice", "Laptop | Dock\nPhone")
    db_session.commit()
    with pytest.raises(TemplateError):
        parse_conditions(db_session, request_type.id, "Kit in Laptop | Dock | Phone")  # typed with the bar: cannot tell them apart
    [[stored]] = parse_conditions(db_session, request_type.id, "Kit = Laptop | Dock")
    assert stored["value"] == "Laptop | Dock"

"""A super admin counts as an agent for assignment (#288): team membership still decides who is offered."""

import pytest
from sqlmodel import select

from app.models import AuditLog, MembershipLevel, Notification, Team, User, UserRole
from app.msp import warn_before_handoff
from app.subrequests import TemplateError, save_template
from app.visibility import ASSIGNEE_ROLES, assignable_agents, team_members
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_handoff_bells_holidays import WARN_AT, bells
from tests.test_msp import at_risk, included_team, mail_on, set_msp  # noqa: F401
from tests.test_subrequests import starter
from tests.test_triage import new_ticket, triaged_ticket


def make_user(client, db_session, email, role, teams=()):
    user_id = login_as(client, email)
    set_role(db_session, user_id, role)
    for team in teams:
        add_membership(db_session, user_id, team.id)
    client.post("/auth/logout")
    return user_id


def super_admin_in(client, db_session, teams=(), email="boss@example.com"):
    """A super admin who is a member of the given teams (admin@example.com is one by configuration)."""
    return make_user(client, db_session, email, UserRole.SUPER_ADMIN, teams)


def ids(users):
    return {user.id for user in users}


def test_one_helper_names_the_roles_that_may_hold_a_ticket():
    assert set(ASSIGNEE_ROLES) == {UserRole.AGENT, UserRole.SUPER_ADMIN}


def test_a_member_super_admin_is_offered_for_the_team_and_a_non_member_is_not(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    member = super_admin_in(client, db_session, [desk])
    outsider = super_admin_in(client, db_session, [network], email="other@example.com")
    ticket = new_ticket(db_session, desk)
    assert member in ids(team_members(db_session, desk.id))
    assert outsider not in ids(team_members(db_session, desk.id))
    assert member in ids(assignable_agents(db_session, ticket))
    assert outsider not in ids(assignable_agents(db_session, ticket))


def test_a_super_admin_who_only_views_the_team_is_not_offered(client, db_session):
    desk = create_team(db_session, "Service Desk")
    user_id = login_as(client, "viewer-boss@example.com")
    set_role(db_session, user_id, UserRole.SUPER_ADMIN, desk.id, MembershipLevel.VIEWER)
    assert user_id not in ids(team_members(db_session, desk.id))


def test_a_parent_teams_super_admin_is_offered_for_a_sub_team(client, db_session):
    support = create_team(db_session, "Support")
    second = Team(name="Second line", parent_team_id=support.id)
    db_session.add(second)
    db_session.commit()
    member = super_admin_in(client, db_session, [support])
    assert member in ids(team_members(db_session, second.id))


def test_an_inactive_super_admin_is_never_offered(client, db_session):
    desk = create_team(db_session, "Service Desk")
    member = super_admin_in(client, db_session, [desk])
    ticket, teamless = new_ticket(db_session, desk), new_ticket(db_session)
    person = db_session.get(User, member)
    person.is_active = False
    db_session.add(person)
    db_session.commit()
    assert member not in ids(team_members(db_session, desk.id))
    assert member not in ids(assignable_agents(db_session, ticket))
    assert member not in ids(assignable_agents(db_session, teamless))


def test_a_ticket_with_no_team_offers_active_agents_and_super_admins(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent_id = make_user(client, db_session, "agent@example.com", UserRole.AGENT, [desk])
    boss = super_admin_in(client, db_session)  # in no team at all
    end_user = make_user(client, db_session, "plain@example.com", UserRole.END_USER)
    offered = ids(assignable_agents(db_session, new_ticket(db_session)))
    assert {agent_id, boss} <= offered
    assert end_user not in offered


def test_re_assign_offers_a_member_super_admin_and_assigns_them(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    boss = super_admin_in(client, db_session, [network])
    ticket = triaged_ticket(db_session, desk)
    login_as(client, "admin@example.com")
    assert f'value="{boss}"' in client.get(f"/tickets/agents-for-team?team_id={network.id}").text
    response = client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": network.id, "agent_id": boss})
    assert response.status_code == 200
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (network.id, boss)


def test_re_assign_refuses_a_super_admin_who_is_not_a_member(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    outsider = super_admin_in(client, db_session, [desk])
    ticket = triaged_ticket(db_session, desk)
    login_as(client, "admin@example.com")
    response = client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": network.id, "agent_id": outsider})
    assert response.status_code == 400
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (desk.id, None)


def test_triage_assign_to_takes_a_member_super_admin_and_refuses_a_non_member(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    category = create_category(db_session, desk, "Hardware")
    member = super_admin_in(client, db_session, [desk])
    outsider = super_admin_in(client, db_session, [network], email="other@example.com")
    first, second = new_ticket(db_session, desk), new_ticket(db_session, desk)
    login_as(client, "admin@example.com")
    data = {"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id}
    refused = client.post(f"/tickets/{first.number}/triage", data={**data, "agent_id": outsider})
    assert refused.status_code == 400
    db_session.refresh(first)
    assert first.triaged_at is None and first.assigned_agent_id is None
    assert client.post(f"/tickets/{second.number}/triage", data={**data, "agent_id": member}).status_code == 200
    db_session.refresh(second)
    assert second.assigned_agent_id == member


def test_a_super_admin_member_can_assign_to_me_an_unassigned_ticket(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged_ticket(db_session, desk)
    boss = login_as(client, "admin@example.com")
    add_membership(db_session, boss, desk.id)
    assert "Assign to me" in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/assign").status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == boss
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "assigned"))] == ["admin@example.com"]


def test_a_super_admin_who_is_not_a_member_is_not_offered_assign_to_me_and_is_refused(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged_ticket(db_session, desk)
    login_as(client, "admin@example.com")
    assert "Assign to me" not in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/assign").status_code == 400
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id is None


def test_assign_to_me_never_takes_a_ticket_assigned_to_someone_else(client, db_session):
    desk = create_team(db_session, "Service Desk")
    other = make_user(client, db_session, "agent@example.com", UserRole.AGENT, [desk])
    ticket = triaged_ticket(db_session, desk, assigned_agent_id=other)
    boss = login_as(client, "admin@example.com")
    add_membership(db_session, boss, desk.id)
    assert "Assign to me" not in client.get(f"/tickets/{ticket.number}").text
    response = client.post(f"/tickets/{ticket.number}/assign")
    assert response.status_code == 400 and "Re-assign" in response.text
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == other


def test_a_super_admin_can_still_pick_another_person_with_assign(client, db_session):
    desk = create_team(db_session, "Service Desk")
    other = make_user(client, db_session, "agent@example.com", UserRole.AGENT, [desk])
    ticket = triaged_ticket(db_session, desk)
    login_as(client, "admin@example.com")
    assert client.post(f"/tickets/{ticket.number}/assign", data={"agent_id": other}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == other


def test_a_super_admin_assignee_gets_the_assigned_bell(client, db_session):
    desk = create_team(db_session, "Service Desk")
    boss = super_admin_in(client, db_session, [desk])
    ticket = triaged_ticket(db_session, desk)
    make_user(client, db_session, "agent@example.com", UserRole.AGENT, [desk])
    login_as(client, "agent@example.com")
    assert client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": desk.id, "agent_id": boss}).status_code == 200
    bell = db_session.exec(select(Notification).where(Notification.kind == "assigned")).all()
    assert [(n.user_id, n.ticket_id) for n in bell] == [(boss, ticket.id)]


def test_a_super_admin_assignee_gets_the_msp_handoff_warning(client, db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    boss = super_admin_in(client, db_session, [team])
    ticket = at_risk(db_session, team, assigned_agent_id=boss)
    assert warn_before_handoff(db_session, WARN_AT) == 1
    db_session.commit()
    (bell,) = bells(db_session, "msp_handoff_warning")
    assert bell.user_id == boss and bell.ticket_id == ticket.id


def test_a_super_admin_can_be_a_sub_request_templates_default_assignee(client, db_session):
    request_type, name, department, laptop, it, laptops = starter(db_session)
    member = super_admin_in(client, db_session, [it])
    outsider = super_admin_in(client, db_session, [], email="other@example.com")
    made = save_template(
        db_session, request_type, None, subject_pattern="Laptop for {Name}", team_id=it.id, category_id=laptops.id,
        assignee_id=member,
    )
    db_session.commit()
    assert made.assignee_id == member
    with pytest.raises(TemplateError):
        save_template(
            db_session, request_type, None, subject_pattern="Dock for {Name}", team_id=it.id, category_id=laptops.id,
            assignee_id=outsider,
        )


def test_the_request_types_page_lists_a_super_admin_as_a_possible_assignee(client, db_session):
    request_type = starter(db_session)[0]  # its sub-request form carries the list
    boss = super_admin_in(client, db_session, [], email="lister@example.com")
    login_as(client, "admin@example.com")
    assert f'<option value="{boss}"' in client.get(f"/admin/request-types?type={request_type.id}").text


def test_a_break_glass_account_is_never_offered(client, db_session):
    desk = create_team(db_session, "Service Desk")
    glass = super_admin_in(client, db_session, [desk], email="glass@example.com")
    person = db_session.get(User, glass)
    person.is_break_glass = True
    db_session.add(person)
    db_session.commit()
    assert glass not in ids(team_members(db_session, desk.id))
    assert glass not in ids(assignable_agents(db_session, new_ticket(db_session)))

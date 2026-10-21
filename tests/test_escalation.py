"""Escalation by category, sub-teams, and the escalator keeping the ticket they escalated."""

from sqlmodel import select

from app.categories import escalation_team_id_for
from app.models import AuditLog, Category, MembershipLevel, Team, Ticket, TicketEscalator, TicketEvent, TicketStatus, User
from app.visibility import Access, access_context, ticket_access, visible_ticket_condition
from tests.conftest import create_category, create_team, login_as
from tests.test_visibility import agent_in, make_ticket


def categorised(db_session, ticket, escalates_to=None, name="Hardware", parent=None, inherits=True):
    """Puts the ticket in a new category whose escalation target is `escalates_to` (the MSP if None)."""
    category = create_category(db_session, None if parent else db_session.get(Team, ticket.team_id), name, parent)
    category.escalation_team_id = escalates_to.id if escalates_to else None
    category.escalation_inherits = inherits
    db_session.add(category)
    ticket.category_id = category.id
    db_session.add(ticket)
    db_session.commit()
    return category


def escalate(client, ticket, reason="Needs more"):
    return client.post(f"/tickets/{ticket.number}/escalate", data={"reason": reason})


def unchanged(db_session, ticket, team):
    db_session.refresh(ticket)
    assert ticket.team_id == team.id and not ticket.is_escalated
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "escalated")).all() == []
    assert db_session.exec(select(TicketEscalator)).all() == []


# ---- When a ticket can escalate ----------------------------------------------


def test_an_untriaged_ticket_cannot_escalate(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id, status=TicketStatus.NEW)
    categorised(db_session, ticket, dev)

    response = escalate(client, ticket)
    assert response.status_code == 403 or (response.status_code == 400 and "Triage" in response.text)
    db_session.refresh(ticket)
    assert ticket.team_id == desk.id and not ticket.is_escalated


def test_a_ticket_with_no_category_cannot_escalate(client, db_session):
    desk = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)

    response = escalate(client, ticket)
    assert response.status_code == 400 and "no category" in response.text
    unchanged(db_session, ticket, desk)


def test_a_category_that_escalates_to_the_msp_cannot_escalate_yet(client, db_session):
    desk = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, escalates_to=None)  # the default: the MSP

    response = escalate(client, ticket)
    assert response.status_code == 400 and "MSP" in response.text
    unchanged(db_session, ticket, desk)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Cannot escalate" in page and "/escalate" not in page.split("Cannot escalate")[0].split("<details>")[-1]


def test_a_ticket_already_with_the_escalation_team_would_go_to_the_msp_and_is_refused(client, db_session):
    desk = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, escalates_to=desk)

    assert escalate(client, ticket).status_code == 400
    unchanged(db_session, ticket, desk)


def test_a_resolved_or_closed_ticket_cannot_escalate(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    for status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        ticket = make_ticket(db_session, desk.id, status=status)
        categorised(db_session, ticket, dev, name=f"Hardware {status.value}")
        response = escalate(client, ticket)
        assert response.status_code == 400 and "cannot be escalated" in response.text
        db_session.refresh(ticket)
        assert ticket.team_id == desk.id and not ticket.is_escalated


def test_the_agent_cannot_choose_the_team(client, db_session):
    desk, dev, finance = (create_team(db_session, n) for n in ("Support", "Dev", "Finance"))
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)

    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x", "target_team_id": finance.id})
    db_session.refresh(ticket)
    assert ticket.team_id == dev.id


def test_escalating_needs_a_reason(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    assert client.post(f"/tickets/{ticket.number}/escalate", data={}).status_code == 422
    unchanged(db_session, ticket, desk)


# ---- What escalating does -----------------------------------------------------


def test_escalating_moves_the_ticket_unassigned_with_an_event_and_an_audit_entry(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    me = agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id, assigned_agent_id=me)
    categorised(db_session, ticket, dev)

    response = escalate(client, ticket, "In-house app bug")
    assert response.status_code == 200 and "Dev" in response.text
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id, ticket.is_escalated) == (dev.id, None, True)
    assert ticket.escalation_reason == "In-house app bug" and ticket.escalated_by_id == me
    event = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "escalated")).one()
    assert (event.from_value, event.to_value, event.detail) == ("Support", "Dev", "In-house app bug")
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "escalated")).all()) == 1


def test_the_escalator_keeps_the_ticket_and_only_that_ticket(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    colleague = agent_in(client, db_session, member_of=[desk], email="colleague@example.com")
    login_as(client, "agent@example.com")
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    other_in_dev = make_ticket(db_session, dev.id)

    escalate(client, ticket)

    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}).status_code == 200
    assert ticket.reference in client.get("/tickets").text
    assert client.get(f"/tickets/{other_in_dev.number}").status_code == 403  # nothing else in Dev

    client.post("/auth/logout")
    login_as(client, "colleague@example.com")
    assert client.get(f"/tickets/{ticket.number}").status_code == 403  # a colleague who did not escalate it
    assert colleague


def test_a_viewer_cannot_escalate_and_so_never_gets_the_escalator_rule(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, viewer_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)

    assert escalate(client, ticket).status_code == 403
    assert db_session.exec(select(TicketEscalator)).all() == []


def test_a_follower_who_did_not_escalate_gets_no_access(client, db_session):
    from app.models import TicketFollower

    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_id = agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, dev.id)
    db_session.add(
        TicketFollower(ticket_id=ticket.id, email="agent@example.com", user_id=agent_id, is_internal=True, added_by_email="x")
    )
    db_session.commit()
    assert client.get(f"/tickets/{ticket.number}").status_code == 403


def test_escalating_again_after_a_reassign_back_keeps_a_single_escalator_row(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    escalate(client, ticket)
    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": desk.id})
    assert escalate(client, ticket).status_code == 200
    assert len(db_session.exec(select(TicketEscalator)).all()) == 1


# ---- Inheritance --------------------------------------------------------------


def test_a_sub_category_follows_its_parent_unless_it_overrides(client, db_session):
    desk, dev, finance = (create_team(db_session, n) for n in ("Support", "Dev", "Finance"))
    main = create_category(db_session, desk, "Software")
    main.escalation_team_id = dev.id
    db_session.add(main)
    db_session.commit()
    follows = create_category(db_session, None, "Email", parent=main)
    overrides = create_category(db_session, None, "Payroll app", parent=main)
    overrides.escalation_inherits, overrides.escalation_team_id = False, finance.id
    to_msp = create_category(db_session, None, "Vendor tool", parent=main)
    to_msp.escalation_inherits, to_msp.escalation_team_id = False, None
    db_session.add_all([overrides, to_msp])
    db_session.commit()

    assert escalation_team_id_for(db_session, main) == dev.id
    assert escalation_team_id_for(db_session, follows) == dev.id
    assert escalation_team_id_for(db_session, overrides) == finance.id
    assert escalation_team_id_for(db_session, to_msp) is None

    main.escalation_team_id = finance.id  # changing the parent moves everyone who inherits
    db_session.add(main)
    db_session.commit()
    assert escalation_team_id_for(db_session, follows) == finance.id
    assert escalation_team_id_for(db_session, to_msp) is None


def test_every_category_defaults_to_the_msp(db_session):
    desk = create_team(db_session, "Support")
    main = create_category(db_session, desk, "Hardware")
    sub = create_category(db_session, None, "Laptop", parent=main)
    assert escalation_team_id_for(db_session, main) is None and escalation_team_id_for(db_session, sub) is None


# ---- Sub-teams: first line and second line ---------------------------------------


def make_support(db_session):
    support = create_team(db_session, "Support")
    second = Team(name="Support 2nd line", parent_team_id=support.id)
    db_session.add(second)
    db_session.commit()
    db_session.refresh(second)
    return support, second


def test_a_parent_team_member_works_its_sub_teams_tickets_but_not_the_other_way(client, db_session):
    support, second = make_support(db_session)
    agent_in(client, db_session, member_of=[support])
    in_second = make_ticket(db_session, second.id)
    in_support = make_ticket(db_session, support.id)
    assert client.get(f"/tickets/{in_second.number}").status_code == 200
    assert client.post(f"/tickets/{in_second.number}/status", data={"status": "in_progress"}).status_code == 200

    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[second], email="second@example.com")
    assert client.get(f"/tickets/{in_second.number}").status_code == 200
    assert client.get(f"/tickets/{in_support.number}").status_code == 403


def test_a_parent_team_viewer_only_reads_the_sub_team(client, db_session):
    support, second = make_support(db_session)
    agent_in(client, db_session, viewer_of=[support])
    ticket = make_ticket(db_session, second.id)
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Fixed it"}).status_code == 403


def test_first_line_escalates_to_second_line_and_keeps_the_ticket(client, db_session):
    support, second = make_support(db_session)
    agent_in(client, db_session, member_of=[support])
    ticket = make_ticket(db_session, support.id)
    categorised(db_session, ticket, second)

    assert escalate(client, ticket).status_code == 200
    db_session.refresh(ticket)
    assert ticket.team_id == second.id and ticket.assigned_agent_id is None
    assert client.get(f"/tickets/{ticket.number}").status_code == 200  # through the parent team

    # Second line cannot escalate to itself: the next step is the MSP.
    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[second], email="second@example.com")
    assert escalate(client, ticket).status_code == 400


def test_sub_teams_do_not_nest(client, db_session):
    support, second = make_support(db_session)
    third = Team(name="Third", parent_team_id=second.id)  # not creatable in the admin, but be safe
    db_session.add(third)
    db_session.commit()
    agent_in(client, db_session, member_of=[support])
    deep = make_ticket(db_session, third.id)
    assert client.get(f"/tickets/{deep.number}").status_code == 403  # one level only


def test_list_query_and_single_ticket_rule_agree_with_sub_teams_and_escalators(client, db_session):
    support, second = make_support(db_session)
    dev, other = create_team(db_session, "Dev"), create_team(db_session, "Other")
    agent_id = agent_in(client, db_session, member_of=[support], viewer_of=[dev])
    for team in (support, second, dev, other):
        make_ticket(db_session, team.id, TicketStatus.OPEN)
        make_ticket(db_session, team.id, TicketStatus.NEW)
    escalated = make_ticket(db_session, other.id)
    db_session.add(TicketEscalator(ticket_id=escalated.id, user_id=agent_id))
    db_session.commit()

    agent = db_session.get(User, agent_id)
    levels = access_context(db_session, agent)
    tickets = db_session.exec(select(Ticket)).all()
    by_rule = {t.id for t in tickets if ticket_access(agent, t, levels) >= Access.READ}
    by_query = {t.id for t in db_session.exec(select(Ticket).where(visible_ticket_condition(agent, levels))).all()}
    assert by_rule == by_query
    assert ticket_access(agent, escalated, levels) == Access.WORK
    assert levels[second.id] == Access.WORK and levels[dev.id] == Access.READ and other.id not in levels


# ---- Admin ------------------------------------------------------------------------


def test_admin_creates_a_sub_team_one_level_deep(client, db_session):
    support = create_team(db_session, "Support")
    login_as(client, "admin@example.com")

    response = client.post(
        "/admin/teams", data={"name": "Support 2nd line", "parent_team_id": support.id}, follow_redirects=False
    )
    assert response.status_code == 303
    second = db_session.exec(select(Team).where(Team.name == "Support 2nd line")).one()
    assert second.parent_team_id == support.id
    assert "Sub-team of Support" in client.get("/admin/teams").text

    assert client.post("/admin/teams", data={"name": "Deeper", "parent_team_id": second.id}).status_code == 400
    assert client.post("/admin/teams", data={"name": "Orphan", "parent_team_id": 999}).status_code == 400
    assert db_session.exec(select(Team).where(Team.name.in_(["Deeper", "Orphan"]))).all() == []


def test_admin_sets_category_escalation_with_audit(client, db_session):
    desk, dev, finance = (create_team(db_session, n) for n in ("Support", "Dev", "Finance"))
    main = create_category(db_session, desk, "Software")
    sub = create_category(db_session, None, "Email", parent=main)
    login_as(client, "admin@example.com")

    def post(category, choice):
        data = {"escalation": choice, "is_active": "on"}
        if category.parent_id is None:
            data["team_id"] = desk.id  # the form always sends a main category's team
        return client.post(f"/admin/categories/{category.id}", data=data, follow_redirects=False)

    assert post(main, str(dev.id)).status_code == 303
    db_session.expire_all()
    assert escalation_team_id_for(db_session, db_session.get(Category, sub.id)) == dev.id  # inherits

    post(sub, str(finance.id))
    db_session.expire_all()
    assert escalation_team_id_for(db_session, db_session.get(Category, sub.id)) == finance.id

    post(sub, "msp")
    db_session.expire_all()
    assert escalation_team_id_for(db_session, db_session.get(Category, sub.id)) is None

    post(sub, "inherit")
    db_session.expire_all()
    assert escalation_team_id_for(db_session, db_session.get(Category, sub.id)) == dev.id
    assert "Same as Software" in client.get("/admin/categories").text

    actions = [r.action for r in db_session.exec(select(AuditLog).where(AuditLog.action == "escalation_changed"))]
    assert len(actions) == 4
    post(sub, "inherit")  # no change, no new audit entry
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "escalation_changed")).all()) == 4


def test_admin_rejects_bad_escalation_choices(client, db_session):
    desk = create_team(db_session, "Support")
    main = create_category(db_session, desk, "Software")
    login_as(client, "admin@example.com")
    for choice in ("inherit", "999", "nonsense"):
        response = client.post(
            f"/admin/categories/{main.id}", data={"escalation": choice, "is_active": "on", "team_id": desk.id}
        )
        assert response.status_code == 400, choice
    db_session.refresh(main)
    assert main.escalation_team_id is None


def test_only_super_admins_set_escalation_and_sub_teams(client, db_session):
    desk = create_team(db_session, "Support")
    main = create_category(db_session, desk, "Software")
    agent_in(client, db_session, member_of=[desk])
    assert client.post(f"/admin/categories/{main.id}", data={"escalation": str(desk.id)}).status_code == 403
    assert client.post("/admin/teams", data={"name": "Sub", "parent_team_id": desk.id}).status_code == 403


def test_the_ticket_page_names_the_escalation_target(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Escalate to Dev" in page and "target_team_id" not in page
    assert MembershipLevel.MEMBER  # the team picker is gone from the form


def test_the_ticket_lists_everyone_who_escalated_it_and_they_all_keep_access(client, db_session):
    desk, dev, platform = (create_team(db_session, n) for n in ("Support", "Dev", "Platform"))
    agent_in(client, db_session, member_of=[desk], email="alice@example.com")
    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[dev], email="bob@example.com")
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    login_as(client, "alice@example.com")
    escalate(client, ticket)  # Support -> Dev

    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    ticket_category = db_session.get(Category, ticket.category_id)
    ticket_category.escalation_team_id = platform.id  # Dev's next step, for this test
    db_session.add(ticket_category)
    db_session.commit()
    assert escalate(client, ticket).status_code == 200  # Dev -> Platform

    page = client.get(f"/tickets/{ticket.number}").text
    assert "alice@example.com" in page and "bob@example.com" in page
    client.post("/auth/logout")
    login_as(client, "alice@example.com")
    assert client.get(f"/tickets/{ticket.number}").status_code == 200  # still hers


def test_parent_team_members_can_be_assigned_a_sub_team_ticket(client, db_session):
    support, second = make_support(db_session)
    first_line = agent_in(client, db_session, member_of=[support])
    ticket = make_ticket(db_session, second.id)
    login_as(client, "admin@example.com")
    assert client.post(f"/tickets/{ticket.number}/assign", data={"agent_id": first_line}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == first_line


def test_an_escalator_cannot_claim_the_ticket_they_sent_away(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    escalate(client, ticket)
    assert "Assign to me" not in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/assign").status_code == 403
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id is None


def test_a_ticket_in_a_sub_team_of_the_target_cannot_escalate_up_to_the_parent(client, db_session):
    support, second = make_support(db_session)
    agent_in(client, db_session, member_of=[support])
    ticket = make_ticket(db_session, second.id)
    categorised(db_session, ticket, support)
    assert escalate(client, ticket).status_code == 400
    db_session.refresh(ticket)
    assert ticket.team_id == second.id


def test_a_repeated_escalate_by_the_same_agent_is_not_a_server_error(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_id = agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    db_session.add(TicketEscalator(ticket_id=ticket.id, user_id=agent_id))  # as if a twin request got there first
    db_session.commit()
    assert escalate(client, ticket).status_code == 200
    assert len(db_session.exec(select(TicketEscalator)).all()) == 1

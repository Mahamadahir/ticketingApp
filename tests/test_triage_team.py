"""The category fills in the team at triage; the triager may pick another, which is recorded."""

from sqlmodel import select

from app.models import Category, Ticket, TicketStatus, UserRole
from app.settings_store import DEFAULT_TRIAGE_TEAM, set_setting
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_splitting import split, subs_of
from tests.test_triage import agent, events, new_ticket, triage


def member(client, db_session, email, name, team):
    """An agent in the team, named so the Assign to options can be told apart."""
    user_id = login_as(client, email, display_name=name)
    set_role(db_session, user_id, UserRole.AGENT, team.id)
    db_session.commit()
    return user_id


def overrides(db_session, ticket):
    return [(e.from_value, e.to_value, e.detail) for e in events(db_session, ticket, "team_overridden")]


# ---- The category's team is the default -----------------------------------------------------------


def test_no_team_chosen_takes_the_categorys_team_and_records_no_override(client, db_session):
    desk, dev = create_team(db_session, "Service Desk"), create_team(db_session, "Dev")
    ticket = new_ticket(db_session, desk)
    apps = create_category(db_session, dev, "Internal apps")
    agent(client, db_session, member_of=[desk])

    assert triage(client, ticket, apps).status_code == 200

    db_session.refresh(ticket)
    assert ticket.team_id == dev.id
    assert overrides(db_session, ticket) == []


def test_choosing_the_categorys_own_team_is_not_an_override(client, db_session):
    desk, dev = create_team(db_session, "Service Desk"), create_team(db_session, "Dev")
    ticket = new_ticket(db_session, desk)
    apps = create_category(db_session, dev, "Internal apps")
    agent(client, db_session, member_of=[desk])

    assert triage(client, ticket, apps, team_id=dev.id).status_code == 200

    db_session.refresh(ticket)
    assert ticket.team_id == dev.id
    assert overrides(db_session, ticket) == []


# ---- The triager picks another team -----------------------------------------------------------------


def test_another_team_wins_and_the_override_is_recorded(client, db_session):
    desk, dev, data = (create_team(db_session, n) for n in ("Service Desk", "Dev", "Data"))
    ticket = new_ticket(db_session, desk)
    apps = create_category(db_session, dev, "Internal apps")
    reports = create_category(db_session, None, "Reports", parent=apps)
    agent(client, db_session, member_of=[desk])

    assert triage(client, ticket, reports, team_id=data.id).status_code == 200

    db_session.refresh(ticket)
    assert ticket.team_id == data.id
    assert ticket.category_id == reports.id  # the category stays what the ticket is about
    assert ticket.status == TicketStatus.OPEN
    assert overrides(db_session, ticket) == [("Dev", "Data", "Internal apps / Reports")]
    assert [(e.from_value, e.to_value) for e in events(db_session, ticket, "team_changed")] == [("Service Desk", "Data")]


def test_the_assignee_must_be_in_the_chosen_team_not_the_categorys(client, db_session):
    desk, dev, data = (create_team(db_session, n) for n in ("Service Desk", "Dev", "Data"))
    ticket = new_ticket(db_session, desk)
    apps = create_category(db_session, dev, "Internal apps")
    developer = member(client, db_session, "dev@example.com", "Dana Developer", dev)
    analyst = member(client, db_session, "analyst@example.com", "Ari Analyst", data)
    agent(client, db_session, member_of=[desk])  # the triager

    assert triage(client, ticket, apps, team_id=data.id, agent_id=developer).status_code == 400
    db_session.refresh(ticket)
    assert ticket.triaged_at is None  # nothing changed

    assert triage(client, ticket, apps, team_id=data.id, agent_id=analyst).status_code == 200
    db_session.refresh(ticket)
    assert (ticket.team_id, ticket.assigned_agent_id) == (data.id, analyst)


def test_an_unknown_team_is_refused(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    hardware = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])

    assert triage(client, ticket, hardware, team_id=9999).status_code == 400
    db_session.refresh(ticket)
    assert ticket.triaged_at is None


# ---- Categories with no team --------------------------------------------------------------------------


def test_a_category_with_no_team_goes_to_the_default_triage_team(client, db_session):
    desk, first_line = create_team(db_session, "Service Desk"), create_team(db_session, "1st line")
    set_setting(db_session, DEFAULT_TRIAGE_TEAM, str(first_line.id))
    db_session.commit()
    passwords = create_category(db_session, None, "Password reset")
    locked = create_category(db_session, None, "Locked out", parent=passwords)
    first, second = new_ticket(db_session, desk), new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])

    assert triage(client, first, passwords).status_code == 200
    assert triage(client, second, locked).status_code == 200  # a sub-category of a teamless main too

    for ticket in (first, second):
        db_session.refresh(ticket)
        assert ticket.team_id == first_line.id
        assert overrides(db_session, ticket) == []


def test_no_team_anywhere_needs_the_triager_to_choose_one(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    passwords = create_category(db_session, None, "Password reset")  # and no default triage team set
    agent(client, db_session, member_of=[desk])

    refused = triage(client, ticket, passwords)
    assert refused.status_code == 400 and "Choose a team" in refused.text

    assert triage(client, ticket, passwords, team_id=desk.id).status_code == 200
    db_session.refresh(ticket)
    assert ticket.team_id == desk.id
    assert overrides(db_session, ticket) == [(None, "Service Desk", "Password reset")]


def test_a_super_admin_can_create_a_main_category_without_a_team(client, db_session):
    login_as(client, "admin@example.com")  # the first user is the super admin

    response = client.post("/admin/categories", data={"name": "Password reset"}, follow_redirects=False)

    assert response.status_code == 303
    created = db_session.exec(select(Category).where(Category.name == "Password reset")).one()
    assert created.team_id is None and created.parent_id is None


# ---- The triage panel ------------------------------------------------------------------------------------


def test_the_panel_fills_in_the_categorys_team_and_its_agents(client, db_session):
    desk, dev, data = (create_team(db_session, n) for n in ("Service Desk", "Dev", "Data"))
    apps = create_category(db_session, dev, "Internal apps")
    member(client, db_session, "dev@example.com", "Dana Developer", dev)
    member(client, db_session, "analyst@example.com", "Ari Analyst", data)
    agent(client, db_session, member_of=[desk])

    panel = client.get(f"/tickets/triage-team?category_id={apps.id}").text
    assert f'<option value="{dev.id}" selected>Dev (the category&#39;s team)</option>' in panel
    assert f'<option value="{data.id}" >Data</option>' in panel  # every team can be picked
    assert "Dana Developer" in panel and "Ari Analyst" not in panel

    # Picking another team lists its agents; going back to blank means the category's team.
    other = client.get(f"/tickets/triage-agents?team_id={data.id}&category_id={apps.id}").text
    assert "Ari Analyst" in other and "Dana Developer" not in other
    blank = client.get(f"/tickets/triage-agents?team_id=&category_id={apps.id}").text
    assert "Dana Developer" in blank and "Ari Analyst" not in blank

    teamless = create_category(db_session, None, "Password reset")  # no default triage team set
    assert "Choose a team" in client.get(f"/tickets/triage-team?category_id={teamless.id}").text

    nothing = client.get("/tickets/triage-team").text
    assert "Choose a team" not in nothing
    assert "selected" not in nothing and "Dana Developer" not in nothing


def test_the_ticket_page_shows_the_team_box(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])

    page = client.get(f"/tickets/{ticket.number}").text

    assert 'id="triage-team"' in page and 'name="team_id"' in page
    assert 'hx-get="/tickets/triage-team"' in page


# ---- Splitting keeps the original's team ----------------------------------------------------------------


def test_parts_of_an_overridden_ticket_stay_with_its_team(client, db_session):
    desk, dev, data = (create_team(db_session, n) for n in ("Service Desk", "Dev", "Data"))
    ticket = new_ticket(db_session, desk)
    apps = create_category(db_session, dev, "Internal apps")
    agent(client, db_session, member_of=[desk, data])
    assert triage(client, ticket, apps, team_id=data.id).status_code == 200

    assert split(client, ticket, ("Fix the report", ""), ("Fix the export", "")).status_code == 200

    parts = subs_of(db_session, ticket)
    assert len(parts) == 2
    for part in parts:
        assert (part.team_id, part.category_id) == (data.id, apps.id)
        assert overrides(db_session, part) == [("Dev", "Data", "Internal apps")]
    assert db_session.get(Ticket, ticket.id).team_id == data.id

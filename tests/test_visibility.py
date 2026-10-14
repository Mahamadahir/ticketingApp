import itertools

from sqlmodel import select

from app.models import (
    AuditLog,
    MembershipLevel,
    Ticket,
    TicketFollower,
    TicketStatus,
    TeamMembership,
    User,
    UserRole,
)
from app.visibility import Access, access_context, ticket_access, visible_ticket_condition
from tests.conftest import escalatable, add_membership, create_category, create_team, login_as, set_role


def make_ticket(db_session, team_id=None, status=TicketStatus.OPEN, subject="Ticket", **fields):
    ticket = Ticket(subject=subject, requester_email="requester@example.com", team_id=team_id, status=status, **fields)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_in(client, db_session, member_of=(), viewer_of=(), email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT)
    for team in member_of:
        add_membership(db_session, agent_id, team.id, MembershipLevel.MEMBER)
    for team in viewer_of:
        add_membership(db_session, agent_id, team.id, MembershipLevel.VIEWER)
    return agent_id


# ---- The rule ---------------------------------------------------------------


def test_member_works_viewer_reads_others_cannot_see(client, db_session):
    mine, watched, other = (create_team(db_session, name) for name in ("Mine", "Watched", "Other"))
    agent_in(client, db_session, member_of=[mine], viewer_of=[watched])
    own_ticket = make_ticket(db_session, mine.id, subject="Own team")
    viewed_ticket = make_ticket(db_session, watched.id, subject="Watched team")
    hidden_ticket = make_ticket(db_session, other.id, subject="Other team")

    listing = client.get("/tickets").text
    assert "Own team" in listing and "Watched team" in listing
    assert "Other team" not in listing

    assert client.get(f"/tickets/{own_ticket.number}").status_code == 200
    assert client.get(f"/tickets/{viewed_ticket.number}").status_code == 200
    assert client.get(f"/tickets/{hidden_ticket.number}").status_code == 403


def test_viewer_cannot_change_anything(client, db_session):
    watched = create_team(db_session, "Watched")
    agent_in(client, db_session, viewer_of=[watched])
    ticket = make_ticket(db_session, watched.id)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "read-only access" in page
    assert "Preview email" not in page and "Add note" not in page

    for path, data in (
        ("email", {"body": "hi"}),
        ("note", {"body": "hi"}),
        ("status", {"status": "resolved"}),
        ("assign", {}),
        ("escalate", {"reason": "x"}),
    ):
        assert client.post(f"/tickets/{ticket.number}/{path}", data=data).status_code == 403, path

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN
    assert ticket.assigned_agent_id is None


def test_every_agent_can_see_untriaged_tickets_but_must_claim_one_to_work_it(client, db_session):
    other = create_team(db_session, "Other")
    agent_id = agent_in(client, db_session)  # no teams at all
    new_in_other_team = make_ticket(db_session, other.id, status=TicketStatus.NEW, subject="Untriaged")
    make_ticket(db_session, None, status=TicketStatus.NEW, subject="No team yet")
    triaged_elsewhere = make_ticket(db_session, other.id, status=TicketStatus.OPEN, subject="Triaged elsewhere")

    listing = client.get("/tickets").text
    assert "Untriaged" in listing and "No team yet" in listing
    assert "Triaged elsewhere" not in listing

    # Visible, with a way to claim it, but not workable until claimed.
    page = client.get(f"/tickets/{new_in_other_team.number}").text
    assert "assign it to yourself" in page and "Assign to me" in page and "Preview email" not in page
    assert client.post(f"/tickets/{new_in_other_team.number}/email", data={"body": "On it"}).status_code == 403
    assert client.post(f"/tickets/{new_in_other_team.number}/status", data={"status": "open"}).status_code == 403
    assert client.post(f"/tickets/{triaged_elsewhere.number}/assign").status_code == 403
    assert client.post(f"/tickets/{triaged_elsewhere.number}/note", data={"body": "x"}).status_code == 403

    # An untriaged ticket can be annotated without claiming or triaging it.
    assert "Add note" in page
    assert client.post(f"/tickets/{new_in_other_team.number}/note", data={"body": "Spoke to the user"}).status_code == 200
    db_session.refresh(new_in_other_team)
    assert new_in_other_team.assigned_agent_id is None and new_in_other_team.status == TicketStatus.NEW

    assert client.post(f"/tickets/{new_in_other_team.number}/assign").status_code == 200
    db_session.refresh(new_in_other_team)
    assert new_in_other_team.assigned_agent_id == agent_id
    # Claimed but still untriaged and in a team they are not in: no access to work it, so no email.
    assert client.post(f"/tickets/{new_in_other_team.number}/email", data={"body": "On it"}).status_code == 403


def test_an_agent_in_the_team_keeps_a_claimed_ticket_after_it_leaves_new(client, db_session):
    """The bug this guards against: the ticket leaving New made the agent lose
    sight of a ticket they had been working. Being in the team is what keeps it."""
    other = create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[other])
    ticket = make_ticket(db_session, other.id, status=TicketStatus.NEW)

    client.post(f"/tickets/{ticket.number}/assign")
    category = create_category(db_session, other)
    triage = client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id})
    assert triage.status_code == 200
    client.post(f"/tickets/{ticket.number}/email", data={"body": "First email"})
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_USER

    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Follow-up"}).status_code == 200
    assert "Follow-up" in client.get(f"/tickets/{ticket.number}").text


def test_claiming_then_triaging_into_a_team_you_are_not_in_hands_it_over_cleanly(client, db_session):
    other = create_team(db_session, "Other")
    agent_in(client, db_session)  # in no team
    ticket = make_ticket(db_session, other.id, status=TicketStatus.NEW)
    client.post(f"/tickets/{ticket.number}/assign")
    category = create_category(db_session, other)

    response = client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id})

    assert response.status_code == 200 and "has been moved to Other" in response.text
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id is None  # not left assigned to someone who cannot open it
    assert client.get(f"/tickets/{ticket.number}").status_code == 403


def test_super_admin_can_assign_any_agent_to_a_ticket_with_no_team(client, db_session):
    agent_id = agent_in(client, db_session, email="agent@example.com")
    client.post("/auth/logout")
    ticket = make_ticket(db_session, None, status=TicketStatus.NEW)
    login_as(client, "admin@example.com")

    assert client.post(f"/tickets/{ticket.number}/assign", data={"agent_id": agent_id}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == agent_id


def test_being_assigned_does_not_keep_access_when_the_ticket_is_in_another_team(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    agent_id = agent_in(client, db_session, member_of=[mine])
    ticket = make_ticket(db_session, mine.id)
    ticket.assigned_agent_id = agent_id
    ticket.team_id = other.id
    db_session.add(ticket)
    db_session.commit()

    assert client.get(f"/tickets/{ticket.number}").status_code == 403
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"}).status_code == 403
    assert str(ticket.reference) not in client.get("/tickets").text
    assert str(ticket.reference) not in client.get("/tickets?mine=true").text


def test_escalating_hands_the_ticket_to_the_other_team_but_the_escalator_keeps_it(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    agent_in(client, db_session, member_of=[mine])
    colleague_id = agent_in(client, db_session, member_of=[mine], email="colleague@example.com")
    login_as(client, "agent@example.com")
    ticket = make_ticket(db_session, mine.id)

    escalatable(db_session, ticket, network)
    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Needs network"})
    assert response.status_code == 200

    # The escalator keeps working this one ticket...
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert ticket.reference in client.get("/tickets").text
    # ...but a colleague who did not escalate it has lost it with the move.
    client.post("/auth/logout")
    login_as(client, "colleague@example.com")
    assert client.get(f"/tickets/{ticket.number}").status_code == 403
    assert ticket.reference not in client.get("/tickets").text


def test_team_filter_works_for_agents_within_what_they_can_see(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_in(client, db_session, member_of=[a, b])
    make_ticket(db_session, a.id, subject="In team A")
    make_ticket(db_session, b.id, subject="In team B")

    filtered = client.get(f"/tickets?team_id={a.id}").text
    assert "In team A" in filtered
    assert "In team B" not in filtered


def test_super_admin_assigns_only_team_members(client, db_session):
    team = create_team(db_session, "Service Desk")
    member_id = agent_in(client, db_session, member_of=[team], email="member@example.com")
    client.post("/auth/logout")
    viewer_id = agent_in(client, db_session, viewer_of=[team], email="viewer@example.com")
    client.post("/auth/logout")
    ticket = make_ticket(db_session, team.id)
    login_as(client, "admin@example.com")

    assert client.post(f"/tickets/{ticket.number}/assign", data={"agent_id": viewer_id}).status_code == 400
    assert client.post(f"/tickets/{ticket.number}/assign", data={"agent_id": member_id}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == member_id


def test_list_query_and_single_ticket_rule_agree_for_every_combination(client, db_session):
    """visible_ticket_condition (SQL) and ticket_access (Python) are written
    separately; this keeps them from drifting apart."""
    mine, watched, other = (create_team(db_session, name) for name in ("Mine", "Watched", "Other"))
    agent_id = agent_in(client, db_session, member_of=[mine], viewer_of=[watched])
    someone_else = login_as(client, "someone-else@example.com")
    login_as(client, "agent@example.com")

    team_choices = [mine.id, watched.id, other.id, None]
    # Triage state: untriaged is visible to every agent. A triaged ticket follows its team,
    # whoever triaged it.
    triage_choices = ["untriaged", "triaged_by_none", "triaged_by_me", "triaged_by_other"]
    assigned_choices = [None, agent_id, someone_else]
    # Followers grant nothing, internal or external: they are about who is copied on mail.
    follower_choices = ["none", "internal_me", "external_me", "internal_other"]
    for team_id, triage_state, assigned, follower in itertools.product(
        team_choices, triage_choices, assigned_choices, follower_choices
    ):
        status = TicketStatus.NEW if triage_state == "untriaged" else TicketStatus.OPEN
        triaged_by = {"triaged_by_me": agent_id, "triaged_by_other": someone_else}.get(triage_state)
        ticket = make_ticket(db_session, team_id, status, assigned_agent_id=assigned, triaged_by_id=triaged_by)
        if follower != "none":
            who = agent_id if follower.endswith("me") else someone_else
            db_session.add(
                TicketFollower(
                    ticket_id=ticket.id,
                    email=f"{follower}@example.com",
                    user_id=who,
                    is_internal=follower.startswith("internal"),
                    added_by_email="test@example.com",
                )
            )
            db_session.commit()

    agent = db_session.get(User, agent_id)
    levels = access_context(db_session, agent)
    every_ticket = db_session.exec(select(Ticket)).all()
    by_rule = {t.id for t in every_ticket if ticket_access(agent, t, levels) >= Access.READ}
    by_query = {t.id for t in db_session.exec(select(Ticket).where(visible_ticket_condition(agent, levels))).all()}

    assert len(every_ticket) == 4 * 4 * 3 * 4
    assert by_rule == by_query
    assert 0 < len(by_rule) < len(every_ticket)

    # Access follows the team. For a triaged ticket in a team the agent is not in, being
    # its assignee, its triager or its internal follower changes nothing.
    outside = [t for t in every_ticket if t.triaged_at and t.team_id not in (mine.id, watched.id)]
    assert outside and not {t.id for t in outside} & by_rule
    assert {t.id for t in outside if t.assigned_agent_id == agent_id or t.triaged_by_id == agent_id}
    # And the other way: in a team they are in, nobody's assignment or follower row takes it away.
    inside = [t for t in every_ticket if t.team_id == mine.id]
    assert {t.id for t in inside} <= by_rule


# ---- Admin ------------------------------------------------------------------


def test_admin_adds_changes_and_removes_memberships_with_audit(client, db_session):
    team = create_team(db_session, "Service Desk")
    target_id = login_as(client, "agent@example.com")
    set_role(db_session, target_id, UserRole.AGENT)
    client.post("/auth/logout")
    login_as(client, "admin@example.com")
    url = f"/admin/users/{target_id}/memberships"

    client.post(url, data={"team_id": team.id, "level": "viewer"}, follow_redirects=False)
    assert db_session.get(TeamMembership, (target_id, team.id)).level == MembershipLevel.VIEWER

    client.post(url, data={"team_id": team.id, "level": "member"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(TeamMembership, (target_id, team.id)).level == MembershipLevel.MEMBER

    client.post(f"{url}/{team.id}/remove", follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(TeamMembership, (target_id, team.id)) is None

    actions = [e.action for e in db_session.exec(select(AuditLog).where(AuditLog.entity_id == target_id))]
    assert actions == ["membership_added", "membership_changed", "membership_removed"]


def test_only_super_admins_manage_memberships(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_id = agent_in(client, db_session, member_of=[team])

    assert client.post(f"/admin/users/{agent_id}/memberships", data={"team_id": team.id, "level": "viewer"}).status_code == 403
    assert client.post(f"/admin/users/{agent_id}/unlock").status_code == 403


def test_membership_endpoints_reject_unknown_ids(client, db_session):
    login_as(client, "admin@example.com")
    assert client.post("/admin/users/9999/memberships", data={"team_id": 1, "level": "member"}).status_code == 404
    assert client.post("/admin/users/1/memberships/9999/remove").status_code == 404


def test_creating_a_local_account_can_add_the_first_membership(client, db_session):
    team = create_team(db_session, "Service Desk")
    login_as(client, "admin@example.com")

    client.post(
        "/admin/users",
        data={
            "email": "contractor@example.com",
            "display_name": "C",
            "password": "correct-horse",
            "role": "agent",
            "team_id": team.id,
            "level": "viewer",
        },
        follow_redirects=False,
    )

    created = db_session.exec(select(User).where(User.email == "contractor@example.com")).one()
    assert db_session.get(TeamMembership, (created.id, team.id)).level == MembershipLevel.VIEWER


def test_users_page_lists_memberships(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_id = agent_in(client, db_session, member_of=[team])
    client.post("/auth/logout")
    login_as(client, "admin@example.com")

    page = client.get("/admin/users").text
    assert "Service Desk (member)" in page
    assert f"/admin/users/{agent_id}/memberships/{team.id}/remove" in page


def test_unlock_clears_the_lock_and_is_audited(client, db_session):
    from datetime import datetime, timedelta, timezone

    from app.auth import hash_password

    locked = User(
        email="locked@example.com",
        display_name="Locked",
        role=UserRole.AGENT,
        password_hash=hash_password("correct-horse"),
        failed_login_count=7,
        locked_until=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(locked)
    db_session.commit()
    db_session.refresh(locked)
    login_as(client, "admin@example.com")

    page = client.get("/admin/users").text
    assert "locked until" in page and "7 failed attempts" in page

    client.post(f"/admin/users/{locked.id}/unlock", follow_redirects=False)

    db_session.expire_all()
    after = db_session.get(User, locked.id)
    assert after.failed_login_count == 0 and after.locked_until is None
    assert [e.action for e in db_session.exec(select(AuditLog).where(AuditLog.entity_id == locked.id))] == ["unlocked"]


# ---- Sign-in follow-ups -----------------------------------------------------


def test_logout_is_post_only(client):
    login_as(client, "someone@example.com")
    assert client.get("/auth/logout").status_code == 405
    assert client.get("/portal", follow_redirects=False).status_code == 200


def test_oauth_state_works_once(client, monkeypatch):
    from app.config import settings
    from app.routers import auth as auth_router

    seen = {}
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(auth_router, "build_auth_url", lambda state: seen.setdefault("state", state) and "https://login.example/authorize")
    monkeypatch.setattr(
        auth_router,
        "acquire_token_by_auth_code",
        lambda code: {"id_token_claims": {"preferred_username": "sso-user@example.com", "name": "SSO User"}},
    )

    client.get("/auth/sso/start", follow_redirects=False)
    state = seen["state"]

    first = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert first.status_code == 303

    replay = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert replay.status_code == 400


def test_memberships_are_refused_for_end_users(client, db_session):
    team = create_team(db_session, "Service Desk")
    requester_id = login_as(client, "requester@example.com")
    client.post("/auth/logout")
    login_as(client, "admin@example.com")

    response = client.post(
        f"/admin/users/{requester_id}/memberships", data={"team_id": team.id, "level": "member"}
    )
    assert response.status_code == 400
    assert db_session.get(TeamMembership, (requester_id, team.id)) is None

    created = client.post(
        "/admin/users",
        data={
            "email": "newuser@example.com",
            "display_name": "N",
            "password": "correct-horse",
            "role": "end_user",
            "team_id": team.id,
        },
    )
    assert created.status_code == 400


def test_a_non_ascii_oauth_state_is_a_400_not_a_server_error(client, monkeypatch):
    from app.config import settings
    from app.routers import auth as auth_router

    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(auth_router, "build_auth_url", lambda state: "https://login.example/authorize")

    client.get("/auth/sso/start", follow_redirects=False)
    response = client.get("/auth/callback", params={"code": "abc", "state": "é"}, follow_redirects=False)
    assert response.status_code == 400


def test_an_assigned_untriaged_ticket_cannot_be_taken_by_another_agent(client, db_session):
    """New and assigned is a valid state (handed to whoever will triage it). The
    claim button must not let a different agent take it over."""
    other_team = create_team(db_session, "Other")
    assignee_id = agent_in(client, db_session, email="assignee@example.com")
    client.post("/auth/logout")
    ticket = make_ticket(db_session, other_team.id, status=TicketStatus.NEW)
    ticket.assigned_agent_id = assignee_id
    db_session.add(ticket)
    db_session.commit()

    agent_in(client, db_session, email="bystander@example.com")
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Assign to me" not in page and "read-only access" in page
    assert client.post(f"/tickets/{ticket.number}/assign").status_code == 403
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == assignee_id and ticket.status == TicketStatus.NEW

    # The assignee can read it and annotate it (it is untriaged), and it stays New until
    # triage. Emailing the requester is not available: untriaged, and not their team.
    client.post("/auth/logout")
    login_as(client, "assignee@example.com")
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "Looking"}).status_code == 200
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Looking"}).status_code == 403

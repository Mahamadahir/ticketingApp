"""Admin > Users (#322, design B): a full-width people table, and a page per person (/admin/users/<id>) drawn from
two registries (PERSON_SECTIONS and PERSON_FACTS in app/admin_people.py). Every form on the person page posts to its
existing route with its existing field names; the rules (last super admin, break-glass role, merged accounts staying
inactive, the audit entries) are the routes' and are tested with them. After a change the browser returns to the person
page, on the card the change came from; a refusal comes back as that page, status 400, with the message in its card."""

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlmodel import select

from app import admin_people
from app import linked_identities as links
from app.activity import LOGIN_SUCCESS
from app.auth import hash_password
from app.database import engine
from app.models import (
    ActivityLog,
    AuditLog,
    MembershipLevel,
    TeamMembership,
    Ticket,
    TicketStatus,
    User,
    UserPermission,
    UserRole,
)
from app.permissions import MANAGE_REQUEST_TEMPLATES, PERMISSIONS
from tests.conftest import add_membership, create_team, login_as, set_role


def boss(client, db_session) -> int:
    user_id = login_as(client, "boss@example.com", "Boss")
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)
    return user_id


def person(db_session, email, role=UserRole.AGENT, name=None, **fields) -> User:
    user = User(email=email, display_name=name or email.split("@")[0], role=role, **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def page(client, user_id) -> str:
    response = client.get(f"/admin/users/{user_id}")
    assert response.status_code == 200
    return response.text


def card(html, key) -> str:
    found = re.search(rf'<section class="card pp-card[^"]*" id="sec-{key}".*?</section>', html, re.S)
    assert found, f"no {key} card"
    return found.group(0)


def jump_keys(html) -> list[str]:
    nav = re.search(r'<ul class="pp-jump">(.*?)</ul>', html, re.S).group(1)
    return re.findall(r'href="#sec-(\w+)"', nav)


def card_keys(html) -> list[str]:
    return re.findall(r'<section class="card pp-card[^"]*" id="sec-(\w+)"', html)


def glance_of(html) -> dict[str, str]:
    """fact key -> the text of its tile."""
    block = re.search(r'<ul class="pp-facts">(.*?)</ul>', html, re.S).group(1)
    return {
        key: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r'<span class="pp-fact-label">.*?</span>', "", body))).strip()
        for key, body in re.findall(r'data-fact="(\w+)">(.*?)</li>', block, re.S)
    }


def fact_href(html, key) -> str | None:
    block = re.search(rf'data-fact="{key}">(.*?)</li>', html, re.S).group(1)
    found = re.search(r'<a href="([^"]+)"', block)
    return found.group(1).replace("&amp;", "&") if found else None


def forms(html) -> dict[str, list[str]]:
    """action -> the field names inside that form, for every POST form in the HTML."""
    out = {}
    for action, body in re.findall(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>', html, re.S):
        out[action] = sorted(set(re.findall(r'name="([^"]+)"', body)))
    return out


def rows(client, query="") -> str:
    html = client.get("/admin/users" + query).text
    return re.search(r"<tbody>.*?</tbody>", html, re.S).group(0)


ALL_KEYS = ["role", "teams", "perms", "signin", "linked", "status"]


# ---- the person page, per kind of account -------------------------------------------------------------------------


def test_an_entra_agent_page_has_every_section_and_forms_post_to_the_existing_routes(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    agent = person(db_session, "ann@example.com", entra_object_id="oid-ann")
    add_membership(db_session, agent.id, team.id)
    html = page(client, agent.id)
    assert card_keys(html) == jump_keys(html) == ALL_KEYS
    assert "Microsoft account" in html and "oid-ann" in html and "0 signed-in sessions" in html
    base = f"/admin/users/{agent.id}"
    posted = forms(html)
    assert posted[base] == ["role"]
    assert posted[f"{base}/permissions"] == ["permissions"]
    assert posted[f"{base}/memberships/{team.id}/remove"] == []
    assert posted[f"{base}/sign-out"] == [] and posted[f"{base}/clear-oid"] == []
    assert posted[f"{base}/active"] == ["is_active"]
    assert f"{base}/unlock" not in posted  # not a local account
    assert posted[f"{base}/memberships"] == ["level", "team_id"]
    assert html.count(f'action="{base}/memberships"') == 2  # the level form of the team, and the add form
    assert "Deactivate</button>" in html and "Activate</button>" not in html
    assert all(a.startswith("/admin/") for a in forms(html.split('class="pp-body"')[1]))  # no form on the page posts elsewhere


def test_the_account_status_card_is_last_and_red(client, db_session):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    html = page(client, agent.id)
    assert card_keys(html)[-1] == "status"
    assert 'class="card pp-card pp-danger" id="sec-status"' in html
    assert 'class="pp-danger-link" href="#sec-status"' in html
    assert 'href="#sec-status">Account status' in html  # the header's quick link


def test_a_super_admin_page_says_all_permissions(client, db_session):
    admin_id = boss(client, db_session)
    html = page(client, admin_id)
    assert card_keys(html) == ALL_KEYS
    assert "All (super admin)." in card(html, "perms") and f"/admin/users/{admin_id}/permissions" not in html
    assert "1 signed-in session" in html  # the signed-in admin holds the one live session


def test_an_end_user_page_has_every_section_but_no_team_form_and_no_permissions(client, db_session):
    boss(client, db_session)
    create_team(db_session, "Service Desk")
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    html = page(client, end.id)
    assert card_keys(html) == ALL_KEYS
    assert "Make this person an agent before adding them to a team." in card(html, "teams")
    assert f"/admin/users/{end.id}/memberships" not in html and "None (agents only)." in card(html, "perms")
    glance = glance_of(html)
    assert "teams" not in glance and "permissions" not in glance and "open_tickets" not in glance  # facts that do not apply are left out


def test_an_end_user_who_still_has_a_membership_can_only_be_removed_from_it(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    add_membership(db_session, end.id, team.id)
    posted = forms(page(client, end.id))
    assert f"/admin/users/{end.id}/memberships/{team.id}/remove" in posted
    assert f"/admin/users/{end.id}/memberships" not in posted


def test_the_permission_boxes_show_what_is_granted(client, db_session):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    html = card(page(client, agent.id), "perms")
    for key in PERMISSIONS:
        assert f'value="{key}"' in html
    assert re.search(rf'value="{MANAGE_REQUEST_TEMPLATES}" checked', html)
    assert html.count(" checked") == 1


def test_a_break_glass_page_has_no_role_form_and_points_to_the_command(client, db_session):
    boss(client, db_session)
    glass = person(db_session, "breakglass1@example.com", role=UserRole.SUPER_ADMIN, is_break_glass=True,
                   password_hash=hash_password("long-enough-password"))
    html = page(client, glass.id)
    assert card_keys(html) == ALL_KEYS
    assert "A break-glass account stays a super admin" in html and "Save role" not in html
    assert f'action="/admin/users/{glass.id}"' not in html  # the role form is the only one on that bare path
    assert "python -m app.breakglass reset breakglass1" in html and "All (super admin)" in html
    assert "authenticator code" in html and "Break-glass" in html
    assert f"/admin/users/{glass.id}/active" in html  # it can still be deactivated
    assert "Break-glass" in rows(client)


def test_a_locked_local_account_shows_locked_and_offers_unlock(client, db_session):
    boss(client, db_session)
    local = person(db_session, "lee@example.com", password_hash=hash_password("long-enough-password"))
    html = page(client, local.id)
    assert card_keys(html) == ALL_KEYS
    assert "Local account (password)" in html and "No Entra id" not in html and "Locked" not in html
    assert f"/admin/users/{local.id}/unlock" not in html and "clear-oid" not in html
    assert glance_of(html)["locked"].startswith("Not locked")
    local.failed_login_count = 5
    local.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.add(local)
    db_session.commit()
    html = page(client, local.id)
    assert "5 failed attempts" in html and forms(html)[f"/admin/users/{local.id}/unlock"] == []
    assert glance_of(html)["locked"].startswith("Locked") and fact_href(html, "locked") == "#sec-signin"
    assert '<span class="chip t-red">Locked</span>' in html.split('id="glance-h"')[0]  # the header chips
    assert "Locked" in rows(client, "?q=lee")


def test_a_merged_account_cannot_be_activated_and_the_route_refuses_in_its_card(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    html = page(client, other.id)
    assert card_keys(html) == ALL_KEYS
    assert f'/admin/users/{main.id}">ann@a.example</a> by a link' in card(html, "linked") and "Merged" in html
    assert re.search(r'<button type="submit" class="btn btn-primary" disabled[^>]*>Activate</button>', html)
    assert glance_of(html)["linked"] == "Merged into ann@a.example" and fact_href(html, "linked") == f"/admin/users/{main.id}"
    refused = client.post(f"/admin/users/{other.id}/active", data={"is_active": "true"})
    assert refused.status_code == 400
    assert "merged into another by a link" in card(refused.text, "status") and 'role="alert"' in card(refused.text, "status")
    assert "ann@b.example" in refused.text  # the person's page, not a bare error
    # the main account lists the merged-in address with its own Unlink
    main_card = card(page(client, main.id), "linked")
    assert "ann@b.example" in main_card and "/unlink" in main_card


def test_an_inactive_account_offers_activate_and_no_new_permissions(client, db_session):
    boss(client, db_session)
    gone = person(db_session, "gone@example.com", is_active=False)
    html = page(client, gone.id)
    assert "(inactive: none can be added)" in html and "Currently inactive." in html
    status = card(html, "status")
    assert 'name="is_active" value="true"' in status and ">Activate</button>" in status and "disabled" not in status
    assert "Inactive" in html


# ---- actions, from the person page --------------------------------------------------------------------------------


def test_the_forms_work_end_to_end_and_each_returns_to_the_person_page_on_its_card(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    agent = person(db_session, "ann@example.com", entra_object_id="oid-ann")
    base = f"/admin/users/{agent.id}"
    posts = [
        (f"{base}/memberships", {"team_id": team.id, "level": "viewer"}, "teams", "teams"),
        (base, {"role": "agent"}, "role", "role"),
        (f"{base}/permissions", {"permissions": MANAGE_REQUEST_TEMPLATES}, "perms", "perms"),
        (f"{base}/sign-out", {}, "signin", "signed_out"),
        (f"{base}/clear-oid", {}, "signin", "oid_cleared"),
        (f"{base}/active", {"is_active": "false"}, "status", "deactivated"),
        (f"{base}/active", {"is_active": "true"}, "status", "activated"),
        (f"{base}/memberships/{team.id}/remove", {}, "teams", "teams"),
    ]
    for url, data, section, done in posts:
        response = client.post(url, data=data, follow_redirects=False)
        assert response.status_code == 303, url
        assert response.headers["location"] == f"{base}?done={done}#sec-{section}", url
    db_session.expire_all()
    assert db_session.get(User, agent.id).entra_object_id is None
    assert db_session.get(TeamMembership, (agent.id, team.id)) is None
    assert [r.permission for r in db_session.exec(select(UserPermission).where(UserPermission.user_id == agent.id))] == [MANAGE_REQUEST_TEMPLATES]
    actions = {e.action for e in db_session.exec(select(AuditLog).where(AuditLog.entity_id == agent.id))}
    assert {"membership_added", "permission_granted", "oid_cleared", "signed_out_everywhere", "deactivated", "activated",
            "membership_removed"} <= actions


def test_a_change_leaves_its_message_on_the_page(client, db_session):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    html = client.post(f"/admin/users/{agent.id}", data={"role": "end_user"}).text  # follows the redirect
    assert 'role="status">Role saved.</p>' in html and "End user" in html
    assert "pp-done" not in page(client, agent.id)  # no message without the query
    assert "pp-done" not in client.get(f"/admin/users/{agent.id}?done=nonsense").text


def test_unlock_returns_to_the_sign_in_card(client, db_session):
    boss(client, db_session)
    local = person(db_session, "lee@example.com", password_hash=hash_password("long-enough-password"), failed_login_count=4,
                   locked_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    response = client.post(f"/admin/users/{local.id}/unlock", follow_redirects=False)
    assert response.headers["location"] == f"/admin/users/{local.id}?done=unlocked#sec-signin"
    db_session.expire_all()
    assert db_session.get(User, local.id).failed_login_count == 0 and db_session.get(User, local.id).locked_until is None
    assert "Locked" not in card(page(client, local.id), "signin")


def test_refusals_show_in_the_right_card_with_the_route_status(client, db_session):
    boss(client, db_session)
    glass = person(db_session, "breakglass1@example.com", role=UserRole.SUPER_ADMIN, is_break_glass=True,
                   password_hash=hash_password("long-enough-password"))
    refused = client.post(f"/admin/users/{glass.id}", data={"role": "agent"})
    assert refused.status_code == 400 and "stays a super admin" in re.search(r'<p class="error" role="alert">(.*?)</p>', card(refused.text, "role")).group(1)
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    team = create_team(db_session, "Service Desk")
    refused = client.post(f"/admin/users/{end.id}/memberships", data={"team_id": team.id, "level": "member"})
    assert refused.status_code == 400 and "Make the user an agent" in card(refused.text, "teams")
    refused = client.post(f"/admin/users/{end.id}/permissions", data={"permissions": MANAGE_REQUEST_TEMPLATES})
    assert refused.status_code == 400 and "Only agents" in card(refused.text, "perms")
    refused = client.post(f"/admin/users/{end.id}/permissions", data={"permissions": "bogus"})
    assert refused.status_code == 400 and "Unknown permission" in card(refused.text, "perms")
    gone = person(db_session, "gone@example.com", is_active=False)
    refused = client.post(f"/admin/users/{gone.id}/permissions", data={"permissions": MANAGE_REQUEST_TEMPLATES})
    assert refused.status_code == 400 and "inactive account" in card(refused.text, "perms")
    refused = client.post(f"/admin/users/{end.id}/clear-oid")
    assert refused.status_code == 400 and "no stored object id" in card(refused.text, "signin")
    # the refusal shows once, in its own card only
    assert refused.text.split('class="pp-body"')[1].count('role="alert"') == 1
    db_session.expire_all()
    assert db_session.get(User, glass.id).role == UserRole.SUPER_ADMIN


def test_unlink_from_the_person_page_uses_the_existing_route_and_returns_there(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    html = card(page(client, main.id), "linked")
    link_action = re.search(r'action="(/admin/links/\d+/unlink)"', html).group(1)
    assert 'name="back"' in html and f'value="{main.id}"' in html
    response = client.post(link_action, data={"back": str(main.id)}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/admin/users/{main.id}?done=unlinked#sec-linked"
    assert "No linked accounts." in card(page(client, main.id), "linked")
    # unlinking again is refused in the card (the link is no longer in force)
    again = client.post(link_action, data={"back": str(main.id)})
    assert again.status_code == 400 and "not in force" in card(again.text, "linked")
    # without `back` (the Link requests tab) the old behaviour stands
    assert client.post(link_action, follow_redirects=False).headers["location"].startswith("/admin/users?tab=links&link_error=")


def test_each_linked_address_has_its_own_unlink(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    one = person(db_session, "ann@b.example", role=UserRole.END_USER)
    two = person(db_session, "ann@c.example", role=UserRole.END_USER)
    for secondary in (one, two):
        client.post("/admin/links", data={"main_id": main.id, "secondary_id": secondary.id})
    html = card(page(client, main.id), "linked")
    assert len(re.findall(r'action="/admin/links/\d+/unlink"', html)) == 2
    assert "ann@b.example" in html and "ann@c.example" in html
    assert glance_of(page(client, main.id))["linked"] == "2 linked identities"


# ---- At a glance -------------------------------------------------------------------------------------------------


def test_at_a_glance_facts_are_right_and_each_link_goes_where_it_says(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    agent = person(db_session, "ann@example.com", entra_object_id="oid-ann")
    add_membership(db_session, agent.id, team.id, MembershipLevel.VIEWER)
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    for status, who, requester in (
        (TicketStatus.OPEN, agent.id, "r@example.com"),
        (TicketStatus.OPEN, agent.id, "r@example.com"),
        (TicketStatus.CLOSED, agent.id, "r@example.com"),  # not open
        (TicketStatus.OPEN, None, "ann@example.com"),  # raised by Ann, not assigned
        (TicketStatus.OPEN, None, " ANN@example.com "),  # raised by Ann, another spelling
    ):
        db_session.add(Ticket(subject="t", requester_email=requester, team_id=team.id, status=status,
                              triaged_at=datetime.now(timezone.utc), assigned_agent_id=who))
    older = datetime.now(timezone.utc) - timedelta(days=3)
    db_session.add(ActivityLog(event_type=LOGIN_SUCCESS, actor_user_id=agent.id, actor_email=agent.email, detail="local", created_at=older))
    db_session.add(ActivityLog(event_type=LOGIN_SUCCESS, actor_user_id=agent.id, actor_email=agent.email, detail="sso"))
    db_session.commit()
    html = page(client, agent.id)
    glance = glance_of(html)
    assert glance["open_tickets"].startswith("2 open tickets")
    assert fact_href(html, "open_tickets") == f"/tickets?assignee={agent.id}&status=active"
    assert glance["raised"].startswith("2 tickets")
    assert fact_href(html, "raised") == f"/tickets?requester={agent.id}"
    assert "by Microsoft" in glance["last_sign_in"]  # the newest sign-in, not the older password one
    assert fact_href(html, "last_sign_in") == "/admin/activity-log?actor=ann%40example.com&event_type=login_success"
    assert glance["sessions"] == "0 signed-in sessions" and fact_href(html, "sessions") == "#sec-signin"
    assert glance["teams"] == "Service Desk (viewer)" and fact_href(html, "teams") == "#sec-teams"
    assert glance["permissions"] == PERMISSIONS[MANAGE_REQUEST_TEMPLATES].label and fact_href(html, "permissions") == "#sec-perms"
    assert "locked" not in glance  # not a local account
    assert glance["linked"] == "0 linked identities" and fact_href(html, "linked") == "#sec-linked"
    shortcuts = re.search(r'<p class="pp-shortcuts">(.*?)</p>', html, re.S).group(1)
    assert 'href="/tickets/activity?person=ann%40example.com">See their activity' in shortcuts
    assert f'href="/admin/audit-log?subject_user_id={agent.id}">Audit log for this person' in shortcuts
    # every link in the card resolves
    for href in (fact_href(html, "open_tickets"), fact_href(html, "raised"), fact_href(html, "last_sign_in"),
                 f"/tickets/activity?person=ann%40example.com", f"/admin/audit-log?subject_user_id={agent.id}"):
        assert client.get(href).status_code == 200, href


def test_the_queue_filters_the_at_a_glance_links_use(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    ann = person(db_session, "ann@example.com")
    bob = person(db_session, "bob@example.com")
    for subject, who, requester in (("Mine one", ann.id, "x@example.com"), ("Bobs", bob.id, "x@example.com"),
                                    ("Raised by bob", None, "bob@example.com")):
        db_session.add(Ticket(subject=subject, requester_email=requester, team_id=team.id, status=TicketStatus.OPEN,
                              triaged_at=datetime.now(timezone.utc), assigned_agent_id=who))
    db_session.commit()
    body = client.get(f"/tickets?assignee={ann.id}&status=active").text
    assert "Mine one" in body and "Bobs" not in body and "Raised by bob" not in body
    assert "Only tickets assigned to ann" in body and f'name="assignee" value="{ann.id}"' in body
    body = client.get(f"/tickets?requester={bob.id}").text
    assert "Raised by bob" in body and "Mine one" not in body and "Only tickets raised by bob" in body
    # a value that names nobody is ignored
    assert "Mine one" in client.get("/tickets?assignee=999999&requester=abc").text


def test_an_inactive_agent_with_open_tickets_is_flagged_in_the_glance(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    gone = person(db_session, "gone@example.com", is_active=False)
    db_session.add(Ticket(subject="t", requester_email="r@example.com", team_id=team.id, status=TicketStatus.OPEN,
                          triaged_at=datetime.now(timezone.utc), assigned_agent_id=gone.id))
    db_session.commit()
    html = page(client, gone.id)
    assert "inactive account" in glance_of(html)["open_tickets"] and 'class="chip t-amber"' in re.search(r'data-fact="open_tickets">(.*?)</li>', html, re.S).group(1)


def test_a_person_who_never_signed_in_says_so(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    assert glance_of(page(client, ann.id))["last_sign_in"] == "Never signed in"


def _count_queries(fn) -> int:
    seen = []

    def count(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(seen)


def test_the_at_a_glance_card_issues_a_fixed_number_of_queries(client, db_session):
    boss(client, db_session)
    team_a, team_b = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    bare = person(db_session, "bare@example.com")
    busy = person(db_session, "busy@example.com")
    for team in (team_a, team_b):
        add_membership(db_session, busy.id, team.id)
    for key in PERMISSIONS:
        db_session.add(UserPermission(user_id=busy.id, permission=key))
    for i in range(12):
        db_session.add(Ticket(subject=f"t{i}", requester_email="busy@example.com" if i % 2 else "r@example.com", team_id=team_a.id,
                              status=TicketStatus.OPEN, triaged_at=datetime.now(timezone.utc), assigned_agent_id=busy.id))
        db_session.add(ActivityLog(event_type=LOGIN_SUCCESS, actor_user_id=busy.id, actor_email=busy.email, detail="sso"))
    db_session.commit()

    def glance_for(user):
        db_session.expire_all()
        ctx = admin_people.PersonContext(db_session, db_session.get(User, user.id))
        return lambda: admin_people.glance(ctx)

    low, high = _count_queries(glance_for(bare)), _count_queries(glance_for(busy))
    assert low == high  # the number of queries does not grow with the person's data
    assert high <= 12


# ---- the registries: a new section or fact is one entry and one template ------------------------------------------


def test_a_section_added_to_the_registry_appears_in_the_page_and_the_jump_links(client, db_session, monkeypatch, tmp_path):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    extra = admin_people.Section(
        "prefs", "Notification preferences", "How they want to be told.", "_person/role.html",
        lambda ctx: {"roles": [], "current": ctx.person.role, "tone": "t-gray", "marker": ctx.person.email},
    )
    monkeypatch.setattr(admin_people, "PERSON_SECTIONS", [*admin_people.PERSON_SECTIONS[:-1], extra, admin_people.PERSON_SECTIONS[-1]])
    html = page(client, agent.id)
    assert card_keys(html) == jump_keys(html) == ["role", "teams", "perms", "signin", "linked", "prefs", "status"]
    assert "Notification preferences" in card(html, "prefs") and "How they want to be told." in card(html, "prefs")


def test_a_section_can_be_hidden_by_its_shown_check(client, db_session, monkeypatch):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    hidden_for_end_users = admin_people.Section(
        "desk", "Service desk", "", "_person/perms.html", admin_people._perms,
        shown=lambda ctx: ctx.person.role != UserRole.END_USER,
    )
    monkeypatch.setattr(admin_people, "PERSON_SECTIONS", [hidden_for_end_users, *admin_people.PERSON_SECTIONS])
    assert "desk" in card_keys(page(client, agent.id)) and "desk" in jump_keys(page(client, agent.id))
    assert "desk" not in card_keys(page(client, end.id)) and "desk" not in jump_keys(page(client, end.id))


def test_a_fact_added_to_the_registry_appears_in_the_glance(client, db_session, monkeypatch):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    extra = admin_people.Fact("devices", "Devices", lambda ctx: admin_people.FactValue("3 devices", "/somewhere", "t-blue", "last seen today"))
    skipped = admin_people.Fact("nothing", "Nothing", lambda ctx: None)
    monkeypatch.setattr(admin_people, "PERSON_FACTS", [*admin_people.PERSON_FACTS, extra, skipped])
    html = page(client, agent.id)
    assert glance_of(html)["devices"] == "3 devices last seen today" and fact_href(html, "devices") == "/somewhere"
    assert "nothing" not in glance_of(html)


# ---- finding the page ---------------------------------------------------------------------------------------------


def test_the_old_user_link_redirects_to_the_person_page_and_a_missing_person_is_not_found(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    response = client.get(f"/admin/users?user={ann.id}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/admin/users/{ann.id}"
    assert ann.email in client.get(f"/admin/users?status=break_glass&user={ann.id}").text  # followed: the person's page
    assert client.get("/admin/users/999999").status_code == 404
    assert client.get("/admin/users/99999999999").status_code == 404  # out of range: not found, not an error
    assert client.get("/admin/users/0").status_code == 404
    for odd in ("?user=abc", "?user=-1", "?new=yes", "?user=%C2%B2", "?team=%C2%B2", "?tab=zzz"):  # hand-edited: never an error
        assert client.get("/admin/users" + odd).status_code in (200,), odd
    assert client.get("/admin/users?user=99999999999").status_code == 200  # out of range: the list


def test_only_a_super_admin_sees_the_pages(client, db_session):
    other = person(db_session, "someone@example.com")
    login_as(client, "plain@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get(f"/admin/users/{other.id}").status_code == 403
    assert client.get("/admin/users?user=1&new=1").status_code == 403
    assert client.get("/admin/users?tab=links").status_code == 403
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    assert client.get(f"/admin/users/{other.id}").status_code == 403
    assert client.post(f"/admin/users/{other.id}", data={"role": "agent"}).status_code == 403


# ---- the list ------------------------------------------------------------------------------------------------------


def test_the_list_has_an_open_button_per_person_and_no_forms(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com", name="Ann Archer")
    body = rows(client, "?q=ann")
    assert f'href="/admin/users/{ann.id}"' in body and f'class="btn" href="/admin/users/{ann.id}">Open<span class="sr-only"> Ann Archer</span>' in body
    assert "<form" not in body and "<select" not in body


def test_the_list_shows_role_teams_and_status_chips(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    ann = person(db_session, "ann@example.com", name="Ann Archer")
    add_membership(db_session, ann.id, team.id)
    body = rows(client, "?q=ann")
    assert "Ann Archer" in body and "Agent" in body and "Service Desk &middot; member" in body and "Active" in body


def test_the_list_marks_merged_local_break_glass_and_locked_accounts(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    person(db_session, "lee@example.com", password_hash=hash_password("long-enough-password"),
           locked_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    person(db_session, "old@example.com", password_hash=hash_password("long-enough-password"),
           locked_until=datetime.now(timezone.utc) - timedelta(minutes=5))
    merged_row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*ann@b\.example.*?</tr>", rows(client), re.S).group(0)
    assert "merged into ann@a.example" in merged_row and "Merged" in merged_row
    lee_row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*lee@example\.com.*?</tr>", rows(client), re.S).group(0)
    assert "Local account" in lee_row and "Locked" in lee_row
    old_row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*old@example\.com.*?</tr>", rows(client), re.S).group(0)
    assert "Local account" in old_row and "Locked" not in old_row  # a lock that has run out is not shown


def test_inactive_agents_with_open_tickets_get_the_amber_edge(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    stranded = person(db_session, "stranded@example.com", is_active=False)
    quiet = person(db_session, "quiet@example.com", is_active=False)
    working = person(db_session, "working@example.com")
    for who, status in ((stranded, TicketStatus.OPEN), (quiet, TicketStatus.CLOSED), (working, TicketStatus.OPEN)):
        db_session.add(Ticket(subject="t", requester_email="r@example.com", team_id=team.id, status=status,
                              triaged_at=datetime.now(timezone.utc), assigned_agent_id=who.id))
    db_session.commit()
    body = rows(client)

    def row(email):
        return re.search(rf"<tr[^>]*>(?:(?!</tr>).)*{re.escape(email)}.*?</tr>", body, re.S).group(0)

    assert 'class="pp-stranded"' in row("stranded@example.com")
    assert "pp-stranded" not in row("quiet@example.com") and "pp-stranded" not in row("working@example.com")
    only = client.get("/admin/users?inactive=open_tickets").text
    assert "stranded@example.com" in only and "quiet@example.com" not in only and "working@example.com" not in only
    assert "Show everyone" in only and "Inactive agents with open tickets" in client.get("/admin/users").text


def test_the_tabs_and_the_pending_count(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users").text
    assert '<a class="tab" href="/admin/users" aria-current="page">People</a>' in html
    assert 'href="/admin/users?tab=links">Link requests</a>' in html  # no chip while none are waiting
    agent = person(db_session, "ann@example.com", role=UserRole.AGENT)
    requester = person(db_session, "ann@c.example", role=UserRole.END_USER)

    links.link_address(db_session, main=agent, address="ann@b.example", oid=None, method="email_code", requester=requester)
    db_session.commit()
    html = client.get("/admin/users").text
    assert re.search(r'Link requests <span class="chip t-amber">1</span>', html)
    tab = client.get("/admin/users?tab=links").text
    assert '<a class="tab" href="/admin/users?tab=links" aria-current="page">Link requests' in tab
    assert "Link requests waiting for you" in tab and "ann@b.example" in tab and "/approve" in tab and "/admin/links/preview" in tab
    assert '<th scope="col">Person</th>' not in tab  # the people table is the other tab


def test_the_link_requests_tab_shows_errors_and_old_addresses_on_request(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users?tab=links&link_error=Nope").text
    assert "Nope" in html and "No link requests are waiting." in html
    assert "/admin/users?tab=links&amp;aliases=1" in html
    assert "Showing old addresses" in client.get("/admin/users?tab=links&aliases=1").text
    preview = client.get("/admin/links/preview?main_email=nobody@example.com&other_email=nobody2@example.com", follow_redirects=False)
    assert preview.headers["location"].startswith("/admin/users?tab=links&link_error=")


def test_link_actions_return_to_the_link_requests_tab(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    response = client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id}, follow_redirects=False)
    assert response.headers["location"] == "/admin/users?tab=links"


def test_a_link_error_does_not_break_the_list(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    html = client.get("/admin/users?link_error=Nope").text
    assert f'href="/admin/users/{ann.id}"' in html


def test_filtering_keeps_the_stranded_view(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users?inactive=open_tickets").text
    form = re.search(r'<form method="get" action="/admin/users" class="pp-filters".*?</form>', html, re.S).group(0)
    assert 'name="inactive" value="open_tickets"' in form
    assert 'name="inactive"' not in client.get("/admin/users").text.split('class="pp-filters"')[1].split("</form>")[0]


def test_the_empty_state(client, db_session):
    boss(client, db_session)
    assert "No one matches." in rows(client, "?q=zzzz-nobody")


# ---- the new account view -----------------------------------------------------------------------------------------


def test_the_new_account_view_posts_to_the_create_route_with_its_fields(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    html = client.get("/admin/users?new=1").text
    assert "New local account" in html and "All users" in html and "pp-table" not in html
    assert forms(re.search(r'<section class="card pp-new".*?</section>', html, re.S).group(0))["/admin/users"] == [
        "display_name", "email", "level", "password", "role", "team_id"]
    response = client.post(
        "/admin/users",
        data={"email": "new@example.com", "display_name": "New", "password": "long-enough-password", "role": "agent",
              "team_id": team.id, "level": "viewer"},
        follow_redirects=False,
    )
    created = db_session.exec(select(User).where(User.email == "new@example.com")).one()
    assert response.headers["location"] == f"/admin/users/{created.id}"
    assert db_session.get(TeamMembership, (created.id, team.id)).level == MembershipLevel.VIEWER


def test_the_header_button_opens_the_new_account_view(client, db_session):
    boss(client, db_session)
    assert 'href="/admin/users?new=1"' in client.get("/admin/users").text


# ---- the filters --------------------------------------------------------------------------------------------------


def test_the_filters_are_a_plain_get_form(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users").text
    form = re.search(r'<form method="get" action="/admin/users" class="pp-filters".*?</form>', html, re.S).group(0)
    for name in ("q", "role", "team", "status"):
        assert f'name="{name}"' in form
    for status in ("active", "inactive", "local", "break_glass"):
        assert f'value="{status}"' in form


def test_search_matches_name_or_email_and_treats_wildcards_literally(client, db_session):
    boss(client, db_session)
    person(db_session, "ann@example.com", name="Ann Archer")
    person(db_session, "bob@example.com", name="Robert Bell")
    assert "ann@example.com" in rows(client, "?q=archer") and "bob@example.com" not in rows(client, "?q=archer")
    assert "bob@example.com" in rows(client, "?q=BOB@") and "ann@example.com" not in rows(client, "?q=BOB@")
    assert "No one matches." in rows(client, "?q=%25")
    assert "No one matches." in rows(client, "?q=a_n")


def test_role_team_and_status_filters(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    member = person(db_session, "member@example.com")
    add_membership(db_session, member.id, team.id)
    person(db_session, "solo@example.com")
    person(db_session, "end@example.com", role=UserRole.END_USER)
    person(db_session, "gone@example.com", is_active=False)
    person(db_session, "local@example.com", password_hash=hash_password("long-enough-password"))
    person(db_session, "breakglass1@example.com", role=UserRole.SUPER_ADMIN, is_break_glass=True,
           password_hash=hash_password("long-enough-password"))

    def emails(query):
        return set(re.findall(r"[\w.]+@example\.com", rows(client, query)))

    assert emails("?role=end_user") == {"end@example.com"}
    assert emails(f"?team={team.id}") == {"member@example.com"}
    assert emails("?status=inactive") == {"gone@example.com"}
    assert "gone@example.com" not in emails("?status=active") and "solo@example.com" in emails("?status=active")
    assert emails("?status=local") == {"local@example.com"}  # a break-glass account has its own status
    assert emails("?status=break_glass") == {"breakglass1@example.com"}
    assert emails(f"?role=agent&team={team.id}&status=active") == {"member@example.com"}
    # a value that means nothing is ignored, never an error
    assert client.get("/admin/users?role=nope&team=abc&status=zzz").status_code == 200
    assert client.get("/admin/users?team=99999999999").status_code == 200
    assert "solo@example.com" in emails("?role=nope&status=zzz")


def test_the_chosen_filters_stay_selected_and_paging_keeps_them(client, db_session):
    boss(client, db_session)
    for i in range(55):
        person(db_session, f"agent{i:02d}@example.com")
    html = client.get("/admin/users?role=agent&status=active").text
    assert re.search(r'<option value="agent" selected>', html) and re.search(r'<option value="active" selected>', html)
    assert "role=agent" in html and "page=2" in html  # the pagination link carries the filters
    assert "agent54@example.com" in client.get("/admin/users?role=agent&status=active&page=2").text


def test_a_merged_away_account_can_be_unlinked_from_its_own_page(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    html = card(page(client, other.id), "linked")
    action = re.search(r'action="(/admin/links/\d+/unlink)"', html).group(1)
    response = client.post(action, data={"back": str(other.id)}, follow_redirects=False)
    assert response.headers["location"] == f"/admin/users/{other.id}?done=unlinked#sec-linked"
    assert "No linked accounts." in card(page(client, other.id), "linked")


def test_old_addresses_kept_after_an_email_change_are_not_listed_or_counted(client, db_session):
    from app.models import LinkedIdentity

    boss(client, db_session)
    ann = person(db_session, "ann@example.com", role=UserRole.END_USER)
    db_session.add(LinkedIdentity(main_user_id=ann.id, email="old@example.com", method="email_changed", status="active"))
    db_session.commit()
    html = page(client, ann.id)
    assert "old@example.com" not in card(html, "linked") and glance_of(html)["linked"] == "0 linked identities"


def test_the_queue_note_names_both_person_filters_and_keeps_both(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    bob = person(db_session, "bob@example.com")
    body = client.get(f"/tickets?assignee={ann.id}&requester={bob.id}").text
    assert "assigned to ann and raised by bob" in body
    assert f'name="assignee" value="{ann.id}"' in body and f'name="requester" value="{bob.id}"' in body

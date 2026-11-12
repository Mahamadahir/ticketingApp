"""Admin > Users (9I, #213): the filtered list beside the panel of the chosen person (?user=<id>) and the new-account panel
(?new=1). Every form in the panel posts to its existing route with its existing field names; the rules (last super admin,
break-glass role, merged accounts staying inactive, the audit entries) are the routes' and are tested with them."""

import re

import pytest
from sqlmodel import select

from app.auth import hash_password
from app.models import AuditLog, MembershipLevel, TeamMembership, User, UserPermission, UserRole
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


def panel(client, user_id) -> str:
    html = client.get(f"/admin/users?user={user_id}").text
    found = re.search(r'<aside class="card users-panel".*?</aside>', html, re.S)
    assert found, "no panel"
    return found.group(0)


def forms(html) -> dict[str, list[str]]:
    """action -> the field names inside that form, for every POST form in the HTML."""
    out = {}
    for action, body in re.findall(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>', html, re.S):
        out[action] = sorted(set(re.findall(r'name="([^"]+)"', body)))
    return out


def rows(client, query="") -> str:
    html = client.get("/admin/users" + query).text
    return re.search(r"<tbody>.*?</tbody>", html, re.S).group(0)


# ---- the panel, per kind of account -------------------------------------------------------------------

def test_an_entra_agent_panel_has_every_section_and_forms_post_to_the_existing_routes(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    agent = person(db_session, "ann@example.com", entra_object_id="oid-ann")
    add_membership(db_session, agent.id, team.id)
    html = panel(client, agent.id)
    for heading in ("Role", "Teams", "Extra permissions", "Sign-in", "Linked identities", "Deactivate"):
        assert f">{heading}</h3>" in html
    assert "Microsoft account" in html and "oid-ann" in html and "0 signed-in sessions" in html
    base = f"/admin/users/{agent.id}"
    posted = forms(html)
    assert posted[base] == ["role"]
    assert posted[f"{base}/permissions"] == ["permissions"]
    assert posted[f"{base}/memberships/{team.id}/remove"] == []
    assert posted[f"{base}/sign-out"] == [] and posted[f"{base}/clear-oid"] == []
    assert posted[f"{base}/active"] == ["is_active"]
    assert f"{base}/unlock" not in posted  # not a local account
    # the level form of a team and the add form both use the membership route with its two fields
    assert posted[f"{base}/memberships"] == ["level", "team_id"]
    assert html.count(f'action="{base}/memberships"') == 2
    assert 'value="false"' in html and "Deactivate" in html and "Activate</button>" not in html
    assert all(a.startswith("/admin/users") for a in posted)  # no form posts anywhere else


def test_the_forms_work_end_to_end_from_the_panel_names(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    agent = person(db_session, "ann@example.com", entra_object_id="oid-ann")
    base = f"/admin/users/{agent.id}"
    # every action redirects back to the person's panel
    for url, data in (
        (f"{base}/memberships", {"team_id": team.id, "level": "viewer"}),
        (f"{base}/memberships", {"team_id": team.id, "level": "member"}),
        (f"{base}/permissions", {"permissions": MANAGE_REQUEST_TEMPLATES}),
        (base, {"role": "agent"}),
        (f"{base}/sign-out", {}),
        (f"{base}/clear-oid", {}),
        (f"{base}/active", {"is_active": "false"}),
        (f"{base}/active", {"is_active": "true"}),
        (f"{base}/memberships/{team.id}/remove", {}),
    ):
        response = client.post(url, data=data, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == f"/admin/users?user={agent.id}", url
    db_session.expire_all()
    assert db_session.get(User, agent.id).entra_object_id is None
    assert db_session.get(TeamMembership, (agent.id, team.id)) is None
    assert [r.permission for r in db_session.exec(select(UserPermission).where(UserPermission.user_id == agent.id))] == [MANAGE_REQUEST_TEMPLATES]
    actions = {e.action for e in db_session.exec(select(AuditLog).where(AuditLog.entity_id == agent.id))}
    assert {"membership_added", "membership_changed", "membership_removed", "permission_granted", "oid_cleared",
            "signed_out_everywhere", "deactivated", "activated"} <= actions


def test_the_permission_boxes_show_what_is_granted(client, db_session):
    boss(client, db_session)
    agent = person(db_session, "ann@example.com")
    db_session.add(UserPermission(user_id=agent.id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    html = panel(client, agent.id)
    for key in PERMISSIONS:
        assert f'value="{key}"' in html
    assert re.search(rf'value="{MANAGE_REQUEST_TEMPLATES}" checked', html)
    assert html.count(" checked") == 1


def test_a_local_account_panel_has_the_unlock_form_only_with_failed_attempts(client, db_session):
    boss(client, db_session)
    local = person(db_session, "lee@example.com", password_hash=hash_password("long-enough-password"))
    html = panel(client, local.id)
    assert "Local account (password)" in html and "No Entra id" not in html
    assert f"/admin/users/{local.id}/unlock" not in html and "clear-oid" not in html
    local.failed_login_count = 3
    db_session.add(local)
    db_session.commit()
    html = panel(client, local.id)
    assert "3 failed attempts" in html and forms(html)[f"/admin/users/{local.id}/unlock"] == []


def test_a_break_glass_panel_has_no_role_form_and_points_to_the_command(client, db_session):
    boss(client, db_session)
    glass = person(db_session, "breakglass1@example.com", role=UserRole.SUPER_ADMIN, is_break_glass=True,
                   password_hash=hash_password("long-enough-password"))
    html = panel(client, glass.id)
    assert "A break-glass account stays a super admin" in html and "Save role" not in html
    assert f'action="/admin/users/{glass.id}"' not in html  # the role form is the only one on that bare path
    assert "python -m app.breakglass reset breakglass1" in html and "All (super admin)" in html
    assert f"/admin/users/{glass.id}/active" in html  # it can still be deactivated
    assert client.post(f"/admin/users/{glass.id}", data={"role": "agent"}).status_code == 400  # the route's rule stands
    assert "Break-glass" in rows(client)


def test_an_inactive_account_offers_activate_and_no_new_permissions(client, db_session):
    boss(client, db_session)
    gone = person(db_session, "gone@example.com", is_active=False)
    html = panel(client, gone.id)
    assert "(inactive: none can be added)" in html
    assert 'name="is_active" value="true"' in html and ">Activate</button>" in html and "disabled" not in html
    assert "Inactive" in html


def test_a_merged_account_cannot_be_activated_from_the_panel_and_the_route_still_refuses(client, db_session):
    boss(client, db_session)
    main = person(db_session, "ann@a.example", role=UserRole.END_USER)
    other = person(db_session, "ann@b.example", role=UserRole.END_USER)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    html = panel(client, other.id)
    assert f"merged into account {main.id}" in html and "Merged" in html
    assert re.search(r'<button type="submit" class="btn btn-primary" disabled[^>]*>Activate</button>', html)
    assert client.post(f"/admin/users/{other.id}/active", data={"is_active": "true"}).status_code == 400
    assert f"Includes {other.email}" in panel(client, main.id)


def test_an_end_user_panel_has_no_add_to_a_team_form_and_no_permissions(client, db_session):
    boss(client, db_session)
    create_team(db_session, "Service Desk")
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    html = panel(client, end.id)
    assert "Make this person an agent before adding them to a team." in html
    assert f"/admin/users/{end.id}/memberships" not in html and "None (agents only)." in html


def test_a_super_admin_panel_says_all_permissions_and_the_last_super_admin_guard_is_the_routes(client, db_session):
    admin_id = boss(client, db_session)
    html = panel(client, admin_id)
    assert "All (super admin)." in html and f"/admin/users/{admin_id}/permissions" not in html


def test_the_panel_opens_for_someone_outside_the_page_and_says_when_nobody_is_found(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    assert ann.email in client.get(f"/admin/users?status=break_glass&user={ann.id}").text
    assert "That person was not found." in client.get("/admin/users?user=999999").text
    assert client.get("/admin/users?user=99999999999").status_code == 200  # out of range: nobody, not an error
    assert "Choose a person." in client.get("/admin/users").text
    for odd in ("?user=abc", "?user=-1", "?new=yes", "?user=%C2%B2", "?team=%C2%B2"):  # hand-edited: nobody, never an error
        assert client.get("/admin/users" + odd).status_code == 200, odd


def test_a_link_error_is_not_carried_into_the_row_links(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    html = client.get("/admin/users?link_error=Nope").text
    assert "Nope" in html and f'href="/admin/users?user={ann.id}"' in html


def test_filtering_keeps_the_stranded_view_and_old_addresses_and_pending_requests_show_at_the_top(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users?inactive=open_tickets&aliases=1").text
    form = re.search(r'<form method="get" action="/admin/users" class="users-filters".*?</form>', html, re.S).group(0)
    assert 'name="inactive" value="open_tickets"' in form and 'name="aliases" value="1"' in form
    assert 'name="inactive"' not in client.get("/admin/users").text.split('class="users-filters"')[1].split("</form>")[0]
    assert "waiting for you" not in client.get("/admin/users").text


def test_an_end_user_who_still_has_a_membership_can_only_be_removed_from_it(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    end = person(db_session, "end@example.com", role=UserRole.END_USER)
    add_membership(db_session, end.id, team.id)
    posted = forms(panel(client, end.id))
    assert f"/admin/users/{end.id}/memberships/{team.id}/remove" in posted
    assert f"/admin/users/{end.id}/memberships" not in posted


def test_the_list_links_each_person_and_keeps_the_filters(client, db_session):
    boss(client, db_session)
    ann = person(db_session, "ann@example.com")
    html = client.get("/admin/users?role=agent&q=ann").text
    assert f'href="/admin/users?role=agent&amp;q=ann&amp;user={ann.id}"' in html
    assert 'aria-current="true"' not in html
    assert 'aria-current="true"' in client.get(f"/admin/users?user={ann.id}").text


# ---- the new-account panel ---------------------------------------------------------------------------

def test_the_new_account_panel_posts_to_the_create_route_with_its_fields(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    page = client.get("/admin/users?new=1").text
    assert "New local account" in page
    assert forms(re.search(r'<aside class="card users-panel".*?</aside>', page, re.S).group(0))["/admin/users"] == [
        "display_name", "email", "level", "password", "role", "team_id"]
    assert "Choose a person." not in page
    response = client.post(
        "/admin/users",
        data={"email": "new@example.com", "display_name": "New", "password": "long-enough-password", "role": "agent",
              "team_id": team.id, "level": "viewer"},
        follow_redirects=False,
    )
    created = db_session.exec(select(User).where(User.email == "new@example.com")).one()
    assert response.headers["location"] == f"/admin/users?user={created.id}"
    assert db_session.get(TeamMembership, (created.id, team.id)).level == MembershipLevel.VIEWER


def test_the_header_button_opens_the_new_account_panel(client, db_session):
    boss(client, db_session)
    assert 'href="/admin/users?new=1"' in client.get("/admin/users").text


# ---- the filters --------------------------------------------------------------------------------------

def test_the_filters_are_a_plain_get_form(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/users").text
    form = re.search(r'<form method="get" action="/admin/users" class="users-filters".*?</form>', html, re.S).group(0)
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


def test_the_list_shows_role_teams_and_status_chips(client, db_session):
    boss(client, db_session)
    team = create_team(db_session, "Service Desk")
    ann = person(db_session, "ann@example.com", name="Ann Archer")
    add_membership(db_session, ann.id, team.id)
    body = rows(client, "?q=ann")
    assert "Ann Archer" in body and "Agent" in body and "Service Desk (member)" in body and "Active" in body


def test_only_a_super_admin_sees_the_page(client, db_session):
    login_as(client, "plain@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/users?user=1&new=1").status_code == 403


def test_the_old_per_row_forms_are_gone_from_the_list(client, db_session):
    boss(client, db_session)
    person(db_session, "ann@example.com")
    body = rows(client)
    assert "<form" not in body and "<select" not in body

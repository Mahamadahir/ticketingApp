"""Admin overhaul 9K, part 1: Teams, Approval groups, Categories and Category review on the admin patterns (header,
table, panel). Behaviour is the routes' and is tested with them (test_teams, test_approval_groups, test_category_admin)."""

import re

from app.approval_groups import add_member, create_group
from app.models import UserRole
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_category_admin import other_ticket


def boss(client, db_session) -> int:
    user_id = login_as(client, "boss@example.com", "Boss")
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)
    return user_id


def panel(html: str) -> str:
    found = re.search(r'<aside class="card users-panel".*?</aside>', html, re.S)
    assert found, "no panel"
    return found.group(0)


def forms(html: str) -> dict[str, list[str]]:
    return {
        action: sorted(set(re.findall(r'name="([^"]+)"', body)))
        for action, body in re.findall(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>', html, re.S)
    }


def test_teams_page_has_header_table_and_a_panel_per_team(client, db_session):
    boss_id = boss(client, db_session)
    support = create_team(db_session, "Support")
    sub = create_team(db_session, "2nd line")
    sub.parent_team_id = support.id
    db_session.add(sub)
    db_session.commit()
    add_membership(db_session, boss_id, support.id)

    html = client.get("/admin/teams").text
    assert "<h1>Teams</h1>" in html and 'href="/admin/teams?new=1"' in html and "<table" in html
    assert "Sub-team of Support" in html
    assert "users-panel" not in html

    opened = panel(client.get(f"/admin/teams?team={support.id}").text)
    assert forms(opened) == {f"/admin/teams/{support.id}/msp-handoff": ["included"]}
    assert "Members (1)" in opened and "boss@example.com" in opened

    created = panel(client.get("/admin/teams?new=1").text)
    assert forms(created) == {"/admin/teams": ["name", "parent_team_id"]}
    # a value that means nothing is ignored, not an error
    assert client.get("/admin/teams?team=abc").status_code == 200
    assert client.get("/admin/teams?team=99999").status_code == 200


def test_teams_page_empty_state(client, db_session):
    boss(client, db_session)
    assert "No teams yet." in client.get("/admin/teams").text


def test_approval_groups_page_has_header_table_and_a_panel_per_group(client, db_session):
    boss_id = boss(client, db_session)
    group = create_group(db_session, "Managers")
    add_member(db_session, group, "boss@example.com")
    db_session.commit()

    html = client.get("/admin/approval-groups").text
    assert "<h1>Approval groups</h1>" in html and 'href="/admin/approval-groups?new=1"' in html and "<table" in html
    assert "users-panel" not in html

    opened = panel(client.get(f"/admin/approval-groups?group={group.id}").text)
    assert forms(opened) == {
        f"/admin/approval-groups/{group.id}/rename": ["name"],
        f"/admin/approval-groups/{group.id}/notice": ["request_notice"],
        f"/admin/approval-groups/{group.id}/members/{boss_id}/remove": [],
        f"/admin/approval-groups/{group.id}/members": ["email"],
    }
    assert "Members (1)" in opened

    assert forms(panel(client.get("/admin/approval-groups?new=1").text)) == {"/admin/approval-groups": ["name"]}
    assert client.get("/admin/approval-groups?group=abc").status_code == 200


def test_approval_groups_page_empty_state(client, db_session):
    boss(client, db_session)
    assert "No approval groups yet." in client.get("/admin/approval-groups").text


def test_categories_page_has_header_tree_and_panels(client, db_session):
    boss(client, db_session)
    desk = create_team(db_session, "Service Desk")
    hardware = create_category(db_session, desk, "Hardware")
    laptops = create_category(db_session, None, "Laptops", parent=hardware)

    html = client.get("/admin/categories").text
    assert "<h1>Categories</h1>" in html and "<table" in html
    assert 'href="/admin/categories?new=main"' in html and 'href="/admin/categories?new=sub"' in html
    assert "Laptops" in html and "Same as Hardware" in html and "users-panel" not in html

    main_panel = panel(client.get(f"/admin/categories?category={hardware.id}").text)
    assert forms(main_panel) == {f"/admin/categories/{hardware.id}": ["escalation", "is_active", "team_id"]}
    sub_panel = panel(client.get(f"/admin/categories?category={laptops.id}").text)
    assert forms(sub_panel) == {f"/admin/categories/{laptops.id}": ["escalation", "is_active"]}

    assert forms(panel(client.get("/admin/categories?new=main").text)) == {"/admin/categories": ["escalation", "name", "team_id"]}
    assert forms(panel(client.get("/admin/categories?new=sub").text)) == {"/admin/categories": ["escalation", "name", "parent_id"]}
    assert client.get("/admin/categories?category=abc").status_code == 200


def test_the_other_category_panel_has_no_active_box(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/categories").text
    other_id = re.search(r'href="/admin/categories\?category=(\d+)"[^>]*>\s*<span class="person-name">Other', html).group(1)
    opened = panel(client.get(f"/admin/categories?category={other_id}").text)
    assert "is_active" not in opened and "Other is always on" in opened


def test_category_review_is_a_table_with_the_form_inline(client, db_session):
    desk = create_team(db_session, "Service Desk")
    create_category(db_session, desk, "Hardware")
    ticket, _ = other_ticket(client, db_session, desk)
    boss(client, db_session)

    html = client.get("/admin/category-review").text
    assert "<h1>Category review</h1>" in html and "<table" in html
    assert {k: v for k, v in forms(html).items() if k.startswith("/admin")} == {
        f"/admin/category-review/{ticket.number}": ["category_id", "new_category_name", "new_category_parent_id", "new_category_team_id"]
    }


def test_category_review_empty_state(client, db_session):
    boss(client, db_session)
    html = client.get("/admin/category-review").text
    assert "Nothing waiting for review" in html and "<table" not in html

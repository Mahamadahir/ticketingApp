"""Admin overhaul 9K, part 2: Request types (the list and the full-page editor with its sections) and Canned responses
(the list beside a panel). Behaviour is the routes' and is tested with them (test_request_types, test_canned ...)."""

import re

from app.models import RequestType, RequestTypeQuestion
from app.settings_store import APPROVAL_DEFAULT_PASS_PERCENT, get_setting
from sqlmodel import select
from tests.test_canned import admin, make as make_canned
from tests.test_request_types import make


def forms(html: str) -> dict[str, list[str]]:
    return {
        action: sorted(set(re.findall(r'name="([^"]+)"', body)))
        for action, body in re.findall(r'<form method="post" action="(/admin/[^"]+)"[^>]*>(.*?)</form>', html, re.S)
    }


def test_the_request_types_list_has_header_table_and_no_editor(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "New starter")
    html = client.get("/admin/request-types").text
    assert "<h1>Request types</h1>" in html and 'href="/admin/request-types?new=1"' in html
    assert "<table" in html and f'href="/admin/request-types?type={kind.id}"' in html
    assert "<h3 id=\"rt-basics\">" not in html and forms(html) == {}


def test_the_editor_has_every_section_and_each_form_posts_to_its_existing_route(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "New starter")
    html = client.get(f"/admin/request-types?type={kind.id}").text
    assert "<h1>New starter</h1>" in html
    for heading in ("Basics", "Defaults", "Approval"):
        assert f">{heading}</h3>" in html
    assert "Form questions for New starter" in html and "Sub-request templates for New starter" in html
    posted = forms(html)
    main = posted[f"/admin/request-types/{kind.id}"]
    for name in (
        "name", "description", "icon", "is_active", "is_featured", "default_team_id", "default_category_id", "default_impact",
        "default_urgency", "due_days_before", "queue_lead_days", "offers_required_by", "approval_group_id", "pass_percent",
        "subrequests_need_own_approval", "approvers_may_self_approve",
    ):
        assert name in main, name
    assert posted[f"/admin/request-types/{kind.id}/questions"] == ["is_required", "kind", "label", "options"]
    assert f"/admin/request-types/{kind.id}/subrequest-templates" in posted


def test_the_editor_lists_questions_with_their_actions_and_an_empty_state_without(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "New starter")
    assert "No questions." in client.get(f"/admin/request-types?type={kind.id}").text
    client.post(f"/admin/request-types/{kind.id}/questions", data={"label": "Start date", "kind": "date"})
    question = db_session.exec(select(RequestTypeQuestion)).one()
    posted = forms(client.get(f"/admin/request-types?type={kind.id}").text)
    assert posted[f"/admin/request-types/questions/{question.id}"] == ["is_active", "is_required", "label"]
    assert f"/admin/request-types/questions/{question.id}/move" in posted
    assert f"/admin/request-types/questions/{question.id}/delete" in posted


def test_the_new_type_page_is_one_form_and_a_value_that_means_nothing_shows_the_list(client, db_session):
    admin(client, db_session)
    html = client.get("/admin/request-types?new=1").text
    assert "<h1>New request type</h1>" in html and list(forms(html)) == ["/admin/request-types"]
    assert "<h1>Request types</h1>" in client.get("/admin/request-types?type=abc").text
    assert "<h1>Request types</h1>" in client.get("/admin/request-types?type=99999").text


def test_saving_a_question_returns_to_the_editor_of_that_type(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "New starter")
    response = client.post(f"/admin/request-types/{kind.id}/questions", data={"label": "Start date", "kind": "date"}, follow_redirects=False)
    assert response.headers["location"] == f"/admin/request-types?type={kind.id}"


def test_the_canned_page_has_header_table_and_panels(client, db_session):
    admin(client, db_session)
    canned = make_canned(db_session, "Password reset")
    html = client.get("/admin/canned").text
    assert "<h1>Canned responses</h1>" in html and "<table" in html and "users-panel" not in html
    assert 'href="/admin/canned?new=1"' in html and f'href="/admin/canned?response={canned.id}"' in html
    opened = re.search(r'<aside class="card users-panel".*?</aside>', client.get(f"/admin/canned?response={canned.id}").text, re.S).group(0)
    assert forms(opened) == {f"/admin/canned/{canned.id}": ["body", "is_active", "title"]}
    created = re.search(r'<aside class="card users-panel".*?</aside>', client.get("/admin/canned?new=1").text, re.S).group(0)
    assert forms(created) == {"/admin/canned": ["body", "title"]}
    assert client.get("/admin/canned?response=abc").status_code == 200


def test_the_canned_empty_state(client, db_session):
    admin(client, db_session)
    assert "No canned responses yet." in client.get("/admin/canned").text


def test_a_query_value_that_means_nothing_shows_the_list(client, db_session):
    admin(client, db_session)
    make(db_session, "New starter")
    for url in ("/admin/request-types?type=99999999999", "/admin/request-types?type=abc", "/admin/request-types?new=0",
                "/admin/canned?response=99999999999", "/admin/canned?new=0"):
        page = client.get(url)
        assert page.status_code == 200 and "<form" not in page.text.split("</header>")[1].split("<table")[0], url


# ---- Percent to pass only with an approval group (#324) ----------------------------------------------------------------------------------


def test_the_approval_section_wraps_what_needs_a_group_and_the_percent_has_its_own_hook(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "New starter")
    for url in ("/admin/request-types?new=1", f"/admin/request-types?type={kind.id}"):
        html = client.get(url).text
        wrapper = re.search(r'<div class="needs-group">(.*?)</div>', html, re.S).group(1)
        for name in ("pass_percent", "subrequests_need_own_approval", "any_one_approver", "approvers_may_self_approve"):
            assert f'name="{name}"' in wrapper
        assert 'name="approval_group_id"' not in wrapper and 'class="field pass-percent"' in wrapper
        assert " required" not in re.search(r'<input type="number" name="pass_percent"[^>]*>', html).group(0)
    css = open("app/static/styles.css", encoding="utf-8").read()
    assert 'option[value=""]:checked) .needs-group { display: none; }' in css and "any_one_approver" in css


def test_without_a_group_a_blank_percent_keeps_the_stored_value_and_a_new_type_gets_the_default(client, db_session):
    admin(client, db_session)
    created = client.post("/admin/request-types", data={"name": "Draft", "approval_group_id": "", "pass_percent": ""}, follow_redirects=False)
    assert created.status_code == 303
    draft = db_session.exec(select(RequestType).where(RequestType.name == "Draft")).one()
    assert draft.pass_percent == get_setting(db_session, APPROVAL_DEFAULT_PASS_PERCENT) and draft.approval_group_id is None
    kind = make(db_session, "Laptop", pass_percent=70)
    saved = client.post(f"/admin/request-types/{kind.id}", data={"name": "Laptop", "approval_group_id": "", "pass_percent": ""}, follow_redirects=False)
    assert saved.status_code == 303
    db_session.expire_all()
    assert db_session.get(RequestType, kind.id).pass_percent == 70 and db_session.get(RequestType, kind.id).approval_group_id is None


def test_with_a_group_the_percent_is_still_required_and_held_to_1_to_100(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "Laptop", pass_percent=70)
    for bad in ("", "0", "101", "abc"):
        response = client.post(f"/admin/request-types/{kind.id}", data={"name": "Laptop", "approval_group_id": kind.approval_group_id, "pass_percent": bad})
        assert response.status_code in (400, 422), bad
    db_session.expire_all()
    assert db_session.get(RequestType, kind.id).pass_percent == 70

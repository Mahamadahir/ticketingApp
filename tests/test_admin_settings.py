"""The grouped Settings page (9J, issue #214)."""

import re

from sqlmodel import select

from app.models import AuditLog, Setting, UserRole
from app.settings_store import (
    APPROVAL_REMIND_AFTER_DAYS,
    AUTO_CLOSE_AFTER_DAYS,
    CHASE_AFTER_DAYS,
    DEFAULT_TRIAGE_TEAM,
    GROUP_KEYS,
    GROUPS,
    MSP_ADDRESS,
    MSP_BUSINESS_END,
    MSP_BUSINESS_START,
    MSP_HANDOFF_HOUR,
    REGISTRY,
    SESSION_IDLE_MINUTES,
    TELL_REQUESTER_RAISED_FOR,
    get_setting,
    group_definitions,
    search_definitions,
    set_setting,
)
from tests.conftest import create_team, login_as, set_role


def admin(client, db_session):
    user_id = login_as(client, "boss@example.com")
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)


def audits(db_session):
    return [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "setting").order_by(AuditLog.id))]


def post_group(client, group, **values):
    return client.post("/admin/settings/group", data={"group": group, **{f"v_{k}": v for k, v in values.items()}}, follow_redirects=False)


# ---- Groups ------------------------------------------------------------------------------------------------------


def test_every_setting_has_a_group_that_exists():
    assert len(set(GROUP_KEYS)) == len(GROUP_KEYS)
    for definition in REGISTRY.values():
        assert definition.group, f"{definition.key} has no group"
        assert definition.group in GROUP_KEYS, f"{definition.key}: unknown group {definition.group!r}"


def test_every_group_holds_a_setting():
    for key in GROUP_KEYS:
        assert group_definitions(key), f"group {key} is empty"


def test_each_group_page_shows_exactly_its_own_settings(client, db_session):
    admin(client, db_session)
    shown = set()
    for key, title, _text in GROUPS:
        page = client.get(f"/admin/settings?group={key}")
        assert page.status_code == 200
        names = set(re.findall(r'name="v_([a-z0-9_]+)"', page.text))
        assert names == {d.key for d in group_definitions(key)}
        assert f"<h2 id=\"group-title\">{title}</h2>" in page.text
        shown |= names
    assert shown == set(REGISTRY)  # none is missed and none appears twice (the sets above were disjoint per group)


def test_the_group_list_is_plain_links_and_marks_the_current_one(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/settings?group=mail")
    assert 'href="/admin/settings?group=msp"' in page.text
    assert re.findall(r'class="grp on" href="[^"]*" aria-current="page">([^<]+)<', page.text) == ["Mail"]
    assert client.get("/admin/settings").status_code == 200  # no group: the first
    assert "Service levels" in re.findall(r'aria-current="page">([^<]+)<', client.get("/admin/settings?group=nonsense").text)


# ---- Find a setting ----------------------------------------------------------------------------------------------


def test_search_matches_label_and_help_text_across_groups(client, db_session):
    admin(client, db_session)
    in_label = client.get("/admin/settings?q=chase").text
    assert "Chase: send the chase email after" in in_label
    assert "Settings matching" in in_label
    # a word only in the help text of one setting
    needle = next(w for w in REGISTRY[DEFAULT_TRIAGE_TEAM].description.lower().split() if len(w) > 6 and w not in REGISTRY[DEFAULT_TRIAGE_TEAM].label.lower())
    hits = {d.key for d in search_definitions(needle)}
    assert DEFAULT_TRIAGE_TEAM in hits
    assert "Default triage team" in client.get(f"/admin/settings?q={needle}").text
    # hits from different groups, each linking to its group
    page = client.get("/admin/settings?q=minutes").text
    assert "group=sign-in-and-sessions#setting-session_idle_minutes" in page and "group=teams#setting-" in page
    assert "No setting matches that." in client.get("/admin/settings?q=zzzzqqqq").text


def test_search_is_case_insensitive_and_needs_every_word(client, db_session):
    admin(client, db_session)
    assert {d.key for d in search_definitions("AUTO-CLOSE")} >= {AUTO_CLOSE_AFTER_DAYS}
    assert search_definitions("auto-close zzzzqqqq") == []
    assert "Auto-close: close a resolved ticket after" in client.get("/admin/settings?q=Auto-Close").text


# ---- Default and the Changed chip --------------------------------------------------------------------------------


def chips(page_text):
    return re.findall(r'<label for="s-([a-z0-9_]+)"[^>]*>[^<]*<span class="chip t-amber">Changed</span>', page_text)


def test_each_row_shows_its_default_unit_and_help(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/settings?group=resolving-and-closing").text
    assert f"Default: {REGISTRY[AUTO_CLOSE_AFTER_DAYS].default}" in page
    assert REGISTRY[AUTO_CLOSE_AFTER_DAYS].description in page
    assert '<span class="setting-unit">days</span>' in page
    assert "Default: Off" in page  # a flag's default reads On or Off
    assert "Changed" not in page  # nothing has been changed yet


def test_the_changed_chip_follows_the_stored_value_against_the_default(client, db_session):
    admin(client, db_session)
    url = "/admin/settings?group=resolving-and-closing"
    assert chips(client.get(url).text) == []
    set_setting(db_session, AUTO_CLOSE_AFTER_DAYS, "9")
    db_session.commit()
    assert chips(client.get(url).text) == [AUTO_CLOSE_AFTER_DAYS]
    set_setting(db_session, AUTO_CLOSE_AFTER_DAYS, REGISTRY[AUTO_CLOSE_AFTER_DAYS].default)  # a stored row equal to the default is not a change
    db_session.commit()
    assert chips(client.get(url).text) == []
    set_setting(db_session, "auto_close_enabled", "on")  # default off
    db_session.commit()
    assert chips(client.get(url).text) == ["auto_close_enabled"]
    assert "Changed" in client.get("/admin/settings?q=auto-close").text  # the chip shows in search results too


# ---- Saving a group ----------------------------------------------------------------------------------------------


def test_saving_a_group_saves_only_that_groups_settings_and_audits_each_change(client, db_session):
    admin(client, db_session)
    set_setting(db_session, SESSION_IDLE_MINUTES, "100")  # another group, must stay as it is
    db_session.commit()
    response = post_group(client, "resolving-and-closing", auto_close_after_days="9", chase_after_days="4", auto_close_enabled="on")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/settings?group=resolving-and-closing&saved=2"  # enabled=on is the default
    db_session.expire_all()
    assert get_setting(db_session, AUTO_CLOSE_AFTER_DAYS) == "9" and get_setting(db_session, CHASE_AFTER_DAYS) == "4"
    assert get_setting(db_session, SESSION_IDLE_MINUTES) == "100"
    assert len(audits(db_session)) == 2  # one per changed setting, the unchanged ones are not logged
    assert any(AUTO_CLOSE_AFTER_DAYS in a and "'7' -> '9'" in a for a in audits(db_session))


def test_a_field_from_another_group_in_the_post_is_ignored(client, db_session):
    admin(client, db_session)
    response = post_group(client, "resolving-and-closing", **{SESSION_IDLE_MINUTES: "5", "auto_close_after_days": "8"})
    assert response.status_code == 303
    db_session.expire_all()
    assert get_setting(db_session, SESSION_IDLE_MINUTES) == REGISTRY[SESSION_IDLE_MINUTES].default
    assert get_setting(db_session, AUTO_CLOSE_AFTER_DAYS) == "8"
    assert post_group(client, "no-such-group").status_code == 404


def test_saving_the_same_values_again_changes_and_audits_nothing(client, db_session):
    admin(client, db_session)
    post_group(client, "mail", inbound_max_cc="12")
    before = audits(db_session)
    response = post_group(client, "mail", inbound_max_cc="12")
    assert response.headers["location"].endswith("saved=0")
    assert audits(db_session) == before
    assert "Nothing changed." in client.get(response.headers["location"]).text


def test_a_refused_value_saves_nothing_and_shows_under_its_field_with_the_rest_kept(client, db_session):
    admin(client, db_session)
    response = post_group(
        client, "resolving-and-closing", auto_close_after_days="abc", chase_after_days="5", auto_close_enabled="on"
    )
    assert response.status_code == 400
    page = response.text
    # the message sits under its own field; the other typed values are kept
    assert re.search(r'name="v_auto_close_after_days" value="abc"[^>]*/>.*?id="err-auto_close_after_days"[^>]*>Enter a whole number from 1 to 1000<', page, re.S)
    assert 'name="v_chase_after_days" value="5"' in page
    assert page.count('class="field-error" id="err-') == 1
    assert "Nothing was saved" in page
    db_session.expire_all()
    assert get_setting(db_session, CHASE_AFTER_DAYS) == REGISTRY[CHASE_AFTER_DAYS].default  # nothing saved, not even the valid one
    assert audits(db_session) == []


def test_every_refused_field_gets_its_own_message(client, db_session):
    admin(client, db_session)
    page = post_group(client, "msp", msp_address="not an address", msp_handoff_hour="24", msp_business_morning_hour="9").text
    assert 'id="err-msp_address"' in page and "Enter one email address" in page
    assert 'id="err-msp_handoff_hour"' in page and "Enter a whole number from 0 to 23" in page
    assert 'id="err-msp_business_morning_hour"' not in page
    assert 'value="not an address"' in page and 'value="24"' in page


def test_business_hours_are_checked_together_in_one_save(client, db_session):
    admin(client, db_session)
    bad = post_group(client, "msp", msp_business_start="10:00", msp_business_end="09:00")
    assert bad.status_code == 400 and "Business hours must end after they start" in bad.text
    ok = post_group(client, "msp", msp_business_start="07:00", msp_business_end="08:00")  # start moves earlier than the stored end too
    assert ok.status_code == 303
    db_session.expire_all()
    assert (get_setting(db_session, MSP_BUSINESS_START), get_setting(db_session, MSP_BUSINESS_END)) == ("07:00", "08:00")


def test_the_single_setting_post_still_works_and_lands_on_its_group(client, db_session):
    admin(client, db_session)
    team = create_team(db_session, "Service Desk")
    response = client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": str(team.id)}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/settings?group=other"
    assert client.post("/admin/settings", data={"key": APPROVAL_REMIND_AFTER_DAYS, "value": "0"}).status_code == 400


def test_a_team_and_a_flag_save_through_the_group_form(client, db_session):
    admin(client, db_session)
    team = create_team(db_session, "Service Desk")
    assert post_group(client, "other", **{DEFAULT_TRIAGE_TEAM: str(team.id)}).status_code == 303
    assert f'<option value="{team.id}" selected>' in client.get("/admin/settings?group=other").text
    assert post_group(client, "notifications", **{TELL_REQUESTER_RAISED_FOR: "off"}).status_code == 303
    assert post_group(client, "notifications", **{TELL_REQUESTER_RAISED_FOR: "maybe"}).status_code == 400


def test_only_a_super_admin_may_use_the_settings_pages(client, db_session):
    login_as(client, "agent@example.com")
    for response in (
        client.get("/admin/settings?group=mail"),
        client.get("/admin/settings/reset?group=mail"),
        post_group(client, "mail", inbound_max_cc="12"),
        client.post("/admin/settings/reset", data={"group": "mail", "confirm": "yes"}),
    ):
        assert response.status_code == 403


# ---- Reset this group --------------------------------------------------------------------------------------------


def test_reset_shows_a_confirm_step_and_changes_nothing_until_it_is_posted(client, db_session):
    admin(client, db_session)
    set_setting(db_session, AUTO_CLOSE_AFTER_DAYS, "9")
    db_session.commit()
    assert "/admin/settings/reset?group=resolving-and-closing" in client.get("/admin/settings?group=resolving-and-closing").text
    confirm = client.get("/admin/settings/reset?group=resolving-and-closing")
    assert confirm.status_code == 200 and "Reset Resolving and closing to defaults?" in confirm.text
    assert "Auto-close: close a resolved ticket after" in confirm.text and "<td>9</td><td>7</td>" in confirm.text
    db_session.expire_all()
    assert get_setting(db_session, AUTO_CLOSE_AFTER_DAYS) == "9" and audits(db_session) == []
    # a POST without the confirmation goes back to the confirm step and changes nothing
    refused = client.post("/admin/settings/reset", data={"group": "resolving-and-closing"}, follow_redirects=False)
    assert refused.status_code == 303 and refused.headers["location"] == "/admin/settings/reset?group=resolving-and-closing"
    db_session.expire_all()
    assert get_setting(db_session, AUTO_CLOSE_AFTER_DAYS) == "9" and audits(db_session) == []


def test_reset_puts_each_changed_setting_back_with_one_audit_entry_each_and_leaves_other_groups(client, db_session):
    admin(client, db_session)
    for key, value in ((AUTO_CLOSE_AFTER_DAYS, "9"), (CHASE_AFTER_DAYS, "5"), (SESSION_IDLE_MINUTES, "100")):
        set_setting(db_session, key, value)
    db_session.commit()
    response = client.post("/admin/settings/reset", data={"group": "resolving-and-closing", "confirm": "yes"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("saved=2")
    db_session.expire_all()
    assert get_setting(db_session, AUTO_CLOSE_AFTER_DAYS) == "7" and get_setting(db_session, CHASE_AFTER_DAYS) == "3"
    assert get_setting(db_session, SESSION_IDLE_MINUTES) == "100"
    entries = audits(db_session)
    assert len(entries) == 2 and all("(reset to default)" in e for e in entries)
    assert any(AUTO_CLOSE_AFTER_DAYS in e and "'9' -> '7'" in e for e in entries)
    # a second reset has nothing to do
    client.post("/admin/settings/reset", data={"group": "resolving-and-closing", "confirm": "yes"})
    assert len(audits(db_session)) == 2


def test_reset_leaves_settings_that_have_no_default_alone(client, db_session):
    admin(client, db_session)
    assert REGISTRY[MSP_ADDRESS].default == "" and not REGISTRY[MSP_ADDRESS].has_default
    set_setting(db_session, MSP_ADDRESS, "help@msp.example")
    set_setting(db_session, MSP_HANDOFF_HOUR, "20")
    db_session.commit()
    confirm = client.get("/admin/settings/reset?group=msp").text
    assert "These have no default, so they stay as they are" in confirm and "MSP: the address tickets are sent to" in confirm
    client.post("/admin/settings/reset", data={"group": "msp", "confirm": "yes"})
    db_session.expire_all()
    assert get_setting(db_session, MSP_ADDRESS) == "help@msp.example"
    assert get_setting(db_session, MSP_HANDOFF_HOUR) == "18"
    assert client.post("/admin/settings/reset", data={"group": "no-such-group", "confirm": "yes"}).status_code == 404


def test_a_group_with_only_unchanged_settings_has_nothing_to_reset(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/settings/reset?group=mail").text
    assert "Nothing to reset." in page and "Reset 0" not in page
    assert db_session.get(Setting, MSP_ADDRESS) is None

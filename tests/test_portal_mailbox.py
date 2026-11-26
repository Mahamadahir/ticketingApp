"""The portal's "forward my mail" form (#309): the request type's mailbox action, the people picker, the times, the keep-a-copy
warning, the request that is raised, and the request page. Exchange is the FakeExecutor; Graph is replaced where a directory
answer is needed."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app import graph_client as graph_module
from app import mailbox_changes
from app.config import settings
from app.mailbox_executor import FakeExecutor, set_executor
from app.models import (
    MailboxChange, RequestType, Setting, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole,
)
from app.request_types import RequestTypeError, TypeDefaults, create_request_type, update_request_type
from app.settings_store import DEFAULT_TIME_ZONE, MAILBOX_CHANGE_MAX_DAYS, MAILBOX_TARGET_BLOCKLIST, set_setting
from tests.conftest import create_category, create_team, login_as, set_role
from app.approval_groups import create_group
from tests.test_request_types import admin, group_of

OWNER = "alex@example.com"
OWNER_OID = "oid-alex"
FRIEND_OID = "oid-jane"


@pytest.fixture(autouse=True)
def fake_exchange(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()
    set_executor(FakeExecutor())
    yield
    set_executor(None)


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def world(db_session):
    """A team and category, and a forwarding request type that triages itself (no approval group)."""
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    request_type = create_request_type(
        db_session, "Forward my mail", "Send my mail to a colleague", None, 51,
        defaults=TypeDefaults(team.id, category.id, "low", "low"), mailbox_action="forwarding",
    )
    db_session.commit()
    db_session.refresh(request_type)
    return SimpleNamespace(team=team, category=category, type=request_type)


def person(db_session, email, name, **fields):
    user = User(email=email, display_name=name, **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def signed_in(client, db_session, email=OWNER, name="Alex Owner", **fields):
    user_id = login_as(client, email, name)
    user = db_session.get(User, user_id)
    for key, value in fields.items():
        setattr(user, key, value)
    db_session.add(user)
    db_session.commit()
    return user_id


def raw_user(oid, name, mail="x@example.com", enabled=True, kind="Member", job="", dept=""):
    return {"id": oid, "displayName": name, "mail": mail, "jobTitle": job, "department": dept, "accountEnabled": enabled, "userType": kind}


@pytest.fixture
def directory(monkeypatch):
    """Graph on, with a directory the test fills in: {object id: raw Graph user}. search_users matches the typed text."""
    entries: dict[str, dict] = {}
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "c")
    monkeypatch.setattr(settings, "graph_client_secret", "s")
    monkeypatch.setattr(
        graph_module.graph_client, "search_users",
        lambda text, limit=25: [r for r in entries.values() if text.lower() in (r["displayName"] + (r["mail"] or "")).lower()],
    )
    monkeypatch.setattr(graph_module.graph_client, "get_user", lambda oid: entries.get(oid))
    return entries


def later(hours):
    """A local date and quarter-hour time `hours` from now in UTC (the zone every test user is in unless a test says otherwise)."""
    moment = (now() + timedelta(hours=hours)).replace(second=0, microsecond=0)
    moment += timedelta(minutes=(15 - moment.minute % 15) % 15)
    return f"{moment:%Y-%m-%d}", f"{moment:%H:%M}"


def good_form(world, **extra):
    end_date, end_time = later(30)
    data = {
        "request_type_id": world.type.id, "target_object_id": FRIEND_OID, "target_name": "Jane Friend", "keep_copy": "on",
        "start_mode": "now", "end_date": end_date, "end_time": end_time,
    }
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def post(client, world, **extra):
    return client.post("/portal/mailbox-change", data=good_form(world, **extra), follow_redirects=False)


def only_change(db_session):
    db_session.expire_all()
    return db_session.exec(select(MailboxChange)).one()


def no_change(db_session):
    db_session.expire_all()
    return db_session.exec(select(MailboxChange)).first() is None and db_session.exec(select(Ticket)).first() is None


@pytest.fixture
def friend(directory):
    directory[FRIEND_OID] = raw_user(FRIEND_OID, "Jane Friend", "jane@example.com", job="Analyst", dept="Finance")
    directory[OWNER_OID] = raw_user(OWNER_OID, "Alex Owner", OWNER)
    return directory[FRIEND_OID]


# ---- The request type ---------------------------------------------------------------------------------------------


def test_a_type_with_an_action_may_have_an_approval_group_for_someone_elses_mailbox(db_session, world):
    """#311: the group is the IT approvers for requests about someone else's mailbox; the person's own mailbox needs no approval."""
    from app.request_types import offers_someone_elses

    assert not offers_someone_elses(world.type)  # no group: only "Mine" is offered
    group = group_of(db_session, "Managers")
    update_request_type(db_session, world.type, "Forward my mail", "", group.id, 51, True)
    assert offers_someone_elses(world.type)
    other = create_request_type(
        db_session, "Other", "", group.id, 51, defaults=TypeDefaults(world.team.id, world.category.id, "low", "low"), mailbox_action="forwarding",
    )
    assert other.approval_group_id == group.id
    with pytest.raises(RequestTypeError, match="at least one member"):  # an active type with a group still needs someone to ask
        create_request_type(
            db_session, "Empty", "", create_group(db_session, "Nobody").id, 51,
            defaults=TypeDefaults(world.team.id, world.category.id, "low", "low"), mailbox_action="forwarding",
        )


def test_a_type_with_an_action_needs_all_four_defaults(db_session, world):
    # (a blank impact or urgency is filled with Low, #325; the team and category are the admin's choice)
    for partial in (TypeDefaults(None, world.category.id, "low", "low"), TypeDefaults(world.team.id, None, "low", "low")):
        with pytest.raises(RequestTypeError, match="triage itself"):
            update_request_type(db_session, world.type, "Forward my mail", "", None, 51, True, defaults=partial)
    with pytest.raises(RequestTypeError, match="triage itself"):
        create_request_type(db_session, "Other", "", None, 51, mailbox_action="forwarding")


def test_switching_an_ordinary_type_to_an_action_checks_its_stored_group_and_defaults(db_session):
    group = group_of(db_session, "Managers")
    request_type = create_request_type(db_session, "Plain", "", group.id, 51)
    with pytest.raises(RequestTypeError, match="triage itself"):
        update_request_type(db_session, request_type, "Plain", "", group.id, 51, True, mailbox_action="send_as", set_mailbox_action=True)
    with pytest.raises(RequestTypeError, match="Choose a mailbox action"):
        update_request_type(db_session, request_type, "Plain", "", None, 51, True, mailbox_action="nonsense", set_mailbox_action=True)


def test_an_active_action_type_needs_no_approval_group(db_session, world):
    assert world.type.is_active and world.type.approval_group_id is None and world.type.mailbox_action == "forwarding"


def test_the_admin_editor_sets_the_action_and_may_save_a_group(client, db_session, world):
    admin(client, db_session)
    group = group_of(db_session, "Managers")
    base = {
        "name": "Forward my mail", "pass_percent": 51, "is_active": "on", "mailbox_action": "forwarding",
        "default_team_id": world.team.id, "default_category_id": world.category.id, "default_impact": "low", "default_urgency": "low",
    }
    assert client.post(f"/admin/request-types/{world.type.id}", data={**base, "approval_group_id": group.id}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(RequestType, world.type.id).approval_group_id == group.id
    assert client.post(f"/admin/request-types/{world.type.id}", data={**base, "mailbox_action": "send_as"}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(RequestType, world.type.id).mailbox_action == "send_as"
    # an ordinary type that is on needs an approval group, so it is switched off to save it without one
    assert client.post(f"/admin/request-types/{world.type.id}", data={**base, "mailbox_action": ""}).status_code == 400
    off = {k: v for k, v in base.items() if k != "is_active"}
    assert client.post(f"/admin/request-types/{world.type.id}", data={**off, "mailbox_action": ""}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(RequestType, world.type.id).mailbox_action is None


def test_only_a_super_admin_changes_the_action(client, db_session, world):
    agent_id = login_as(client, "desk@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    from app.permissions import MANAGE_REQUEST_TEMPLATES
    from app.models import UserPermission  # noqa: F401  (the grant model)
    user = db_session.get(User, agent_id)
    db_session.add(UserPermission(user_id=agent_id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    base = {
        "name": "Forward my mail", "pass_percent": 51, "is_active": "on", "default_team_id": world.team.id,
        "default_category_id": world.category.id, "default_impact": "low", "default_urgency": "low",
    }
    assert client.post(f"/admin/request-types/{world.type.id}", data={**base, "mailbox_action": ""}).status_code == 403
    # leaving it as it is (what the disabled editor posts back) is allowed
    assert client.post(f"/admin/request-types/{world.type.id}", data={**base, "mailbox_action": "forwarding"}, follow_redirects=False).status_code == 303
    assert user is not None


# ---- Offered, or not -----------------------------------------------------------------------------------------------


def test_the_kind_is_a_card_and_a_tile_when_the_feature_is_on(client, db_session, world):
    signed_in(client, db_session)
    assert "Forward my mail" in client.get("/portal/new").text
    assert "Forward my mail" in client.get("/portal").text


def test_the_kind_is_not_offered_when_the_feature_is_off_and_the_form_says_so(client, db_session, world):
    signed_in(client, db_session)
    set_executor(None)  # MAILBOX_EXECUTOR is unset in tests
    assert "Forward my mail" not in client.get("/portal/new").text
    assert "Forward my mail" not in client.get("/portal").text
    page = client.get(f"/portal/new?request_type_id={world.type.id}")
    assert "not available right now" in page.text and "mb-form" not in page.text


def test_a_post_while_the_feature_is_off_shows_not_available_and_writes_nothing(client, db_session, world, friend):
    signed_in(client, db_session)
    set_executor(None)
    response = post(client, world)
    assert response.status_code == 400 and "not available right now" in response.text
    assert no_change(db_session)


def test_an_action_without_a_portal_form_is_not_offered(client, db_session, world, monkeypatch):
    signed_in(client, db_session)
    monkeypatch.setattr("app.request_types.PORTAL_MAILBOX_ACTIONS", ("forwarding",))  # as before #310
    world.type.mailbox_action = "send_as"
    db_session.add(world.type)
    db_session.commit()
    assert "Forward my mail" not in client.get("/portal/new").text


def test_the_ordinary_raise_route_refuses_an_action_type(client, db_session, world):
    signed_in(client, db_session)
    response = client.post("/portal/new", data={"subject": "x", "description": "y", "request_type_id": world.type.id})
    assert response.status_code == 400 and "own form" in response.text
    assert db_session.exec(select(Ticket)).first() is None


def test_the_form_shows_the_mailbox_zone_and_never_a_mailbox_field(client, db_session, world):
    signed_in(client, db_session, time_zone="Europe/London")
    page = client.get(f"/portal/new?request_type_id={world.type.id}").text
    assert OWNER in page and "Europe/London" in page and "15-minute steps" in page
    assert 'name="mailbox' not in page and 'name="owner' not in page and 'type="email"' not in page
    assert 'name="keep_copy" id="mb-keep-copy" checked' in page
    assert 'value="09:15"' in page and 'value="09:10"' not in page


# ---- The people picker ---------------------------------------------------------------------------------------------


def test_the_picker_needs_a_signed_in_person(client, db_session, world):
    response = client.get("/portal/people?q=jane", follow_redirects=False)
    assert response.status_code in (302, 303, 307, 401, 403) and "oid" not in response.text


def test_the_picker_lists_only_enabled_members_with_mail_and_shows_name_title_and_department(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    directory["g"] = raw_user("g", "Jane Guest", "jane.guest@other.com", kind="Guest")
    directory["d"] = raw_user("d", "Jane Disabled", "jane.d@example.com", enabled=False)
    directory["n"] = raw_user("n", "Jane Nomail", None)
    directory["b"] = raw_user("b", "Jane Blocked", "break@example.com")
    set_setting(db_session, MAILBOX_TARGET_BLOCKLIST, "break@example.com")
    db_session.commit()
    text = client.get("/portal/people?q=jane").text
    assert "Jane Friend" in text and "Analyst" in text and "Finance" in text and 'data-oid="oid-jane"' in text
    for hidden in ("Guest", "Disabled", "Nomail", "Blocked"):
        assert hidden not in text


def test_the_picker_never_lists_the_person_themselves(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    assert "Alex Owner" not in client.get("/portal/people?q=alex").text


def test_the_picker_wants_two_characters_and_gives_at_most_ten(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    assert "at least 2" in client.get("/portal/people?q=j").text and 'data-oid' not in client.get("/portal/people?q=j").text
    for index in range(15):
        directory[f"p{index}"] = raw_user(f"p{index}", f"Pat {index}", f"pat{index}@example.com")
    assert client.get("/portal/people?q=pat").text.count("data-oid=") == 10


def test_the_picker_is_rate_limited(client, db_session, world, friend, monkeypatch):
    signed_in(client, db_session)
    monkeypatch.setattr(settings, "rate_limit_people_search_per_minute", 3)
    codes = [client.get("/portal/people?q=jane").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_a_directory_failure_is_said_plainly(client, db_session, world, friend, monkeypatch):
    signed_in(client, db_session)

    def broken(text, limit=25):
        raise RuntimeError("graph down")

    monkeypatch.setattr(graph_module.graph_client, "search_users", broken)
    response = client.get("/portal/people?q=jane")
    assert response.status_code == 200 and "could not be searched" in response.text and "graph down" not in response.text


def test_with_graph_off_a_fake_directory_of_local_users_stands_in(client, db_session, world):
    signed_in(client, db_session)
    person(db_session, "jane@example.com", "Jane Friend")
    person(db_session, "off@example.com", "Jane Off", is_active=False)
    person(db_session, "bg@example.com", "Jane Break", is_break_glass=True)
    text = client.get("/portal/people?q=jane").text
    assert "Jane Friend" in text and "Jane Off" not in text and "Jane Break" not in text


def test_the_graph_search_uses_search_with_the_eventual_consistency_header(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"value": [{"id": "1"}]})

    monkeypatch.setattr(graph_module.httpx, "get", fake_get)
    monkeypatch.setattr(graph_module.GraphClient, "_get_access_token", lambda self: "tok")
    rows = graph_module.GraphClient().search_users('Ja"ne')
    assert rows == [{"id": "1"}] and seen["url"].endswith("/users")
    assert seen["headers"]["ConsistencyLevel"] == "eventual" and seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["params"]["$search"] == '"displayName:Ja ne" OR "mail:Ja ne"'  # a quote cannot break out of the search term
    assert seen["params"]["$filter"] == "accountEnabled eq true"  # userType is checked in code: `eq 'Member'` hides a null userType


# ---- Submitting: who ------------------------------------------------------------------------------------------------


def test_the_mailbox_is_always_the_signed_in_person_and_a_posted_one_is_ignored(client, db_session, world, friend):
    signed_in(client, db_session, entra_object_id=OWNER_OID)
    response = post(client, world, mailbox="boss@example.com", owner_address="boss@example.com", owner_object_id="oid-boss")
    assert response.status_code == 303
    change = only_change(db_session)
    assert change.owner_address == OWNER and change.owner_object_id == OWNER_OID
    assert change.target_address == "jane@example.com" and change.target_object_id == FRIEND_OID
    assert change.created_by == OWNER and change.kind == "forwarding"


@pytest.mark.parametrize("oid, entry, message", [
    ("oid-guest", raw_user("oid-guest", "G", "g@other.com", kind="Guest"), "inside the organisation"),
    ("oid-off", raw_user("oid-off", "D", "d@example.com", enabled=False), "switched off"),
    ("oid-nomail", raw_user("oid-nomail", "N", None), "no mailbox"),
    ("oid-blocked", raw_user("oid-blocked", "B", "break@example.com"), "cannot be sent to that address"),
    (OWNER_OID, None, "yourself"),
    ("oid-missing", None, "could not be found"),
    ("", None, "Choose who"),
])
def test_a_doctored_object_id_is_refused_on_submit(client, db_session, world, friend, directory, oid, entry, message):
    signed_in(client, db_session, entra_object_id=OWNER_OID)
    if entry is not None:
        directory[oid] = entry
    set_setting(db_session, MAILBOX_TARGET_BLOCKLIST, "break@example.com")
    db_session.commit()
    response = post(client, world, target_object_id=oid)
    assert response.status_code == 400 and message in response.text
    assert no_change(db_session)


def test_the_requester_cannot_be_chosen_even_with_another_object_id_for_their_address(client, db_session, world, friend, directory):
    signed_in(client, db_session)  # no Entra id on the account: matched by address
    directory["oid-alias"] = raw_user("oid-alias", "Alex Again", OWNER)
    assert "yourself" in post(client, world, target_object_id="oid-alias").text
    assert no_change(db_session)


def test_a_sign_in_name_is_not_accepted_in_place_of_an_object_id(client, db_session, world, friend, monkeypatch):
    signed_in(client, db_session)
    monkeypatch.setattr(graph_module.graph_client, "get_user", lambda oid: friend)  # Graph also reads a UPN
    assert "could not be found" in post(client, world, target_object_id="jane@example.com").text
    assert no_change(db_session)


def test_a_directory_failure_on_submit_writes_nothing(client, db_session, world, friend, monkeypatch):
    signed_in(client, db_session)

    def broken(oid):
        raise RuntimeError("down")

    monkeypatch.setattr(graph_module.graph_client, "get_user", broken)
    response = post(client, world)
    assert response.status_code == 400 and "could not check that person" in response.text
    assert no_change(db_session)


def test_after_a_refusal_the_form_comes_back_filled_in_but_forgets_a_refused_person(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    directory["oid-off"] = raw_user("oid-off", "Dee Off", "dee@example.com", enabled=False)
    page = post(client, world, target_object_id="oid-off", target_name="Dee Off", keep_copy=None).text
    assert "switched off" in page and 'name="target_object_id" id="mb-oid" value=""' in page
    keep_copy_input = page[page.index('name="keep_copy"'):].split("/>")[0]
    assert "checked" not in keep_copy_input  # the copy was off in what was sent, and the form says so again


# ---- Submitting: times ----------------------------------------------------------------------------------------------


def test_now_is_the_default_start_and_the_change_is_made_for_the_signed_in_person(client, db_session, world, friend):
    signed_in(client, db_session)
    before = now()
    response = post(client, world, start_mode=None)
    assert response.status_code == 303
    change = only_change(db_session)
    assert before - timedelta(seconds=5) <= change.starts_at.replace(tzinfo=timezone.utc) <= now() + timedelta(seconds=5)


def test_a_later_start_and_an_end_are_read_in_the_persons_zone(client, db_session, world, friend):
    signed_in(client, db_session, time_zone="America/New_York")
    start = (now() + timedelta(days=3)).astimezone(ZoneInfo("America/New_York"))
    end = start + timedelta(days=2)
    response = post(
        client, world, start_mode="later", start_date=f"{start:%Y-%m-%d}", start_time="09:15", end_date=f"{end:%Y-%m-%d}", end_time="17:45",
    )
    assert response.status_code == 303
    change = only_change(db_session)
    want_start = datetime(start.year, start.month, start.day, 9, 15, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    want_end = datetime(end.year, end.month, end.day, 17, 45, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert change.starts_at.replace(tzinfo=timezone.utc) == want_start and change.ends_at.replace(tzinfo=timezone.utc) == want_end


@pytest.mark.parametrize("bad", ["09:10", "09:07", "9:15", "25:00", "09:15:30", ""])
def test_only_quarter_hours_are_accepted(client, db_session, world, friend, bad):
    signed_in(client, db_session)
    end_date, _ = later(30)
    response = post(client, world, end_date=end_date, end_time=bad)
    assert response.status_code == 400 and "15-minute steps" in response.text
    assert no_change(db_session)


def test_a_later_start_must_have_a_quarter_hour_time_too(client, db_session, world, friend):
    signed_in(client, db_session)
    date, _ = later(5)
    response = post(client, world, start_mode="later", start_date=date, start_time="09:20")
    assert response.status_code == 400 and "start time in 15-minute steps" in response.text


def test_the_end_is_required(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, end_date="")
    assert response.status_code == 400 and "end date" in response.text
    assert no_change(db_session)


def test_an_end_that_is_not_after_the_start_is_refused(client, db_session, world, friend):
    signed_in(client, db_session)
    start_date, _ = later(48)
    response = post(client, world, start_mode="later", start_date=start_date, start_time="12:00", end_date=start_date, end_time="12:00")
    assert response.status_code == 400 and "after the start" in response.text
    response = post(client, world, start_mode="later", start_date=start_date, start_time="12:00", end_date=start_date, end_time="11:45")
    assert response.status_code == 400 and "after the start" in response.text


def test_an_end_in_the_past_is_refused_for_a_start_of_now(client, db_session, world, friend):
    signed_in(client, db_session)
    yesterday = f"{now() - timedelta(days=2):%Y-%m-%d}"
    assert "after the start" in post(client, world, end_date=yesterday, end_time="09:00").text


def test_a_start_in_the_past_is_refused_unless_it_is_now(client, db_session, world, friend):
    signed_in(client, db_session)
    past = f"{now() - timedelta(days=2):%Y-%m-%d}"
    end_date, end_time = later(30)
    response = post(client, world, start_mode="later", start_date=past, start_time="09:00", end_date=end_date, end_time=end_time)
    assert response.status_code == 400 and "already passed" in response.text
    assert no_change(db_session)


def test_a_later_start_needs_a_date(client, db_session, world, friend):
    signed_in(client, db_session)
    assert "start date" in post(client, world, start_mode="later", start_date="").text


def test_a_span_over_the_setting_is_refused_and_exactly_the_setting_is_not(client, db_session, world, friend):
    signed_in(client, db_session)
    set_setting(db_session, MAILBOX_CHANGE_MAX_DAYS, "10")
    db_session.commit()
    tomorrow = (now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    start = f"{tomorrow:%Y-%m-%d}"
    exactly = f"{tomorrow + timedelta(days=10):%Y-%m-%d}"
    over = f"{tomorrow + timedelta(days=11):%Y-%m-%d}"
    common = {"start_mode": "later", "start_date": start, "start_time": "09:00", "end_time": "09:00"}
    refused = post(client, world, end_date=over, **common)
    assert refused.status_code == 400 and "at most 10 days" in refused.text
    assert no_change(db_session)
    assert post(client, world, end_date=exactly, **common).status_code == 303


def test_the_span_setting_defaults_to_a_year(db_session):
    from app.settings_store import count_setting

    assert count_setting(db_session, MAILBOX_CHANGE_MAX_DAYS) == 365


def test_a_local_time_the_clocks_skip_is_refused(client, db_session, world, friend):
    signed_in(client, db_session, time_zone="Europe/London")
    response = post(client, world, end_date="2027-03-28", end_time="01:30")  # the hour UK clocks skip
    assert response.status_code == 400 and "does not exist" in response.text
    assert no_change(db_session)


# ---- The keep-a-copy warning ----------------------------------------------------------------------------------------


def test_copy_off_without_confirmation_is_refused_and_the_confirm_block_is_shown(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, keep_copy=None)
    assert response.status_code == 200
    assert no_change(db_session)
    assert "Your mail will not be kept. Mail sent to you will go only to Jane Friend. Nothing will arrive in your own mailbox until " in response.text
    assert 'id="mb-confirm-block"' in response.text and 'name="confirm_no_copy" value="1"' in response.text
    assert 'id="mb-confirm-field"' not in response.text  # the script's own field would shadow the button's value


def test_copy_off_with_confirmation_is_accepted(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world, keep_copy=None, confirm_no_copy="1").status_code == 303
    assert only_change(db_session).keep_copy is False


def test_a_confirmation_does_not_excuse_another_refusal(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, keep_copy=None, confirm_no_copy="1", end_date="")
    assert response.status_code == 400 and "end date" in response.text
    assert no_change(db_session)


def test_another_refusal_comes_before_the_warning(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, keep_copy=None, end_date="")
    assert response.status_code == 400 and "mb-confirm-block" not in response.text


def test_the_copy_is_kept_when_the_box_is_ticked_without_any_confirmation(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world).status_code == 303
    assert only_change(db_session).keep_copy is True


def test_the_page_carries_the_modal_with_cancel_focused_and_a_disabled_countdown_button(client, db_session, world, friend):
    signed_in(client, db_session)
    page = client.get(f"/portal/new?request_type_id={world.type.id}").text
    assert '<dialog class="mb-dialog" id="mb-dialog"' in page and 'aria-labelledby="mb-dialog-title"' in page
    assert 'id="mb-dialog-cancel" autofocus' in page
    assert 'class="btn btn-danger" id="mb-dialog-confirm" disabled' in page
    assert 'id="mb-countdown" role="status" aria-live="polite"' in page
    assert "mailbox-form.js" in page


def test_the_script_waits_five_seconds_and_sends_the_confirmation():
    script = open("app/static/mailbox-form.js", encoding="utf-8").read()
    assert "WAIT_SECONDS = 5" in script and "showModal" in script and 'confirmField.value = "1"' in script
    assert "cancel.focus()" in script


# ---- What a request does --------------------------------------------------------------------------------------------


def test_a_request_raises_a_self_triaged_service_request_and_schedules_it(client, db_session, world, friend):
    signed_in(client, db_session)
    start = (now() + timedelta(days=2)).replace(second=0, microsecond=0)
    start_date, start_time = f"{start:%Y-%m-%d}", "10:30"
    end_date = f"{start + timedelta(days=3):%Y-%m-%d}"
    response = post(client, world, start_mode="later", start_date=start_date, start_time=start_time, end_date=end_date, end_time="16:00")
    assert response.status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    assert response.headers["location"] == f"/portal/{ticket.number}"
    assert ticket.type == TicketType.SERVICE_REQUEST and ticket.source == TicketSource.PORTAL
    assert ticket.requester_email == OWNER and ticket.triaged_at is not None and ticket.team_id == world.team.id
    assert ticket.request_type_id == world.type.id and ticket.status == TicketStatus.SCHEDULED
    change = only_change(db_session)
    assert change.ticket_id == ticket.id
    assert change.starts_at.replace(tzinfo=timezone.utc) == datetime.fromisoformat(f"{start_date}T10:30:00+00:00")
    assert change.ends_at.replace(tzinfo=timezone.utc) == datetime.fromisoformat(f"{end_date}T16:00:00+00:00")
    assert ticket.sla_starts_at.replace(tzinfo=timezone.utc) == change.starts_at.replace(tzinfo=timezone.utc)
    assert ticket.due_on == change.starts_at.date()
    assert "Jane Friend" in db_session.exec(select(Ticket)).one().subject


def test_a_refused_request_leaves_no_ticket_behind(client, db_session, world, friend):
    signed_in(client, db_session)
    post(client, world, end_date="")
    assert no_change(db_session)


def test_a_type_whose_defaults_stopped_holding_cannot_be_used_and_leaves_nothing(client, db_session, world, friend):
    signed_in(client, db_session)
    world.category.is_active = False  # switched off since the type was saved: it can no longer triage itself
    db_session.add(world.category)
    db_session.commit()
    response = post(client, world)
    assert response.status_code == 400 and "could not be set up" in response.text
    assert no_change(db_session)


# ---- The request page -----------------------------------------------------------------------------------------------


def make_request(client, db_session, world, **extra):
    assert post(client, world, **extra).status_code == 303
    db_session.expire_all()
    return db_session.exec(select(Ticket)).one(), only_change(db_session)


def page_of(client, ticket):
    return client.get(f"/portal/{ticket.number}").text


def set_state(db_session, change, state, **fields):
    change.state = state
    for key, value in fields.items():
        setattr(change, key, value)
    db_session.add(change)
    db_session.commit()


def test_the_request_page_says_it_in_plain_words_with_cancel_before_the_start(client, db_session, world, friend):
    signed_in(client, db_session)
    start_date, _ = later(48)
    end_date, _ = later(100)
    ticket, change = make_request(client, db_session, world, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="17:00")
    page = page_of(client, ticket)
    assert "Forwarding my mail" in page and "Scheduled" in page
    assert "Forwarded to" in page and "Jane Friend" in page and "jane@example.com" in page
    assert "A copy in my mailbox" in page and "Kept" in page and "Until" in page
    assert "Cancel this request" in page and f"/portal/{ticket.number}/mailbox/cancel" in page and "Stop forwarding now" not in page


def test_a_copy_that_is_not_kept_is_said_so(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, _ = make_request(client, db_session, world, keep_copy=None, confirm_no_copy="1")
    assert "Not kept" in page_of(client, ticket)


def test_a_start_of_now_offers_neither_cancel_nor_stop_until_it_is_on(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, _ = make_request(client, db_session, world)
    page = page_of(client, ticket)
    assert "Cancel this request" not in page and "Stop forwarding now" not in page


def test_the_active_state_offers_stop_now_with_a_confirm_step(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    set_state(db_session, change, "active", applied_at=now())
    page = page_of(client, ticket)
    assert ">On<" in page and "Stop forwarding now" in page and "Yes, stop forwarding" in page and "Cancel this request" not in page


@pytest.mark.parametrize("state, words", [
    ("applying", "Starting"), ("removing", "Stopping"), ("ended", "Stopped"), ("cancelled", "Cancelled"), ("failed", "Could not be set up"),
])
def test_the_page_words_each_state(client, db_session, world, friend, state, words):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    set_state(db_session, change, state)
    page = page_of(client, ticket)
    assert words in page and "Cancel this request" not in page and "Stop forwarding now" not in page


def test_a_failed_removal_still_reads_as_stopping_and_tells_the_requester_nothing_more(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    set_state(db_session, change, "failed", applied_at=now() - timedelta(hours=1), last_error="Exchange said no")
    page = page_of(client, ticket)
    assert "Stopping" in page and "Exchange said no" not in page and "could not be set up" not in page.lower()


def test_cancel_before_the_start_drops_the_change_and_resolves_the_request(client, db_session, world, friend):
    signed_in(client, db_session)
    start_date, _ = later(48)
    end_date, _ = later(100)
    ticket, change = make_request(client, db_session, world, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="17:00")
    response = client.post(f"/portal/{ticket.number}/mailbox/cancel", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/portal/{ticket.number}"
    db_session.expire_all()
    assert db_session.get(MailboxChange, change.id).state == "cancelled"
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.RESOLVED
    assert "Cancelled" in page_of(client, ticket)


def test_cancel_after_the_start_has_come_is_refused_and_changes_nothing(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)  # starts now
    client.post(f"/portal/{ticket.number}/mailbox/cancel", follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(MailboxChange, change.id).state == "scheduled"


def test_stop_now_brings_the_end_forward_while_it_is_on(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    set_state(db_session, change, "active", applied_at=now())
    response = client.post(f"/portal/{ticket.number}/mailbox/stop", follow_redirects=False)
    assert response.status_code == 303
    db_session.expire_all()
    stopped = db_session.get(MailboxChange, change.id)
    assert stopped.ends_at.replace(tzinfo=timezone.utc) <= now() + timedelta(seconds=5)
    assert stopped.state == "active"  # the worker's next run removes it


def test_stop_now_is_refused_unless_it_is_on(client, db_session, world, friend):
    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    original_end = change.ends_at
    client.post(f"/portal/{ticket.number}/mailbox/stop", follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(MailboxChange, change.id).ends_at == original_end


def test_nobody_else_can_cancel_or_stop_or_see_the_change(client, db_session, world, friend):
    signed_in(client, db_session)
    start_date, _ = later(48)
    end_date, _ = later(100)
    ticket, change = make_request(client, db_session, world, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="17:00")
    client.post("/auth/logout")
    login_as(client, "mallory@example.com", "Mallory")
    assert client.post(f"/portal/{ticket.number}/mailbox/cancel").status_code == 404
    assert client.post(f"/portal/{ticket.number}/mailbox/stop").status_code == 404
    assert client.get(f"/portal/{ticket.number}").status_code == 404
    db_session.expire_all()
    assert db_session.get(MailboxChange, change.id).state == "scheduled"


def test_an_ordinary_request_has_no_mailbox_card_and_no_mailbox_actions(client, db_session, world, friend):
    signed_in(client, db_session)
    client.post("/portal/new", data={"subject": "Printer", "description": "It is on fire"})
    ticket = db_session.exec(select(Ticket)).one()
    assert "mailbox-change" not in page_of(client, ticket)
    assert client.post(f"/portal/{ticket.number}/mailbox/cancel").status_code == 404


# ---- Settings ---------------------------------------------------------------------------------------------------------


def test_the_blocklist_setting_is_tidied_and_checked(client, db_session):
    key = MAILBOX_TARGET_BLOCKLIST
    from app.routers.admin import _clean_setting_value
    from app.settings_store import REGISTRY

    assert _clean_setting_value(db_session, REGISTRY[key], " B@Example.com ;a@example.com  b@example.com") == "a@example.com, b@example.com"
    with pytest.raises(ValueError):
        _clean_setting_value(db_session, REGISTRY[key], "nope")
    assert _clean_setting_value(db_session, REGISTRY[key], "") == ""
    assert REGISTRY[MAILBOX_CHANGE_MAX_DAYS].default == "365" and REGISTRY[key].group == REGISTRY[MAILBOX_CHANGE_MAX_DAYS].group


def test_the_card_is_only_for_the_person_whose_mailbox_it_is_even_on_their_ticket(client, db_session, world, friend):
    from app import portal_mailbox

    signed_in(client, db_session)
    ticket, change = make_request(client, db_session, world)
    owner = db_session.exec(select(User).where(User.email == OWNER)).one()
    other = person(db_session, "other@example.com", "Other One")
    assert portal_mailbox.view(db_session, ticket, owner) is not None
    assert portal_mailbox.view(db_session, ticket, other) is None


def test_a_second_forwarding_that_overlaps_a_live_one_is_refused_and_a_double_click_makes_one(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world).status_code == 303
    again = post(client, world)
    assert again.status_code == 400 and "already have forwarding" in again.text
    db_session.expire_all()
    assert len(db_session.exec(select(MailboxChange)).all()) == 1 and len(db_session.exec(select(Ticket)).all()) == 1


def test_forwarding_after_the_first_ends_or_once_it_is_cancelled_is_allowed(client, db_session, world, friend):
    signed_in(client, db_session)
    first_end, _ = later(30)
    assert post(client, world, end_date=first_end, end_time="09:00").status_code == 303
    start = (now() + timedelta(days=5)).strftime("%Y-%m-%d")
    end = (now() + timedelta(days=6)).strftime("%Y-%m-%d")
    assert post(client, world, start_mode="later", start_date=start, start_time="09:00", end_date=end, end_time="09:00").status_code == 303
    change = db_session.exec(select(MailboxChange).order_by(MailboxChange.id)).first()
    change.state = "cancelled"
    db_session.add(change)
    db_session.commit()
    assert post(client, world).status_code == 303


def test_the_apps_own_mailbox_is_never_offered_or_accepted(client, db_session, world, friend, directory, monkeypatch):
    signed_in(client, db_session)
    monkeypatch.setattr(settings, "graph_shared_mailbox", "itsupport@example.com")
    directory["sm"] = raw_user("sm", "Jane Support Desk", "ItSupport@example.com")
    assert "Support Desk" not in client.get("/portal/people?q=jane").text
    assert "cannot be sent to that address" in post(client, world, target_object_id="sm").text


def test_an_internal_refusal_is_not_shown_to_the_requester(client, db_session, world, friend):
    signed_in(client, db_session)
    world.category.is_active = False
    db_session.add(world.category)
    db_session.commit()
    text = post(client, world).text
    assert "contact IT Support" in text and "triage" not in text and "defaults" not in text


# ---- Review follow-ups (#325) ---------------------------------------------------------------------------------------


def forwarding_row(db_session, owner, target, owner_oid=None, target_oid=None, starts=None, hours=48, state="active", applied=True):
    ticket = Ticket(subject="x", description="x", requester_email=owner, source=TicketSource.PORTAL, type=TicketType.SERVICE_REQUEST)
    db_session.add(ticket)
    db_session.flush()
    start = starts or now() - timedelta(hours=1)
    row = MailboxChange(
        ticket_id=ticket.id, kind="forwarding", owner_address=owner, owner_object_id=owner_oid, target_address=target,
        target_object_id=target_oid, starts_at=start, ends_at=start + timedelta(hours=hours), state=state,
        applied_at=now() if applied else None, created_by=owner,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_forwarding_to_someone_who_already_forwards_to_you_is_refused_as_a_loop(client, db_session, world, friend):
    signed_in(client, db_session, entra_object_id=OWNER_OID)
    forwarding_row(db_session, "jane@example.com", OWNER, FRIEND_OID, OWNER_OID)
    answer = post(client, world)
    assert answer.status_code == 400 and "mail is already forwarded to you during that time" in answer.text
    assert "loop" in answer.text and no_change_of_mine(db_session)


def no_change_of_mine(db_session):
    db_session.expire_all()
    return len(db_session.exec(select(MailboxChange).where(MailboxChange.owner_address == OWNER)).all()) == 0


def test_a_loop_is_found_by_address_alone_and_ignores_a_stopped_or_non_overlapping_forwarding(client, db_session, world, friend):
    signed_in(client, db_session)
    ended = forwarding_row(db_session, "jane@example.com", OWNER, state="ended")
    far = forwarding_row(db_session, "jane@example.com", OWNER, starts=now() + timedelta(days=10), state="scheduled", applied=False)
    assert post(client, world).status_code == 303  # neither is live in that span
    live = forwarding_row(db_session, "jane@example.com", OWNER, state="scheduled", applied=False)
    assert "loop" in post(client, world).text  # (a second request would also overlap; the loop wording comes first)
    live.state = "failed"  # removal failed: Exchange may still hold it
    live.applied_at = now()
    db_session.add(live)
    db_session.commit()
    assert "loop" in post(client, world).text
    live.applied_at = None  # a start that failed never made it
    db_session.add(live)
    db_session.commit()
    db_session.refresh(ended), db_session.refresh(far)
    first = db_session.exec(select(MailboxChange).where(MailboxChange.owner_address == OWNER)).one()
    first.state = "cancelled"
    db_session.add(first)
    db_session.commit()
    assert post(client, world).status_code == 303


def test_a_chain_is_not_a_loop(client, db_session, world, friend):
    signed_in(client, db_session)
    forwarding_row(db_session, "jane@example.com", "third@example.com", FRIEND_OID, "oid-third")  # Jane to a third person
    assert post(client, world).status_code == 303  # Alex to Jane: A to B to C


def test_forwarding_to_a_person_forwarding_to_a_different_person_than_me_is_allowed_but_the_reverse_direction_is_not_a_loop(client, db_session, world, friend):
    signed_in(client, db_session)
    forwarding_row(db_session, OWNER, "jane@example.com")  # Alex already forwards to Jane (a plain overlap, not the loop wording)
    answer = post(client, world)
    assert answer.status_code == 400 and "already have forwarding" in answer.text and "loop" not in answer.text


def test_a_forwarding_whose_removal_failed_still_blocks_an_overlapping_request(client, db_session, world, friend):
    signed_in(client, db_session)
    forwarding_row(db_session, OWNER, "other@example.com", state="failed", applied=True)
    answer = post(client, world)
    assert answer.status_code == 400 and "already have forwarding" in answer.text
    row = db_session.exec(select(MailboxChange)).one()
    row.applied_at = None  # a start that failed was never made, so it does not block
    db_session.add(row)
    db_session.commit()
    assert post(client, world).status_code == 303


def test_both_submits_take_the_owners_lock_before_the_overlap_check(client, db_session, world, friend, monkeypatch):
    from app import mailbox_changes, portal_mailbox

    taken, order = [], []
    real = mailbox_changes.lock_owner
    monkeypatch.setattr(portal_mailbox, "lock_owner", lambda session, address: (taken.append(address), real(session, address)))
    signed_in(client, db_session)
    assert post(client, world).status_code == 303
    assert taken[:2] == ["alex@example.com", "jane@example.com"] and set(taken) == {OWNER, "jane@example.com"}  # both mailboxes, in order


def test_a_second_session_waits_for_the_owners_lock_until_the_first_commits(db_session):
    """Two sessions for one owner: the second blocks on the advisory lock until the first ends its transaction, a different owner
    does not wait."""
    import threading
    import time as time_module

    from sqlmodel import Session

    from app.database import engine
    from app.mailbox_changes import lock_owner

    first = Session(engine)
    lock_owner(first, "Alex@Example.com")
    got = []

    def second():
        with Session(engine) as other:
            lock_owner(other, "alex@example.com")  # same owner, case ignored
            got.append(time_module.monotonic())

    started = time_module.monotonic()
    thread = threading.Thread(target=second)
    thread.start()
    with Session(engine) as unrelated:
        lock_owner(unrelated, "someone-else@example.com")  # not blocked
    time_module.sleep(0.5)
    assert got == []  # still waiting
    first.commit()
    thread.join(5)
    first.close()
    assert len(got) == 1 and got[0] - started >= 0.5


def test_an_account_with_a_blank_user_type_is_listed_and_accepted_but_a_guest_is_not(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    directory["oid-null"] = raw_user("oid-null", "Jane Nulltype", "jane.null@example.com", kind=None)
    directory["oid-blank"] = raw_user("oid-blank", "Jane Blanktype", "jane.blank@example.com", kind="  ")
    directory["oid-guest"] = raw_user("oid-guest", "Jane Guestly", "jane.guest@example.com", kind="guest")
    text = client.get("/portal/people?q=jane").text
    assert "Jane Nulltype" in text and "Jane Blanktype" in text and "Jane Guestly" not in text
    assert post(client, world, target_object_id="oid-null").status_code == 303  # and the submit's own check agrees
    refused = post(client, world, target_object_id="oid-guest")
    assert refused.status_code == 400 and "inside the organisation" in refused.text

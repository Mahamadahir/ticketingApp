"""2C-ii (selection): which notifications want a Teams copy, when it is due, and the person's delay.
The sending itself (the Teams post) is plugged into hand_to_teams separately."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.database import engine
from app.models import Notification, Team, User, UserRole
from app.notification_settings import save_choices
from app.notifications import (
    count_unread,
    default_teams_delay,
    due_for_teams,
    hand_to_teams,
    mark_all_read,
    notify,
)
from app.settings_store import TEAMS_UNREAD_MINUTES, REGISTRY, set_setting
from tests.conftest import add_membership, create_team, login_as, set_role
from tests.test_notification_settings import user_named
from tests.test_notifications import agent_in_desk, back_to, note
from tests.test_raising import desk
from tests.test_team_activity import classified, ticket_named

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
def post_settings(client, *ticked, teams_delay=None):
    data = {"on": list(ticked)}
    if teams_delay is not None:
        data["teams_delay"] = teams_delay
    return client.post("/settings/notifications", data=data)  # follows the redirect: 200 when saved

pytestmark = pytest.mark.usefixtures("no_hold_while_active")  # the hold while the person is active has its own tests (test_teams_hold.py)


ALL_ON = {"assigned:in_app", "assigned:teams", "mentioned:in_app", "mentioned:teams", "replied:in_app", "replied:teams", "reopened:in_app", "reopened:teams", "raised_for_you:email", "follower_update:email", "approval_request:email", "approval_waiting:in_app", "approval_waiting:teams", "agent_emailed:in_app", "awaiting_you:in_app", "ticket_resolved:in_app", "ticket_closed:in_app", "approval_outcome:in_app", "subrequest_created:in_app", "due_today:in_app", "due_today:teams", "msp_unacknowledged:in_app", "msp_handoff_warning:in_app", "msp_handoff_warning:teams", "msp_handed_off:in_app", "msp_handed_off:teams"}


def an_agent(client, db_session, email="bob@example.com"):
    team = create_team(db_session, "Support")
    agent_id = login_as(client, email, "Bob Builder")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return db_session.get(User, agent_id), team


def choose(db_session, person, ticked):
    save_choices(db_session, person, set(ticked))
    db_session.commit()


def row(db_session, person, *, age_minutes, read=False, bell=True, wanted=True, handled=False, kind="mentioned"):
    n = Notification(
        user_id=person.id, kind=kind, text="x", bell=bell, teams_wanted=wanted,
        created_at=NOW - timedelta(minutes=age_minutes),
        read_at=NOW if (read or not bell) else None,
        teams_handled_at=NOW if handled else None,
    )
    db_session.add(n)
    db_session.commit()
    return n.id


def due_ids(db_session):
    return [n.id for n in due_for_teams(db_session, NOW)]


# ---- What a notification records when it is written ---------------------------------------------------


@pytest.mark.parametrize(
    "ticked,bell,teams,read",
    [
        (ALL_ON, True, True, False),  # the default: both
        (ALL_ON - {"mentioned:in_app"}, False, True, True),  # Teams only: written already read
        (ALL_ON - {"mentioned:teams"}, True, False, False),  # the bell only
    ],
)
def test_a_notification_records_which_channels_the_person_wanted(client, db_session, ticked, bell, teams, read):
    bob, _ = an_agent(client, db_session)
    choose(db_session, bob, ticked)

    made = notify(db_session, bob, "mentioned", None, "text", "someone@example.com")
    db_session.commit()

    assert (made.bell, made.teams_wanted, made.read_at is not None) == (bell, teams, read)


def test_with_both_off_there_is_no_notification_at_all(client, db_session):
    bob, _ = an_agent(client, db_session)
    choose(db_session, bob, ALL_ON - {"mentioned:in_app", "mentioned:teams"})

    assert notify(db_session, bob, "mentioned", None, "text", "someone@example.com") is None
    assert db_session.exec(select(Notification)).all() == []


def test_a_teams_only_notification_never_shows_in_the_bell(client, db_session):
    bob, _ = an_agent(client, db_session)
    choose(db_session, bob, ALL_ON - {"mentioned:in_app"})
    notify(db_session, bob, "mentioned", None, "secret subject", "someone@example.com")
    db_session.commit()

    assert count_unread(db_session, bob) == 0
    assert client.get("/notifications/count").text == "Notifications"
    assert "secret subject" not in client.get("/notifications").text
    assert mark_all_read(db_session, bob) == 0
    assert len(db_session.exec(select(Notification)).all()) == 1  # but the worker still has it


def test_old_rows_written_before_this_change_keep_working(client, db_session):
    bob, _ = an_agent(client, db_session)
    db_session.execute(
        text("insert into notification (user_id, kind, text, created_at) values (:u, 'assigned', 'old', now())"),
        {"u": bob.id},
    )
    db_session.commit()

    old = db_session.exec(select(Notification)).one()
    assert (old.bell, old.teams_wanted, old.teams_handled_at) == (True, False, None)  # in the bell, no Teams flood
    assert count_unread(db_session, bob) == 1


# ---- When a Teams copy is due --------------------------------------------------------------------------


def test_when_a_copy_is_due(client, db_session):
    bob, _ = an_agent(client, db_session)  # the default delay is 5 minutes
    too_soon = row(db_session, bob, age_minutes=4)
    unread_past_the_delay = row(db_session, bob, age_minutes=6)
    read_in_time = row(db_session, bob, age_minutes=60, read=True)
    teams_only_just_now = row(db_session, bob, age_minutes=0, bell=False)
    not_wanted = row(db_session, bob, age_minutes=60, wanted=False)
    already_dealt_with = row(db_session, bob, age_minutes=60, handled=True)

    assert due_ids(db_session) == [unread_past_the_delay, teams_only_just_now]
    assert {too_soon, read_in_time, not_wanted, already_dealt_with}.isdisjoint(due_ids(db_session))


def test_exactly_on_the_delay_is_due_and_a_second_before_is_not(client, db_session):
    bob, _ = an_agent(client, db_session)
    on_time = row(db_session, bob, age_minutes=5)

    assert due_ids(db_session) == [on_time]
    assert [n.id for n in due_for_teams(db_session, NOW - timedelta(seconds=1))] == []


def test_a_persons_own_delay_beats_the_default_and_the_default_is_a_setting(client, db_session):
    bob, _ = an_agent(client, db_session)
    seven_minutes_old = row(db_session, bob, age_minutes=7)
    assert default_teams_delay(db_session) == 5
    assert due_ids(db_session) == [seven_minutes_old]

    bob.teams_delay_minutes = 10  # a longer delay of their own
    db_session.add(bob)
    db_session.commit()
    assert due_ids(db_session) == []

    bob.teams_delay_minutes = None  # back to the default, which an admin changes
    db_session.add(bob)
    set_setting(db_session, TEAMS_UNREAD_MINUTES, "30")
    db_session.commit()
    assert default_teams_delay(db_session) == 30
    assert due_ids(db_session) == []
    set_setting(db_session, TEAMS_UNREAD_MINUTES, "1")
    db_session.commit()
    assert due_ids(db_session) == [seven_minutes_old]


def test_each_person_has_their_own_clock(client, db_session):
    bob, team = an_agent(client, db_session)
    cat = User(email="cat@example.com", display_name="Cat", role=UserRole.AGENT, teams_delay_minutes=30)
    db_session.add(cat)
    db_session.commit()
    add_membership(db_session, cat.id, team.id)
    bobs = row(db_session, bob, age_minutes=10)
    cats = row(db_session, cat, age_minutes=10)

    assert due_ids(db_session) == [bobs]  # Cat set 30 minutes
    assert cats not in due_ids(db_session)


def test_a_blank_admin_default_falls_back_to_five_minutes(client, db_session):
    set_setting(db_session, TEAMS_UNREAD_MINUTES, "")  # the settings page allows a blank
    db_session.commit()
    assert default_teams_delay(db_session) == 5


def test_the_admin_default_is_a_real_setting(client, db_session):
    assert REGISTRY[TEAMS_UNREAD_MINUTES].kind == "minutes" and REGISTRY[TEAMS_UNREAD_MINUTES].default == "5"
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    assert client.post("/admin/settings", data={"key": TEAMS_UNREAD_MINUTES, "value": "12"}).status_code == 200
    db_session.expire_all()
    assert default_teams_delay(db_session) == 12
    assert client.post("/admin/settings", data={"key": TEAMS_UNREAD_MINUTES, "value": "soon"}).status_code == 400


# ---- Handing a copy over ------------------------------------------------------------------------------


def test_a_due_notification_is_handed_over_once(client, db_session):
    bob, _ = an_agent(client, db_session)
    first = row(db_session, bob, age_minutes=9)
    second = row(db_session, bob, age_minutes=7)
    row(db_session, bob, age_minutes=1)  # not due yet
    handed = []

    result = hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW)
    db_session.commit()

    assert result == (2, 0) and handed == [first, second]  # oldest first
    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW) == (0, 0)  # and only once
    assert handed == [first, second]
    db_session.expire_all()
    assert db_session.get(Notification, first).teams_handled_at is not None


def test_it_is_dropped_if_they_turned_teams_off_in_the_meantime(client, db_session):
    bob, _ = an_agent(client, db_session)
    stale = row(db_session, bob, age_minutes=9, kind="mentioned")
    choose(db_session, bob, ALL_ON - {"mentioned:teams"})  # changed their mind after it was written
    handed = []

    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW) == (0, 1)

    assert handed == []
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Notification, stale).teams_handled_at is not None  # dealt with, not retried forever


def test_it_is_dropped_if_they_can_no_longer_open_the_ticket(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    back_to(client)
    classified(client, category, "Moved away")
    ticket = ticket_named(db_session, "Moved away")
    note(client, ticket, "@bob look")
    [mention] = db_session.exec(select(Notification)).all()
    mention.created_at = NOW - timedelta(minutes=9)
    db_session.add(mention)
    other = create_team(db_session, "Network")
    ticket.team_id = other.id  # now in a team Bob is not in
    db_session.add(ticket)
    db_session.commit()
    handed = []

    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW) == (0, 1)
    assert handed == []  # a Teams message would say what the ticket is


def test_a_notification_about_no_ticket_is_still_handed_over(client, db_session):
    bob, _ = an_agent(client, db_session)
    row(db_session, bob, age_minutes=9)  # ticket_id is empty
    handed = []
    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW) == (1, 0)


def test_a_run_is_limited_to_a_batch(client, db_session):
    bob, _ = an_agent(client, db_session)
    ids = [row(db_session, bob, age_minutes=9 + i) for i in range(5)]
    handed = []

    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW, limit=2) == (2, 0)
    assert hand_to_teams(db_session, lambda session, n: handed.append(n.id), NOW, limit=10) == (3, 0)

    assert sorted(handed) == sorted(ids) and len(set(handed)) == 5


def test_if_handing_over_fails_nothing_from_that_run_is_kept(client, db_session):
    bob, _ = an_agent(client, db_session)
    first = row(db_session, bob, age_minutes=9)
    second = row(db_session, bob, age_minutes=8)

    def enqueue(session, n):
        if n.id == second:
            raise RuntimeError("the outbox is broken")

    with pytest.raises(RuntimeError):
        hand_to_teams(db_session, enqueue, NOW)
    db_session.rollback()  # what the worker does when a job raises

    db_session.expire_all()
    assert db_session.get(Notification, first).teams_handled_at is None  # so it is tried again next run
    assert db_session.get(Notification, second).teams_handled_at is None


def test_two_workers_cannot_take_the_same_rows(client, db_session):
    bob, _ = an_agent(client, db_session)
    row(db_session, bob, age_minutes=9)
    with Session(engine) as one, Session(engine) as two:
        taken = due_for_teams(one, NOW)  # locks the row until `one` ends
        assert len(taken) == 1
        assert due_for_teams(two, NOW) == []  # skipped, not waited for, not duplicated


# ---- The person's delay on the settings page ------------------------------------------------------------


def test_agents_see_and_save_their_delay_and_others_do_not(client, db_session):
    bob, _ = an_agent(client, db_session)
    page = client.get("/settings/notifications").text
    assert 'name="teams_delay"' in page and 'placeholder="5"' in page

    assert post_settings(client, *ALL_ON, teams_delay="12").status_code == 200
    db_session.expire_all()
    assert user_named(db_session, "bob@example.com").teams_delay_minutes == 12
    assert 'value="12"' in client.get("/settings/notifications").text

    assert post_settings(client, *ALL_ON, teams_delay=" ").status_code == 200  # blank: back to the default
    db_session.expire_all()
    assert user_named(db_session, "bob@example.com").teams_delay_minutes is None

    client.post("/auth/logout")
    login_as(client, "jo@example.com")  # an end user is never notified, so has no delay
    assert 'name="teams_delay"' not in client.get("/settings/notifications").text
    post_settings(client, "raised_for_you:email", teams_delay="3")
    db_session.expire_all()
    assert user_named(db_session, "jo@example.com").teams_delay_minutes is None


def test_an_end_user_cannot_trip_over_a_delay_they_do_not_have(client, db_session):
    login_as(client, "jo@example.com")
    assert post_settings(client, "raised_for_you:email", teams_delay="not a number").status_code == 200  # ignored
    db_session.expire_all()
    assert user_named(db_session, "jo@example.com").teams_delay_minutes is None


@pytest.mark.parametrize("bad", ["0", "1441", "-1", "abc", "2.5", "٣", "5 minutes"])
def test_a_bad_delay_saves_nothing(client, db_session, bad):
    bob, _ = an_agent(client, db_session)

    response = post_settings(client, "raised_for_you:email", teams_delay=bad)  # also unticking all the agent events

    assert response.status_code == 400 and "whole number of minutes" in response.text
    db_session.expire_all()
    assert user_named(db_session, "bob@example.com").teams_delay_minutes is None


def test_a_bad_delay_leaves_the_tick_boxes_untouched_too(client, db_session):
    from app.models import NotificationPreference

    an_agent(client, db_session)
    assert post_settings(client, "raised_for_you:email", teams_delay="0").status_code == 400
    assert db_session.exec(select(NotificationPreference)).all() == []  # nothing was stored


def test_changing_the_delay_is_audited(client, db_session):
    from app.models import AuditLog

    an_agent(client, db_session)
    post_settings(client, *ALL_ON, teams_delay="12")
    post_settings(client, *ALL_ON, teams_delay="12")  # unchanged: no second entry
    post_settings(client, *ALL_ON, teams_delay="")

    details = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "notification_settings")).all()]
    assert details == ["teams delay default -> 12", "teams delay 12 -> default"]


# ---- The schema ---------------------------------------------------------------------------------------


def test_the_worker_has_a_cheap_way_to_find_what_is_due(db_session):
    indexes = {i["name"]: i for i in inspect(db_session.get_bind()).get_indexes("notification")}
    due = indexes["ix_notification_teams_due"]
    assert due["column_names"] == ["created_at"]
    assert "teams_wanted" in str(due["dialect_options"]["postgresql_where"])

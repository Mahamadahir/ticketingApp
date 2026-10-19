from sqlmodel import select

from app.models import AuditLog, NotificationPreference, User, UserRole
from app.notification_settings import EMAIL, IN_APP, TEAMS, is_enabled
from app.raising import should_tell_requester
from tests.conftest import login_as, set_role
from tests.test_notifications import agent_in_desk, back_to, classified, note, notifications_for, ticket_named
from tests.test_raising import desk


def user_named(db_session, email):
    db_session.expire_all()
    return db_session.exec(select(User).where(User.email == email)).one()


def preferences_of(db_session, email):
    user = user_named(db_session, email)
    return {
        (p.kind, p.channel): p.enabled
        for p in db_session.exec(select(NotificationPreference).where(NotificationPreference.user_id == user.id)).all()
    }


def save(client, *ticked):
    return client.post("/settings/notifications", data={"on": list(ticked)}, follow_redirects=False)


# ---- Defaults and the page --------------------------------------------------------------------------


def test_defaults_are_on_and_the_page_shows_them(client, db_session):
    desk(client, db_session)
    page = client.get("/settings/notifications").text
    assert "A ticket is assigned to you" in page and "You are mentioned in an internal note" in page
    assert 'value="assigned:in_app" checked' in page and 'value="mentioned:teams" checked' in page
    assert "An agent raises a ticket for you" in page
    assert preferences_of(db_session, "agent@example.com") == {}  # nothing stored until they differ
    assert is_enabled(db_session, user_named(db_session, "agent@example.com"), "assigned", IN_APP)


def test_a_requester_sees_only_their_own_events_and_must_be_signed_in(client, db_session):
    response = client.get("/settings/notifications", follow_redirects=False)
    assert response.status_code == 307 and "login" in response.headers["location"]
    login_as(client, "jane@example.com", "Jane")
    page = client.get("/settings/notifications").text
    assert "An agent raises a ticket for you" in page
    assert "assigned to you" not in page and "mentioned in an internal note" not in page


# ---- Saving -----------------------------------------------------------------------------------------


def test_saving_stores_only_what_differs_from_the_default_and_audits_it(client, db_session):
    desk(client, db_session)
    # Everything ticked but the bell for assignments, and Teams for mentions.
    response = save(client, "assigned:teams", "mentioned:in_app", "raised_for_you:email")
    assert response.status_code == 303
    assert preferences_of(db_session, "agent@example.com") == {
        ("assigned", IN_APP): False,
        ("mentioned", TEAMS): False,
    }
    entry = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "notification_settings")).one()
    assert entry.actor_email == "agent@example.com"
    assert "assigned/in_app off" in entry.detail and "mentioned/teams off" in entry.detail

    # Ticking them again goes back to the default, and the rows go.
    save(client, "assigned:in_app", "assigned:teams", "mentioned:in_app", "mentioned:teams", "raised_for_you:email")
    assert preferences_of(db_session, "agent@example.com") == {}


def test_saving_the_same_thing_twice_changes_and_audits_nothing_the_second_time(client, db_session):
    desk(client, db_session)
    save(client, "mentioned:in_app")
    save(client, "mentioned:in_app")
    entries = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "notification_settings")).all()
    assert len(entries) == 1


def test_a_requester_cannot_set_an_agent_event_and_nobody_sets_someone_elses(client, db_session):
    login_as(client, "jane@example.com", "Jane")
    save(client, "assigned:in_app", "mentioned:teams", "raised_for_you:in_app", "raised_for_you:email")
    assert preferences_of(db_session, "jane@example.com") == {}  # nothing unknown stored; email is the default

    desk(client, db_session)
    save(client, "mentioned:in_app", "mentioned:teams", "raised_for_you:email")
    assert preferences_of(db_session, "jane@example.com") == {}
    assert preferences_of(db_session, "agent@example.com") == {("assigned", IN_APP): False, ("assigned", TEAMS): False}


# ---- The bell obeys it ------------------------------------------------------------------------------


def test_turning_the_bell_off_for_an_event_stops_that_notification_only(client, db_session):
    team, category, _ = desk(client, db_session)
    agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    save(client, "assigned:in_app", "assigned:teams", "mentioned:teams")  # no bell for mentions
    back_to(client)
    classified(client, category, "Router down")
    ticket = ticket_named(db_session, "Router down")

    note(client, ticket, "Look at this, @bob")
    assert notifications_for(db_session, "bob@example.com") == []

    bob = user_named(db_session, "bob@example.com")
    client.post("/auth/logout")
    login_as(client, "bob@example.com", "Bob Builder")
    save(client, "assigned:in_app", "assigned:teams", "mentioned:teams")
    back_to(client)
    note(client, ticket, "Again @bob")
    assert notifications_for(db_session, "bob@example.com") == []

    client.post("/auth/logout")
    login_as(client, "bob@example.com", "Bob Builder")
    save(client, "assigned:in_app", "assigned:teams", "mentioned:in_app", "mentioned:teams")  # back on
    back_to(client)
    note(client, ticket, "Third time @bob")
    assert [n.kind for n in notifications_for(db_session, "bob@example.com")] == ["mentioned"]
    assert bob.id


# ---- "Raised for you" -------------------------------------------------------------------------------


def test_a_person_can_opt_out_of_the_raised_for_you_email(client, db_session):
    login_as(client, "jane@example.com", "Jane")
    assert should_tell_requester(db_session, "jane@example.com") is True
    assert should_tell_requester(db_session, "Nobody@example.com") is True  # no account: the global switch decides
    save(client)  # the email box unticked
    db_session.expire_all()
    assert preferences_of(db_session, "jane@example.com") == {("raised_for_you", EMAIL): False}
    assert should_tell_requester(db_session, " JANE@example.com ") is False
    assert should_tell_requester(db_session) is True  # without an address, only the global switch

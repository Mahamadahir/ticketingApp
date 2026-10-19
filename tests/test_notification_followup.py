"""Follow-up to 2B: the requester's own opt-out is applied when a ticket is raised for them, saving the
settings page twice at once is safe, and the cross-site protection the page relies on is pinned."""

import threading
import time

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import NotificationPreference, Ticket, TicketEvent, User
from app.notification_settings import save_choices
from app.settings_store import TELL_REQUESTER_RAISED_FOR, set_setting
from tests.conftest import login_as
from tests.test_raising import desk, raise_it


def requester_account(client, email="walkin@example.com"):
    """An account that has signed in once, then signed out."""
    login_as(client, email, "Walk In")
    client.post("/auth/logout")


def turn_off_the_email(db_session, email):
    person = db_session.exec(select(User).where(User.email == email)).one()
    save_choices(db_session, person, set())  # nothing ticked: every channel off
    db_session.commit()


def notice_for(db_session, subject):
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket).where(Ticket.subject == subject)).one()
    return ticket, db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "requester_notice")
    ).all()


# ---- 1. The requester's own choice is applied, and recorded, when the ticket is raised --------------


def test_a_requester_who_has_not_opted_out_is_recorded_as_to_be_told(client, db_session):
    requester_account(client)
    desk(client, db_session)

    raise_it(client, subject="Desk visit", for_email="walkin@example.com")

    ticket, [notice] = notice_for(db_session, "Desk visit")
    assert (notice.to_value, notice.actor_email) == ("will be told", "agent@example.com")
    assert "will be told by email" in notice.detail
    assert "will be told" in client.get(f"/tickets/{ticket.number}").text  # the agent can see it


def test_a_requester_who_turned_it_off_is_recorded_as_not_to_be_told(client, db_session):
    requester_account(client)
    turn_off_the_email(db_session, "walkin@example.com")
    desk(client, db_session)

    raise_it(client, subject="Opted out", for_email="walkin@example.com")

    _, [notice] = notice_for(db_session, "Opted out")
    assert notice.to_value == "not told"
    assert "turned this off in their own notification settings" in notice.detail


def test_the_global_switch_off_wins_over_everyone(client, db_session):
    requester_account(client)
    set_setting(db_session, TELL_REQUESTER_RAISED_FOR, "off")
    db_session.commit()
    desk(client, db_session)

    raise_it(client, subject="Switched off", for_email="walkin@example.com")

    _, [notice] = notice_for(db_session, "Switched off")
    assert (notice.to_value, notice.detail) == ("not told", "switched off for everyone in Settings")


def test_someone_with_no_account_is_decided_by_the_global_switch_alone(client, db_session):
    desk(client, db_session)

    raise_it(client, subject="No account", for_email="stranger@example.com")

    _, [notice] = notice_for(db_session, "No account")
    assert notice.to_value == "will be told"


def test_a_ticket_people_raise_for_themselves_has_no_such_notice(client, db_session):
    login_as(client, "jo@example.com")

    raise_it(client, subject="Mine")

    _, notices = notice_for(db_session, "Mine")
    assert notices == []


def test_the_form_says_the_person_can_opt_out(client, db_session):
    desk(client, db_session)
    note = client.get("/portal/new").text
    assert "unless they have turned that off in their own notification settings" in note


# ---- 2. Saving the settings page twice at once ----------------------------------------------------------


def test_two_saves_at_once_queue_up_instead_of_clashing(client, db_session):
    person = db_session.get(User, login_as(client, "agent@example.com", "Agent Ann"))
    first_has_the_lock, let_first_finish = threading.Event(), threading.Event()
    errors, finished = [], []

    def first():
        try:
            with Session(engine) as session:
                user = session.get(User, person.id)
                save_choices(session, user, set())  # takes the per-person lock, writes the "off" rows
                first_has_the_lock.set()
                let_first_finish.wait(10)
                session.commit()
        except Exception as error:
            errors.append(("first", error))
            first_has_the_lock.set()

    def second():
        try:
            with Session(engine) as session:
                user = session.get(User, person.id)
                save_choices(session, user, set())  # the same save from a second tab
                session.commit()
                finished.append("second")
        except Exception as error:
            errors.append(("second", error))

    one = threading.Thread(target=first)
    one.start()
    assert first_has_the_lock.wait(10)
    two = threading.Thread(target=second)
    two.start()
    time.sleep(0.7)
    assert finished == []  # the second is waiting for the first, not racing it
    let_first_finish.set()
    one.join(10)
    two.join(10)

    assert errors == []
    assert finished == ["second"]
    db_session.expire_all()
    rows = db_session.exec(select(NotificationPreference).where(NotificationPreference.user_id == person.id)).all()
    keys = [(r.kind, r.channel) for r in rows]
    assert len(keys) == len(set(keys)) > 0  # each setting once


# ---- 3. The cross-site protection the settings page relies on -----------------------------------------


def test_the_session_cookie_is_same_site_lax_so_other_sites_cannot_post_as_the_person(client):
    """Unticked means off, so a cross-site form post to /settings/notifications would otherwise
    switch someone's notifications off. Browsers do not send a SameSite=Lax cookie on a cross-site
    POST; weakening this setting would reopen that, so it is pinned here."""
    response = client.post("/auth/dev-login", data={"email": "a@example.com", "display_name": "A"}, follow_redirects=False)
    cookie = response.headers["set-cookie"].lower()
    assert "samesite=lax" in cookie
    assert "httponly" in cookie


def test_saving_needs_a_signed_in_person(client):
    response = client.post("/settings/notifications", data={"on": "assigned:in_app"}, follow_redirects=False)
    assert response.status_code in (303, 307, 401, 403)
    assert "/settings/notifications?saved" not in response.headers.get("location", "")

"""#122: an approver is told in the bell that a request waits for their vote, whatever their role, whatever
their approval-email setting; the bell entry has its own setting and opens in the portal."""

from sqlmodel import select

from app.models import Notification, NotificationPreference, User, UserRole
from app.notification_settings import APPROVAL_REQUEST, APPROVAL_WAITING, EMAIL, IN_APP
from tests.conftest import login_as
from tests.test_approvals import person, raised_and_triaged, request_type


def bell(db_session, email):
    db_session.expire_all()
    user = db_session.exec(select(User).where(User.email == email)).one()
    return db_session.exec(
        select(Notification).where(Notification.user_id == user.id, Notification.kind == APPROVAL_WAITING)
    ).all()


def test_each_approver_gets_a_bell_entry_when_the_request_starts(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "other@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "other@example.com"]))
    for email in ("boss@example.com", "other@example.com"):
        [entry] = bell(db_session, email)
        assert entry.ticket_id == ticket.id and "waiting for your approval" in entry.text and entry.bell is True


def test_an_approver_who_is_not_an_agent_sees_the_bell_and_it_opens_the_portal(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    client.post("/auth/logout")
    login_as(client, "boss@example.com")

    assert 'href="/notifications"' in client.get("/portal").text  # the bell is in the navbar for everyone
    assert client.get("/notifications/count").text == "Notifications (1)"
    page = client.get("/notifications").text
    assert "waiting for your approval" in page and f"{ticket.reference} " in page and ticket.tag not in page
    [entry] = bell(db_session, "boss@example.com")
    response = client.post(f"/notifications/{entry.id}/open", follow_redirects=False)
    assert response.headers["location"] == f"/portal/{ticket.number}"
    assert client.get("/notifications/count").text == "Notifications"


def test_it_shows_even_with_the_approval_email_switched_off(client, db_session):
    person(client, db_session, "boss@example.com")
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    db_session.add(NotificationPreference(user_id=boss.id, kind=APPROVAL_REQUEST, channel=EMAIL, enabled=False))
    db_session.commit()
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert len(bell(db_session, "boss@example.com")) == 1


def test_switching_the_bell_entry_off_stops_it(client, db_session):
    person(client, db_session, "boss@example.com")
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    db_session.add(NotificationPreference(user_id=boss.id, kind=APPROVAL_WAITING, channel=IN_APP, enabled=False))
    db_session.commit()
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert [n for n in bell(db_session, "boss@example.com") if n.bell] == []  # (the Teams copy is a separate choice)


def test_an_agent_approver_without_access_to_the_ticket_still_sees_it_and_opens_the_portal(client, db_session):
    from tests.conftest import create_team, set_role

    person(client, db_session, "boss@example.com")
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    other_team = create_team(db_session, "Elsewhere")
    set_role(db_session, boss.id, UserRole.AGENT, other_team.id)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    client.post("/auth/logout")
    login_as(client, "boss@example.com")

    assert "waiting for your approval" in client.get("/notifications").text
    [entry] = bell(db_session, "boss@example.com")
    assert client.post(f"/notifications/{entry.id}/open", follow_redirects=False).headers["location"] == f"/portal/{ticket.number}"


def test_an_agent_approver_who_can_open_the_ticket_is_still_sent_to_the_portal_to_vote(client, db_session):
    from tests.conftest import set_role

    person(client, db_session, "boss@example.com")
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    set_role(db_session, boss.id, UserRole.SUPER_ADMIN)  # can open every ticket
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    client.post("/auth/logout")
    login_as(client, "boss@example.com")

    [entry] = bell(db_session, "boss@example.com")
    assert client.post(f"/notifications/{entry.id}/open", follow_redirects=False).headers["location"] == f"/portal/{ticket.number}"

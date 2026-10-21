"""The bell shows, opens and counts a notification only while the agent can still open its ticket.
Access follows the ticket's team only (app/visibility.py), so losing it by a team change or by
leaving the team hides the row; followers and the notified person's past access grant nothing."""

from sqlmodel import select

from app.followers import add_follower
from app.models import Notification, Ticket, TeamMembership, User, UserRole
from app.notifications import bell_tickets, count_unread, notify
from tests.conftest import create_team, login_as, set_role
from tests.test_notifications import agent_in_desk, back_to, note, notifications_for
from tests.test_raising import desk
from tests.test_team_activity import classified, ticket_named


def bob_with_two(client, db_session):
    """Bob (desk team) is told about two tickets; returns (bob, hidden-later ticket, kept ticket)."""
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Secret subject")
    secret = ticket_named(db_session, "Secret subject")
    note(client, secret, "@bob look")
    classified(client, category, "Kept subject")
    kept = ticket_named(db_session, "Kept subject")
    note(client, kept, "@bob look too")
    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    assert client.get("/notifications/count").text == "Notifications (2)"
    return bob, secret, kept


def secret_notification(db_session, secret):
    return next(n for n in notifications_for(db_session, "bob@example.com") if n.ticket_id == secret.id)


def assert_hidden(client, db_session, secret):
    hidden = secret_notification(db_session, secret)
    page = client.get("/notifications").text
    assert "Secret subject" not in page and f"#{secret.number}" not in page and "Kept subject" in page
    assert client.get("/notifications/count").text == "Notifications (1)"
    opened = client.post(f"/notifications/{hidden.id}/open", follow_redirects=False)
    assert opened.status_code == 404
    client.post("/notifications/read-all")
    assert secret_notification(db_session, secret).read_at is None  # left alone, not read away
    assert client.get("/notifications/count").text == "Notifications"


def test_access_lost_by_a_team_change_hides_opens_and_counts(client, db_session):
    bob, secret, _ = bob_with_two(client, db_session)
    moved = db_session.get(Ticket, secret.id)
    moved.team_id = create_team(db_session, "Network").id
    db_session.add(moved)
    db_session.commit()

    assert_hidden(client, db_session, secret)


def test_access_lost_by_leaving_the_team_hides_opens_and_counts(client, db_session):
    bob, secret, kept = bob_with_two(client, db_session)
    other = create_team(db_session, "Network")
    for row in db_session.exec(select(TeamMembership).where(TeamMembership.user_id == bob.id)).all():
        db_session.delete(row)
    db_session.add(TeamMembership(user_id=bob.id, team_id=other.id))
    keep = db_session.get(Ticket, kept.id)
    keep.team_id = other.id
    db_session.add(keep)
    db_session.commit()

    assert_hidden(client, db_session, secret)


def test_a_super_admin_sees_and_opens_every_notification(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Admin ticket")
    ticket = ticket_named(db_session, "Admin ticket")
    client.post("/auth/logout")
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    made = notify(db_session, db_session.get(User, admin_id), "assigned", ticket, "x", "agent@example.com")
    db_session.commit()

    assert client.get("/notifications").status_code == 200
    assert client.get("/notifications/count").text == "Notifications (1)"
    opened = client.post(f"/notifications/{made.id}/open", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == f"/tickets/{ticket.number}"
    assert bell_tickets(db_session, db_session.get(User, admin_id), [made]) == {ticket.id: ticket}


def test_following_a_ticket_does_not_keep_its_notification_visible(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    outsider = db_session.get(User, login_as(client, "outsider@example.com"))
    set_role(db_session, outsider.id, UserRole.AGENT, other.id)
    back_to(client)
    classified(client, category, "Followed only")
    ticket = ticket_named(db_session, "Followed only")
    add_follower(db_session, ticket, "outsider@example.com", "agent@example.com")
    made = notify(db_session, outsider, "assigned", ticket, f"{ticket.tag} Followed only was assigned to you", "agent@example.com")
    db_session.commit()
    client.post("/auth/logout")
    login_as(client, "outsider@example.com")

    assert "Followed only" not in client.get("/notifications").text
    assert client.get("/notifications/count").text == "Notifications"
    assert count_unread(db_session, outsider) == 0
    assert client.post(f"/notifications/{made.id}/open", follow_redirects=False).status_code == 404
    assert bell_tickets(db_session, outsider, [made]) == {}


def test_a_notification_with_no_ticket_still_shows_and_counts(client, db_session):
    desk(client, db_session)
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    made = notify(db_session, ann, "assigned", None, "A general notice", "someone@example.com")
    db_session.commit()

    assert "A general notice" in client.get("/notifications").text
    assert client.get("/notifications/count").text == "Notifications (1)"
    assert client.post(f"/notifications/{made.id}/open", follow_redirects=False).headers["location"] == "/notifications"

"""The assigned agent is told when the requester (or a follower) replies, whatever the ticket's status."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.followers import add_follower
from app.models import Notification, NotificationPreference, Ticket, TicketSource, TicketStatus, User
from app.ticket_service import create_ticket
from tests.conftest import create_team, login_as
from tests.test_notifications import agent_in_desk, back_to, notifications_for
from tests.test_raising import desk, raise_it
from tests.test_splitting import split, subs_of, world


def assigned_ticket(client, db_session, status=TicketStatus.OPEN, requester="jo@example.com"):
    """A triaged ticket in the desk team assigned to Ann, in the given status (New: untriaged, no owner)."""
    team, category, ann_id = desk(client, db_session)
    ticket = create_ticket(
        db_session, subject="Printer jam", requester_email=requester, source=TicketSource.PORTAL, team_id=team.id
    )
    if status != TicketStatus.NEW:
        ticket.triaged_at = datetime.now(timezone.utc)
        ticket.assigned_agent_id = ann_id
    ticket.status = status
    db_session.add(ticket)
    db_session.commit()
    return ticket, team, ann_id


def comment_as(client, email, ticket, body="the issue is resolved"):
    client.post("/auth/logout")
    login_as(client, email)
    response = client.post(f"/portal/{ticket.number}/comment", data={"body": body})
    assert response.status_code == 200, response.text


def all_notifications(db_session):
    db_session.expire_all()
    return db_session.exec(select(Notification)).all()


@pytest.mark.parametrize(
    "status",
    [
        TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.AWAITING_USER, TicketStatus.WITH_MSP,
        TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.AWAITING_APPROVAL,
    ],
)
def test_a_reply_notifies_the_assignee_whatever_the_status(client, db_session, status):
    ticket, team, ann_id = assigned_ticket(client, db_session, status)

    comment_as(client, "jo@example.com", ticket)

    [told] = notifications_for(db_session, "agent@example.com")
    assert (told.kind, told.ticket_id, told.actor_email, told.read_at) == ("replied", ticket.id, "jo@example.com", None)
    assert told.text == f"{ticket.tag} Printer jam: the requester replied"
    expected = TicketStatus.IN_PROGRESS if status == TicketStatus.AWAITING_USER else status
    assert db_session.get(Ticket, ticket.id).status == expected  # status behaviour is unchanged


def test_a_reply_on_a_new_ticket_has_no_assignee_so_tells_nobody(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session, TicketStatus.NEW)

    comment_as(client, "jo@example.com", ticket)

    assert all_notifications(db_session) == []


def test_a_reply_on_a_triaged_ticket_nobody_owns_tells_nobody(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket.assigned_agent_id = None
    db_session.add(ticket)
    db_session.commit()

    comment_as(client, "jo@example.com", ticket)

    assert all_notifications(db_session) == []


def test_a_follower_reply_notifies(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    add_follower(db_session, ticket, "kim@example.com", "jo@example.com")
    db_session.commit()

    comment_as(client, "kim@example.com", ticket, "me too")

    [told] = notifications_for(db_session, "agent@example.com")
    assert (told.kind, told.actor_email) == ("replied", "kim@example.com")


def test_an_agent_replying_on_their_own_ticket_is_not_told(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session, requester="agent@example.com")

    comment_as(client, "agent@example.com", ticket, "my own reply")

    assert all_notifications(db_session) == []


def test_agent_emails_and_notes_do_not_notify(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    back_to(client)
    raise_it(client, subject="Triaged", for_email="walkin@example.com", category_id=category.id,
             impact="low", urgency="low", agent_id=bob.id)
    ticket = db_session.exec(select(Ticket).where(Ticket.subject == "Triaged")).one()

    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "internal"}).status_code == 200
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "an email"}).status_code == 200

    assert {n.kind for n in all_notifications(db_session)} == {"assigned"}  # only the assignment


def test_creating_a_ticket_does_not_notify(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    back_to(client)
    # An agent raises on someone's behalf and assigns it (the first message is recorded as the requester's).
    raise_it(client, subject="On behalf", for_email="walkin@example.com", category_id=category.id,
             impact="low", urgency="low", agent_id=bob.id)
    # An agent raises for themselves, assigned to Bob.
    raise_it(client, subject="Own", category_id=category.id, impact="low", urgency="low", agent_id=bob.id)
    # A requester raises through the portal.
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert raise_it(client, subject="Mine").status_code == 204
    # The dev simulate-inbound call.
    assert client.post(
        "/webhooks/dev/simulate-inbound", data={"requester_email": "x@example.com", "subject": "Mail", "body": "hi"}
    ).status_code == 200

    assert {n.kind for n in all_notifications(db_session)} <= {"assigned"}


def test_a_reply_on_a_split_original_tells_each_open_parts_assignee_once(client, db_session):
    original, desk_team = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""), ("Three", ""))
    bob = agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    cat = agent_in_desk(client, db_session, "cat@example.com", "Cat")
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    one, two, three = subs_of(db_session, original)
    original = db_session.get(Ticket, original.id)
    original.assigned_agent_id = bob.id
    one.assigned_agent_id = bob.id  # the same person as the original: told once
    two.assigned_agent_id = cat.id
    three.assigned_agent_id = ann.id
    three.status = TicketStatus.RESOLVED  # finished, so not refreshed and its assignee not told
    for t in (original, one, two, three):
        db_session.add(t)
    db_session.commit()
    original_id, two_id = original.id, two.id

    comment_as(client, "me@example.com", original)

    assert [n.ticket_id for n in notifications_for(db_session, "bob@example.com")] == [original_id]
    assert [n.ticket_id for n in notifications_for(db_session, "cat@example.com")] == [two_id]
    assert notifications_for(db_session, "agent@example.com") == []


def set_preference(db_session, email, channel, enabled):
    user = db_session.exec(select(User).where(User.email == email)).one()
    db_session.add(NotificationPreference(user_id=user.id, kind="replied", channel=channel, enabled=enabled))
    db_session.commit()


def test_the_persons_channel_settings_are_respected(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)

    set_preference(db_session, "agent@example.com", "in_app", False)  # bell off, Teams still on
    comment_as(client, "jo@example.com", ticket)
    [row] = notifications_for(db_session, "agent@example.com")
    assert (row.bell, row.teams_wanted) == (False, True) and row.read_at is not None  # the Teams-only row

    set_preference(db_session, "agent@example.com", "teams", False)  # both off: nothing more
    comment_as(client, "jo@example.com", ticket)
    assert len(notifications_for(db_session, "agent@example.com")) == 1


def test_nobody_is_told_about_a_ticket_they_cannot_open(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    other = create_team(db_session, "Network")
    ticket.team_id = other.id  # moved to a team Ann is not in
    db_session.add(ticket)
    db_session.commit()

    comment_as(client, "jo@example.com", ticket)

    assert all_notifications(db_session) == []


def test_record_inbound_notifies_only_when_told_it_is_a_reply(client, db_session):
    from app.messages import record_inbound

    ticket, team, ann_id = assigned_ticket(client, db_session)

    record_inbound(db_session, ticket, sender="Jo", body="the first message")  # how a ticket is created
    db_session.commit()
    assert all_notifications(db_session) == []

    record_inbound(db_session, ticket, sender="Jo", body="a reply", reply_by="jo@example.com")
    db_session.commit()
    assert [n.kind for n in all_notifications(db_session)] == ["replied"]

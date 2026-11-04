"""#130: the bell for everyone. The requester, and external followers with an account, get bell entries about their
own tickets: an agent emailed, waiting on you, resolved, closed, the approval outcome. They open the portal."""

import pytest
from sqlmodel import select

from app.models import Notification, NotificationPreference, Ticket, TicketFollower, TicketStatus, User, UserRole
from app.notification_settings import IN_APP
from app.notifications import (
    AGENT_EMAILED, APPROVAL_OUTCOME, AWAITING_YOU, REQUESTER_KINDS, TICKET_CLOSED, TICKET_RESOLVED, notify,
)
from app.ticket_service import set_status
from tests.conftest import login_as, set_role
from tests.test_approvals import person, raised_and_triaged, request_type
from tests.test_email_preview import follow, ticket_for_email
from tests.test_resolution import resolve


def entries(db_session, email, kind=None):
    db_session.expire_all()
    user = db_session.exec(select(User).where(User.email == email)).one()
    query = select(Notification).where(Notification.user_id == user.id, Notification.kind.in_(REQUESTER_KINDS))
    if kind:
        query = query.where(Notification.kind == kind)
    return db_session.exec(query.order_by(Notification.id)).all()


def kinds(db_session, email):
    return [n.kind for n in entries(db_session, email)]


def with_accounts(client, db_session, *emails):
    for email in emails:
        person(client, db_session, email)


def agent_session(client, db_session):
    client.post("/auth/logout")
    login_as(client, "desk@example.com", "Desk")


def email_them(client, ticket, body="Please restart it", **extra):
    return client.post(f"/tickets/{ticket.number}/email", data={"body": body, "cc_form": "1", **extra})


def test_an_agent_email_that_asks_for_a_reply_gives_one_waiting_on_you_entry(client, db_session):
    with_accounts(client, db_session, "walkin@example.com", "boss@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")

    assert email_them(client, ticket).status_code == 200  # the default status is Awaiting user

    for email in ("walkin@example.com", "boss@example.com"):  # the requester and the follower
        assert kinds(db_session, email) == [AWAITING_YOU]


def test_an_agent_email_that_leaves_the_status_gives_an_emailed_entry_that_opens_the_portal(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    assert email_them(client, ticket, next_status="in_progress").status_code == 200

    [entry] = entries(db_session, "walkin@example.com")
    assert entry.kind == AGENT_EMAILED and entry.bell is True and "emailed you" in entry.text

    client.post("/auth/logout")
    login_as(client, "walkin@example.com")
    assert client.get("/notifications/count").text == "Notifications (1)"
    page = client.get("/notifications").text
    assert f"{ticket.reference} " in page and "emailed you" in page and ">Open<" in page
    response = client.post(f"/notifications/{entry.id}/open", follow_redirects=False)
    assert response.headers["location"] == f"/portal/{ticket.number}"
    assert client.get("/notifications/count").text == "Notifications"


def test_an_email_when_already_awaiting_user_still_tells_them(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    email_them(client, ticket)
    email_them(client, ticket)  # still Awaiting user: no status change, so the second says an agent wrote

    assert kinds(db_session, "walkin@example.com") == [AWAITING_YOU, AGENT_EMAILED]


def test_resolving_gives_one_resolved_entry_not_an_emailed_one(client, db_session):
    with_accounts(client, db_session, "walkin@example.com", "boss@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")

    assert resolve(client, ticket).status_code == 200

    for email in ("walkin@example.com", "boss@example.com"):
        assert kinds(db_session, email) == [TICKET_RESOLVED]


def test_closing_gives_a_closed_entry(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    db_session.expire_all()
    ticket = db_session.get(Ticket, ticket.id)
    set_status(db_session, ticket, TicketStatus.CLOSED, "desk@example.com")
    db_session.commit()

    assert kinds(db_session, "walkin@example.com") == [TICKET_RESOLVED, TICKET_CLOSED]


def test_an_automatic_close_with_no_actor_tells_them(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    set_status(db_session, db_session.get(Ticket, ticket.id), TicketStatus.AWAITING_USER, None)
    db_session.commit()
    assert kinds(db_session, "walkin@example.com") == [AWAITING_YOU]


def test_a_person_without_an_account_gets_nothing_and_nothing_fails(client, db_session):
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "stranger@example.com")
    assert email_them(client, ticket).status_code == 200
    assert db_session.exec(select(Notification).where(Notification.kind.in_(REQUESTER_KINDS))).all() == []


def test_never_the_person_who_caused_it(client, db_session):
    """An agent who raised the ticket for themselves and then resolves it is not told."""
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    ticket.requester_email = "agent@example.com"
    db_session.add(ticket)
    db_session.commit()
    resolve(client, ticket)
    assert entries(db_session, "agent@example.com") == []


def test_an_agent_who_is_the_requester_is_told_and_it_opens_the_portal(client, db_session):
    """Someone else (the system) resolves a ticket an agent raised for themselves: the agent gets the entry, and it
    opens the portal, even though they could not open the ticket as an agent."""
    from tests.conftest import create_team

    other = create_team(db_session, "Elsewhere")
    agent_id = login_as(client, "elsewhere@example.com", "Else")
    set_role(db_session, agent_id, UserRole.AGENT, other.id)
    client.post("/auth/logout")
    login_as(client, "desk@example.com", "Desk")
    ticket = ticket_for_email(client, db_session)
    ticket.requester_email = "elsewhere@example.com"
    db_session.add(ticket)
    db_session.commit()
    resolve(client, ticket)

    [entry] = entries(db_session, "elsewhere@example.com")
    client.post("/auth/logout")
    login_as(client, "elsewhere@example.com")
    assert client.get("/notifications/count").text == "Notifications (1)"
    assert f"{ticket.reference} " in client.get("/notifications").text
    assert client.post(f"/notifications/{entry.id}/open", follow_redirects=False).headers["location"] == f"/portal/{ticket.number}"


@pytest.mark.parametrize("kind", REQUESTER_KINDS)
def test_each_event_can_be_switched_off_in_the_settings(client, db_session, kind):
    with_accounts(client, db_session, "walkin@example.com")
    walkin = db_session.exec(select(User).where(User.email == "walkin@example.com")).one()
    db_session.add(NotificationPreference(user_id=walkin.id, kind=kind, channel=IN_APP, enabled=False))
    db_session.commit()
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    ticket = db_session.get(Ticket, ticket.id)
    assert notify(db_session, walkin, kind, ticket, "x", "someone@example.com") is None


def test_the_settings_page_lists_them_on_by_default_for_an_end_user(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    login_as(client, "walkin@example.com")
    page = client.get("/settings/notifications").text
    for kind in REQUESTER_KINDS:
        assert f'name="{kind}:in_app"' in page or f"{kind}:in_app" in page


def test_the_other_agent_kinds_are_still_refused_for_an_end_user(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    walkin = db_session.exec(select(User).where(User.email == "walkin@example.com")).one()
    assert notify(db_session, walkin, "assigned", None, "x", None) is None
    assert notify(db_session, walkin, "replied", None, "x", None) is None


def test_an_approval_outcome_tells_the_requester_once_and_a_decline_does_not_also_say_closed(client, db_session):
    from app.approvals import DECLINED, decide, current_approval, votes_of

    person(client, db_session, "jo@example.com")
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    [(vote, _)] = votes_of(db_session, current_approval(db_session, ticket))
    assert decide(db_session, vote, DECLINED, "No") == DECLINED
    db_session.commit()

    [entry] = entries(db_session, "jo@example.com")
    assert entry.kind == APPROVAL_OUTCOME and "was declined" in entry.text
    assert entry.actor_email == "boss@example.com"


def test_an_approval_that_passes_tells_the_requester(client, db_session):
    from app.approvals import APPROVED, current_approval, decide, votes_of

    person(client, db_session, "jo@example.com")
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    [(vote, _)] = votes_of(db_session, current_approval(db_session, ticket))
    decide(db_session, vote, APPROVED, "")
    db_session.commit()
    assert [(n.kind, "was approved" in n.text) for n in entries(db_session, "jo@example.com")] == [(APPROVAL_OUTCOME, True)]


def switch_off(db_session, email, kind):
    user = db_session.exec(select(User).where(User.email == email)).one()
    db_session.add(NotificationPreference(user_id=user.id, kind=kind, channel=IN_APP, enabled=False))
    db_session.commit()


def test_with_waiting_on_you_off_an_email_that_awaits_a_reply_still_says_emailed(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    switch_off(db_session, "walkin@example.com", AWAITING_YOU)
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    email_them(client, ticket)
    assert kinds(db_session, "walkin@example.com") == [AGENT_EMAILED]


def test_with_resolved_off_the_resolution_email_still_says_emailed(client, db_session):
    with_accounts(client, db_session, "walkin@example.com")
    switch_off(db_session, "walkin@example.com", TICKET_RESOLVED)
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    assert kinds(db_session, "walkin@example.com") == [AGENT_EMAILED]


def test_a_withdrawal_tells_the_followers_but_not_the_requester(client, db_session):
    from app.approvals import withdraw
    from app.followers import add_follower

    person(client, db_session, "jo@example.com")
    person(client, db_session, "friend@example.com")
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    add_follower(db_session, ticket, "friend@example.com", "jo@example.com")
    withdraw(db_session, ticket, "jo@example.com")
    db_session.commit()
    assert entries(db_session, "jo@example.com") == []
    [entry] = entries(db_session, "friend@example.com")
    assert entry.kind == APPROVAL_OUTCOME and "was withdrawn" in entry.text


def test_a_never_follow_listed_follower_is_not_belled(client, db_session):
    from app.never_follow import add_listed

    with_accounts(client, db_session, "walkin@example.com", "boss@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    add_listed(db_session, "boss@example.com", "shared box", "agent@example.com")
    db_session.commit()
    resolve(client, ticket)
    assert kinds(db_session, "boss@example.com") == []
    assert kinds(db_session, "walkin@example.com") == [TICKET_RESOLVED]


def test_an_entry_about_a_ticket_you_no_longer_follow_is_hidden(client, db_session):
    with_accounts(client, db_session, "walkin@example.com", "boss@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    resolve(client, ticket)
    follower_id = db_session.exec(select(TicketFollower).where(TicketFollower.email == "boss@example.com")).one().id
    assert client.post(f"/tickets/{ticket.number}/followers/{follower_id}/remove").status_code == 200

    client.post("/auth/logout")
    login_as(client, "boss@example.com")
    assert client.get("/notifications/count").text == "Notifications"
    assert "Printer jam" not in client.get("/notifications").text


def test_resolving_the_parts_of_a_split_does_not_also_tell_about_the_original(client, db_session):
    from tests.test_splitting import split, subs_of

    with_accounts(client, db_session, "walkin@example.com")
    agent_session(client, db_session)
    ticket = ticket_for_email(client, db_session)
    assert split(client, ticket, ("Part one", "a"), ("Part two", "b")).status_code in (200, 303)
    for part in subs_of(db_session, ticket):
        assert resolve(client, part).status_code == 200
    db_session.expire_all()
    assert {n.ticket_id for n in entries(db_session, "walkin@example.com")} == {p.id for p in subs_of(db_session, ticket)}

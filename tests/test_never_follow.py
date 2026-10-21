"""The never-follow list (4B-2): listed addresses never become followers, by inbound Cc, by
joining a thread, or by hand; removal allows them again."""

import pytest
from sqlmodel import select

from app import inbound
from app.followers import NeverFollowListed, add_follower, followers_of
from app.inbound import ingest_mail
from app.models import AuditLog, NeverFollowAddress, Ticket, TicketEvent, User, UserRole
from app.never_follow import NeverFollowError, add_listed, is_listed
from tests.conftest import create_team, login_as, set_role
from tests.test_inbound_mail import MAILBOX, mail, open_ticket  # noqa: F401  (MAILBOX also sets the mailbox fixture)
from tests.test_inbound_mail import mailbox  # noqa: F401

LISTED = "all-staff@corp.example"


def make_ticket(db_session, team_id=None, requester="me@example.com"):
    ticket = Ticket(subject="Printer", requester_email=requester, team_id=team_id)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def emails(db_session, ticket):
    db_session.expire_all()
    return sorted(f.email for f in followers_of(db_session, ticket))


# ---- The list itself --------------------------------------------------------------------


def test_the_list_matches_exactly_and_ignores_case(db_session):
    add_listed(db_session, LISTED, "Distribution list", "admin@example.com")
    db_session.commit()
    assert is_listed(db_session, "ALL-Staff@Corp.Example")
    assert not is_listed(db_session, "other@corp.example")
    assert not is_listed(db_session, "x@all-staff.corp.example")  # no domain matching
    with pytest.raises(NeverFollowError):
        add_listed(db_session, LISTED, "", "admin@example.com")


# ---- Enforcement ------------------------------------------------------------------------


def test_add_follower_refuses_a_listed_address_in_any_case(db_session):
    add_listed(db_session, LISTED, "", "admin@example.com")
    ticket = make_ticket(db_session)
    for typed in (LISTED, "  All-Staff@CORP.example "):
        with pytest.raises(NeverFollowListed):
            add_follower(db_session, ticket, typed, "agent@example.com")
    assert emails(db_session, ticket) == []


def test_an_agents_own_address_is_not_checked_when_they_become_an_internal_follower(db_session):
    # Escalation adds the agent as an internal follower; a listed account address must not block it.
    agent = User(email="agent@example.com", display_name="Agent", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.commit()
    add_listed(db_session, "agent@example.com", "", "admin@example.com")
    ticket = make_ticket(db_session)
    assert add_follower(db_session, ticket, "x", "agent@example.com", internal_user=agent) is not None


def test_a_manual_add_by_an_agent_shows_an_error_and_removal_allows_it_again(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    row = add_listed(db_session, LISTED, "", "admin@example.com")
    db_session.commit()

    response = client.post(f"/tickets/{ticket.number}/followers", data={"email": LISTED.upper()})
    assert response.status_code == 400 and "never-follow list" in response.text
    assert emails(db_session, ticket) == []
    assert not db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_added")).all()

    db_session.delete(db_session.get(NeverFollowAddress, row.id))
    db_session.commit()
    assert client.post(f"/tickets/{ticket.number}/followers", data={"email": LISTED}).status_code == 200
    assert emails(db_session, ticket) == [LISTED]


def test_a_requester_cannot_copy_a_listed_address_from_the_portal(client, db_session):
    login_as(client, "me@example.com")
    ticket = make_ticket(db_session, requester="me@example.com")
    add_listed(db_session, LISTED, "", "admin@example.com")
    db_session.commit()
    response = client.post(f"/portal/{ticket.number}/followers", data={"email": LISTED})
    assert response.status_code == 400 and "never-follow list" in response.text
    assert emails(db_session, ticket) == []


def test_a_listed_cc_is_skipped_and_the_others_still_follow(db_session):
    add_listed(db_session, LISTED, "", "admin@example.com")
    add_listed(db_session, "helpdesk-alias@corp.example", "", "admin@example.com")
    db_session.commit()
    result = ingest_mail(
        db_session,
        mail(cc=["ALL-STAFF@corp.example", "pat@example.com", "helpdesk-alias@corp.example", "sam@example.com"]),
    )
    db_session.commit()
    assert result.ticket is not None
    assert emails(db_session, result.ticket) == ["pat@example.com", "sam@example.com"]



def test_a_listed_sender_midway_through_a_thread_does_not_join_it(db_session):
    ticket = open_ticket(db_session, message_id="a")
    add_listed(db_session, LISTED, "", "admin@example.com")
    db_session.commit()
    assert inbound._join_thread(db_session, ticket, LISTED) is False
    assert emails(db_session, ticket) == []


def test_a_cc_is_followed_again_once_the_address_is_removed(db_session):
    row = add_listed(db_session, LISTED, "", "admin@example.com")
    db_session.commit()
    first = ingest_mail(db_session, mail("m1", cc=[LISTED]))
    db_session.delete(row)
    db_session.commit()
    second = ingest_mail(db_session, mail("m2", cc=[LISTED], conversation_id="other"))
    db_session.commit()
    assert emails(db_session, first.ticket) == []
    assert emails(db_session, second.ticket) == [LISTED]


# ---- Admin page -------------------------------------------------------------------------


def test_an_admin_adds_and_removes_an_address_with_audit(client, db_session):
    login_as(client, "admin@example.com")
    response = client.post("/admin/never-follow", data={"email": " All-Staff@Corp.Example ", "reason": "Distribution list"}, follow_redirects=False)
    assert response.status_code == 303
    row = db_session.exec(select(NeverFollowAddress)).one()
    assert (row.email, row.reason, row.added_by) == (LISTED, "Distribution list", "admin@example.com")
    page = client.get("/admin/never-follow")
    assert LISTED in page.text and "Distribution list" in page.text

    assert client.post("/admin/never-follow", data={"email": LISTED.upper()}).status_code == 400  # a repeat
    assert client.post("/admin/never-follow", data={"email": "not-an-address"}).status_code == 400

    assert client.post(f"/admin/never-follow/{row.id}/remove", follow_redirects=False).status_code == 303
    assert client.post(f"/admin/never-follow/{row.id}/remove").status_code == 404
    assert db_session.exec(select(NeverFollowAddress)).all() == []
    actions = [(a.action, a.detail) for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "never_follow").order_by(AuditLog.id)).all()]
    assert actions == [("added", f"{LISTED}: Distribution list"), ("removed", LISTED)]


def test_only_a_super_admin_manages_the_list(client, db_session):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    assert client.get("/admin/never-follow").status_code == 403
    assert client.post("/admin/never-follow", data={"email": LISTED}).status_code == 403
    assert db_session.exec(select(NeverFollowAddress)).all() == []


def test_an_address_listed_after_it_followed_stays_but_is_not_copied(db_session):
    from app.followers import cc_list

    ticket = make_ticket(db_session)
    add_follower(db_session, ticket, LISTED, "agent@example.com")
    add_follower(db_session, ticket, "pat@example.com", "agent@example.com")
    db_session.commit()
    add_listed(db_session, LISTED, "", "admin@example.com")
    db_session.commit()
    assert cc_list(db_session, ticket) == ["pat@example.com"]
    assert emails(db_session, ticket) == [LISTED, "pat@example.com"]
    # Adding it again is a quiet no-op, not an error: it is already following.
    assert add_follower(db_session, ticket, LISTED, "agent@example.com") is None


def test_add_listed_normalises_the_address(db_session):
    row = add_listed(db_session, " All-Staff@Corp.Example ", "", "admin@example.com")
    assert row.email == LISTED

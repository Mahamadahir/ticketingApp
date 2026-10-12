from sqlmodel import select

from app.followers import MAX_EXTERNAL_FOLLOWERS, cc_list, followers_of
from app.models import (
    AuditLog,
    Message,
    Ticket,
    TicketEvent,
    TicketFollower,
    TicketStatus,
    UserRole,
)
from tests.conftest import add_membership, create_team, login_as, set_role

SECRET_NOTE = "Internal only: do not share"


def make_ticket(db_session, team_id=None, status=TicketStatus.OPEN, requester="me@example.com"):
    ticket = Ticket(subject="Printer", requester_email=requester, team_id=team_id, status=status)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_for(client, db_session, team, email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


def follower_emails(db_session, ticket):
    db_session.expire_all()
    return [f.email for f in followers_of(db_session, ticket)]


# ---- Agent side ----------------------------------------------------------------


def test_an_agent_adds_a_follower_with_event_and_audit(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    response = client.post(f"/tickets/{ticket.id}/followers", data={"email": "  Boss@Example.ORG "})
    assert response.status_code == 200

    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    events = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_added")).all()
    assert [(e.to_value, e.actor_email) for e in events] == [("boss@example.org", "agent@example.com")]
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "follower_added"))] == [
        "boss@example.org"
    ]
    assert "boss@example.org" in client.get(f"/tickets/{ticket.id}").text


def test_duplicates_and_the_requesters_own_address_are_ignored(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    agent_for(client, db_session, team)

    for address in ("boss@example.org", "BOSS@example.org", "me@example.com", "ME@example.com"):
        assert client.post(f"/tickets/{ticket.id}/followers", data={"email": address}).status_code == 200

    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    assert len(db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_added")).all()) == 1


def test_bad_addresses_and_too_many_followers_are_rejected(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    for bad in ("not-an-email", "a@b", "two@x.org,three@x.org", "a b@x.org", "<script>@x.org"):
        assert client.post(f"/tickets/{ticket.id}/followers", data={"email": bad}).status_code == 400, bad
    assert follower_emails(db_session, ticket) == []

    for number in range(MAX_EXTERNAL_FOLLOWERS):
        assert client.post(f"/tickets/{ticket.id}/followers", data={"email": f"p{number}@x.org"}).status_code == 200
    assert client.post(f"/tickets/{ticket.id}/followers", data={"email": "one-too-many@x.org"}).status_code == 400
    assert len(follower_emails(db_session, ticket)) == MAX_EXTERNAL_FOLLOWERS


def test_an_agent_removes_a_follower(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.id}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]

    assert client.post(f"/tickets/{ticket.id}/followers/{follower.id}/remove").status_code == 200

    assert follower_emails(db_session, ticket) == []
    removed = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_removed")).all()
    assert [e.from_value for e in removed] == ["boss@example.org"]
    # A follower of another ticket cannot be removed through this one.
    assert client.post(f"/tickets/{ticket.id}/followers/9999/remove").status_code == 404


def test_only_someone_who_works_the_ticket_manages_followers(client, db_session):
    watched = create_team(db_session, "Watched")
    ticket = make_ticket(db_session, watched.id)
    agent_id = login_as(client, "viewer@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    from app.models import MembershipLevel

    add_membership(db_session, agent_id, watched.id, MembershipLevel.VIEWER)

    assert client.post(f"/tickets/{ticket.id}/followers", data={"email": "boss@example.org"}).status_code == 403
    page = client.get(f"/tickets/{ticket.id}").text
    assert "Add follower" not in page
    assert follower_emails(db_session, ticket) == []


# ---- Copying people on email -----------------------------------------------------


def test_an_email_records_who_was_copied_at_send_time(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.id}/followers", data={"email": "boss@example.org"})
    client.post(f"/tickets/{ticket.id}/followers", data={"email": "peer@example.org"})

    assert "Copied to: boss@example.org, peer@example.org" in client.get(f"/tickets/{ticket.id}").text
    client.post(f"/tickets/{ticket.id}/email", data={"body": "First update"})

    first = db_session.exec(select(Message)).one()
    assert first.cc == "boss@example.org, peer@example.org"

    # Removing a follower later does not rewrite who received the earlier email.
    boss = [f for f in followers_of(db_session, ticket) if f.email == "boss@example.org"][0]
    client.post(f"/tickets/{ticket.id}/followers/{boss.id}/remove")
    client.post(f"/tickets/{ticket.id}/email", data={"body": "Second update"})

    db_session.expire_all()
    messages = db_session.exec(select(Message).order_by(Message.id)).all()
    assert [m.cc for m in messages] == ["boss@example.org, peer@example.org", "peer@example.org"]
    assert "cc: boss@example.org, peer@example.org" in client.get(f"/tickets/{ticket.id}").text


def test_an_email_with_no_followers_has_no_cc(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.id}/email", data={"body": "Hello"})

    assert db_session.exec(select(Message)).one().cc is None
    assert "Copied to" not in client.get(f"/tickets/{ticket.id}").text


# ---- The escalator is an internal follower ---------------------------------------


def test_escalating_makes_the_agent_an_internal_follower_once(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id)
    agent_for(client, db_session, mine)

    for _ in range(2):
        client.post(f"/tickets/{ticket.id}/escalate", data={"reason": "Needs network", "target_team_id": network.id})

    followers = followers_of(db_session, ticket)
    assert [(f.email, f.is_internal) for f in followers] == [("agent@example.com", True)]
    assert cc_list(db_session, ticket) == ["agent@example.com"]

    # Read access comes from the follower row now, wherever the ticket went.
    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200 and "read-only access" in page.text
    assert "(internal)" in page.text
    # Internal followers cannot be removed from the ticket page.
    assert client.post(f"/tickets/{ticket.id}/followers/{followers[0].id}/remove").status_code == 403


def test_escalating_works_for_an_account_whose_email_fails_the_typed_address_check(client, db_session):
    """Regression: the dev account test@test has no dot in its domain. Escalating
    re-validated it as a typed-in address and crashed with a 500."""
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team, email="Dotless@Localhost")

    response = client.post(f"/tickets/{ticket.id}/escalate", data={"reason": "Flagging this"})

    assert response.status_code == 200
    assert [(f.email, f.is_internal) for f in followers_of(db_session, ticket)] == [("dotless@localhost", True)]
    # People still cannot type such an address into the follower box.
    assert client.post(f"/tickets/{ticket.id}/followers", data={"email": "typed@localhost"}).status_code == 400


def test_an_internal_follower_cannot_be_removed_even_by_someone_who_works_the_ticket(client, db_session):
    """Escalating without changing team leaves the agent able to work the ticket,
    so only the follower guard itself stands in the way of removing the row."""
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.id}/escalate", data={"reason": "Flagging this"})
    internal = followers_of(db_session, ticket)[0]
    assert internal.is_internal and client.get(f"/tickets/{ticket.id}").status_code == 200
    assert client.post(f"/tickets/{ticket.id}/status", data={"status": "resolved"}).status_code == 200  # works it

    assert client.post(f"/tickets/{ticket.id}/followers/{internal.id}/remove").status_code == 404
    assert follower_emails(db_session, ticket) == ["agent@example.com"]


def test_removing_the_follower_row_removes_the_escalators_access(client, db_session):
    """Access really does come from the follower row, not the old escalated_by_id."""
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id)
    agent_for(client, db_session, mine)
    client.post(f"/tickets/{ticket.id}/escalate", data={"reason": "x", "target_team_id": network.id})
    assert client.get(f"/tickets/{ticket.id}").status_code == 200

    for follower in db_session.exec(select(TicketFollower)).all():
        db_session.delete(follower)
    db_session.commit()

    db_session.refresh(ticket)
    assert ticket.escalated_by_id is not None  # still recorded as history
    assert client.get(f"/tickets/{ticket.id}").status_code == 403


def test_an_external_follower_who_is_an_agent_gets_no_agent_access(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    ticket = make_ticket(db_session, other.id)
    agent_for(client, db_session, mine, email="colleague@example.com")
    client.post("/auth/logout")
    # Someone adds the agent's address as an ordinary (external) follower.
    db_session.add(
        TicketFollower(ticket_id=ticket.id, email="colleague@example.com", added_by_email="x@example.com")
    )
    db_session.commit()
    login_as(client, "colleague@example.com")

    assert client.get(f"/tickets/{ticket.id}").status_code == 403


# ---- Portal --------------------------------------------------------------------


def test_the_requester_adds_and_removes_followers(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.id}/followers", data={"email": "Boss@Example.org"}).status_code == 200
    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    assert "boss@example.org" in client.get(f"/portal/{ticket.id}").text
    assert client.post(f"/portal/{ticket.id}/followers", data={"email": "nope"}).status_code == 400

    follower = followers_of(db_session, ticket)[0]
    assert client.post(f"/portal/{ticket.id}/followers/{follower.id}/remove").status_code == 200
    assert follower_emails(db_session, ticket) == []


def test_a_follower_with_an_account_reads_the_ticket_but_cannot_change_it(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.id}/note", data={"body": SECRET_NOTE})
    client.post(f"/tickets/{ticket.id}/email", data={"body": "We are on it"})
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.id}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    listing = client.get("/portal").text
    assert "Following" in listing and ticket.reference in listing

    page = client.get(f"/portal/{ticket.id}")
    assert page.status_code == 200
    assert "We are on it" in page.text
    assert SECRET_NOTE not in page.text and "Internal note" not in page.text
    assert "You are following this request" in page.text and "Add comment" not in page.text

    assert client.post(f"/portal/{ticket.id}/comment", data={"body": "hello"}).status_code == 404
    assert client.post(f"/portal/{ticket.id}/followers", data={"email": "x@example.org"}).status_code == 404
    assert client.post(f"/portal/{ticket.id}/followers/{follower.id}/remove").status_code == 404
    assert client.get(f"/tickets/{ticket.id}").status_code == 403  # no agent console either


def test_removing_a_follower_removes_their_portal_access(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.id}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    assert client.get(f"/portal/{ticket.id}").status_code == 200
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.id}/followers/{follower.id}/remove")
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    assert client.get(f"/portal/{ticket.id}").status_code == 404
    assert "Following" not in client.get("/portal").text


def test_strangers_and_the_requesters_own_tickets_are_not_in_following(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "stranger@example.org")
    assert client.get(f"/portal/{ticket.id}").status_code == 404
    assert "Following" not in client.get("/portal").text
    client.post("/auth/logout")

    # A follower row for the requester's own address is never created, and if one
    # exists it does not make their own ticket appear twice.
    db_session.add(TicketFollower(ticket_id=ticket.id, email="me@example.com", added_by_email="x"))
    db_session.commit()
    login_as(client, "me@example.com")
    listing = client.get("/portal").text
    assert "Following" not in listing and listing.count(ticket.reference) == 1


def test_internal_followers_are_not_shown_to_the_requester(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id, requester="me@example.com")
    agent_for(client, db_session, mine, email="secret-agent@example.com")
    client.post(f"/tickets/{ticket.id}/escalate", data={"reason": "x", "target_team_id": network.id})
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    assert "secret-agent@example.com" not in client.get(f"/portal/{ticket.id}").text

from sqlmodel import select

from app.followers import MAX_EXTERNAL_FOLLOWERS, cc_list, followers_of
from app.models import (
    AuditLog,
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketEvent,
    TicketFollower,
    TicketStatus,
    UserRole,
)
from tests.conftest import escalatable, add_membership, create_team, login_as, set_role

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

    response = client.post(f"/tickets/{ticket.number}/followers", data={"email": "  Boss@Example.ORG "})
    assert response.status_code == 200

    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    events = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_added")).all()
    assert [(e.to_value, e.actor_email) for e in events] == [("boss@example.org", "agent@example.com")]
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "follower_added"))] == [
        "boss@example.org"
    ]
    assert "boss@example.org" in client.get(f"/tickets/{ticket.number}").text


def test_duplicates_and_the_requesters_own_address_are_ignored(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    agent_for(client, db_session, team)

    for address in ("boss@example.org", "BOSS@example.org", "me@example.com", "ME@example.com"):
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": address}).status_code == 200

    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    assert len(db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_added")).all()) == 1


def test_bad_addresses_and_too_many_followers_are_rejected(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    for bad in ("not-an-email", "a@b", "two@x.org,three@x.org", "a b@x.org", "<script>@x.org"):
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": bad}).status_code == 400, bad
    assert follower_emails(db_session, ticket) == []

    for number in range(MAX_EXTERNAL_FOLLOWERS):
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": f"p{number}@x.org"}).status_code == 200
    assert client.post(f"/tickets/{ticket.number}/followers", data={"email": "one-too-many@x.org"}).status_code == 400
    assert len(follower_emails(db_session, ticket)) == MAX_EXTERNAL_FOLLOWERS


def test_an_agent_removes_a_follower(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]

    assert client.post(f"/tickets/{ticket.number}/followers/{follower.id}/remove").status_code == 200

    assert follower_emails(db_session, ticket) == []
    removed = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "follower_removed")).all()
    assert [e.from_value for e in removed] == ["boss@example.org"]
    # A follower of another ticket cannot be removed through this one.
    assert client.post(f"/tickets/{ticket.number}/followers/9999/remove").status_code == 404


def test_only_someone_who_works_the_ticket_manages_followers(client, db_session):
    watched = create_team(db_session, "Watched")
    ticket = make_ticket(db_session, watched.id)
    agent_id = login_as(client, "viewer@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    from app.models import MembershipLevel

    add_membership(db_session, agent_id, watched.id, MembershipLevel.VIEWER)

    assert client.post(f"/tickets/{ticket.number}/followers", data={"email": "boss@example.org"}).status_code == 403
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Add follower" not in page
    assert follower_emails(db_session, ticket) == []


# ---- Copying people on email -----------------------------------------------------


def test_an_email_records_who_was_copied_at_send_time(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/followers", data={"email": "boss@example.org"})
    client.post(f"/tickets/{ticket.number}/followers", data={"email": "peer@example.org"})

    page = client.get(f"/tickets/{ticket.number}").text
    for address in ("boss@example.org", "peer@example.org"):
        assert f'name="cc" value="{address}" checked' in page
    client.post(f"/tickets/{ticket.number}/email", data={"body": "First update"})

    first = db_session.exec(select(Message)).one()
    assert first.cc == "boss@example.org, peer@example.org"

    # Removing a follower later does not rewrite who received the earlier email.
    boss = [f for f in followers_of(db_session, ticket) if f.email == "boss@example.org"][0]
    client.post(f"/tickets/{ticket.number}/followers/{boss.id}/remove")
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Second update"})

    db_session.expire_all()
    messages = db_session.exec(select(Message).order_by(Message.id)).all()
    assert [m.cc for m in messages] == ["boss@example.org, peer@example.org", "peer@example.org"]
    assert "cc: boss@example.org, peer@example.org" in client.get(f"/tickets/{ticket.number}").text


def test_an_email_with_no_followers_has_no_cc(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"})

    assert db_session.exec(select(Message)).one().cc is None
    assert "Copy this email to" not in client.get(f"/tickets/{ticket.number}").text


# ---- The escalator is an internal follower ---------------------------------------


def test_escalating_makes_the_agent_an_internal_follower_once(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id)
    agent_for(client, db_session, mine)

    escalatable(db_session, ticket, network)
    assert client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Needs network"}).status_code == 200
    # Already with the target team, so the next step would be the MSP: refused, no second row.
    assert client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Needs network"}).status_code == 400

    followers = followers_of(db_session, ticket)
    assert [(f.email, f.is_internal) for f in followers] == [("agent@example.com", True)]
    assert cc_list(db_session, ticket) == ["agent@example.com"]

    # The escalator keeps working the ticket they escalated, so they may also manage its followers.
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert client.post(f"/tickets/{ticket.number}/followers/{followers[0].id}/remove").status_code == 200


def test_escalating_works_for_an_account_whose_email_fails_the_typed_address_check(client, db_session):
    """Regression: the dev account test@test has no dot in its domain. Escalating
    re-validated it as a typed-in address and crashed with a 500."""
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team, email="Dotless@Localhost")

    escalatable(db_session, ticket)
    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Flagging this"})

    assert response.status_code == 200
    assert [(f.email, f.is_internal) for f in followers_of(db_session, ticket)] == [("dotless@localhost", True)]
    # People still cannot type such an address into the follower box.
    assert client.post(f"/tickets/{ticket.number}/followers", data={"email": "typed@localhost"}).status_code == 400


def test_an_internal_follower_can_be_removed_by_someone_who_works_the_ticket(client, db_session):
    """The escalator is a follower, but their access never depended on it, so whoever
    works the ticket may remove them like any other follower."""
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    escalatable(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Flagging this"})
    internal = followers_of(db_session, ticket)[0]
    assert internal.is_internal and client.get(f"/tickets/{ticket.number}").status_code == 200

    assert client.post(f"/tickets/{ticket.number}/followers/{internal.id}/remove").status_code == 200
    assert follower_emails(db_session, ticket) == []


def test_the_escalators_access_comes_from_escalating_not_from_the_follower_row(client, db_session):
    """Following grants nothing: delete the follower row and the escalator still works the
    ticket they escalated, while an agent who is only a follower has no access."""
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id)
    agent_for(client, db_session, mine)
    escalatable(db_session, ticket, network)
    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})
    assert response.status_code == 200 and "Network" in response.text
    assert client.get(f"/tickets/{ticket.number}").status_code == 200

    for follower in db_session.exec(select(TicketFollower)).all():
        db_session.delete(follower)
    db_session.commit()
    assert client.get(f"/tickets/{ticket.number}").status_code == 200  # still theirs to open

    other = make_ticket(db_session, network.id)  # their escalation opened no other Network ticket
    assert client.get(f"/tickets/{other.number}").status_code == 403


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

    assert client.get(f"/tickets/{ticket.number}").status_code == 403


# ---- Portal --------------------------------------------------------------------


def test_the_requester_adds_and_removes_followers(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.number}/followers", data={"email": "Boss@Example.org"}).status_code == 200
    assert follower_emails(db_session, ticket) == ["boss@example.org"]
    assert "boss@example.org" in client.get(f"/portal/{ticket.number}").text
    assert client.post(f"/portal/{ticket.number}/followers", data={"email": "nope"}).status_code == 400

    follower = followers_of(db_session, ticket)[0]
    assert client.post(f"/portal/{ticket.number}/followers/{follower.id}/remove").status_code == 200
    assert follower_emails(db_session, ticket) == []


def test_a_follower_with_an_account_can_read_and_reply_but_not_manage_the_ticket(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": SECRET_NOTE})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "We are on it"})
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    listing = client.get("/portal").text
    assert "(following)" in listing and ticket.reference in listing

    page = client.get(f"/portal/{ticket.number}")
    assert page.status_code == 200
    assert "We are on it" in page.text
    assert SECRET_NOTE not in page.text and "Internal note" not in page.text
    assert "Add comment" in page.text  # followers are not read-only
    assert "Also keeping up to date" not in page.text  # but they do not manage the follower list

    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Thanks, that fixed it"}).status_code == 200
    reply = db_session.exec(select(Message).where(Message.body == "Thanks, that fixed it")).one()
    assert (reply.direction, reply.visibility, reply.sender) == (
        MessageDirection.INBOUND,
        MessageVisibility.PUBLIC,
        "Test User",
    )
    assert client.post(f"/portal/{ticket.number}/followers", data={"email": "x@example.org"}).status_code == 404
    assert client.post(f"/portal/{ticket.number}/followers/{follower.id}/remove").status_code == 404
    assert client.get(f"/tickets/{ticket.number}").status_code == 403  # no agent console either


def test_removing_a_follower_removes_their_portal_access(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    assert client.get(f"/portal/{ticket.number}").status_code == 200
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers/{follower.id}/remove")
    client.post("/auth/logout")

    login_as(client, "boss@example.org")
    assert client.get(f"/portal/{ticket.number}").status_code == 404
    assert "(following)" not in client.get("/portal").text


def test_strangers_and_the_requesters_own_tickets_are_not_in_following(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "stranger@example.org")
    assert client.get(f"/portal/{ticket.number}").status_code == 404
    assert "(following)" not in client.get("/portal").text
    client.post("/auth/logout")

    # A follower row for the requester's own address is never created, and if one
    # exists it does not make their own ticket appear twice.
    db_session.add(TicketFollower(ticket_id=ticket.id, email="me@example.com", added_by_email="x"))
    db_session.commit()
    login_as(client, "me@example.com")
    listing = client.get("/portal").text
    assert "(following)" not in listing and listing.count(ticket.reference) == 1


def test_internal_followers_are_not_shown_to_the_requester(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id, requester="me@example.com")
    agent_for(client, db_session, mine, email="secret-agent@example.com")
    escalatable(db_session, ticket, network)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    assert "secret-agent@example.com" not in client.get(f"/portal/{ticket.number}").text


# ---- Review fixes -----------------------------------------------------------------


def test_an_agent_who_raised_the_ticket_is_still_recorded_as_a_follower_after_escalating_it_away(client, db_session):
    """Regression: the requester shortcut in add_follower also skipped the internal
    follower row. The row no longer grants access (that follows the team)."""
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id, requester="agent@example.com")
    agent_for(client, db_session, mine)

    escalatable(db_session, ticket, network)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})

    assert [(f.email, f.is_internal) for f in followers_of(db_session, ticket)] == [("agent@example.com", True)]
    # They are the recipient of the emails, so they are never also copied on them.
    assert cc_list(db_session, ticket) == []


def test_promoting_an_existing_follower_to_internal_is_recorded_and_keeps_portal_access(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    agent_for(client, db_session, team, email="colleague@example.com")
    db_session.add(TicketFollower(ticket_id=ticket.id, email="colleague@example.com", added_by_email="me@example.com"))
    db_session.commit()
    assert ticket.reference in client.get("/portal").text

    escalatable(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})

    followers = followers_of(db_session, ticket)
    assert [(f.email, f.is_internal) for f in followers] == [("colleague@example.com", True)]
    promoted = db_session.exec(
        select(TicketEvent).where(TicketEvent.event_type == "follower_added", TicketEvent.detail == "internal")
    ).all()
    assert len(promoted) == 1
    assert ticket.reference in client.get("/portal").text  # still listed as following


def test_a_follower_cannot_see_who_else_is_copied(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")
    for address in ("alice@example.org", "bob@example.org"):
        client.post(f"/portal/{ticket.number}/followers", data={"email": address})
    assert "bob@example.org" in client.get(f"/portal/{ticket.number}").text  # the requester sees the list
    client.post("/auth/logout")

    login_as(client, "alice@example.org")
    page = client.get(f"/portal/{ticket.number}").text
    assert "bob@example.org" not in page and "alice@example.org" not in page
    assert "Also keeping up to date" not in page


def test_adding_the_same_follower_at_the_same_time_is_a_quiet_no_op(db_session):
    """Two people (or a double click) adding the same address must not produce an
    error; one row results."""
    import threading

    from sqlmodel import Session

    from app.database import engine
    from app.followers import add_follower

    ticket = make_ticket(db_session)
    errors, barrier = [], threading.Barrier(6)

    def add():
        try:
            with Session(engine) as session:
                fresh = session.get(Ticket, ticket.id)
                barrier.wait()
                add_follower(session, fresh, "boss@example.org", "x@example.com")
                session.commit()
        except Exception as error:  # noqa: BLE001 - the point is that nothing escapes
            errors.append(error)

    threads = [threading.Thread(target=add) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert follower_emails(db_session, ticket) == ["boss@example.org"]


def test_malformed_addresses_are_rejected_and_good_ones_accepted(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    for bad in ("a@b..c", "a@.b.c", "a@b.c.", ".a@x.org", "a.@x.org", "a..b@x.org", "a@-b.org", "a@b-.org", "a@b.c1", "a@b"):
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": bad}).status_code == 400, bad
    assert follower_emails(db_session, ticket) == []

    for good in ("first.last+tag@sub.example.co.uk", "o'brien@example.org", "a@b.co"):
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": good}).status_code == 200, good
    assert len(follower_emails(db_session, ticket)) == 3


def test_removing_a_follower_is_audited_by_address_not_id(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/followers", data={"email": "boss@example.org"})
    follower = followers_of(db_session, ticket)[0]

    client.post(f"/tickets/{ticket.number}/followers/{follower.id}/remove")

    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "follower_removed")).all()
    assert [entry.detail for entry in audit] == ["boss@example.org"]


# ---- Followers respond, and the agent chooses who is copied --------------------------


def test_a_follower_reply_reaches_the_agents_labelled_as_from_a_follower(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, requester="me@example.com")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    client.post(f"/portal/{ticket.number}/comment", data={"body": "From the requester"})
    client.post("/auth/logout")
    login_as(client, "boss@example.org")
    before = db_session.get(Ticket, ticket.id).updated_at
    client.post(f"/portal/{ticket.number}/comment", data={"body": "From the boss"})
    client.post("/auth/logout")

    agent_for(client, db_session, team)
    page = client.get(f"/tickets/{ticket.number}").text
    assert page.index("From the requester") < page.index("From the boss")
    # The label is the kind chip of the message card the body sits in.
    requester_part = page.split("From the requester")[0].rsplit('<span class="chip', 1)[1].split(">", 1)[1].strip()
    follower_part = page.split("From the boss")[0].rsplit('<span class="chip', 1)[1].split(">", 1)[1].strip()
    assert requester_part.startswith("Portal reply<") and follower_part.startswith("Portal reply from follower")
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).updated_at > before


def test_a_follower_reply_is_shown_to_the_requester_and_cannot_be_forged_by_a_stranger(client, db_session):
    ticket = make_ticket(db_session, requester="me@example.com")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    client.post("/auth/logout")
    login_as(client, "boss@example.org")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Boss here"})
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    assert "Boss here" in client.get(f"/portal/{ticket.number}").text
    client.post("/auth/logout")

    login_as(client, "stranger@example.org")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Let me in"}).status_code == 404
    assert [m.body for m in db_session.exec(select(Message))] == ["Boss here"]


def test_an_internal_follower_cannot_comment_from_the_portal(client, db_session):
    mine, network = create_team(db_session, "Mine"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, mine.id, requester="me@example.com")
    agent_for(client, db_session, mine, email="escalator@example.com")
    escalatable(db_session, ticket, network)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})

    page = client.get(f"/portal/{ticket.number}")
    assert page.status_code == 200 and "Add comment" not in page.text
    assert "Reply from the agent console" in page.text
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "hello"}).status_code == 404


def emailed(client, ticket, **data):
    return client.post(f"/tickets/{ticket.number}/email", data={"body": "An update", **data})


def last_cc(db_session):
    db_session.expire_all()
    return db_session.exec(select(Message).order_by(Message.id.desc())).first().cc


def ticket_with_two_followers(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    for address in ("boss@example.org", "peer@example.org"):
        client.post(f"/tickets/{ticket.number}/followers", data={"email": address})
    return ticket


def test_the_agent_can_leave_a_follower_off_one_email_without_removing_them(client, db_session):
    ticket = ticket_with_two_followers(client, db_session)

    assert emailed(client, ticket, cc_form="1", cc=["peer@example.org"]).status_code == 200

    assert last_cc(db_session) == "peer@example.org"
    assert follower_emails(db_session, ticket) == ["boss@example.org", "peer@example.org"]  # still followers
    emailed(client, ticket)  # no choice made: everyone is copied again
    assert last_cc(db_session) == "boss@example.org, peer@example.org"


def test_ticking_nobody_copies_nobody_and_a_request_without_the_form_copies_everybody(client, db_session):
    ticket = ticket_with_two_followers(client, db_session)

    emailed(client, ticket, cc_form="1")  # the form was shown and every box unticked
    assert last_cc(db_session) is None
    emailed(client, ticket)  # not from the form at all
    assert last_cc(db_session) == "boss@example.org, peer@example.org"


def test_only_followers_can_be_copied_and_a_refusal_sends_nothing(client, db_session):
    ticket = ticket_with_two_followers(client, db_session)

    response = emailed(client, ticket, cc_form="1", cc=["boss@example.org", "outsider@example.org"])

    assert response.status_code == 400 and "outsider@example.org" in response.text
    assert db_session.exec(select(Message)).all() == []
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN  # the refused email did not start the work


def test_an_internal_follower_is_always_copied(client, db_session):
    mine = create_team(db_session, "Mine")
    ticket = make_ticket(db_session, mine.id)
    agent_for(client, db_session, mine, email="escalator@example.com")
    escalatable(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Flagging this"})
    client.post(f"/tickets/{ticket.number}/followers", data={"email": "boss@example.org"})

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'value="escalator@example.com" checked disabled' in page and "always copied" in page

    emailed(client, ticket, cc_form="1")  # nothing ticked, and the disabled box is never sent
    assert last_cc(db_session) == "escalator@example.com"
    emailed(client, ticket, cc_form="1", cc=["boss@example.org"])
    assert last_cc(db_session) == "escalator@example.com, boss@example.org"


def test_an_agent_who_is_also_the_requester_is_not_offered_or_copied(client, db_session):
    mine = create_team(db_session, "Mine")
    ticket = make_ticket(db_session, mine.id, requester="agent@example.com")
    agent_for(client, db_session, mine)
    escalatable(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})  # becomes an internal follower of own ticket

    assert "Copy this email to" not in client.get(f"/tickets/{ticket.number}").text
    emailed(client, ticket)
    assert last_cc(db_session) is None

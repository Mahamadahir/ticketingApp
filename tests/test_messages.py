from pathlib import Path

from sqlmodel import select

from app.messages import agent_timeline, requester_visible_messages
from app.models import (
    AuditLog,
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketStatus,
    UserRole,
)
from tests.conftest import create_category, create_team, login_as, set_role

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_NOTE = "Internal only: the user is a contractor, be careful"


def make_ticket(db_session, team_id=None, status=TicketStatus.OPEN, requester="me@example.com"):
    ticket = Ticket(subject="Printer", requester_email=requester, team_id=team_id, status=status)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_for(client, db_session, team):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


# ---- Notes -------------------------------------------------------------------


def test_a_note_is_internal_and_does_not_change_the_ticket(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status=TicketStatus.NEW)
    agent_id = agent_for(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/note", data={"body": SECRET_NOTE}).status_code == 200

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.NEW  # a note does not triage
    note = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).one()
    assert note.direction == MessageDirection.INTERNAL
    assert note.visibility == MessageVisibility.AGENTS_ONLY
    assert note.author_user_id == agent_id
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "note_added")).all()
    assert len(audit) == 1


def test_a_note_never_reaches_the_requester(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": SECRET_NOTE})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "We are looking into it"})
    client.post("/auth/logout")

    login_as(client, "me@example.com")
    portal_page = client.get(f"/portal/{ticket.number}").text
    assert "We are looking into it" in portal_page
    assert SECRET_NOTE not in portal_page and "Internal note" not in portal_page

    # Still hidden after the requester writes back, which re-renders the page.
    after_comment = client.post(f"/portal/{ticket.number}/comment", data={"body": "Thanks"}).text
    assert SECRET_NOTE not in after_comment


def test_requester_visible_messages_returns_public_only_for_every_combination(db_session):
    ticket = make_ticket(db_session)
    for direction in MessageDirection:
        for visibility in MessageVisibility:
            db_session.add(
                Message(
                    ticket_id=ticket.id,
                    sender="x",
                    body=f"{direction.value}/{visibility.value}",
                    direction=direction,
                    visibility=visibility,
                )
            )
    db_session.commit()

    visible = requester_visible_messages(db_session, ticket)
    assert visible and all(m.visibility == MessageVisibility.PUBLIC for m in visible)
    # One public row per direction, except that an internal message is never
    # shown even when it is wrongly marked public (belt and braces).
    assert {m.direction for m in visible} == set(MessageDirection) - {MessageDirection.INTERNAL}


def test_a_note_wrongly_marked_public_still_never_reaches_the_requester(db_session):
    ticket = make_ticket(db_session)
    db_session.add(
        Message(
            ticket_id=ticket.id,
            sender="agent",
            body=SECRET_NOTE,
            direction=MessageDirection.INTERNAL,
            visibility=MessageVisibility.PUBLIC,  # the bad row
        )
    )
    db_session.commit()

    assert requester_visible_messages(db_session, ticket) == []


def test_an_internal_message_cannot_be_recorded_as_public(db_session):
    from app.messages import _record

    ticket = make_ticket(db_session)
    try:
        _record(
            db_session, ticket, sender="x", body="x",
            direction=MessageDirection.INTERNAL, visibility=MessageVisibility.PUBLIC,
        )
    except ValueError:
        return
    raise AssertionError("an internal message was accepted as public")


def test_triaged_and_untriaged_are_one_rule(db_session):
    """Emailing and access control share one definition of untriaged: nobody has
    triaged it yet. A ticket created past New counts as triaged, so the two can
    never disagree."""
    from app.messages import is_triaged
    from app.visibility import is_untriaged

    for team_id in (None, create_team(db_session, "Service Desk").id):
        for status in TicketStatus:
            ticket = make_ticket(db_session, team_id, status=status)
            assert is_triaged(ticket) == (not is_untriaged(ticket)) == (status != TicketStatus.NEW)


def test_a_portal_comment_is_recorded_as_public_inbound(client, db_session):
    ticket = make_ticket(db_session)
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Still broken"})

    message = db_session.exec(select(Message)).one()
    assert (message.direction, message.visibility) == (MessageDirection.INBOUND, MessageVisibility.PUBLIC)


def test_portal_code_only_reads_messages_through_the_safe_function():
    """The portal must not touch ticket.messages (every message, notes included).
    This catches someone reintroducing the shortcut in a route or template."""
    guarded = [REPO_ROOT / "app" / "routers" / "portal.py", *sorted((REPO_ROOT / "app" / "templates").glob("portal_*.html"))]
    for path in guarded:
        assert "ticket.messages" not in path.read_text(encoding="utf-8"), path.name


def test_viewers_and_end_users_cannot_add_notes(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    login_as(client, "me@example.com")  # the requester, an end user

    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "x"}).status_code == 403
    assert db_session.exec(select(Message)).all() == []


# ---- Email user ----------------------------------------------------------------


def test_email_user_is_unavailable_until_the_ticket_is_triaged(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status=TicketStatus.NEW)
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Email user" not in page and "Triage this ticket" in page
    assert "Add note" in page

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"})
    assert response.status_code == 400
    assert db_session.exec(select(Message)).all() == []

    # Triage is what makes it emailable; the status buttons cannot do it.
    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "open"}).status_code == 400
    category = create_category(db_session, team)
    triage = client.post(
        f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id}
    )
    assert triage.status_code == 200
    assert "Email user" in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"}).status_code == 200


def test_an_email_is_public_outbound_and_shown_to_the_requester(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "Have you tried rebooting?"})

    message = db_session.exec(select(Message)).one()
    assert message.direction == MessageDirection.OUTBOUND
    assert message.visibility == MessageVisibility.PUBLIC
    assert [m.id for m in requester_visible_messages(db_session, ticket)] == [message.id]


# ---- Timeline --------------------------------------------------------------------


def test_the_timeline_interleaves_messages_notes_and_events_in_order(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status=TicketStatus.NEW)
    agent_for(client, db_session, team)

    category = create_category(db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "First, a note"})
    client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Then an email"})
    client.post(f"/portal/{ticket.number}/comment", data={"body": "ignored: wrong user"})  # 404, adds nothing

    items = agent_timeline(db_session, ticket)
    summary = [
        (item.message.direction.value if item.message else item.event.event_type) for item in items
    ]
    assert summary == ["internal", "triaged", "status_changed", "outbound", "status_changed"]
    assert [item.at for item in items] == sorted(item.at for item in items)

    page = client.get(f"/tickets/{ticket.number}").text
    assert page.index("First, a note") < page.index("Then an email")
    assert "Internal note" in page and "Email to requester" in page and "status changed" in page


def test_an_inbound_portal_comment_appears_in_the_agent_timeline(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Still broken"})
    client.post("/auth/logout")
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "From requester" in page and "Still broken" in page

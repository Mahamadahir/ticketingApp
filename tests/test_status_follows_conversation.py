"""Status follows the conversation: emailing the user puts the ticket in Awaiting
user by itself, and a reply from the requester or a follower hands it back."""

import pytest
from sqlmodel import select

from app.models import AuditLog, Message, Ticket, TicketEvent, TicketStatus, UserRole
from tests.conftest import create_team, login_as, set_role


def make_ticket(db_session, team_id, status=TicketStatus.OPEN, requester="me@example.com"):
    ticket = Ticket(subject="Printer", requester_email=requester, team_id=team_id, status=status)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def agent_for(client, db_session, team):
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)


def status_of(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id).status


def status_events(db_session, ticket):
    db_session.expire_all()
    return [
        (e.from_value, e.to_value, e.actor_email)
        for e in db_session.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "status_changed")
        )
    ]


@pytest.mark.parametrize("start", [TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
def test_emailing_the_user_puts_the_ticket_in_awaiting_user_with_no_choice(client, db_session, start):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, start)
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "After sending" not in page  # there is nothing for the agent to choose or tick

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer is it?"}).status_code == 200

    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER
    assert status_events(db_session, ticket) == [(start.value, "awaiting_user", "agent@example.com")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [a.detail for a in audit] == [f"{start.value} -> awaiting_user"]


@pytest.mark.parametrize("status", [TicketStatus.WITH_SUPPLIER, TicketStatus.WITH_MSP, TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_emailing_leaves_statuses_that_are_not_waiting_on_us_alone(client, db_session, status):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status)
    agent_for(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "An update"}).status_code == 200

    assert status_of(db_session, ticket) == status
    assert status_events(db_session, ticket) == []


def test_a_second_email_while_awaiting_the_user_changes_nothing_more(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "First"})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Second"})

    assert len(status_events(db_session, ticket)) == 1


def test_a_refused_email_does_not_change_the_status(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    refused = client.post(
        f"/tickets/{ticket.number}/email", data={"body": "x", "cc_form": "1", "cc": "stranger@example.org"}
    )

    assert refused.status_code == 400
    assert status_of(db_session, ticket) == TicketStatus.OPEN


def test_the_requester_replying_hands_the_ticket_back(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_USER)
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "It is the one on floor 2"}).status_code == 200

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert status_events(db_session, ticket) == [("awaiting_user", "in_progress", "me@example.com")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [(a.actor_email, a.detail) for a in audit] == [("me@example.com", "awaiting_user -> in_progress")]


def test_a_follower_replying_also_hands_it_back(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_USER)
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/followers", data={"email": "boss@example.org"})
    client.post("/auth/logout")
    login_as(client, "boss@example.org")

    client.post(f"/portal/{ticket.number}/comment", data={"body": "Boss here, it is floor 2"})

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert status_events(db_session, ticket) == [("awaiting_user", "in_progress", "boss@example.org")]


@pytest.mark.parametrize(
    "status",
    [TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WITH_SUPPLIER, TicketStatus.WITH_MSP, TicketStatus.RESOLVED, TicketStatus.CLOSED],
)
def test_a_reply_only_changes_a_ticket_that_is_awaiting_the_user(client, db_session, status):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status)
    login_as(client, "me@example.com")

    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Hello?"}).status_code == 200

    assert status_of(db_session, ticket) == status
    assert status_events(db_session, ticket) == []


def test_the_whole_conversation_moves_the_status_back_and_forth(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer?"})
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Floor 2"})
    client.post("/auth/logout")
    login_as(client, "agent@example.com")  # the same agent, already in the team
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Thanks, trying a fix"})

    assert [(f, t) for f, t, _ in status_events(db_session, ticket)] == [
        ("open", "awaiting_user"),
        ("awaiting_user", "in_progress"),
        ("in_progress", "awaiting_user"),
    ]
    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER


def test_an_agent_can_still_set_the_status_by_hand(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Which printer?"})

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS

# ---- The agent's explicit choice (4B-5) -----------------------------------------------------------

OFFERED = ["open", "in_progress", "awaiting_user", "with_supplier", "with_msp"]


def select_options(page):
    """One (offered values, selected values) pair per next_status dropdown on the page."""
    import re

    out = []
    for block in re.findall(r'<select name="next_status">(.*?)</select>', page, re.S):
        values = re.findall(r'<option value="([a-z_]*)"( selected)?', block)
        out.append(([v for v, _ in values], [v for v, chosen in values if chosen]))
    return out


def test_both_forms_offer_the_same_five_statuses_and_never_resolved_or_closed(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.IN_PROGRESS)
    agent_for(client, db_session, team)

    email_box, note_box = select_options(client.get(f"/tickets/{ticket.number}").text)

    # The first option sends nothing: the server applies the default when the form arrives, so a stale
    # page cannot overwrite a status that changed in between.
    assert email_box[0] == [""] + OFFERED and note_box[0] == [""] + OFFERED
    assert email_box[1] == [""] and note_box[1] == [""]
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Default: awaiting user" in page and "No change (in progress)" in page


@pytest.mark.parametrize("chosen", OFFERED)
def test_the_chosen_status_is_what_an_email_sets(client, db_session, chosen):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.OPEN)
    agent_for(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Update", "next_status": chosen}).status_code == 200

    assert status_of(db_session, ticket) == TicketStatus(chosen)
    expected = [] if chosen == "open" else [("open", chosen, "agent@example.com")]
    assert status_events(db_session, ticket) == expected


def test_an_email_can_keep_in_progress_instead_of_awaiting_the_user(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.IN_PROGRESS)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/email", data={"body": "Working on it", "next_status": "in_progress"})

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS
    assert status_events(db_session, ticket) == []


def test_emailing_a_ticket_with_the_supplier_defaults_to_leaving_it_there(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.WITH_SUPPLIER)
    agent_for(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Default: with supplier" in page
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Update"})
    assert status_of(db_session, ticket) == TicketStatus.WITH_SUPPLIER


@pytest.mark.parametrize("bad", ["resolved", "closed", "new", "awaiting_approval", "nonsense"])
def test_resolved_closed_and_the_rest_are_refused_from_an_email_or_a_note(client, db_session, bad):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    email = client.post(f"/tickets/{ticket.number}/email", data={"body": "Done", "next_status": bad})
    note = client.post(f"/tickets/{ticket.number}/note", data={"body": "Done", "next_status": bad})
    preview = client.post(f"/tickets/{ticket.number}/email/preview", data={"body": "Done", "next_status": bad})

    assert (email.status_code, note.status_code, preview.status_code) == (400, 400, 400)
    assert status_of(db_session, ticket) == TicketStatus.OPEN
    db_session.expire_all()
    assert db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() == []  # nothing sent or noted


def test_a_note_changes_the_status_only_when_asked(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "Just a note"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.OPEN

    assert client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Chased the vendor", "next_status": "with_supplier"}
    ).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.WITH_SUPPLIER
    assert status_events(db_session, ticket) == [("open", "with_supplier", "agent@example.com")]
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert [a.detail for a in audit] == ["open -> with_supplier"]
    notes = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert [m.visibility.value for m in notes] == ["agents_only", "agents_only"]  # still internal


def test_a_note_to_awaiting_user_pauses_the_sla_clock_like_any_other_change(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    agent_for(client, db_session, team)

    client.post(f"/tickets/{ticket.number}/note", data={"body": "Need info", "next_status": "awaiting_user"})

    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).sla_paused_at is not None


def test_a_ticket_waiting_for_approval_offers_no_dropdown_and_refuses_a_change(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_APPROVAL)
    agent_for(client, db_session, team)

    assert select_options(client.get(f"/tickets/{ticket.number}").text) == []
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "n", "next_status": "open"}).status_code == 400
    # Without the field an email or note leaves it waiting for its approvers.
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello"}).status_code == 200
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "n"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("status", [TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_a_finished_ticket_offers_no_dropdown_and_cannot_be_reopened_this_way(client, db_session, status):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, status)
    agent_for(client, db_session, team)

    assert select_options(client.get(f"/tickets/{ticket.number}").text) == []
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "n", "next_status": "open"}).status_code == 400
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "n", "next_status": "open"}).status_code == 400
    assert status_of(db_session, ticket) == status


def test_a_split_original_offers_no_dropdown_and_its_status_still_follows_its_parts(client, db_session):
    from tests.test_splitting import split, subs_of, world

    original, _ = world(client, db_session)
    split(client, original, ("Keyboard", ""), ("Office", ""))
    db_session.refresh(original)

    assert select_options(client.get(f"/tickets/{original.number}").text) == []
    for path in ("note", "email"):
        response = client.post(f"/tickets/{original.number}/{path}", data={"body": "n", "next_status": "with_msp"})
        assert response.status_code == 400
    assert client.post(f"/tickets/{original.number}/email", data={"body": "Hello"}).status_code == 200
    assert status_of(db_session, original) == TicketStatus.OPEN  # unchanged: the parts decide

    # A part is an ordinary ticket: it takes the choice, and the original is re-derived from the parts.
    first, _second = subs_of(db_session, original)
    assert select_options(client.get(f"/tickets/{first.number}").text)
    client.post(f"/tickets/{first.number}/note", data={"body": "Waiting on the vendor", "next_status": "with_supplier"})
    assert status_of(db_session, first) == TicketStatus.WITH_SUPPLIER
    assert status_of(db_session, original) == TicketStatus.IN_PROGRESS


def test_an_untriaged_ticket_offers_no_dropdown_and_a_note_cannot_move_it(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    ticket.triaged_at = None
    db_session.add(ticket)
    db_session.commit()
    agent_for(client, db_session, team)

    assert select_options(client.get(f"/tickets/{ticket.number}").text) == []
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "n", "next_status": "in_progress"}).status_code == 400
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "n"}).status_code == 200
    assert status_of(db_session, ticket) == TicketStatus.OPEN


def test_a_plain_note_does_not_overwrite_a_status_that_changed_since_the_page_loaded(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id, TicketStatus.AWAITING_USER)
    agent_for(client, db_session, team)
    _, note_box = select_options(client.get(f"/tickets/{ticket.number}").text)
    assert note_box[1] == [""]  # what the untouched dropdown posts
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "Floor 2"})  # hands it back: In progress
    client.post("/auth/logout")
    login_as(client, "agent@example.com")

    client.post(f"/tickets/{ticket.number}/note", data={"body": "FYI", "next_status": ""})

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS


def test_send_and_preview_give_the_same_error_for_an_untriaged_ticket_with_a_bad_status(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = make_ticket(db_session, team.id)
    ticket.triaged_at = None
    db_session.add(ticket)
    db_session.commit()
    agent_for(client, db_session, team)

    data = {"body": "Hello", "next_status": "resolved"}
    send = client.post(f"/tickets/{ticket.number}/email", data=data)
    preview = client.post(f"/tickets/{ticket.number}/email/preview", data=data)

    assert send.status_code == preview.status_code == 400
    assert "Triage the ticket" in send.text and "Triage the ticket" in preview.text

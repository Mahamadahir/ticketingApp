"""The portal request page (9E, #205): the progress steps, the conversation in the requester's words, the status wording
(never With MSP or With supplier), and files on a requester's reply, which follow the agent-side upload rules."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import reply_files
from app.config import settings
from app.main import app
from app.messages import requester_status, requester_steps
from app.models import (
    Attachment,
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
    UserRole,
)
from app.settings_store import INBOUND_MAX_ATTACHMENTS, set_setting
from app.storage import LocalStorage, get_storage
from app.visibility import access_context, may_view_attachment
from tests.conftest import create_team, login_as, set_role

PDF = b"%PDF-1.7\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 3000


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


def make(db, status=TicketStatus.OPEN, email="jo@example.com", **fields):
    ticket = Ticket(subject="Outlook keeps asking for a password", requester_email=email, status=status, triaged_at=datetime.now(timezone.utc), **fields)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def say(db, ticket, body, direction, *, visibility=MessageVisibility.PUBLIC, sender="x@example.com", author=None, channel="email"):
    message = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, visibility=visibility, author_user_id=author, channel=channel,
    )
    db.add(message)
    db.commit()
    return message


def files(*items):
    return [("files", (name, data, "application/octet-stream")) for name, data in items]


def steps_of(db, ticket, zone="UTC"):
    return [(s.label, s.state) for s in requester_steps(ticket, requester_status(db, ticket, zone, can_reopen=True))]


# ---- the progress steps --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (TicketStatus.NEW, [("Received", "current"), ("Being worked on", "todo"), ("Resolved", "todo")]),
        (TicketStatus.OPEN, [("Received", "done"), ("Being worked on", "current"), ("Resolved", "todo")]),
        (TicketStatus.IN_PROGRESS, [("Received", "done"), ("Being worked on", "current"), ("Resolved", "todo")]),
        (TicketStatus.WITH_MSP, [("Received", "done"), ("Being worked on", "current"), ("Resolved", "todo")]),
        (TicketStatus.WITH_SUPPLIER, [("Received", "done"), ("Being worked on", "current"), ("Resolved", "todo")]),
        (
            TicketStatus.AWAITING_USER,
            [("Received", "done"), ("Being worked on", "done"), ("Waiting for you", "current"), ("Resolved", "todo")],
        ),
        (TicketStatus.AWAITING_APPROVAL, [("Received", "done"), ("Waiting for approval", "current"), ("Resolved", "todo")]),
        (TicketStatus.RESOLVED, [("Received", "done"), ("Being worked on", "done"), ("Resolved", "current")]),
        (TicketStatus.CLOSED, [("Received", "done"), ("Being worked on", "done"), ("Closed", "current")]),
    ],
)
def test_the_steps_for_each_status(db_session, status, expected):
    assert steps_of(db_session, make(db_session, status)) == expected


def test_a_scheduled_request_names_its_day_as_the_second_step(db_session):
    ticket = make(db_session, TicketStatus.SCHEDULED, due_on=date(2026, 11, 12))
    assert steps_of(db_session, ticket)[1] == ("Scheduled for Thursday 12 November", "current")


def test_the_page_marks_the_current_step_for_screen_readers_and_the_chip_uses_the_requesters_words(client, db_session):
    ticket = make(db_session, TicketStatus.AWAITING_USER)
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert page.count('aria-current="step"') == 1
    assert 'Waiting for you<span class="sr-only"> (current step)</span>' in page
    assert 'Received<span class="sr-only"> (done)</span>' in page and 'Resolved<span class="sr-only"> (not yet)</span>' in page
    assert '<span class="chip t-amber">Waiting for your reply</span>' in page
    assert "<h1>Outlook keeps asking for a password</h1>" in page and "INC-" in page and "Problem report" in page
    assert "rq-reply-wanted" in page  # the reply box is highlighted while the request waits for the person


def test_the_reply_box_is_not_highlighted_when_nothing_is_wanted(client, db_session):
    ticket = make(db_session, TicketStatus.OPEN)
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert "Reply to IT Support" in page and "rq-reply-wanted" not in page and "Waiting for you" not in page


@pytest.mark.parametrize("status", [TicketStatus.WITH_MSP, TicketStatus.WITH_SUPPLIER])
def test_the_requester_is_never_told_about_the_partner_or_supplier(client, db_session, status):
    ticket = make(db_session, status)
    say(db_session, ticket, "We have passed it on", MessageDirection.OUTBOUND, sender="Sam")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    lowered = page.lower()
    assert "with msp" not in lowered and "with supplier" not in lowered and "with_msp" not in lowered and "with_supplier" not in lowered
    assert "supplier" not in lowered
    assert '<span class="chip t-blue">Being worked on</span>' in page


# ---- the conversation -----------------------------------------------------------------------------------------


def test_the_conversation_is_newest_first_in_the_requesters_words(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_id = login_as(client, "sam.reed@example.com", "Sam Reed")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    jo_id = login_as(client, "jo@example.com", "Jo Lee")
    ticket = make(db_session, TicketStatus.AWAITING_USER, team_id=team.id, assigned_agent_id=agent_id)
    say(db_session, ticket, "FIRST words", MessageDirection.INBOUND, sender="jo@example.com")
    say(db_session, ticket, "SECOND words", MessageDirection.OUTBOUND, sender="Sam Reed", author=agent_id)
    say(db_session, ticket, "THIRD words", MessageDirection.INBOUND, sender="Jo Lee", author=jo_id, channel="portal")
    say(db_session, ticket, "AUTOMATIC words", MessageDirection.OUTBOUND, sender="IT Support (automatic)")

    page = client.get(f"/portal/{ticket.number}").text

    assert page.index("AUTOMATIC words") < page.index("THIRD words") < page.index("SECOND words") < page.index("FIRST words")
    assert page.count('<span class="rq-who">You</span>') == 2
    assert '<span class="rq-who">Sam from IT Support</span>' in page and '<span class="rq-who">IT Support</span>' in page
    assert "Sam Reed" not in page.split("Conversation")[1].split("rq-side")[0]  # only the first name, in the thread
    assert "<dt>Handled by</dt><dd>Sam from IT Support</dd>" in page


def test_an_unassigned_request_is_handled_by_it_support(client, db_session):
    ticket = make(db_session)
    login_as(client, "jo@example.com")
    assert "<dt>Handled by</dt><dd>IT Support</dd>" in client.get(f"/portal/{ticket.number}").text


def test_notes_their_files_and_agent_only_events_are_never_shown(client, db_session, storage):
    team = create_team(db_session, "Service Desk")
    agent_id = login_as(client, "agent@example.com", "Sam Reed")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    ticket = make(db_session, TicketStatus.OPEN, team_id=team.id)
    say(db_session, ticket, "Public hello", MessageDirection.OUTBOUND, sender="Sam Reed", author=agent_id)
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="assigned", actor_email="agent@example.com", detail="EVENTDETAIL"))
    db_session.commit()
    assert client.post(
        f"/tickets/{ticket.number}/note", data={"body": "SECRET note"}, files=files(("secret.pdf", PDF)),
    ).status_code == 200
    note_file = db_session.exec(select(Attachment)).one()

    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert "Public hello" in page
    assert "SECRET note" not in page and "secret.pdf" not in page and "EVENTDETAIL" not in page
    assert client.get(f"/attachments/{note_file.id}").status_code == 404


# ---- files on a requester's reply -----------------------------------------------------------------------------


def test_a_reply_with_files_stores_them_on_a_portal_message_and_both_sides_can_open_them(client, db_session, storage):
    team = create_team(db_session, "Service Desk")
    ticket = make(db_session, TicketStatus.AWAITING_USER, team_id=team.id)
    jo_id = login_as(client, "jo@example.com", "Jo Lee")

    response = client.post(
        f"/portal/{ticket.number}/comment", data={"body": "Here is the screenshot"}, files=files(("shot.png", PNG), ("log.pdf", PDF)),
    )

    assert response.status_code == 200
    message = db_session.exec(select(Message).where(Message.body == "Here is the screenshot")).one()
    assert message.channel == "portal" and message.direction == MessageDirection.INBOUND
    stored = db_session.exec(select(Attachment).order_by(Attachment.id)).all()
    assert [(a.filename, a.message_id, a.uploaded_by_id, a.ticket_id) for a in stored] == [
        ("shot.png", message.id, jo_id, ticket.id), ("log.pdf", message.id, jo_id, ticket.id),
    ]
    assert all(storage.exists(a.blob_key) for a in stored)
    assert all(f'href="/attachments/{a.id}"' in response.text for a in stored)  # listed on the page they got back
    assert client.get(f"/attachments/{stored[0].id}", follow_redirects=False).status_code in (200, 302, 307)

    client.post("/auth/logout")  # an agent of the team reads the same files on the agent page
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    page = client.get(f"/tickets/{ticket.number}").text
    assert all(f'href="/attachments/{a.id}"' in page for a in stored)
    agent = db_session.get(User, agent_id)
    assert may_view_attachment(db_session, agent, stored[0], db_session.get(Ticket, ticket.id), access_context(db_session, agent))


def test_a_reply_without_files_is_unchanged(client, db_session, storage):
    ticket = make(db_session, TicketStatus.AWAITING_USER)
    login_as(client, "jo@example.com")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Just words"}).status_code == 200  # urlencoded, as before
    # What a browser sends for a file box left empty: one part with a blank file name
    raw = (
        b'--B\r\nContent-Disposition: form-data; name="body"\r\n\r\nEmpty file box\r\n--B\r\n'
        b'Content-Disposition: form-data; name="files"; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n\r\n--B--\r\n'
    )
    assert client.post(
        f"/portal/{ticket.number}/comment", content=raw, headers={"content-type": "multipart/form-data; boundary=B"},
    ).status_code == 200
    assert db_session.exec(select(Attachment)).all() == []
    assert len(db_session.exec(select(Message).where(Message.channel == "portal")).all()) == 2


def refused(client, db_session, ticket, *items, body="With files"):
    response = client.post(f"/portal/{ticket.number}/comment", data={"body": body}, files=files(*items))
    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.exec(select(Message).where(Message.body == body)).all() == [] and db_session.exec(select(Attachment)).all() == []
    return response.text


def test_a_blocked_type_an_empty_file_and_a_file_over_the_limit_are_refused_and_nothing_is_kept(client, db_session, storage, monkeypatch):
    ticket = make(db_session, TicketStatus.AWAITING_USER)
    login_as(client, "jo@example.com")
    assert "cannot be attached" in refused(client, db_session, ticket, ("setup.exe", PDF))
    assert "is empty" in refused(client, db_session, ticket, ("empty.pdf", b""))
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024)  # the same limit inbound mail has
    assert "larger than the" in refused(client, db_session, ticket, ("big.pdf", PDF + b"y" * 2000))
    assert list(storage.root.rglob("*/*")) == []
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "small ok"}, files=files(("ok.pdf", PDF[:100]))).status_code == 200


def test_more_files_than_the_count_setting_are_refused(client, db_session, storage):
    ticket = make(db_session, TicketStatus.AWAITING_USER)
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "2")
    db_session.commit()
    login_as(client, "jo@example.com")
    assert "at most 2 files" in refused(client, db_session, ticket, ("a.pdf", PDF), ("b.pdf", PDF + b"1"), ("c.pdf", PDF + b"2"))
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "two"}, files=files(("a.pdf", PDF), ("b.pdf", PDF + b"1"))).status_code == 200


def test_the_reply_goes_through_the_agent_side_upload_path(client, db_session, storage, monkeypatch):
    """One path, no second copy of the rules: the same read_uploads and check_file the agent's note uses."""
    calls = []
    original = reply_files.check_file
    monkeypatch.setattr(reply_files, "check_file", lambda name, data: calls.append(name) or original(name, data))
    ticket = make(db_session, TicketStatus.AWAITING_USER)
    login_as(client, "jo@example.com")
    client.post(f"/portal/{ticket.number}/comment", data={"body": "x"}, files=files(("a.pdf", PDF)))
    assert calls == ["a.pdf"]


def test_a_reply_with_files_on_a_closed_request_attaches_them_to_the_new_one(client, db_session, storage):
    ticket = make(db_session, TicketStatus.CLOSED)
    login_as(client, "jo@example.com")
    response = client.post(f"/portal/{ticket.number}/comment", data={"body": "Back again"}, files=files(("shot.png", PNG)), follow_redirects=False)
    assert response.status_code == 303
    attachment = db_session.exec(select(Attachment)).one()
    message = db_session.get(Message, attachment.message_id)
    assert message.body == "Back again" and message.channel == "portal" and attachment.ticket_id != ticket.id


def test_a_follower_can_add_files_and_the_requester_sees_them(client, db_session, storage):
    from app.followers import add_follower

    ticket = make(db_session, TicketStatus.OPEN)
    add_follower(db_session, ticket, "pal@example.com", "jo@example.com")
    db_session.commit()
    login_as(client, "pal@example.com", "Pal Jones")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "FYI"}, files=files(("a.pdf", PDF))).status_code == 200
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    attachment = db_session.exec(select(Attachment)).one()
    assert f'href="/attachments/{attachment.id}"' in client.get(f"/portal/{ticket.number}").text


def test_the_page_says_how_many_files_and_how_big(client, db_session):
    ticket = make(db_session, TicketStatus.OPEN)
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert 'hx-encoding="multipart/form-data"' in page and 'name="files"' in page and "Add a file" in page
    assert "Up to 20 files, 10 MB each." in page

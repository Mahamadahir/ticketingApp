from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.attachments import (
    AttachmentRejected,
    clean_filename,
    detect_content_type,
    store_attachment,
)
from app.config import settings
from app.main import app
from app.models import (
    Attachment,
    Message,
    MessageDirection,
    MessageVisibility,
    Ticket,
    TicketFollower,
    TicketStatus,
    User,
    UserRole,
)
from app.storage import LocalStorage, get_storage
from tests.conftest import create_team, login_as, set_role

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 8000
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"x" * 32


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


def make_ticket(db_session, team_id=None, requester="me@example.com", triaged=True):
    ticket = Ticket(
        subject="Printer",
        requester_email=requester,
        team_id=team_id,
        status=TicketStatus.OPEN if triaged else TicketStatus.NEW,
        triaged_at=datetime.now(timezone.utc) if triaged else None,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def make_message(db_session, ticket, internal=False):
    message = Message(
        ticket_id=ticket.id,
        sender="someone",
        body="hi",
        direction=MessageDirection.INTERNAL if internal else MessageDirection.INBOUND,
        visibility=MessageVisibility.AGENTS_ONLY if internal else MessageVisibility.PUBLIC,
    )
    db_session.add(message)
    db_session.commit()
    db_session.refresh(message)
    return message


def attach(db_session, storage, ticket, data=PDF, filename="report.pdf", message=None, **kwargs):
    attachment = store_attachment(db_session, storage, ticket=ticket, filename=filename, data=data, message=message, **kwargs)
    db_session.commit()
    return attachment


# ---- Detecting what a file is ------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (PDF, "application/pdf"),
        (b"PK\x03\x04rest", "application/zip"),
        (b"<html><script>alert(1)</script></html>", "application/octet-stream"),
        (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>', "application/octet-stream"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "application/octet-stream"),
    ],
)
def test_the_type_comes_from_the_bytes(data, expected):
    assert detect_content_type(data) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("C:\\Users\\me\\Desktop\\scan.pdf", "scan.pdf"),
        ("../../etc/passwd", "passwd"),
        ("  .hidden.  ", "hidden"),
        ("bad\x00name\r\n.txt", "badname.txt"),
        ("", "attachment"),
        ("///", "attachment"),
    ],
)
def test_a_filename_is_just_a_name(name, expected):
    assert clean_filename(name) == expected


def test_a_very_long_name_is_cut_but_keeps_its_extension():
    name = clean_filename("a" * 500 + ".pdf")
    assert len(name) <= 200 and name.endswith(".pdf")


# ---- Storing -----------------------------------------------------------------------


def test_a_file_is_stored_with_its_details(db_session, storage):
    ticket = make_ticket(db_session)
    uploader = User(email="uploader@example.com", display_name="Uploader", role=UserRole.AGENT)
    db_session.add(uploader)
    db_session.commit()
    attachment = attach(db_session, storage, ticket, PDF, r"..\Quote.pdf", uploaded_by=uploader)

    assert attachment.filename == "Quote.pdf"
    assert attachment.size == len(PDF)
    assert attachment.content_type == "application/pdf"
    assert attachment.blob_key == f"{attachment.content_hash[:2]}/{attachment.content_hash}"
    assert storage.read(attachment.blob_key) == PDF
    assert attachment.uploaded_by_id == uploader.id


def test_the_claimed_name_does_not_decide_the_type(db_session, storage):
    ticket = make_ticket(db_session)
    attachment = attach(db_session, storage, ticket, b"<script>alert(1)</script>", "photo.png")
    assert attachment.content_type == "application/octet-stream"


def test_the_same_file_twice_is_one_blob_and_two_rows(db_session, storage):
    ticket = make_ticket(db_session)
    first = attach(db_session, storage, ticket, PDF, "a.pdf")
    puts = []
    original_put = storage.put
    storage.put = lambda *args: (puts.append(args), original_put(*args))
    second = attach(db_session, storage, ticket, PDF, "b.pdf")
    assert first.blob_key == second.blob_key and first.id != second.id
    assert puts == []  # already stored: not uploaded again


def test_an_empty_file_is_refused(db_session, storage):
    with pytest.raises(AttachmentRejected, match="empty"):
        attach(db_session, storage, make_ticket(db_session), b"", "nothing.txt")


def test_a_file_over_the_limit_is_refused(db_session, storage, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024)
    ticket = make_ticket(db_session)
    attach(db_session, storage, ticket, b"x" * 1024, "ok.bin")
    with pytest.raises(AttachmentRejected, match="limit"):
        attach(db_session, storage, ticket, b"x" * 1025, "big.bin")
    assert len(db_session.exec(select(Attachment)).all()) == 1


@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "x.ps1", "archive.tar.js", "evil.lnk"])
def test_programs_cannot_be_attached(db_session, storage, name):
    with pytest.raises(AttachmentRejected):
        attach(db_session, storage, make_ticket(db_session), b"MZ....", name)
    assert db_session.exec(select(Attachment)).all() == []


def test_a_message_of_another_ticket_is_refused(db_session, storage):
    ticket, other = make_ticket(db_session), make_ticket(db_session)
    with pytest.raises(AttachmentRejected):
        attach(db_session, storage, ticket, PDF, "a.pdf", message=make_message(db_session, other))


def test_a_tiny_inline_picture_is_not_kept(db_session, storage):
    ticket = make_ticket(db_session)
    assert attach(db_session, storage, ticket, PNG, "logo.png", inline=True) is None
    assert db_session.exec(select(Attachment)).all() == []
    assert not list(storage.root.rglob("*")) or not any(p.is_file() for p in storage.root.rglob("*"))


def test_a_tiny_picture_someone_attached_is_kept(db_session, storage):
    assert attach(db_session, storage, make_ticket(db_session), PNG, "dot.png") is not None


def test_a_repeated_inline_picture_is_stored_once(db_session, storage):
    ticket = make_ticket(db_session)
    first = attach(db_session, storage, ticket, BIG_PNG, "banner.png", inline=True)
    second = attach(db_session, storage, ticket, BIG_PNG, "banner.png", inline=True)
    assert second.id == first.id
    assert len(db_session.exec(select(Attachment)).all()) == 1
    # Another ticket's copy is its own row, though it shares the blob.
    other = attach(db_session, storage, make_ticket(db_session), BIG_PNG, "banner.png", inline=True)
    assert other.id != first.id and other.blob_key == first.blob_key


# ---- Downloading -------------------------------------------------------------------


def agent_on(client, db_session, team, email="agent@example.com"):
    agent_id = login_as(client, email)
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return agent_id


def test_an_agent_on_the_team_gets_a_pdf_as_a_download(client, db_session, storage):
    team = create_team(db_session, "Service Desk")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id), PDF, "Quote £1.pdf")
    agent_on(client, db_session, team)

    response = client.get(f"/attachments/{attachment.id}")

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("data, name, content_type", [(PNG, "a.png", "image/png"), (JPEG, "a.jpg", "image/jpeg"), (GIF, "a.gif", "image/gif"), (WEBP, "a.webp", "image/webp")])
def test_safe_pictures_are_shown_in_the_page(client, db_session, storage, data, name, content_type):
    team = create_team(db_session, "Service Desk")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id), data, name)
    agent_on(client, db_session, team)

    response = client.get(f"/attachments/{attachment.id}")

    assert response.headers["content-type"] == content_type
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "data, name",
    [
        (b"<html><script>alert(1)</script></html>", "page.html"),
        (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "image.svg"),
        (b"<html>pretending</html>", "photo.png"),  # named like a picture, is not one
    ],
)
def test_html_and_svg_are_never_shown_in_the_page(client, db_session, storage, data, name):
    team = create_team(db_session, "Service Desk")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id), data, name)
    agent_on(client, db_session, team)

    response = client.get(f"/attachments/{attachment.id}")

    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_agent_outside_the_team_is_refused(client, db_session, storage):
    team, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id), PDF)
    agent_on(client, db_session, other)

    assert client.get(f"/attachments/{attachment.id}").status_code == 403


def test_any_agent_reads_a_file_on_an_untriaged_ticket(client, db_session, storage):
    team, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id, triaged=False), PDF)
    agent_on(client, db_session, other)

    assert client.get(f"/attachments/{attachment.id}").status_code == 200


def test_a_missing_file_is_a_404(client, db_session, storage):
    team = create_team(db_session, "Service Desk")
    agent_on(client, db_session, team)
    assert client.get("/attachments/9999").status_code == 404


def test_signed_out_goes_to_login(client, db_session, storage):
    attachment = attach(db_session, storage, make_ticket(db_session), PDF)
    response = client.get(f"/attachments/{attachment.id}", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "/auth/login" in response.headers["location"]


def test_the_requester_gets_files_of_messages_they_can_see(client, db_session, storage):
    ticket = make_ticket(db_session, create_team(db_session, "Service Desk").id)
    attachment = attach(db_session, storage, ticket, PDF, message=make_message(db_session, ticket))
    login_as(client, "me@example.com")

    assert client.get(f"/attachments/{attachment.id}").status_code == 200


def test_the_requester_never_gets_a_file_from_an_internal_note(client, db_session, storage):
    ticket = make_ticket(db_session, create_team(db_session, "Service Desk").id)
    attachment = attach(db_session, storage, ticket, PDF, message=make_message(db_session, ticket, internal=True))
    login_as(client, "me@example.com")

    assert client.get(f"/attachments/{attachment.id}").status_code == 404


def test_someone_else_gets_nothing(client, db_session, storage):
    ticket = make_ticket(db_session, create_team(db_session, "Service Desk").id)
    attachment = attach(db_session, storage, ticket, PDF, message=make_message(db_session, ticket))
    login_as(client, "stranger@example.com")

    assert client.get(f"/attachments/{attachment.id}").status_code == 404


def test_an_outside_follower_gets_public_files_only(client, db_session, storage):
    ticket = make_ticket(db_session, create_team(db_session, "Service Desk").id)
    public = attach(db_session, storage, ticket, PDF, message=make_message(db_session, ticket))
    note = attach(db_session, storage, ticket, PDF + b"2", message=make_message(db_session, ticket, internal=True))
    db_session.add(TicketFollower(ticket_id=ticket.id, email="boss@example.com", added_by_email="agent@example.com"))
    db_session.commit()
    login_as(client, "boss@example.com")

    assert client.get(f"/attachments/{public.id}").status_code == 200
    assert client.get(f"/attachments/{note.id}").status_code == 404


def test_a_file_without_a_message_belongs_to_whoever_added_it(client, db_session, storage):
    ticket = make_ticket(db_session)
    requester_id = login_as(client, "me@example.com")
    mine = attach(db_session, storage, ticket, PDF, uploaded_by=db_session.get(User, requester_id))
    someone_elses = attach(db_session, storage, ticket, PDF + b"2")  # added by the system or an agent

    assert client.get(f"/attachments/{mine.id}").status_code == 200
    assert client.get(f"/attachments/{someone_elses.id}").status_code == 404


def test_with_a_blob_store_the_browser_is_redirected_not_streamed(client, db_session, tmp_path):
    class Redirecting(LocalStorage):
        def download_url(self, key, *, filename, content_type, inline):
            return f"https://stticketing.blob.core.windows.net/attachments/{key}?sig=x&inline={inline}"

        def read(self, key):  # pragma: no cover - must not be called
            raise AssertionError("the app must not stream when it can redirect")

    store = Redirecting(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    try:
        team = create_team(db_session, "Service Desk")
        attachment = attach(db_session, store, make_ticket(db_session, team.id), PNG, "a.png")
        agent_on(client, db_session, team)

        response = client.get(f"/attachments/{attachment.id}", follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_storage, None)

    assert response.status_code == 302
    assert response.headers["location"] == f"https://stticketing.blob.core.windows.net/attachments/{attachment.blob_key}?sig=x&inline=True"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"  # the signed address must not leak


def test_a_file_whose_blob_is_gone_is_a_clean_404(client, db_session, storage):
    team = create_team(db_session, "Service Desk")
    attachment = attach(db_session, storage, make_ticket(db_session, team.id), PDF)
    agent_on(client, db_session, team)
    (storage.root / attachment.blob_key).unlink()

    assert client.get(f"/attachments/{attachment.id}").status_code == 404


def test_an_agent_who_raised_a_ticket_outside_their_teams_sees_only_what_a_requester_would(client, db_session, storage):
    team, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = make_ticket(db_session, team.id, requester="agent@example.com")
    public = attach(db_session, storage, ticket, PDF, message=make_message(db_session, ticket))
    note = attach(db_session, storage, ticket, PDF + b"2", message=make_message(db_session, ticket, internal=True))
    agent_on(client, db_session, other)

    assert client.get(f"/attachments/{public.id}").status_code == 200
    assert client.get(f"/attachments/{note.id}").status_code == 403


def test_the_same_logo_on_another_message_is_its_own_row(db_session, storage):
    ticket = make_ticket(db_session)
    first = attach(db_session, storage, ticket, BIG_PNG, "logo.png", inline=True, message=make_message(db_session, ticket, internal=True))
    second = attach(db_session, storage, ticket, BIG_PNG, "logo.png", inline=True, message=make_message(db_session, ticket))
    assert first.id != second.id and first.blob_key == second.blob_key

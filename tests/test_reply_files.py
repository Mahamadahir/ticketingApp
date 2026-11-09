"""#201: files on an agent's replies and notes. The checks are the stored-file rules' own (app/attachments.py check_file) and the
inbound mail count; the files are held with the preview, attached when it is confirmed, and removed when it is abandoned."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import mail, outbox, reply_files, scheduled_jobs
from app.attachments import files_by_message, keys_in_use
from app.config import settings
from app.main import app
from app.models import Attachment, Message, OutboundEmail, PendingBlobDelete, ReplyHeldFile, TeamsPost, Ticket, User
from app.settings_store import INBOUND_MAX_ATTACHMENTS, REPLY_ATTACH_MAX_MB, REPLY_HELD_FILE_HOURS, set_setting
from app.storage import LocalStorage, get_storage
from app.visibility import access_context, may_view_attachment
from tests.conftest import login_as
from tests.test_email_preview import preview, recorded, ticket_for_email

PDF = b"%PDF-1.7\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 3000


@pytest.fixture
def storage(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    monkeypatch.setattr(mail, "get_storage", lambda: store)
    monkeypatch.setattr(reply_files, "get_storage", lambda: store)
    yield store
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def upload(*items):
    return [("files", (name, data, "application/octet-stream")) for name, data in items]


def preview_with(client, ticket, *items, body="Please see the attached", **extra):
    return client.post(f"/tickets/{ticket.number}/email/preview", data={"body": body, **extra}, files=upload(*items) or None)


def held(db_session):
    db_session.expire_all()
    return db_session.exec(select(ReplyHeldFile).order_by(ReplyHeldFile.id)).all()


def draft_of(page):
    return page.split('name="draft" value="')[1].split('"')[0]


def confirm(client, ticket, page, **extra):
    data = {"body": "Please see the attached", "draft": draft_of(page), **extra}
    return client.post(f"/tickets/{ticket.number}/email", data=data)


# ---- The attach control ----------------------------------------------------------------------------------


def test_the_composer_has_an_attach_control_on_both_forms_and_the_new_button_label(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    assert page.count('type="file"') == 2 and page.count('name="files"') == 2
    assert 'id="email-files"' in page and 'id="note-files"' in page and "multiple" in page
    assert page.count('hx-encoding="multipart/form-data"') == 2
    assert "Preview message</button>" in page and "Preview email" not in page


# ---- Upload within and over the limits -------------------------------------------------------------------


def test_files_within_the_limits_are_listed_by_name_and_size_and_held_with_the_draft(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    before = recorded(db_session)

    response = preview_with(client, ticket, ("quote.pdf", PDF), ("jam.png", PNG))

    assert response.status_code == 200
    page = response.text
    assert "quote.pdf" in page and "jam.png" in page and "(209 bytes)" in page and "(2.9 KB)" in page
    assert "The files go with the email as attachments" in page
    rows = held(db_session)
    assert [(r.filename, r.size, r.user_id, r.ticket_id) for r in rows][0][:2] == ("quote.pdf", len(PDF))
    assert len(rows) == 2 and all(storage.exists(r.blob_key) for r in rows) and {r.token for r in rows} == {draft_of(page)}
    assert recorded(db_session) == before  # a preview records no message, audit entry or event
    assert db_session.exec(select(Attachment)).all() == []


def test_a_preview_without_files_holds_nothing_and_has_no_draft(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket).text
    assert 'name="draft"' not in page and held(db_session) == []


def test_a_file_over_the_size_limit_is_refused_at_the_preview(client, db_session, storage, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_bytes", 100)
    ticket = ticket_for_email(client, db_session)
    response = preview_with(client, ticket, ("big.pdf", PDF))
    assert response.status_code == 400 and "big.pdf" in response.text and "larger than the 0 MB limit" in response.text
    assert held(db_session) == []


def test_a_file_that_only_runs_and_an_empty_file_are_refused_with_the_stored_file_rules(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    blocked = preview_with(client, ticket, ("setup.exe", b"MZ" + b"\x00" * 50))
    assert blocked.status_code == 400 and "Files ending .exe cannot be attached" in blocked.text
    empty = preview_with(client, ticket, ("nothing.txt", b""))
    assert empty.status_code == 400 and "The file is empty" in empty.text
    assert held(db_session) == []


def test_more_files_than_inbound_mail_keeps_are_refused(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "2")
    db_session.commit()
    assert preview_with(client, ticket, ("a.pdf", PDF), ("b.pdf", PDF + b"1")).status_code == 200
    refused = preview_with(client, ticket, ("a.pdf", PDF), ("b.pdf", PDF + b"1"), ("c.pdf", PDF + b"2"))
    assert refused.status_code == 400 and "at most 2 files" in refused.text


def test_the_same_check_is_used_for_inbound_mail_and_agent_files():
    """One set of rules: store_attachment calls check_file, and so does the preview."""
    import inspect

    from app import attachments

    assert "check_file(" in inspect.getsource(attachments.store_attachment)
    assert "check_file(" in inspect.getsource(reply_files.read_uploads)


# ---- The Graph size limit ----------------------------------------------------------------------------------


def test_files_over_what_one_email_carries_are_refused_at_the_preview_and_nothing_is_held(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    big = b"%PDF-1.7\n" + b"x" * 1_200_000
    response = preview_with(client, ticket, ("a.pdf", big), ("b.pdf", big + b"1"))
    assert response.status_code == 400
    assert "more than the 2 MB one email can carry" in response.text and "2.3 MB" in response.text
    assert held(db_session) == [] and list(storage.root.rglob("*/*")) == []

    one = preview_with(client, ticket, ("a.pdf", big))
    assert one.status_code == 200  # one file of 1.2 MB fits


def test_the_setting_lowers_the_total_and_graphs_limit_caps_it(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, REPLY_ATTACH_MAX_MB, "1")
    db_session.commit()
    assert preview_with(client, ticket, ("a.pdf", b"%PDF-1.7\n" + b"x" * 1_200_000)).status_code == 400
    set_setting(db_session, REPLY_ATTACH_MAX_MB, "100")  # no more than Graph takes in one send
    assert reply_files.email_budget_bytes(db_session) == 2 * 1024 * 1024


# ---- Abandoned files ---------------------------------------------------------------------------------------


def test_keeping_editing_drops_the_held_files_through_the_blob_delete_queue(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("quote.pdf", PDF)).text
    key = held(db_session)[0].blob_key
    assert f'hx-post="/tickets/{ticket.number}/email/discard"' in page

    response = client.post(f"/tickets/{ticket.number}/email/discard", data={"draft": draft_of(page)})

    assert response.status_code == 200 and response.text == ""
    assert held(db_session) == []
    assert [e.blob_key for e in db_session.exec(select(PendingBlobDelete)).all()] == [key]  # the blob-cleanup job deletes it


def test_previewing_again_replaces_the_earlier_draft(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    first = preview_with(client, ticket, ("a.pdf", PDF)).text
    second = preview_with(client, ticket, ("b.pdf", PDF + b"2")).text
    assert draft_of(first) != draft_of(second)
    assert [r.filename for r in held(db_session)] == ["b.pdf"]
    assert len(db_session.exec(select(PendingBlobDelete)).all()) == 1  # a.pdf's blob
    gone = confirm(client, ticket, first)  # the first preview's button no longer has files behind it
    assert gone.status_code == 400 and "no longer held" in gone.text


def test_an_unconfirmed_draft_expires_and_its_files_are_queued_for_deletion(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    preview_with(client, ticket, ("a.pdf", PDF))
    row = held(db_session)[0]

    assert reply_files.expire(db_session) == 0  # still fresh
    set_setting(db_session, REPLY_HELD_FILE_HOURS, "2")
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db_session.add(row)
    db_session.commit()
    assert reply_files.expire(db_session) == 1
    db_session.commit()

    assert held(db_session) == [] and [e.blob_key for e in db_session.exec(select(PendingBlobDelete)).all()] == [row.blob_key]


def test_the_expiry_is_a_recurring_job():
    assert [j for j in scheduled_jobs.SCHEDULED_JOBS if j.name == "reply-files"][0].run is reply_files.expire


def test_a_blob_another_holder_uses_is_not_queued_for_deletion(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("a.pdf", PDF)).text
    key = held(db_session)[0].blob_key
    assert keys_in_use(db_session, {key}) == {key}
    other = ReplyHeldFile(ticket_id=ticket.id, user_id=held(db_session)[0].user_id, token="other", filename="same.pdf", size=1, blob_key=key)
    db_session.add(other)
    db_session.commit()
    client.post(f"/tickets/{ticket.number}/email/discard", data={"draft": draft_of(page)})
    assert db_session.exec(select(PendingBlobDelete)).all() == []  # the second draft still points at the same bytes


def test_anonymising_the_requester_drops_the_files_held_for_their_ticket(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    preview_with(client, ticket, ("a.pdf", PDF))
    key = held(db_session)[0].blob_key
    assert reply_files.release_for_tickets(db_session, [ticket.id]) == 1
    db_session.commit()
    assert held(db_session) == [] and [e.blob_key for e in db_session.exec(select(PendingBlobDelete)).all()] == [key]


# ---- Confirming: the files go out on the email -------------------------------------------------------------


def test_confirming_attaches_the_files_to_the_message_and_the_email_carries_them(client, db_session, storage, dev_mail, monkeypatch):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("quote.pdf", PDF), ("jam.png", PNG)).text

    response = confirm(client, ticket, page)

    assert response.status_code == 200
    db_session.expire_all()
    [message] = [m for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.author_user_id and m.direction.value == "outbound"]
    files = db_session.exec(select(Attachment).where(Attachment.message_id == message.id).order_by(Attachment.id)).all()
    assert [(f.filename, f.size, f.content_type) for f in files] == [("quote.pdf", len(PDF), "application/pdf"), ("jam.png", len(PNG), "image/png")]
    assert all(f.uploaded_by_id is not None and storage.exists(f.blob_key) for f in files)
    assert held(db_session) == [] and db_session.exec(select(PendingBlobDelete)).all() == []  # in use now, so they stay
    row = db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).one()
    assert row.attachment_ids == ",".join(str(f.id) for f in files)

    built = mail.build_message_mail(db_session, row)
    assert [(n, t, d) for n, t, d in built.files] == [("quote.pdf", "application/pdf", PDF), ("jam.png", "image/png", PNG)]
    assert built.body.startswith("Please see the attached") and "could NOT" not in built.body

    sent = {}
    monkeypatch.setattr(mail.graph_client, "send_mail", lambda *a, **k: sent.update(args=a, kwargs=k))
    mail.send_through_graph(built)
    assert sent["kwargs"]["attachments"] == built.files


def test_a_reply_without_files_still_carries_none(client, db_session, storage, dev_mail):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Plain"})
    row = db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).one()
    assert row.attachment_ids is None and mail.build_message_mail(db_session, row).files == []


def test_a_held_file_that_cannot_be_read_stops_the_confirm_and_keeps_the_draft(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("a.pdf", PDF)).text
    storage.delete(held(db_session)[0].blob_key)
    response = confirm(client, ticket, page)
    assert response.status_code == 400 and "could not be read back" in response.text
    assert len(held(db_session)) == 1 and db_session.exec(select(Attachment)).all() == []


def test_a_file_that_cannot_be_read_at_send_time_is_named_in_the_email_and_does_not_stop_it(client, db_session, storage, dev_mail):
    ticket = ticket_for_email(client, db_session)
    confirm(client, ticket, preview_with(client, ticket, ("a.pdf", PDF)).text)
    db_session.expire_all()
    storage.delete(db_session.exec(select(Attachment)).one().blob_key)
    built = mail.build_message_mail(db_session, db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).one())
    assert built.files == [] and "these files could NOT be attached: a.pdf" in built.body


def test_nobody_can_confirm_with_another_agents_draft(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("a.pdf", PDF)).text
    from app.models import UserRole
    from tests.conftest import set_role

    other = login_as(client, "second@example.com", "Second Agent")
    set_role(db_session, other, UserRole.AGENT, db_session.get(Ticket, ticket.id).team_id)
    response = confirm(client, ticket, page)
    assert response.status_code == 400 and "no longer held" in response.text
    assert len(held(db_session)) == 1 and db_session.exec(select(Message).where(Message.author_user_id == other)).all() == []


def test_a_refused_confirm_leaves_the_draft_and_records_nothing(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("a.pdf", PDF)).text
    response = confirm(client, ticket, page, next_status="new")
    assert response.status_code == 400
    assert len(held(db_session)) == 1 and db_session.exec(select(Attachment)).all() == []


# ---- The preview says message, and where it goes -----------------------------------------------------------


def test_the_preview_says_message_and_that_it_goes_by_email_for_an_email_requester(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    page = preview(client, ticket).text
    assert "The message is recorded on the ticket and goes to the requester by email." in page
    assert "Send message</button>" in page and "Send email" not in page


# ---- Internal notes ----------------------------------------------------------------------------------------


def post_note(client, ticket, *items, body="Checked the log", **extra):
    return client.post(f"/tickets/{ticket.number}/note", data={"body": body, **extra}, files=upload(*items) or None)


def test_files_on_an_internal_note_are_stored_with_it_and_listed_for_agents_only(client, db_session, storage, dev_mail):
    ticket = ticket_for_email(client, db_session)

    response = post_note(client, ticket, ("log.pdf", PDF))

    assert response.status_code == 200 and "log.pdf" in response.text
    db_session.expire_all()
    note = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.body == "Checked the log")).one()
    file = db_session.exec(select(Attachment).where(Attachment.message_id == note.id)).one()
    assert (file.filename, file.size) == ("log.pdf", len(PDF)) and storage.exists(file.blob_key)
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).all() == []
    assert db_session.exec(select(TeamsPost)).all() == []


def test_a_notes_file_never_reaches_the_portal_or_the_requester(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    post_note(client, ticket, ("secret.pdf", PDF))
    file = db_session.exec(select(Attachment)).one()
    client.post("/auth/logout")

    login_as(client, "walkin@example.com", "Walk In")
    page = client.get(f"/portal/{ticket.number}")
    assert page.status_code == 200 and "secret.pdf" not in page.text and "Checked the log" not in page.text
    assert client.get(f"/attachments/{file.id}").status_code == 404
    requester = db_session.exec(select(User).where(User.email == "walkin@example.com")).one()
    assert not may_view_attachment(db_session, requester, file, db_session.get(Ticket, ticket.id), access_context(db_session, requester))


def test_a_note_with_a_refused_file_is_not_added_at_all(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    before = recorded(db_session)
    response = post_note(client, ticket, ("setup.exe", b"MZ" + b"\x00" * 20))
    assert response.status_code == 400 and "Files ending .exe cannot be attached" in response.text
    assert recorded(db_session) == before and db_session.exec(select(Attachment)).all() == []


def test_a_note_takes_no_more_files_than_the_limit(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "1")
    db_session.commit()
    assert post_note(client, ticket, ("a.pdf", PDF), ("b.pdf", PDF + b"1")).status_code == 400


def test_a_note_may_carry_more_than_one_emails_worth_of_files(client, db_session, storage):
    """The Graph limit is about the email; a note is never emailed."""
    ticket = ticket_for_email(client, db_session)
    big = b"%PDF-1.7\n" + b"x" * 1_500_000
    assert post_note(client, ticket, ("a.pdf", big), ("b.pdf", big + b"1")).status_code == 200


# ---- What the requester sees of an email's files ------------------------------------------------------------


def test_the_requester_sees_and_downloads_the_files_of_an_email_in_the_portal(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    confirm(client, ticket, preview_with(client, ticket, ("quote.pdf", PDF)).text)
    file = db_session.exec(select(Attachment)).one()
    client.post("/auth/logout")

    login_as(client, "walkin@example.com", "Walk In")
    page = client.get(f"/portal/{ticket.number}").text
    assert f'<a href="/attachments/{file.id}">quote.pdf</a>' in page
    assert client.get(f"/attachments/{file.id}", follow_redirects=False).status_code == 200


# ---- Anyone, not only the owner, is held to the same rule ----------------------------------------------------


def test_the_agent_page_lists_the_files_of_an_email(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    confirm(client, ticket, preview_with(client, ticket, ("quote.pdf", PDF)).text)
    page = client.get(f"/tickets/{ticket.number}").text
    file = db_session.exec(select(Attachment)).one()
    assert f'href="/attachments/{file.id}">quote.pdf' in page


# ---- Teams replies ------------------------------------------------------------------------------------------

from app import messages  # noqa: E402
from app.attachments import NewFile  # noqa: E402
from tests.test_teams_bot import SERVICE, activity, make_user, post, token  # noqa: E402,F401
from tests.test_teams_bot import bot, metadata  # noqa: E402,F401
from tests.test_teams_intake import (  # noqa: E402,F401  (fixtures)
    Sent, an_agent, ann, connector, downloads, isolate, public, roster, say, tickets, triaged,
)


def test_a_teams_reply_with_files_says_so_and_points_to_the_portal_and_the_bot_sends_no_file(public, db_session, ann, dev_mail, storage):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    agent = an_agent(db_session)

    messages.email_requester(db_session, ticket, agent, "Please see the quote", files=[NewFile("quote.pdf", PDF)], storage=storage)
    db_session.commit()

    assert db_session.exec(select(OutboundEmail)).all() == []  # Teams only, the default: the email does not go
    sent = Sent()
    assert sent.deliver(db_session) == (1, 0)
    [delivery] = sent.direct
    assert delivery.card is None
    assert delivery.text.endswith(f"\n\n1 file attached: open it in the portal: https://tickets.example.org/portal/{ticket.number}")
    assert "quote.pdf" not in delivery.text  # not even the name: the bot sends words only


def test_two_files_are_counted_and_no_link_still_says_where_to_look(public, db_session, ann, dev_mail, storage, monkeypatch):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(
        db_session, ticket, an_agent(db_session), "Two files", files=[NewFile("a.pdf", PDF), NewFile("b.pdf", PDF + b"1")], storage=storage,
    )
    db_session.commit()
    monkeypatch.setattr(settings, "public_base_url", "")
    sent = Sent()
    sent.deliver(db_session)
    assert sent.direct[0].text.endswith("\n\n2 files attached: open it in the portal.")


def test_a_teams_reply_without_files_has_no_file_line(public, db_session, ann, dev_mail):
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    messages.email_requester(db_session, ticket, an_agent(db_session), "No files")
    db_session.commit()
    sent = Sent()
    sent.deliver(db_session)
    assert "attached" not in sent.direct[0].text and sent.direct[0].text.endswith(f"Open in the portal: https://tickets.example.org/portal/{ticket.number}")


def test_when_the_person_chose_teams_and_email_the_email_copy_carries_the_file(public, db_session, ann, dev_mail, storage):
    ann.teams_replies_also_email = True
    db_session.add(ann)
    db_session.commit()
    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    message = messages.email_requester(db_session, ticket, an_agent(db_session), "See file", files=[NewFile("quote.pdf", PDF)], storage=storage)
    db_session.commit()

    [email] = db_session.exec(select(OutboundEmail)).all()
    file = db_session.exec(select(Attachment).where(Attachment.message_id == message.id)).one()
    assert email.attachment_ids == str(file.id) and len(db_session.exec(select(TeamsPost)).all()) == 1
    assert [name for name, _, _ in mail.build_message_mail(db_session, email).files] == ["quote.pdf"]


def test_the_preview_says_where_a_message_goes_and_what_a_teams_requester_is_told_about_files(public, db_session, ann, dev_mail):
    from app.templating import templates

    say(public, "Printer is jammed")
    ticket = triaged(db_session, tickets(db_session)[0])
    preview = messages.preview_email(db_session, ticket, "Hello")
    assert (preview.by_teams, preview.also_email) == (True, False)
    preview.files = [("quote.pdf", 209)]
    html = " ".join(templates.get_template("_email_preview.html").render(ticket=ticket, preview=preview, draft="abc", ticked=[], cc_form=False).split())
    assert "(in Teams)" in html and "goes to the requester in Teams." in html and "Send message" in html
    assert "In Teams the requester is told 1 file is attached and given the portal link to open it." in html
    assert "<b>Files:</b>" in html and "quote.pdf" in html



# ---- Found in review -------------------------------------------------------------------------------------


def test_a_preview_confirmed_twice_sends_one_message(client, db_session, storage, dev_mail):
    ticket = ticket_for_email(client, db_session)
    page = preview_with(client, ticket, ("quote.pdf", PDF)).text
    assert confirm(client, ticket, page).status_code == 200
    again = confirm(client, ticket, page)
    assert again.status_code == 400 and "no longer held" in again.text
    assert len(db_session.exec(select(Attachment)).all()) == 1
    assert len(db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).all()) == 1


def test_files_too_big_for_one_email_are_refused_before_any_is_read(client, db_session, storage, monkeypatch):
    ticket = ticket_for_email(client, db_session)
    monkeypatch.setattr(reply_files, "check_file", lambda *a: pytest.fail("a file was read and checked"))
    big = b"%PDF-1.7\n" + b"x" * 1_200_000
    response = preview_with(client, ticket, ("a.pdf", big), ("b.pdf", big + b"1"))
    assert response.status_code == 400 and "one email can carry" in response.text


def test_one_wording_of_a_file_size():
    from app.templating import _file_size

    assert [_file_size(n) for n in (209, 1023, 1024, 2970)] == ["209 bytes", "1023 bytes", "1.0 KB", "2.9 KB"]

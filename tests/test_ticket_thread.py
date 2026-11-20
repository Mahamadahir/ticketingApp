"""#255, the ticket page redesign part B: the activity thread (newest first, one box per action, changes saved together as one box,
quoted email history folded) and the ... menu on a note (show or hide it from the requester, delete with Undo, the purge, and what
data protection still sees). What a requester may see is still only `messages.requester_visible_messages`; these tests pin the
agent thread's markup and the new note routes."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select

from app.attachments import store_attachment
from app.main import app
from app.messages import (
    EVENT_NOTE_VISIBILITY,
    add_note,
    add_visible_note,
    agent_timeline,
    delete_note,
    requester_visible_messages,
    set_note_visible,
    undo_delete_note,
)
from app.models import (
    Attachment,
    AuditLog,
    Message,
    MessageDirection,
    MessageVisibility,
    Notification,
    OutboundEmail,
    PendingBlobDelete,
    TeamsPost,
    Ticket,
    TicketEvent,
    User,
    UserRole,
)
from app.msp import SCOPE_THREAD, _thread_messages
from app.note_purge import purge_deleted_notes
from app.notifications import mark_read
from app.quoted_text import split_quoted
from app.reply_channel import AGENT, EMAIL, PORTAL
from app.search import search_condition
from app.settings_store import NOTE_KEEP_DELETED_DAYS, REGISTRY, GROUP_TICKET_PAGE
from app.storage import LocalStorage, get_storage
from app.thread import build_thread
from app.ticket_service import record_event
from app.visibility import access_context
from tests.conftest import add_membership, create_team, login_as, set_role
from tests.test_sla import triaged_ticket
from tests.test_subject_access import world  # noqa: F401  (a fixture)
from tests.test_ticket_redesign import counts, newest, page, post_visible
from tests.test_triage import agent

HTMX = {"HX-Request": "true"}


@pytest.fixture
def second():
    with TestClient(app) as other:
        yield other


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


def thread_html(client, ticket, **query):
    return page(client, ticket, **query).text.split('id="thread"')[1].split("</ol>")[0]


def boxes(html):
    """The thread's boxes as text, in page order (newest first)."""
    return html.split("<article ")[1:]


def add(db, ticket, body, direction, *, minutes_ago=0, channel=None, sender="Someone", author=None, visibility=None, **extra):
    message = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, channel=channel,
        visibility=visibility or (MessageVisibility.AGENTS_ONLY if direction == MessageDirection.INTERNAL else MessageVisibility.PUBLIC),
        author_user_id=author, created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **extra,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def user_id(db, email):
    return db.exec(select(User.id).where(User.email == email)).one()


def audits(db, action):
    db.expire_all()
    return db.exec(select(AuditLog).where(AuditLog.action == action)).all()


def events_of(db, ticket, kind):
    db.expire_all()
    return db.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


# ---- Order, kinds and labels ------------------------------------------------------------------------------------


def test_the_thread_is_newest_first(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, "First words", MessageDirection.INBOUND, minutes_ago=30, channel=EMAIL)
    add(db_session, ticket, "Middle words", MessageDirection.OUTBOUND, minutes_ago=20, channel=EMAIL)
    add(db_session, ticket, "Last words", MessageDirection.INBOUND, minutes_ago=10, channel=EMAIL)
    html = thread_html(client, ticket)
    assert html.index("Last words") < html.index("Middle words") < html.index("First words")
    assert html.index("Triaged this ticket") < html.index("Last words")  # the triage was done just now, so it is the newest box


def test_each_kind_has_its_box_and_label(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    me = user_id(db_session, "agent@example.com")
    add(db_session, ticket, "Raising words", MessageDirection.INBOUND, minutes_ago=50, channel=EMAIL, sender="Ann Able <me@example.com>")
    add(db_session, ticket, "Out words", MessageDirection.OUTBOUND, minutes_ago=40, channel=EMAIL, sender="Agent", author=me, cc="x@example.com")
    add(db_session, ticket, "Reply words", MessageDirection.INBOUND, minutes_ago=30, channel=EMAIL, sender="Ann Able <me@example.com>")
    add(db_session, ticket, "Portal words", MessageDirection.INBOUND, minutes_ago=25, channel=PORTAL, sender="Ann Able")
    add(db_session, ticket, "Secret words", MessageDirection.INTERNAL, minutes_ago=20, channel=AGENT, sender="Agent", author=me)
    add(db_session, ticket, "Shown words", MessageDirection.OUTBOUND, minutes_ago=15, channel=AGENT, sender="Agent", author=me, visible_note=True)
    record_event(db_session, ticket, "reopened", None, detail="The requester replied")
    db_session.commit()
    by = {k: next(b for b in boxes(thread_html(client, ticket)) if k in b) for k in
          ("Raising words", "Out words", "Reply words", "Portal words", "Secret words", "Shown words", "The requester replied")}

    assert "Raised by email" in by["Raising words"] and "tp-msg-email-in" in by["Raising words"]
    assert "Emailed the requester" in by["Out words"] and "tp-msg-email-out" in by["Out words"] and "Copied: x@example.com" in by["Out words"]
    assert "Replied by email" in by["Reply words"]
    assert "Replied in the portal" in by["Portal words"]
    assert "Internal note · agents only" in by["Secret words"] and "tp-msg-note" in by["Secret words"]
    assert "Note · visible to the requester" in by["Shown words"] and "Shown in the portal. Not sent by email." in by["Shown words"]
    assert "tp-msg-visible" in by["Shown words"] and "tp-msg-note" not in by["Shown words"]
    assert "tp-msg-system" in by["The requester replied"] and "Reopened the ticket" in by["The requester replied"]


def test_a_box_names_the_person_with_their_address_on_hover_and_never_prints_an_address_in_a_sentence(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    bob = login_as(TestClient(app), "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, desk.id)
    record_event(db_session, ticket, "assigned", "agent@example.com", None, "bob@example.com")
    record_event(db_session, ticket, "follower_added", "agent@example.com", None, "stranger.person@elsewhere.example")
    db_session.commit()
    html = thread_html(client, ticket)
    assert "Assigned the ticket to Bob Rowe." in html
    assert "Added Stranger Person as a follower." in html
    assert "bob@example.com" not in html.replace('title="bob@example.com"', "") and "stranger.person@elsewhere.example" not in html
    assert 'title="agent@example.com"' in html  # the person's address is on hover


def test_changes_show_their_values_in_a_plain_sentence_and_a_reason(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low", "reason": "Fewer people hit"})
    html = thread_html(client, ticket)
    assert re.search(r"Changed the priority from P1 \(high impact, high urgency\) to P4 \(low impact, low urgency\)\. Reason: Fewer people hit", html)


def test_there_is_no_show_more_folding(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, "Line\n" * 200, MessageDirection.INBOUND, channel=EMAIL)
    html = page(client, ticket).text
    assert "Show more" not in html and "show more" not in html and "Read more" not in html
    assert html.count("Line\n") >= 200  # the whole body is on the page


# ---- Changes saved together --------------------------------------------------------------------------------------


def test_events_written_by_one_save_share_a_group_and_a_new_save_gets_a_new_one(db_session, client):
    ticket, _ = triaged_ticket(client, db_session)
    record_event(db_session, ticket, "type_changed", "agent@example.com", "incident", "problem")
    record_event(db_session, ticket, "due_changed", "agent@example.com", "none", "2026-12-01")
    db_session.commit()
    record_event(db_session, ticket, "required_by_changed", "agent@example.com", "none", "2026-12-02")
    db_session.commit()
    rows = {e.event_type: e for e in db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all()}
    assert rows["type_changed"].group_id and rows["type_changed"].group_id == rows["due_changed"].group_id
    assert rows["required_by_changed"].group_id != rows["type_changed"].group_id
    assert rows["type_changed"].created_at != rows["due_changed"].created_at  # the timestamps alone could not say it


def test_changes_saved_together_are_one_box_and_separate_saves_are_separate_boxes(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    record_event(db_session, ticket, "type_changed", "agent@example.com", "incident", "problem")
    record_event(db_session, ticket, "due_changed", "agent@example.com", "none", "2026-12-01")
    db_session.commit()
    record_event(db_session, ticket, "required_by_changed", "agent@example.com", "none", "2026-12-02")
    db_session.commit()
    found = boxes(thread_html(client, ticket))
    together = [b for b in found if "Changed the type from Incident to Problem." in b]
    assert len(together) == 1 and "Changed the Due date from none to 2026-12-01." in together[0]
    alone = [b for b in found if "Changed Required by" in b]
    assert len(alone) == 1 and "Changed the type" not in alone[0]


def test_a_real_save_that_changes_two_things_is_one_box(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    other = create_team(db_session, "Network")
    bob = login_as(TestClient(app), "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, other.id)
    response = client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": other.id, "agent_id": str(bob)})
    assert response.status_code == 200
    kinds = {e.event_type: e.group_id for e in db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all()}
    assert kinds["team_changed"] and kinds["team_changed"] == kinds["assigned"]
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    built = build_thread(db_session, ticket, agent_timeline(db_session, ticket), me, access_context(db_session, me), None, {})
    moved = [b for b in built if any("Moved the ticket from Service Desk to Network." in line for line in b.lines)]
    assert len(moved) == 1 and any("Assigned the ticket to Bob Rowe." in line for line in moved[0].lines)


def test_two_requests_make_two_groups(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low", "reason": "one"})
    client.post(f"/tickets/{ticket.number}/priority", data={"impact": "high", "urgency": "high", "reason": "two"})
    events = events_of(db_session, ticket, "priority_changed")
    assert len(events) == 2 and events[0].group_id != events[1].group_id
    assert len([b for b in boxes(thread_html(client, ticket)) if "Changed the priority" in b]) == 2


def test_triage_is_one_box_listing_its_fields(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    found = [b for b in boxes(thread_html(client, ticket)) if "Triaged this ticket" in b]
    assert len(found) == 1
    box = found[0]
    for label, value in (("Type", "Incident"), ("Category", "Hardware"), ("Team", "Service Desk"), ("Priority", "P1 (high impact, high urgency)"),
                         ("Assigned to", "Unassigned")):
        assert re.search(rf"<dt>{label}</dt><dd>{re.escape(value)}</dd>", box), label
    assert "Changed the status" not in box and "Moved the ticket" not in box  # nothing the triage did is repeated as its own line


def test_resolving_is_a_resolved_box_with_the_notes(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    response = client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Replaced the toner", "confirm": "1"})
    assert response.status_code == 200
    found = [b for b in boxes(thread_html(client, ticket)) if "Resolved the ticket" in b]
    assert len(found) == 1 and "Replaced the toner" in found[0] and "tp-msg-resolved" in found[0]


# ---- Quoted email history ----------------------------------------------------------------------------------------

GMAIL = "Thanks, that fixed it.\n\nOn Tue, 17 Nov 2026 at 10:15, IT Support <it@example.com> wrote:\n> Have you tried turning it off?\n> Regards"
GMAIL_WRAPPED = "Still broken\n\nOn Tue, 17 Nov 2026 at 10:15, IT Support\n<it@example.com> wrote:\n\nHave you tried turning it off?"
IPHONE = "Yes please\n\nSent from my iPhone\n\nOn 17 Nov 2026, at 10:15, IT Support <it@example.com> wrote:\n\n> Shall I close it?"
OUTLOOK = "All good now.\n\nFrom: IT Support <it@example.com>\nSent: 17 November 2026 10:15\nTo: Ann Able\nSubject: [INC-1] Printer\n\nHave you tried turning it off?"
OUTLOOK_RULE = "Done.\n\n________________________________\nFrom: IT Support <it@example.com>\nSent: Tuesday, 17 November 2026 10:15\nTo: Ann\nCc: Bob\nSubject: Re: Printer\n\nold words"
ORIGINAL = "OK\n\n-----Original Message-----\nFrom: IT\nSent: Tuesday\nTo: Ann\nSubject: Printer\n\nold words"
QUOTE_BLOCK = "Agreed\n\n> earlier line one\n> earlier line two\n"


@pytest.mark.parametrize(
    "body,kept",
    [(GMAIL, "Thanks, that fixed it."), (GMAIL_WRAPPED, "Still broken"), (IPHONE, "Yes please\n\nSent from my iPhone"),
     (OUTLOOK, "All good now."), (OUTLOOK_RULE, "Done."), (ORIGINAL, "OK"), (QUOTE_BLOCK, "Agreed")],
)
def test_quoted_history_is_split_off_in_common_formats_and_nothing_is_lost(body, kept):
    visible, quoted = split_quoted(body)
    assert visible == kept and quoted
    assert (visible + "\n" + quoted).split() == body.split()  # every word is in one half or the other


@pytest.mark.parametrize(
    "body",
    [
        "Just a plain reply\nwith two lines",
        "From: IT <it@example.com>\nSent: Tuesday\nTo: Ann\nSubject: forwarded\n\nforwarded words",  # nothing of their own above it
        "He wrote: it is fine\nOn the other hand I think",  # looks a little like it but is not an attribution
        "",
    ],
)
def test_where_nothing_is_recognised_the_whole_email_is_shown(body):
    assert split_quoted(body) == (body, None)


def test_the_page_folds_the_quoted_text_of_an_emailed_reply_behind_a_toggle(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, GMAIL, MessageDirection.INBOUND, minutes_ago=5, channel=EMAIL, sender="Ann Able <me@example.com>")
    add(db_session, ticket, "A plain reply with nothing quoted", MessageDirection.INBOUND, minutes_ago=3, channel=EMAIL, sender="Ann Able <me@example.com>")
    found = boxes(thread_html(client, ticket))
    folded = next(b for b in found if "Thanks, that fixed it." in b)
    plain = next(b for b in found if "A plain reply" in b)
    assert "<details class=\"tp-quoted\">" in folded and "Show quoted text" in folded and "Have you tried turning it off?" in folded
    assert folded.index("Thanks, that fixed it.") < folded.index("Show quoted text") < folded.index("Have you tried turning it off?")
    assert "Show quoted text" not in plain and "tp-quoted" not in plain


def test_only_a_requesters_emailed_reply_is_split(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, GMAIL, MessageDirection.OUTBOUND, minutes_ago=5, channel=EMAIL)
    add(db_session, ticket, GMAIL.replace("fixed it", "noted"), MessageDirection.INTERNAL, minutes_ago=4, channel=AGENT)
    assert "Show quoted text" not in thread_html(client, ticket)


# ---- The ... menu on a note --------------------------------------------------------------------------------------


def menu_on(html, text):
    box = next(b for b in boxes(html) if text in b)
    return "Options for this note" in box


def test_only_the_author_and_super_admins_see_the_menu(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Author words"})
    post_visible(client, ticket, "Visible author words")
    add(db_session, ticket, "An email", MessageDirection.OUTBOUND, channel=EMAIL)
    assert menu_on(thread_html(client, ticket), "Author words")
    assert menu_on(thread_html(client, ticket), "Visible author words")
    assert not menu_on(thread_html(client, ticket), "An email")  # an email is already sent: no menu

    colleague = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, colleague, UserRole.AGENT, desk.id)
    html = thread_html(second, ticket)
    assert "Author words" in html and not menu_on(html, "Author words") and not menu_on(html, "Visible author words")

    admin = TestClient(app)
    set_role(db_session, login_as(admin, "root@example.com", "Root Admin"), UserRole.SUPER_ADMIN)
    html = thread_html(admin, ticket)
    assert menu_on(html, "Author words") and menu_on(html, "Visible author words")


def test_another_agent_is_refused_by_the_routes_too(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Author words"})
    note = newest(db_session, ticket)
    colleague = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, colleague, UserRole.AGENT, desk.id)
    for action, data in (("visibility", {"visible": "1"}), ("delete", {}), ("undo", {})):
        assert second.post(f"/tickets/{ticket.number}/notes/{note.id}/{action}", data=data).status_code == 403
    db_session.refresh(note)
    assert note.direction == MessageDirection.INTERNAL and note.deleted_at is None


def test_an_email_cannot_be_deleted_or_shown_or_hidden(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    email = add(db_session, ticket, "An email", MessageDirection.OUTBOUND, channel=EMAIL)
    for action in ("visibility", "delete"):
        assert client.post(f"/tickets/{ticket.number}/notes/{email.id}/{action}", data={"visible": "0"}).status_code == 404


def test_showing_a_note_to_the_requester_records_an_event_and_audit_and_sends_nothing(client, second, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Your part is on order"})
    note = newest(db_session, ticket)
    requester = login_as(second, "me@example.com")
    before = counts(db_session)
    assert "Your part is on order" not in second.get(f"/portal/{ticket.number}").text

    response = client.post(f"/tickets/{ticket.number}/notes/{note.id}/visibility", data={"visible": "1"}, headers=HTMX)
    assert response.status_code == 200
    db_session.refresh(note)
    assert (note.direction, note.visible_note) == (MessageDirection.OUTBOUND, True)
    assert counts(db_session) == before  # no email, no Teams post, no bell
    assert not db_session.exec(select(Notification).where(Notification.user_id == requester)).all()
    assert "Your part is on order" in second.get(f"/portal/{ticket.number}").text
    event = events_of(db_session, ticket, EVENT_NOTE_VISIBILITY)[0]
    assert (event.to_value, event.actor_email) == ("visible", "agent@example.com")
    assert audits(db_session, "note_made_visible")
    assert "Made a note visible to the requester." in thread_html(client, ticket)
    assert note.id in {m.id for m in requester_visible_messages(db_session, ticket)}


def test_hiding_a_visible_note_works_and_the_menu_warns_the_requester_may_have_read_it(client, second, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    post_visible(client, ticket)
    note = newest(db_session, ticket)
    login_as(second, "me@example.com")
    assert "Your part is on order" in second.get(f"/portal/{ticket.number}").text
    html = thread_html(client, ticket)
    assert "The requester may already have read this note. Hiding it removes it from the portal from now on." in html
    assert "Show to the requester" not in html  # it is visible already: the menu offers Hide

    assert client.post(f"/tickets/{ticket.number}/notes/{note.id}/visibility", data={"visible": "0"}).status_code == 200
    db_session.refresh(note)
    assert (note.direction, note.visibility, note.visible_note) == (MessageDirection.INTERNAL, MessageVisibility.AGENTS_ONLY, False)
    assert "Your part is on order" not in second.get(f"/portal/{ticket.number}").text
    assert events_of(db_session, ticket, EVENT_NOTE_VISIBILITY)[0].to_value == "hidden"
    assert audits(db_session, "note_hidden")
    html = thread_html(client, ticket)
    assert "Show to the requester" in html and "Hid a note from the requester." in html


def test_the_menu_offers_show_on_an_internal_note_and_hide_on_a_visible_one(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Private words"})
    html = thread_html(client, ticket)
    assert "Show to the requester" in html and "Hide from the requester" not in html and ">Delete<" in html


def test_a_note_made_visible_before_triage_stays_held_back_there(client, second, db_session):
    desk = create_team(db_session, "Service Desk")
    from tests.test_triage import new_ticket

    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Early words"})
    note = newest(db_session, ticket)
    login_as(second, "me@example.com")
    assert client.post(f"/tickets/{ticket.number}/notes/{note.id}/visibility", data={"visible": "1"}).status_code == 200
    db_session.refresh(note)
    assert note.visible_note and note.id not in {m.id for m in requester_visible_messages(db_session, ticket)}
    assert "Early words" not in second.get(f"/portal/{ticket.number}").text


def test_asking_for_the_state_it_is_already_in_changes_nothing(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Private words"})
    note = newest(db_session, ticket)
    assert client.post(f"/tickets/{ticket.number}/notes/{note.id}/visibility", data={"visible": "0"}).status_code == 200
    assert not events_of(db_session, ticket, EVENT_NOTE_VISIBILITY)


def test_showing_a_note_that_has_files_warns_that_the_files_become_visible_too(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    note = add_note(db_session, ticket, me, "Note with a log")
    store_attachment(db_session, storage, ticket=ticket, filename="log.txt", data=b"log", message=note, uploaded_by=me)
    db_session.commit()
    assert "The file attached to this note will be visible to the requester too." in thread_html(client, ticket)


def test_msp_mail_keeps_its_label_and_the_unverified_warning(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, "From the supplier", MessageDirection.OUTBOUND, channel=EMAIL, sender="MSP Desk", msp_forward="pending", msp_unverified=True)
    box = next(b for b in boxes(thread_html(client, ticket)) if "From the supplier" in b)
    assert "From the MSP" in box and "MSP mail" in box and "sender not verified" in box and "Emailed the requester" not in box
    assert "agents only until forwarded" in box


def test_a_note_visibility_event_never_holds_the_notes_words(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "A secret password hunter2"})
    note = newest(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/notes/{note.id}/visibility", data={"visible": "1"})
    event = events_of(db_session, ticket, EVENT_NOTE_VISIBILITY)[0]
    client.post(f"/tickets/{ticket.number}/notes/{note.id}/delete")
    assert event.detail is None and "hunter2" not in thread_html(client, ticket)


# ---- Delete and Undo ---------------------------------------------------------------------------------------------


def test_delete_hides_the_note_everywhere_and_leaves_an_undo_in_its_place(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    post_visible(client, ticket, "Deletable visible words")
    visible = newest(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Deletable private words"})
    private = newest(db_session, ticket)
    login_as(second, "me@example.com")
    assert "Deletable visible words" in second.get(f"/portal/{ticket.number}").text
    assert ticket.id in db_session.exec(select(Ticket.id).where(search_condition(db_session, "Deletable"))).all()
    assert any(m.id == visible.id for m in _thread_messages(db_session, ticket, SCOPE_THREAD))
    assert "Deletable private words" in client.get("/tickets/activity").text

    for note in (visible, private):
        assert client.post(f"/tickets/{ticket.number}/notes/{note.id}/delete", headers=HTMX).status_code == 200
    db_session.expire_all()
    assert visible.deleted_at is not None and visible.deleted_by_id == user_id(db_session, "agent@example.com")

    html = thread_html(client, ticket)
    assert "Deletable" not in html
    assert html.count("Note deleted") == 2 and "Undo" in html
    assert "Deletable visible words" not in second.get(f"/portal/{ticket.number}").text
    assert not db_session.exec(select(Ticket.id).where(search_condition(db_session, "Deletable"))).all()
    assert not [m for m in _thread_messages(db_session, ticket, SCOPE_THREAD) if m.id in (visible.id, private.id)]
    assert "Deletable" not in client.get("/tickets/activity").text
    assert not [m for m in requester_visible_messages(db_session, ticket) if m.id == visible.id]
    assert len(audits(db_session, "note_deleted")) == 2

    # Undo brings each back
    assert client.post(f"/tickets/{ticket.number}/notes/{visible.id}/undo").status_code == 200
    db_session.expire_all()
    assert visible.deleted_at is None and visible.deleted_by_id is None and visible.visible_note
    assert "Deletable visible words" in second.get(f"/portal/{ticket.number}").text
    assert "Deletable visible words" in thread_html(client, ticket) and "Deletable private words" not in thread_html(client, ticket)
    assert audits(db_session, "note_restored")


def test_the_deleted_place_offers_undo_only_to_those_who_may(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Gone words"})
    note = newest(db_session, ticket)
    client.post(f"/tickets/{ticket.number}/notes/{note.id}/delete")
    assert re.search(r"Note deleted</b>.*?by Test User", thread_html(client, ticket), re.S)
    assert f"/notes/{note.id}/undo" in thread_html(client, ticket)
    colleague = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, colleague, UserRole.AGENT, desk.id)
    html = thread_html(second, ticket)
    assert "Note deleted" in html and "/undo" not in html and "Gone words" not in html


def test_deleting_withdraws_the_unread_mentions_and_undo_does_not_bring_them_back(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    bob = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, desk.id)
    carol = login_as(TestClient(app), "carol@example.com", "Carol Cole")
    set_role(db_session, carol, UserRole.AGENT, desk.id)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Please look @bob and @carol"})
    note = newest(db_session, ticket)
    rows = db_session.exec(select(Notification).where(Notification.kind == "mentioned")).all()
    assert {r.user_id for r in rows} == {bob, carol} and all(r.message_id == note.id for r in rows)
    mark_read(db_session, db_session.get(User, carol), next(r.id for r in rows if r.user_id == carol))
    db_session.commit()

    client.post(f"/tickets/{ticket.number}/notes/{note.id}/delete")
    db_session.expire_all()
    bobs = next(r for r in db_session.exec(select(Notification).where(Notification.user_id == bob)).all())
    carols = next(r for r in db_session.exec(select(Notification).where(Notification.user_id == carol)).all())
    assert bobs.dismissed_at is not None and bobs.read_at is not None  # withdrawn: gone from the bell
    assert carols.dismissed_at is None  # she had read it: left alone
    from app.notifications import count_unread

    assert count_unread(db_session, db_session.get(User, bob)) == 0
    client.post(f"/tickets/{ticket.number}/notes/{note.id}/undo")
    db_session.expire_all()
    assert db_session.get(Notification, bobs.id).dismissed_at is not None
    assert count_unread(db_session, db_session.get(User, bob)) == 0


def test_a_deleted_notes_files_are_hidden_at_once(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    note = add_note(db_session, ticket, me, "Note with a file")
    file = store_attachment(db_session, storage, ticket=ticket, filename="log.txt", data=b"some log text", message=note, uploaded_by=me)
    db_session.commit()
    assert client.get(f"/attachments/{file.id}").status_code == 200
    assert "log.txt" in page(client, ticket).text

    client.post(f"/tickets/{ticket.number}/notes/{note.id}/delete")
    assert client.get(f"/attachments/{file.id}").status_code in (403, 404)
    assert "log.txt" not in page(client, ticket).text
    client.post(f"/tickets/{ticket.number}/notes/{note.id}/undo")
    assert client.get(f"/attachments/{file.id}").status_code == 200


def test_a_deleted_note_never_triggers_the_send_warning(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    bob = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, desk.id)
    from app.messages import latest_message_id, posts_since

    seen = latest_message_id(db_session, ticket)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob typed this"})
    bobs_note = newest(db_session, ticket)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    assert [m.id for m in posts_since(db_session, ticket, me, seen)] == [bobs_note.id]
    second.post(f"/tickets/{ticket.number}/notes/{bobs_note.id}/delete")
    assert posts_since(db_session, ticket, me, seen) == []


# ---- The purge ---------------------------------------------------------------------------------------------------


def test_the_setting_is_whole_days_one_to_a_thousand_default_one_on_the_ticket_page_group():
    definition = REGISTRY[NOTE_KEEP_DELETED_DAYS]
    assert (definition.kind, definition.default, definition.group) == ("count", "1", GROUP_TICKET_PAGE)


def test_the_purge_removes_a_note_its_attachments_and_files_after_the_window_and_not_before(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    note = add_note(db_session, ticket, me, "Purge me")
    kept = add_note(db_session, ticket, me, "Keep me")
    file = store_attachment(db_session, storage, ticket=ticket, filename="a.txt", data=b"purge-me-bytes", message=note, uploaded_by=me)
    shared = store_attachment(db_session, storage, ticket=ticket, filename="b.txt", data=b"shared-bytes", message=note, uploaded_by=me)
    other = store_attachment(db_session, storage, ticket=ticket, filename="c.txt", data=b"shared-bytes", message=kept, uploaded_by=me)
    key, shared_key = file.blob_key, shared.blob_key
    note_id, kept_id = note.id, kept.id
    assert shared_key == other.blob_key
    db_session.commit()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    for n in (note, kept):
        delete_note(db_session, ticket, n, me, now=now - timedelta(hours=23))
    db_session.commit()

    assert purge_deleted_notes(db_session, now=now, storage=storage) == 0  # 23 hours: still inside the one day
    assert db_session.get(Message, note_id) is not None and storage.exists(key)

    assert purge_deleted_notes(db_session, now=now + timedelta(hours=2), storage=storage) == 2  # past the window
    db_session.expire_all()
    assert db_session.get(Message, note_id) is None and db_session.get(Message, kept_id) is None
    assert not db_session.exec(select(Attachment).where(Attachment.ticket_id == ticket.id)).all()
    assert not storage.exists(key) and not storage.exists(shared_key)
    assert not db_session.exec(select(PendingBlobDelete)).all()


def test_the_purge_works_from_deleted_at_and_the_setting_not_from_the_last_run(client, db_session, storage):
    from app.settings_store import set_setting

    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    note = add_note(db_session, ticket, me, "Three day note")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    delete_note(db_session, ticket, note, me, now=now - timedelta(days=2))
    set_setting(db_session, NOTE_KEEP_DELETED_DAYS, "3")
    db_session.commit()
    assert purge_deleted_notes(db_session, now=now, storage=storage) == 0
    assert purge_deleted_notes(db_session, now=now + timedelta(days=1, minutes=1), storage=storage) == 1
    assert purge_deleted_notes(db_session, now=now + timedelta(days=5), storage=storage) == 0  # a second run ends in the same place


def test_after_the_purge_undo_is_a_404_and_a_note_that_was_not_deleted_is_never_purged(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    gone = add_note(db_session, ticket, me, "Gone for good")
    alive = add_note(db_session, ticket, me, "Still here")
    db_session.commit()
    gone_id, alive_id = gone.id, alive.id
    delete_note(db_session, ticket, gone, me, now=datetime.now(timezone.utc) - timedelta(days=3))
    db_session.commit()
    purge_deleted_notes(db_session, storage=storage)
    assert client.post(f"/tickets/{ticket.number}/notes/{gone_id}/undo").status_code == 404
    assert db_session.get(Message, alive_id) is not None


def test_the_purge_is_one_line_in_the_recurring_jobs():
    from app.scheduled_jobs import SCHEDULED_JOBS, registry_errors

    job = next(j for j in SCHEDULED_JOBS if j.name == "note-purge")
    assert job.run is purge_deleted_notes and job.every_seconds == 86400
    assert not registry_errors(SCHEDULED_JOBS)


# ---- Data protection ---------------------------------------------------------------------------------------------


def test_a_deleted_but_not_purged_note_is_still_in_the_subject_access_export(client, db_session, world):
    from tests.test_subject_access import admin_client, export, part

    me = world["agent"]
    note = add_visible_note(db_session, world["own"], me, "Visible note that was then deleted")
    delete_note(db_session, world["own"], note, me)
    db_session.commit()
    admin_client(client, db_session)
    archive = export(client)
    texts = {n["body"]: n for t in part(archive, "internal_notes.json")["tickets"] for n in t["notes"]}
    assert "Visible note that was then deleted" in texts and texts["Visible note that was then deleted"]["deleted_at"]
    assert "Visible note that was then deleted" not in archive.read("tickets.json").decode()  # the requester can no longer see it


def test_anonymisation_covers_a_deleted_note(client, db_session, world, storage):
    from tests.test_anonymisation import anonymise

    me = world["agent"]
    note = add_note(db_session, world["own"], me, "Mentions ann@a.example by address")
    delete_note(db_session, world["own"], note, me)
    db_session.commit()
    from tests.test_subject_access import admin_client

    admin_client(client, db_session)
    assert anonymise(client).status_code == 200
    db_session.expire_all()
    assert "ann@a.example" not in (db_session.get(Message, note.id).body or "")

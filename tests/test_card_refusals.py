"""#266 to #268: a refused action on the agent ticket page shows its message in its own card and keeps the form as it was.
#266 Add note (text, the visible toggle, the status choice; files must be chosen again), #267 the confirm button of a preview
(Email send, Resolve, Close, Send to MSP, Take back), #268 the Triage card's select boxes. The card is drawn by `card_errors` in
app/routers/tickets.py; what is refused, and why, is decided where it always was."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import mail, reply_files
from app.main import app
from app.models import ReplyHeldFile, Ticket, UserRole
from app.settings_store import INBOUND_MAX_ATTACHMENTS, MSP_ADDRESS, set_setting
from app.storage import LocalStorage, get_storage
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_email_preview import ticket_for_email
from tests.test_request_questions import agent_of, email_ticket, question
from tests.test_request_type_defaults import world
from tests.test_request_types import make

HTMX = {"HX-Request": "true"}
PDF = b"%PDF-1.7\n" + b"x" * 200


@pytest.fixture
def storage(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    monkeypatch.setattr(mail, "get_storage", lambda: store)
    monkeypatch.setattr(reply_files, "get_storage", lambda: store)
    yield store
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def second():
    with TestClient(app) as other:
        yield other


def in_card(response, title, htmx):
    """The refusal is in the card (never retargeted to the top of the page) and the page is whole or a fragment as asked."""
    assert "HX-Retarget" not in response.headers
    assert response.status_code == (200 if htmx else 400)
    assert f'<h2 id="tp-card-title">{title}</h2>' in response.text and 'role="alert" id="tp-card-error"' in response.text
    assert ("<html" in response.text) is (not htmx)
    return re.search(r'id="tp-card-error">([^<]*)<', response.text).group(1)


def selected(html, name):
    """The value of the selected option of the select box called `name` ("" when none is marked)."""
    block = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', html, re.S).group(1)
    found = re.search(r'<option value="([^"]*)"\s+selected', block)
    return found.group(1) if found else ""


def status_choices(html):
    block = re.search(r'<select name="next_status"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [v for v in re.findall(r'<option value="([^"]+)"', block)]


# ---- #266: Add note --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True])
def test_an_empty_note_is_refused_in_the_note_card(client, db_session, htmx):
    ticket = ticket_for_email(client, db_session)
    response = client.post(f"/tickets/{ticket.number}/note", data={"body": "   "}, headers=HTMX if htmx else {})
    assert in_card(response, "Add a note", htmx) == "Write the note first"
    assert "Choose your files again" not in response.text  # no file was chosen, so nothing to say
    assert db_session.exec(select(Ticket)).one().id == ticket.id


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_note_keeps_its_text_the_visible_toggle_and_the_status_choice(client, db_session, storage, htmx):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "1")
    db_session.commit()
    status = status_choices(client.get(f"/tickets/{ticket.number}?action=note").text)[0]
    files = [("files", ("a.pdf", PDF, "application/pdf")), ("files", ("b.pdf", PDF, "application/pdf"))]
    response = client.post(
        f"/tickets/{ticket.number}/note",
        data={"body": "Keep this text", "visible_to_requester": "1", "next_status": status}, files=files, headers=HTMX if htmx else {},
    )
    assert "at most 1 file" in in_card(response, "Add a note", htmx)
    html = response.text
    assert "Keep this text</textarea>" in html
    assert re.search(r'name="visible_to_requester" value="1" checked', html)
    assert selected(html, "next_status") == status
    assert 'id="refused-files"' in html and "Choose your files again" in html
    # Nothing was recorded.
    db_session.expire_all()
    assert "Keep this text" not in [m.body for m in ticket.messages]


@pytest.mark.parametrize("htmx", [False, True])
def test_a_note_with_a_refused_file_is_shown_in_the_card_with_the_file_line(client, db_session, storage, htmx):
    ticket = ticket_for_email(client, db_session)
    response = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "With a bad file"},
        files=[("files", ("run.exe", b"MZ" + b"0" * 100, "application/octet-stream"))], headers=HTMX if htmx else {},
    )
    in_card(response, "Add a note", htmx)
    assert "With a bad file</textarea>" in response.text and "Choose your files again" in response.text


def test_files_already_held_stay_kept_after_a_refused_send_anyway(client, second, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    bob = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, ticket.team_id)
    seen = client.get(f"/tickets/{ticket.number}?action=note").text.split('name="seen" value="')[1].split('"')[0]
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob got in first"})

    held = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Ann's note", "seen": seen}, files=[("files", ("plan.pdf", PDF, "application/pdf"))]
    )
    assert "Not sent yet" in held.text and "plan.pdf" in held.text  # the #246 warning is as it was
    draft = held.text.split('name="draft" value="')[1].split('"')[0]

    # Send anyway, but the note was blanked: refused in the card, the held file still attached to the next try.
    response = client.post(f"/tickets/{ticket.number}/note", data={"body": " ", "seen": seen, "confirm": "1", "draft": draft}, headers=HTMX)
    in_card(response, "Add a note", True)
    assert f'name="draft" value="{draft}"' in response.text and "Still attached from before: plan.pdf" in response.text
    assert len(db_session.exec(select(ReplyHeldFile)).all()) == 1


def test_a_draft_that_is_no_longer_held_is_not_carried_into_the_card(client, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    response = client.post(f"/tickets/{ticket.number}/note", data={"body": "text", "draft": "gone"}, headers=HTMX)
    assert "no longer held" in in_card(response, "Add a note", True)
    assert 'name="draft"' not in response.text and "text</textarea>" in response.text


# ---- #267: the confirm buttons inside previews -----------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_email_send_shows_in_the_email_card_with_the_text_kept(client, db_session, storage, htmx):
    ticket = ticket_for_email(client, db_session)
    response = client.post(
        f"/tickets/{ticket.number}/email", data={"body": "Hello there", "draft": "gone"}, headers=HTMX if htmx else {},
    )
    assert "no longer held" in in_card(response, "Email the requester", htmx)
    assert "Hello there</textarea>" in response.text and 'name="draft"' not in response.text
    empty = client.post(f"/tickets/{ticket.number}/email", data={"body": " "}, headers=HTMX if htmx else {})
    assert in_card(empty, "Email the requester", htmx) == "Write the email first"


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize(
    "path, data, title",
    [
        ("resolve", {"resolution_notes": " "}, "Resolve"),
        ("close", {"closing_notes": " "}, "Close"),
        ("send-to-msp", {"reason": " "}, "Send to the MSP"),
    ],
)
def test_a_refused_confirm_shows_in_its_own_card(client, db_session, path, data, title, htmx):
    ticket = ticket_for_email(client, db_session)
    response = client.post(f"/tickets/{ticket.number}/{path}", data=data, headers=HTMX if htmx else {})
    in_card(response, title, htmx)


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_send_to_msp_keeps_the_reason_and_the_scope(client, db_session, htmx):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, MSP_ADDRESS, "help@msp.example")  # without one the card shows a note, not the form
    db_session.commit()
    response = client.post(
        f"/tickets/{ticket.number}/send-to-msp", data={"reason": "Needs the MSP", "scope": "original", "digest": "stale"},
        headers=HTMX if htmx else {},
    )
    in_card(response, "Send to the MSP", htmx)
    assert "Needs the MSP</textarea>" in response.text
    assert re.search(r'name="scope" value="original" checked', response.text)


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_resolve_keeps_the_notes_typed(client, db_session, htmx):
    ticket = ticket_for_email(client, db_session)
    # A resolve the service refuses after the preview: an unknown follower is copied.
    response = client.post(
        f"/tickets/{ticket.number}/resolve",
        data={"resolution_notes": "Replaced the toner", "cc_form": "1", "cc": "stranger@example.com"}, headers=HTMX if htmx else {},
    )
    in_card(response, "Resolve", htmx)
    assert "Replaced the toner</textarea>" in response.text


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_close_keeps_the_notes_typed(client, db_session, htmx):
    ticket = ticket_for_email(client, db_session)
    response = client.post(
        f"/tickets/{ticket.number}/close",
        data={"closing_notes": "Not needed any more", "cc_form": "1", "cc": "stranger@example.com"}, headers=HTMX if htmx else {},
    )
    in_card(response, "Close", htmx)
    assert "Not needed any more</textarea>" in response.text


def test_a_refusal_for_a_card_the_ticket_no_longer_offers_stays_the_plain_error(client, db_session):
    ticket = ticket_for_email(client, db_session)  # not with the MSP, so the Take back card is not on offer
    response = client.post(f"/tickets/{ticket.number}/take-back-from-msp", headers=HTMX)
    assert response.headers.get("HX-Retarget") == "#flash" and 'id="tp-card-error"' not in response.text


def test_the_held_back_warning_for_an_email_still_comes_with_the_text_kept(client, second, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    bob = login_as(second, "bob@example.com", "Bob Rowe")
    set_role(db_session, bob, UserRole.AGENT, ticket.team_id)
    seen = client.get(f"/tickets/{ticket.number}?action=email").text.split('name="seen" value="')[1].split('"')[0]
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob got in first"})
    held = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann's email", "seen": seen})
    assert "Not sent yet" in held.text and "Ann&#39;s email</textarea>" in held.text.replace("Ann's", "Ann&#39;s")


# ---- #268: the Triage card keeps its select choices ------------------------------------------------------------


def triage_world(client, db_session):
    team, category = world(db_session)
    kind = make(db_session)
    question(db_session, kind, "Department", required=True)  # left unanswered, so the save is refused
    size = question(db_session, kind, "Size", kind="choice", options="Small\nLarge")
    ticket = email_ticket(db_session, team)
    agent_id = agent_of(client, db_session, team)
    return team, category, kind, size, ticket, agent_id


@pytest.mark.parametrize("htmx", [False, True])
def test_a_refused_triage_keeps_every_select_choice(client, db_session, htmx):
    team, category, kind, size, ticket, agent_id = triage_world(client, db_session)
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "service_request", "impact": "high", "urgency": "medium", "category_id": category.id,
            "agent_id": agent_id, "request_type_id": kind.id, f"q_{size.id}": "Large", "due_on": "2030-01-02",
        },
        headers=HTMX if htmx else {},
    )
    assert "Department" in in_card(response, "Triage", htmx)
    html = response.text
    assert selected(html, "ticket_type") == "service_request"
    assert selected(html, "impact") == "high" and selected(html, "urgency") == "medium"
    assert selected(html, "category_id") == str(category.id)
    assert selected(html, "agent_id") == str(agent_id)
    assert selected(html, "request_type_id") == str(kind.id)
    assert selected(html, f"q_{size.id}") == "Large"
    assert 'name="due_on" value="2030-01-02"' in html
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).triaged_at is None


def test_a_choice_that_is_no_longer_valid_is_not_kept(client, second, db_session):
    team, category, kind, size, ticket, agent_id = triage_world(client, db_session)
    outsider = login_as(second, "other@example.com")  # a person who is not an agent of the category's team
    set_role(db_session, outsider, UserRole.AGENT)
    other_team = create_team(db_session, "Elsewhere")
    elsewhere = create_category(db_session, other_team, "Network")
    elsewhere.is_active = False
    db_session.add(elsewhere)
    db_session.commit()
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": elsewhere.id, "agent_id": outsider,
            "request_type_id": "999999",
        },
        headers=HTMX,
    )
    assert 'id="tp-card-error"' in response.text
    html = response.text
    assert selected(html, "category_id") == ""  # switched off since the page was drawn
    assert selected(html, "agent_id") == ""
    assert selected(html, "request_type_id") == ""  # not an active kind of request
    again = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "request_type_id": kind.id, f"q_{size.id}": "Gigantic"},
        headers=HTMX,
    ).text
    assert selected(again, "request_type_id") == str(kind.id)
    assert selected(again, f"q_{size.id}") == "" and "no longer listed" not in again  # a pick-list answer not on offer
    assert selected(html, "ticket_type") == "incident" and selected(html, "impact") == "low"


# ---- #269: the note menu closes like the other menus -----------------------------------------------------------


def test_the_note_menu_is_a_details_closed_by_the_shared_script_on_an_outside_click_and_escape(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "A note with a menu"})
    html = client.get(f"/tickets/{ticket.number}").text
    assert re.search(r'<details class="tp-more tp-note-menu">\s*<summary class="btn icon-btn" aria-label="Options for this note">', html)  # works without scripts
    script = html.split("The ⋯ menu on a thread note")[1].split("</script>")[0]
    assert "details.tp-note-menu[open]" in script
    assert "contains(event.target)" in script and 'event.key !== "Escape"' in script
    assert 'querySelector("summary").focus()' in script  # focus goes back to the ⋯ button

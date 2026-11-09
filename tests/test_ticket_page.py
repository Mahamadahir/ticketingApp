"""The agent ticket page (9C): which action buttons show in which state (each under its existing rule), the composer
modes, the internal-note styling, the More actions menu as a disclosure, the Service level card and the files on a
message. Who may do what is still decided by the services and app/visibility.py; the page only draws it."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Attachment, Message, MessageDirection, MessageVisibility, Ticket, TicketStatus, UserRole
from app.sla import sla_rows
from app.templating import _initials
from tests.conftest import add_membership, create_team, login_as, set_role
from tests.test_sla import triaged_ticket
from tests.test_triage import agent, new_ticket
from tests.test_splitting import split, subs_of

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def page(client, ticket):
    return client.get(f"/tickets/{ticket.number}").text


def bar_buttons(html):
    """The labels of the header's action bar (the summaries of the disclosures and the plain buttons)."""
    bar = html.split('aria-label="Ticket actions"')[1].split("</header>")[0]
    return re.findall(r'<summary class="btn[^"]*">([^<]+)</summary>', bar)


def menu_items(html):
    menu = html.split('class="tp-menu"')[1].split("</header>")[0]
    return [re.sub(r"\s+", " ", text).strip() for text in re.findall(r"<summary>([^<]+)</summary>", menu)]


def save(db_session, ticket, **fields):
    for name, value in fields.items():
        setattr(ticket, name, value)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)


# ---- Which actions show, by state -------------------------------------------------------------------------------


def test_an_untriaged_ticket_offers_the_triage_card_and_no_resolve(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    html = page(client, ticket)

    assert 'id="triage-heading"' in html and 'id="triage-form"' in html
    assert bar_buttons(html) == ["Escalate", "Send to MSP", "More actions"]  # Assign to me is a plain button for an agent
    assert "Resolve" not in bar_buttons(html) and "Reopen" not in bar_buttons(html)
    assert "Change status" not in menu_items(html) and "Close" not in menu_items(html)
    assert "Assign to me" in html


def test_a_super_admin_can_assign_an_untriaged_ticket_from_the_bar(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    admin = login_as(client, "admin@example.com")
    set_role(db_session, admin, UserRole.SUPER_ADMIN)
    html = page(client, ticket)

    assert bar_buttons(html)[0] == "Assign"
    assert "Assign to (without triaging)" in html


def test_an_assigned_open_ticket_offers_the_working_actions(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket)

    assert bar_buttons(html) == ["Assign", "Escalate", "Send to MSP", "More actions", "Resolve"]  # nobody holds it yet
    assert 'id="triage-form"' not in html
    items = menu_items(html)
    assert items[0] == "Change status" and "Close" in items and "Split ticket" in items and "Link" in items
    assert "Take back from MSP" not in items and "Reopen" not in bar_buttons(html)


def test_a_ticket_with_the_msp_offers_take_back_and_shows_the_msp_card(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.WITH_MSP, msp_ticket_number="MSP-4821")
    html = page(client, ticket)

    assert "Take back from MSP" in menu_items(html)
    assert 'id="msp-heading"' in html and "MSP ticket #MSP-4821" in html
    assert "With MSP" in html.split('class="tp-meta"')[1].split("</div>")[0]


def test_a_linked_ticket_offers_unlink_in_the_linked_card_and_no_link_form(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    assert "Link" in menu_items(page(client, ticket))
    problem = new_ticket(db_session, desk, ticket_type="problem")
    save(db_session, ticket, linked_parent_id=problem.id)
    html = page(client, ticket)

    assert "Link" not in menu_items(html) and f'hx-post="/tickets/{ticket.number}/unlink"' in html
    assert 'id="linked-heading"' in html and problem.reference in html


def test_a_ticket_without_the_msp_has_no_msp_card_or_take_back(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket)

    assert 'id="msp-heading"' not in html and "Take back from MSP" not in menu_items(html)


def test_a_resolved_ticket_offers_reopen_not_resolve(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.RESOLVED, resolved_at=NOW, resolution_notes="Replaced the roller")
    html = page(client, ticket)
    labels = bar_buttons(html)

    assert "Reopen" in labels and "Resolve" not in labels
    assert '<summary class="btn btn-primary">Reopen</summary>' in html  # the primary action once there is nothing to resolve
    assert 'id="resolution-heading"' in html and "Replaced the roller" in html
    assert "Close" not in menu_items(html)  # Close is for a ticket that is not resolved (check_can_close)


def test_a_closed_ticket_offers_neither_resolve_nor_reopen(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.CLOSED, resolved_at=NOW, closed_at=NOW, resolution_notes="Done")
    labels = bar_buttons(page(client, ticket))

    assert "Resolve" not in labels and "Reopen" not in labels
    assert "Close" not in menu_items(page(client, ticket))


def test_a_split_original_offers_split_and_lists_its_parts_and_a_part_offers_neither_split_nor_the_list(client, db_session):
    original, _ = triaged_ticket(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, _ = subs_of(db_session, original)
    html = page(client, original)

    assert 'id="linked-heading"' in html and "Sub-tickets" in html and first.reference in html
    assert "Split ticket" in menu_items(html)
    part = page(client, first)
    assert "Split ticket" not in menu_items(part) and f"Split from" in part


def test_a_read_only_agent_gets_no_action_bar(client, db_session):
    original, desk = triaged_ticket(client, db_session)
    client.post("/auth/logout")
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")
    html = page(client, original)

    assert "You have read-only access" in html
    assert 'aria-label="Ticket actions"' not in html
    assert 'class="card tp-composer"' not in html or "mode-email" not in html


# ---- The More actions menu --------------------------------------------------------------------------------------


def test_the_more_menu_and_its_items_are_disclosures_the_keyboard_can_open(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket)

    # A <details> with a <summary> is focusable and opens with Enter or Space, with no script; the shared name closes the others.
    assert re.search(r'<details class="tp-action" name="ticket-action">\s*<summary class="btn">More actions</summary>', html)
    assert html.count('<details class="tp-action" name="ticket-action">') == len(bar_buttons(html))
    assert len(menu_items(html)) >= 4


# ---- The composer -----------------------------------------------------------------------------------------------


def test_the_composer_starts_on_email_user_for_a_triaged_ticket_with_both_forms(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket)

    assert re.search(r'id="mode-email"\s*checked', html) and not re.search(r'id="mode-note"\s*checked', html)
    assert 'for="mode-email">Email user</label>' in html and "Internal note</label>" in html
    assert f'hx-post="/tickets/{ticket.number}/email/preview"' in html and f'hx-post="/tickets/{ticket.number}/note"' in html
    assert 'name="next_status"' in html and 'id="email-body"' in html and 'id="email-preview"' in html
    assert "Goes to me@example.com and is copied to 0 followers." in html
    assert "Only agents can see internal notes." in html
    assert "Preview message</button>" in html and "Add note</button>" in html


def test_the_composer_starts_on_internal_note_when_the_ticket_cannot_be_emailed_yet(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    html = page(client, ticket)

    assert re.search(r'id="mode-note"\s*checked', html) and not re.search(r'id="mode-email"\s*checked', html)
    assert "Triage this ticket before emailing the requester." in html
    assert f'hx-post="/tickets/{ticket.number}/email/preview"' not in html


# ---- Internal notes and files in the timeline -------------------------------------------------------------------


def add_message(db_session, ticket, sender, body, direction, visibility, minutes_ago):
    message = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, visibility=visibility,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db_session.add(message)
    db_session.commit()
    db_session.refresh(message)
    return message


def test_an_internal_note_is_tinted_and_labelled_and_a_public_message_is_not(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add_message(db_session, ticket, "me@example.com", "Public words", MessageDirection.INBOUND, MessageVisibility.PUBLIC, 30)
    add_message(db_session, ticket, "agent@example.com", "Secret words", MessageDirection.INTERNAL, MessageVisibility.AGENTS_ONLY, 20)
    thread = page(client, ticket).split('id="thread"')[1]
    public, internal = [c for c in thread.split("<article ")[1:3]]

    assert 'class="tp-msg"' in public and "Internal note" not in public and "From requester" in public
    assert "tp-msg-note" in internal and "Internal note · not visible to requester" in internal and "Secret words" in internal
    # and the requester never sees it (the portal's own tests cover the rule; this keeps the new markup out of it)
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    portal = client.get(f"/portal/{ticket.number}").text
    assert "Secret words" not in portal and "tp-msg-note" not in portal and "Internal note" not in portal


def test_the_files_that_came_with_a_message_are_listed_on_it_including_pasted_pictures(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    message = add_message(db_session, ticket, "me@example.com", "See the screenshot", MessageDirection.INBOUND, MessageVisibility.PUBLIC, 10)
    for name, inline in (("error.png", False), ("pasted-screenshot.png", True)):
        db_session.add(Attachment(ticket_id=ticket.id, message_id=message.id, filename=name, size=2048, content_type="image/png",
                                  content_hash=name.ljust(64, "0"), blob_key=name, is_inline=inline))
    db_session.commit()
    attachment = db_session.exec(select(Attachment)).first()
    html = page(client, ticket)

    assert f'<a href="/attachments/{attachment.id}">error.png</a>' in html and "(2.0 KB)" in html
    assert "pasted-screenshot.png" in html  # a picture kept from the body is evidence too (tiny signature logos are never stored)


# ---- The Service level card and the small helpers ---------------------------------------------------------------


def row(ticket, now=NOW):
    return [(r.label, r.tone, r.text) for r in sla_rows(ticket, now, 25)]


def clocked(**fields):
    base = dict(subject="x", requester_email="r@example.com", status=TicketStatus.OPEN, priority="p2",
                created_at=NOW - timedelta(hours=4))
    return Ticket(**{**base, **fields})


def test_the_service_level_rows_use_the_queues_wording():
    open_ticket = clocked(first_responded_at=NOW - timedelta(hours=3, minutes=42), first_response_due_at=NOW - timedelta(hours=3),
                          resolution_due_at=NOW + timedelta(hours=8))
    assert row(open_ticket) == [("First response", "green", "Met · 18m"), ("Resolution", "green", "8h 0m left")]
    late = clocked(first_responded_at=NOW, first_response_due_at=NOW - timedelta(hours=1), resolution_due_at=NOW - timedelta(minutes=80))
    assert row(late) == [("First response", "red", "Missed · 4h 0m"), ("Resolution", "red", "Breached 1h 20m ago")]
    waiting = clocked(first_responded_at=NOW, first_response_due_at=NOW, resolution_due_at=NOW + timedelta(hours=3),
                      sla_paused_at=NOW - timedelta(hours=1))
    assert row(waiting)[1] == ("Resolution", "blue", "Paused · waiting on requester")
    near = clocked(first_responded_at=NOW, first_response_due_at=NOW, resolution_due_at=NOW + timedelta(minutes=30))
    assert row(near)[1] == ("Resolution", "amber", "30m left")  # inside the last quarter of its window


def test_a_finished_ticket_is_met_or_missed_and_a_ticket_with_no_clock_has_no_rows():
    done = clocked(status=TicketStatus.RESOLVED, resolved_at=NOW - timedelta(hours=1), first_responded_at=NOW - timedelta(hours=3),
                   first_response_due_at=NOW - timedelta(hours=2), resolution_due_at=NOW)
    assert row(done) == [("First response", "green", "Met · 1h 0m"), ("Resolution", "green", "Met · 3h 0m")]
    assert row(clocked(is_backdated=True, resolution_due_at=NOW)) == [] and row(clocked(priority=None)) == []


def test_time_the_clock_was_stopped_is_not_counted_as_time_taken():
    done = clocked(status=TicketStatus.RESOLVED, resolved_at=NOW, resolution_due_at=NOW + timedelta(hours=1), sla_paused_seconds=3 * 3600)
    assert row(done) == [("Resolution", "green", "Met · 1h 0m")]  # 4h since creation, 3h of it waiting on the requester


def test_the_page_shows_the_service_level_card_and_the_header_chip(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket)

    assert 'id="sla-heading">Service level</h2>' in html and "<span>First response</span>" in html and "<span>Resolution</span>" in html
    assert re.search(r'<span class="chip t-\w+">\s*<svg[^>]*>.*?</svg>\s*[^<]*(to first response|left)', html, re.S)


def test_initials_for_the_avatar():
    assert _initials("Daniel Okafor") == "DO" and _initials("Sam Rowe <sam@example.com>") == "SR"
    assert _initials("daniel.okafor@example.com") == "DO" and _initials("priya@example.com") == "PR" and _initials("") == "?"
    assert _initials("<j.smith@example.com>") == "JS" and _initials('"Smith, John" <j@example.com>') == "JS"

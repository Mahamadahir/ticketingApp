"""#254, the ticket page redesign part A: the header and its action chips, the cards they open, the Add note composer (the @ list and
the "Visible to the requester" note), the right-hand column's boxes (the Triage edits with their reasons, the Service level edits, the
Attachments box with its rename). Who may do what is still decided by app/visibility.py and the services; these tests pin what the
page offers and what each new route records."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import notifications
from app.attachments import RenameRefused, TicketFile, rename_attachment, store_attachment
from app.main import app
from app.messages import add_visible_note, requester_visible_messages
from app.models import (
    Attachment,
    AuditLog,
    Message,
    MessageDirection,
    Notification,
    OutboundEmail,
    TeamsPost,
    Ticket,
    TicketEvent,
    TicketSource,
    TicketStatus,
    User,
    UserRole,
)
from app.sla import SlaView, header_sla
from app.storage import LocalStorage, get_storage
from app.ticket_actions import CARDS, action_chips
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_sla import triaged_ticket
from tests.test_triage import agent, new_ticket

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


def page(client, ticket, **query):
    suffix = "&".join(f"{k}={v}" for k, v in query.items())
    return client.get(f"/tickets/{ticket.number}" + (f"?{suffix}" if suffix else ""))


def chip_labels(html):
    """The labels of the header's chip row (links and buttons), not the ones under the more button."""
    if 'id="tp-chips"' not in html:
        return []
    nav = html.split('id="tp-chips"')[1].split("</nav>")[0]
    row = nav.split('class="tp-more"')[0]
    return [re.sub(r"\s+", " ", t).strip() for t in re.findall(r"<(?:a|button)\b[^>]*>([^<]+)</(?:a|button)>", row) if t.strip()]


def primary_chips(html):
    """The labels of the chips drawn as the primary button (Resolve, Reopen, or Triage on an untriaged ticket)."""
    nav = html.split('id="tp-chips"')[1].split("</nav>")[0]
    return [t.strip() for t in re.findall(r'<a class="btn btn-primary[^"]*"[^>]*>([^<]+)</a>', nav)]


def more_labels(html):
    if 'class="tp-more"' not in html:
        return []
    menu = html.split('class="tp-more"')[1].split("</nav>")[0]
    return [re.sub(r"\s+", " ", t).strip() for t in re.findall(r"<a\b[^>]*>([^<]+)</a>", menu) if t.strip()]


def save(db_session, ticket, **fields):
    for name, value in fields.items():
        setattr(ticket, name, value)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)


def events_of(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def audits(db_session, action):
    db_session.expire_all()
    return db_session.exec(select(AuditLog).where(AuditLog.action == action)).all()


# ---- The chip rules, on their own -------------------------------------------------------------------------------

BASE = dict(
    can_work=True, can_note=True, triaged=True, triaged_actions=True, can_triage=False, is_super_admin=False, can_self_assign=False,
    assigned=True, can_reassign=True, with_msp=False, can_take_back=False, can_link=False, can_split=False, can_resolve=True,
    can_reopen=False, can_reclose=False, can_close=True, can_backdate=True,
)


def chips(**over):
    return action_chips(**{**BASE, **over})


def labels(result):
    return [c.label for c in result.row] + ([result.primary.label] if result.primary else []), [c.label for c in result.more]


def test_a_triaged_ticket_offers_email_note_reassign_escalate_and_msp_then_resolve_and_the_more_items():
    row, more = labels(chips())
    assert row == ["Email user", "Add note", "Re-assign", "Escalate", "Send to MSP", "Resolve"]
    assert more == ["Backdate", "Close"]


def test_an_untriaged_ticket_offers_triage_in_place_of_reassign_and_escalate():
    row, more = labels(chips(triaged=False, triaged_actions=False, can_triage=True, can_reassign=False, can_resolve=False, can_close=False))
    assert row == ["Email user", "Add note", "Triage", "Send to MSP"]
    assert "Re-assign" not in row and "Escalate" not in row and "Resolve" not in row
    assert chips(triaged=False, triaged_actions=False, can_triage=True, can_resolve=False).row[2].primary  # Triage is the primary chip


def test_a_ticket_at_the_msp_offers_take_back_in_place_of_send_to_msp():
    row, _ = labels(chips(with_msp=True, can_take_back=True))
    assert "Take back" in row and "Send to MSP" not in row


def test_link_and_split_only_when_allowed():
    assert "Link" not in labels(chips())[0] and "Split" not in labels(chips())[0]
    row, _ = labels(chips(can_link=True, can_split=True))
    assert "Link" in row and "Split" in row


def test_a_resolved_ticket_offers_reopen_or_reclose_as_the_primary_and_no_resolve():
    done = chips(can_resolve=False, can_reopen=True, can_close=False)
    assert done.primary.label == "Reopen" and done.primary.primary
    assert chips(can_resolve=False, can_reclose=True).primary.label == "Reclose"
    both = chips(can_resolve=False, can_reopen=True, can_reclose=True)
    assert both.primary.label == "Reopen" and "Reclose" in [c.label for c in both.row]


def test_assign_to_me_is_a_plain_button_and_only_for_an_unassigned_ticket():
    result = chips(assigned=False, can_self_assign=True)
    assert [c.label for c in result.row if c.post] == ["Assign to me"]
    assert not any(c.post for c in chips(assigned=True, can_self_assign=True).row)
    assert "assign_me" not in chips(assigned=False, can_self_assign=True).keys()  # not a card


def test_a_read_only_agent_gets_only_the_note_chip_and_nothing_without_a_note():
    assert labels(chips(can_work=False))[0] == ["Add note"]
    assert chips(can_work=False, can_note=False).keys() == []


def test_the_more_button_is_left_out_when_it_holds_nothing():
    assert chips(can_backdate=False, can_close=False).more == []


def test_every_chip_key_that_opens_a_card_has_a_card():
    everything = chips(can_link=True, can_split=True, can_reopen=True, can_reclose=True, can_triage=True, is_super_admin=True,
                       with_msp=True, can_take_back=True)
    assert set(everything.keys()) <= set(CARDS)


# ---- The chips on the page --------------------------------------------------------------------------------------


def test_the_page_draws_the_chip_row_for_a_triaged_open_ticket(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket).text

    assert chip_labels(html) == ["Email user", "Add note", "Assign to me", "Assign", "Escalate", "Send to MSP", "Link", "Split", "Resolve"]
    assert more_labels(html) == ["Close"]  # Backdate joins it for a ticket an agent raised for someone (backdating.may_backdate)
    save(db_session, ticket, source=TicketSource.AGENT)
    assert more_labels(page(client, ticket).text) == ["Backdate", "Close"]
    assert 'aria-label="More actions"' in html and "More actions</summary>" not in html  # the old menu is gone
    assert primary_chips(html) == ["Resolve"]  # Resolve is the one primary button


def test_an_untriaged_ticket_page_has_triage_in_place_of_reassign_and_escalate(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    labels_ = chip_labels(page(client, ticket).text)

    assert "Triage" in labels_ and "Escalate" not in labels_ and "Re-assign" not in labels_ and "Resolve" not in labels_
    assert labels_[:2] == ["Email user", "Add note"]


def test_a_ticket_with_the_msp_offers_take_back_and_not_send_to_msp(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.WITH_MSP, msp_ticket_number="MSP-4821")
    labels_ = chip_labels(page(client, ticket).text)

    assert "Take back" in labels_ and "Send to MSP" not in labels_


def test_a_resolved_ticket_page_offers_reopen_not_resolve(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc), resolution_notes="Done")
    html = page(client, ticket).text

    assert "Reopen" in chip_labels(html) and "Resolve" not in chip_labels(html)
    assert primary_chips(html) == ["Reopen"]
    assert "Close" not in more_labels(html)


def test_link_and_split_chips_follow_the_rules_of_the_services(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    labels_ = chip_labels(page(client, ticket).text)
    assert "Link" in labels_ and "Split" in labels_
    save(db_session, ticket, status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc), resolution_notes="x")
    assert "Split" not in chip_labels(page(client, ticket).text)  # a finished ticket cannot be split


def test_a_read_only_agent_sees_no_working_chips(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    client.post("/auth/logout")
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")
    html = page(client, ticket).text

    assert "You have read-only access" in html and 'id="tp-chips"' not in html
    assert page(client, ticket, action="resolve").text.count('id="tp-card-title"') == 0  # no card opens by hand-typing the address


def test_the_header_has_the_chips_the_info_line_reported_by_and_the_presence_strip(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket).text
    head = html.split('<header class="tp-head">')[1].split("</header>")[0]

    assert "<h1>Printer on fire</h1>" in head and f">{ticket.reference}</span>" in head and ">Incident</span>" in head
    assert "Reported by email &middot;" in head or "Reported by email ·" in head
    assert 'id="presence"' in head and 'id="tp-chips"' in head  # the #246 strip lives in the header, with its attributes
    assert 'hx-vals="js:{...ticketPresenceVals()}"' in head and "data-leave=" in head
    assert "Requester" in head and "Team" in head and "Assigned to" in head
    assert "Reported by" not in html.split('<aside class="tp-side"')[1]  # source and reported moved out of the right-hand column


def test_the_on_hold_chip_shows_only_while_the_ticket_is_on_hold(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    assert "On hold for the MSP" not in page(client, ticket).text
    save(db_session, ticket, msp_hold=True)
    assert "On hold for the MSP" in page(client, ticket).text.split('class="tp-meta"')[1].split("</div>")[0]


def test_the_sla_chip_says_which_clock_breached():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    ticket = Ticket(subject="x", requester_email="r@example.com", status=TicketStatus.OPEN, priority="p2", created_at=now - timedelta(hours=4),
                    first_responded_at=now, first_response_due_at=now, resolution_due_at=now - timedelta(minutes=80))
    from app.sla import sla_view

    chip = header_sla(ticket, sla_view(ticket, now, 25), now)
    assert (chip.tone, chip.text) == ("red", "Resolution breached 1h 20m ago")
    ok = Ticket(subject="x", requester_email="r@example.com", status=TicketStatus.OPEN, priority="p2", created_at=now,
                first_responded_at=now, first_response_due_at=now, resolution_due_at=now + timedelta(hours=8))
    assert header_sla(ok, sla_view(ok, now, 25), now).text == "8h 0m left"


# ---- The cards -------------------------------------------------------------------------------------------------


def test_every_chip_opens_its_card_with_and_without_htmx_and_cancel_closes_it(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    save(db_session, ticket, source=TicketSource.AGENT)  # only such a ticket can be backdated
    html = page(client, ticket).text
    keys = re.findall(r"\?action=(\w+)#tp-card", html)
    assert {"email", "note", "reassign", "escalate", "msp", "resolve", "backdate", "close", "link", "split"} <= set(keys)
    for key in keys:
        plain = page(client, ticket, action=key)
        assert plain.status_code == 200 and "<html" in plain.text, key
        assert f'<h2 id="tp-card-title">{CARDS[key][0]}</h2>' in plain.text, key
        assert 'aria-current="true"' in plain.text.split('id="tp-chips"')[1].split("</nav>")[0], key  # the open chip is marked
        partial = client.get(f"/tickets/{ticket.number}?action={key}", headers=HTMX)
        assert "<html" not in partial.text and 'id="tp-card-title"' in partial.text and 'id="tp-card"' in partial.text, key
    closed = page(client, ticket)
    assert '<div id="tp-card" class="tp-card-slot"></div>' in closed.text.replace("\r", "").replace("\n", "")  # no card until a chip is chosen
    opened = page(client, ticket, action="resolve").text
    assert 'hx-get="/tickets/%d" hx-target="#tp-card"' % ticket.number in opened and ">Cancel</a>" in opened  # Cancel puts the slot back


def test_a_card_not_offered_to_this_person_or_this_state_opens_nothing(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    for key in ("resolve", "reassign", "escalate", "nonsense", "../x"):
        assert 'id="tp-card-title"' not in page(client, ticket, action=key).text, key
    assert 'id="triage-form"' in page(client, ticket, action="triage").text  # the triage form is the Triage card
    assert 'id="triage-form"' not in page(client, ticket).text


def test_the_resolve_card_still_posts_to_its_preview(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket, action="resolve").text
    assert f'hx-post="/tickets/{ticket.number}/resolve/preview"' in html and 'id="resolve-preview"' in html
    assert client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Fixed"}).status_code == 200


# ---- Add note: @ list, the visible note ------------------------------------------------------------------------


def colleague(second, db_session, desk, email, name, role=UserRole.AGENT):
    user_id = login_as(second, email, name)
    set_role(db_session, user_id, role, desk.id if role == UserRole.AGENT else None)
    return user_id


def mentions(client, ticket, q=""):
    response = client.get(f"/tickets/{ticket.number}/mentions", params={"q": q})
    assert response.status_code == 200
    return response.json()["agents"]


def test_the_at_list_offers_only_agents_who_can_open_the_ticket(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    other_team = create_team(db_session, "Network")
    colleague(second, db_session, desk, "bob@example.com", "Bob Rowe")
    outsider = login_as(TestClient(app), "carol@example.com", "Carol Outside")
    set_role(db_session, outsider, UserRole.AGENT, other_team.id)
    inactive = login_as(TestClient(app), "dan@example.com", "Dan Gone")
    set_role(db_session, inactive, UserRole.AGENT, desk.id)
    db_session.get(User, inactive).is_active = False
    db_session.commit()
    boss = login_as(TestClient(app), "boss@example.com", "Big Boss")
    set_role(db_session, boss, UserRole.SUPER_ADMIN)

    emails = {a["email"] for a in mentions(client, ticket)}

    assert emails == {"bob@example.com", "boss@example.com"}  # not the author, not another team's agent, not the inactive one
    assert {a["email"] for a in mentions(client, ticket, "bo")} == {"bob@example.com", "boss@example.com"}
    assert [a["email"] for a in mentions(client, ticket, "ro")] == ["bob@example.com"]  # a word of the name
    assert mentions(client, ticket, "zzz") == []


def test_an_untriaged_ticket_can_be_opened_by_every_agent_so_all_are_listed(client, second, db_session):
    desk = create_team(db_session, "Service Desk")
    other = create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    colleague(second, db_session, other, "carol@example.com", "Carol Outside")
    assert [a["email"] for a in mentions(client, ticket)] == ["carol@example.com"]


def test_what_is_inserted_is_what_the_server_reads_back(client, second, db_session):
    """The inserted text is the local part, or the whole address when two agents share it, so the mention notifies that person."""
    ticket, desk = triaged_ticket(client, db_session)
    bob = colleague(second, db_session, desk, "bob@example.com", "Bob Rowe")
    other = login_as(TestClient(app), "sam@example.com", "Sam One")
    set_role(db_session, other, UserRole.AGENT, desk.id)
    twin = login_as(TestClient(app), "sam@elsewhere.org", "Sam Two")
    set_role(db_session, twin, UserRole.AGENT, desk.id)
    inserts = {a["email"]: a["insert"] for a in mentions(client, ticket)}

    assert inserts == {"bob@example.com": "bob", "sam@example.com": "sam@example.com", "sam@elsewhere.org": "sam@elsewhere.org"}
    client.post(f"/tickets/{ticket.number}/note", data={"body": f"cc @{inserts['sam@elsewhere.org']} and @bob"})
    db_session.expire_all()
    told = {n.user_id for n in db_session.exec(select(Notification).where(Notification.kind == notifications.MENTIONED)).all()}
    assert told == {bob, twin}


def test_the_at_list_is_for_someone_who_may_add_a_note_and_never_a_requester(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post("/auth/logout")
    login_as(client, "me@example.com")
    assert client.get(f"/tickets/{ticket.number}/mentions").status_code in (302, 303, 307, 401, 403)


def test_typing_a_local_part_by_hand_still_mentions(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    bob = colleague(second, db_session, desk, "bob@example.com", "Bob Rowe")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "over to @bob please"})
    db_session.expire_all()
    assert [n.user_id for n in db_session.exec(select(Notification).where(Notification.kind == notifications.MENTIONED)).all()] == [bob]


def test_the_note_card_has_the_at_list_the_attach_control_and_the_eye_toggle_off_by_default(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    html = page(client, ticket, action="note").text

    assert f'data-mention-url="/tickets/{ticket.number}/mentions"' in html and 'id="note-mentions"' in html
    assert 'id="note-files"' in html  # attach a file
    assert 'name="visible_to_requester"' in html and not re.search(r'name="visible_to_requester"[^>]*checked', html)
    assert "Visible to the requester" in html
    assert "will see this note in the portal. It is not sent by email." in html
    assert "ticket-mentions.js" in html


def post_visible(client, ticket, body="Your part is on order", **extra):
    return client.post(f"/tickets/{ticket.number}/note", data={"body": body, "visible_to_requester": "1", **extra})


def newest(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id.desc())).first()


def counts(db_session):
    db_session.expire_all()
    return (
        len(db_session.exec(select(OutboundEmail)).all()), len(db_session.exec(select(TeamsPost)).all()),
        len(db_session.exec(select(Notification)).all()),
    )


def test_a_visible_note_is_shown_in_the_portal_and_never_emailed_sent_to_teams_or_belled(client, second, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    requester = login_as(second, "me@example.com")
    before = counts(db_session)

    response = post_visible(client, ticket)

    assert response.status_code == 200
    note = newest(db_session, ticket)
    assert (note.body, note.direction, note.visible_note) == ("Your part is on order", MessageDirection.OUTBOUND, True)
    assert counts(db_session) == before  # no email, no Teams post, no bell for anyone
    assert not db_session.exec(select(Notification).where(Notification.user_id == requester)).all()
    portal = second.get(f"/portal/{ticket.number}").text
    assert "Your part is on order" in portal
    assert audits(db_session, "visible_note_added")


def test_a_note_is_private_by_default(client, second, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    login_as(second, "me@example.com")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Secret words"})
    note = newest(db_session, ticket)

    assert (note.direction, note.visible_note) == (MessageDirection.INTERNAL, False)
    assert "Secret words" not in second.get(f"/portal/{ticket.number}").text
    assert not [m for m in requester_visible_messages(db_session, ticket) if m.id == note.id]


def test_a_visible_note_is_held_back_until_the_ticket_is_triaged(client, second, db_session):
    desk = create_team(db_session, "Service Desk")
    category = create_category(db_session, desk, "Hardware")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    login_as(second, "me@example.com")

    assert post_visible(client, ticket).status_code == 200
    note = newest(db_session, ticket)
    assert note.visible_note
    assert note.id not in {m.id for m in requester_visible_messages(db_session, ticket)}
    assert "Your part is on order" not in second.get(f"/portal/{ticket.number}").text
    assert "the requester sees it once the ticket is triaged" in client.get(f"/tickets/{ticket.number}").text  # the agent is told

    client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id})
    db_session.expire_all()
    assert note.id in {m.id for m in requester_visible_messages(db_session, db_session.get(Ticket, ticket.id))}
    assert "Your part is on order" in second.get(f"/portal/{ticket.number}").text


def test_a_follower_sees_a_visible_note_like_any_message_the_requester_may_see(client, second, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    assert client.post(f"/tickets/{ticket.number}/followers", data={"email": "fran@example.com"}).status_code == 200
    login_as(second, "fran@example.com")
    post_visible(client, ticket, "Update for the team")

    assert "Update for the team" in second.get(f"/portal/{ticket.number}").text


def test_a_visible_note_needs_an_agent_who_works_the_ticket(client, db_session):
    """On an untriaged ticket every agent may add a private note, but only one who works it may make a note visible."""
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "ok private"}).status_code == 200
    assert post_visible(client, ticket).status_code == 403
    assert "visible_to_requester" not in client.get(f"/tickets/{ticket.number}?action=note").text


def test_the_thread_marks_a_visible_note_with_the_eye_and_does_not_tint_it_like_a_private_one(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    post_visible(client, ticket)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Private words"})
    thread = page(client, ticket).text.split('id="thread"')[1]
    articles = thread.split("<article ")[1:]
    visible = [a for a in articles if "Your part is on order" in a][0]
    private = [a for a in articles if "Private words" in a][0]

    assert "tp-msg-visible" in visible and "tp-msg-note" not in visible and "Visible to the requester" in visible and "<svg" in visible
    assert "tp-msg-note" in private and "Internal note" in private


def test_a_visible_note_is_not_the_first_response(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    post_visible(client, ticket)
    db_session.refresh(ticket)
    assert ticket.first_responded_at is None


def test_a_visible_note_is_held_back_with_a_warning_when_another_agent_posted_first(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    colleague(second, db_session, desk, "bob@example.com", "Bob Rowe")
    seen = re.search(r'name="seen" value="(\d+)"', page(client, ticket, action="note").text).group(1)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob got in first"})

    held = post_visible(client, ticket, "Ann's visible words", seen=seen)

    assert "Not sent yet" in held.text and "Bob Rowe</b> added an internal note" in held.text
    assert 'name="visible_to_requester" value="1"' in held.text  # Send anyway keeps it visible
    assert 'data-compose="note"' in held.text and re.search(r'name="visible_to_requester"[^>]*checked', held.text)
    done = post_visible(client, ticket, "Ann's visible words", seen=seen, confirm="1")
    assert done.status_code == 200 and newest(db_session, ticket).visible_note


def test_the_warning_names_a_visible_note_another_agent_posted(client, second, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    colleague(second, db_session, desk, "bob@example.com", "Bob Rowe")
    seen = re.search(r'name="seen" value="(\d+)"', page(client, ticket, action="note").text).group(1)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob tells them", "visible_to_requester": "1"})

    held = client.post(f"/tickets/{ticket.number}/note", data={"body": "Mine", "seen": seen})

    assert "Bob Rowe</b> added a note the requester can see" in held.text


def test_team_activity_calls_it_a_note_the_requester_can_see(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    post_visible(client, ticket)
    page_text = client.get("/tickets/activity?kind=notes").text
    assert "note the requester can see" in page_text


def test_add_visible_note_is_the_only_path_and_keeps_the_internal_rule(db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    author = db_session.get(User, login_as(TestClient(app), "ann@example.com", "Ann"))
    note = add_visible_note(db_session, ticket, author, "Hello")
    db_session.commit()
    assert note.visible_note and note.direction == MessageDirection.OUTBOUND


# ---- The Triage box --------------------------------------------------------------------------------------------


def triage_box(html):
    return html.split('id="box-triage"')[1].split("</section>")[0]


def test_the_triage_box_lists_the_fields_with_pencils_for_an_agent_who_works_the_ticket(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    box = triage_box(page(client, ticket).text)

    for what in ("Type", "Category", "Impact", "Urgency", "Priority"):
        assert f"<dt>{what}</dt>" in box
    for field in ("type", "category", "priority"):
        assert f"?edit={field}#box-triage" in box
    assert "Hardware" in box and "P1" in box


def test_an_untriaged_ticket_has_no_edits_in_its_triage_box(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    box = triage_box(page(client, ticket).text)
    assert "Not triaged yet" in box and "?edit=" not in box
    assert 'id="triage-form"' not in page(client, ticket, edit="category").text


def test_the_edit_forms_open_in_place_with_and_without_htmx(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    for field, needle in (("category", 'action="/tickets/%d/category"'), ("priority", 'action="/tickets/%d/priority"'), ("type", 'action="/tickets/%d/type"')):
        plain = page(client, ticket, edit=field)
        assert needle % ticket.number in plain.text and "<html" in plain.text
        partial = client.get(f"/tickets/{ticket.number}?edit={field}", headers=HTMX)
        assert needle % ticket.number in partial.text and "<html" not in partial.text and 'id="box-triage"' in partial.text
        assert 'hx-post="/tickets/%d/' % ticket.number in plain.text  # htmx posts it; the form is also a plain form
    assert 'name="reason"' in page(client, ticket, edit="category").text and 'name="reason"' in page(client, ticket, edit="priority").text
    assert 'name="reason"' not in page(client, ticket, edit="type").text  # a reason is for priority and category only


def test_changing_the_category_needs_a_reason_and_does_not_move_the_ticket(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    network = create_team(db_session, "Network")
    other = create_category(db_session, network, "Networking")
    team_before, assignee_before = ticket.team_id, ticket.assigned_agent_id

    refused = client.post(f"/tickets/{ticket.number}/category", data={"category_id": other.id})
    blank = client.post(f"/tickets/{ticket.number}/category", data={"category_id": other.id, "reason": "   "})
    db_session.refresh(ticket)
    assert refused.status_code == 400 and blank.status_code == 400 and ticket.category_id != other.id
    assert not events_of(db_session, ticket, "category_changed")

    ok = client.post(f"/tickets/{ticket.number}/category", data={"category_id": other.id, "reason": "It is a network fault"})
    db_session.refresh(ticket)
    assert ok.status_code == 200 and ticket.category_id == other.id
    assert (ticket.team_id, ticket.assigned_agent_id) == (team_before, assignee_before)  # Re-assign and Escalate move a ticket, not this
    [event] = events_of(db_session, ticket, "category_changed")
    assert (event.from_value, event.to_value, event.detail) == ("Hardware", "Networking", "It is a network fault")
    assert event.actor_email == "agent@example.com"
    [entry] = audits(db_session, "category_changed")
    assert entry.entity_id == ticket.id and "Hardware -> Networking" in entry.detail and "It is a network fault" in entry.detail
    assert "Networking" in triage_box(page(client, ticket).text)


def test_the_same_category_changes_nothing(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/category", data={"category_id": ticket.category_id, "reason": "no real change"})
    assert not events_of(db_session, ticket, "category_changed") and not audits(db_session, "category_changed")


def test_a_category_cannot_be_changed_on_an_untriaged_or_finished_ticket_or_by_a_reader(client, db_session):
    ticket, desk = triaged_ticket(client, db_session)
    other = create_category(db_session, desk, "Software")
    save(db_session, ticket, status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc), resolution_notes="x")
    assert client.post(f"/tickets/{ticket.number}/category", data={"category_id": other.id, "reason": "late"}).status_code == 400
    new = new_ticket(db_session, desk)
    assert client.post(f"/tickets/{new.number}/category", data={"category_id": other.id, "reason": "early"}).status_code == 400
    client.post("/auth/logout")
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")
    assert client.post(f"/tickets/{ticket.number}/category", data={"category_id": other.id, "reason": "nosy"}).status_code in (302, 303, 403, 404)
    assert "?edit=" not in triage_box(client.get(f"/tickets/{ticket.number}").text)


def test_changing_impact_or_urgency_needs_a_reason_and_records_event_and_audit(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)  # high / high: P1
    refused = client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low"})
    db_session.refresh(ticket)
    assert refused.status_code == 400 and ticket.priority == "p1"
    assert client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low", "reason": " "}).status_code == 400

    ok = client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low", "reason": "Only one person affected"})
    db_session.refresh(ticket)
    assert ok.status_code == 200 and (ticket.priority, ticket.impact, ticket.urgency) == ("p4", "low", "low")
    [event] = events_of(db_session, ticket, "priority_changed")
    assert event.detail == "Only one person affected" and event.from_value.startswith("P1") and event.to_value.startswith("P4")
    [entry] = audits(db_session, "priority_changed")
    assert "Only one person affected" in entry.detail and "P4" in entry.detail


def test_changing_the_type_records_the_event_and_the_audit_and_keeps_the_number(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    number = ticket.number
    ok = client.post(f"/tickets/{ticket.number}/type", data={"ticket_type": "problem"})
    db_session.refresh(ticket)

    assert ok.status_code == 200 and ticket.type.value == "problem" and ticket.number == number
    assert events_of(db_session, ticket, "type_changed")[-1].to_value == "problem" and audits(db_session, "type_changed")
    assert client.post(f"/tickets/{ticket.number}/type", data={"ticket_type": "problem"}).status_code == 200  # no change, no second record
    assert len(events_of(db_session, ticket, "type_changed")) == 1


# ---- The Service level box -------------------------------------------------------------------------------------


def sla_box(html):
    return html.split('id="box-sla"')[1].split("</section>")[0]


def test_the_service_level_box_holds_the_clocks_due_required_by_status_and_hold(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    box = sla_box(page(client, ticket).text)

    assert "<span>First response</span>" in box and "<span>Resolution</span>" in box
    assert "<dt>Status</dt>" in box and "?edit=status#box-sla" in box
    assert "<dt>Due</dt>" in box and "?edit=due#box-sla" in box
    client.post(f"/tickets/{ticket.number}/assign")  # only the assigned agent may hold
    assert "Hold (keep out of the MSP handoff)" in sla_box(page(client, ticket).text)  # the hold control lives here now
    assert 'id="msp-heading"' not in page(client, ticket).text


def test_the_status_pencil_offers_only_in_progress_and_awaiting_user(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    form = sla_box(page(client, ticket, edit="status").text)

    assert re.findall(r'<option value="(\w+)"', form) == ["in_progress", "awaiting_user"]
    assert f'action="/tickets/{ticket.number}/status/edit"' in form


@pytest.mark.parametrize("status", ["in_progress", "awaiting_user"])
def test_those_two_statuses_can_be_set_in_place_and_are_recorded(client, db_session, status):
    ticket, _ = triaged_ticket(client, db_session)
    ok = client.post(f"/tickets/{ticket.number}/status/edit", data={"status": status})
    db_session.refresh(ticket)

    assert ok.status_code == 200 and ticket.status.value == status
    assert [e.to_value for e in events_of(db_session, ticket, "status_changed")][-1] == status
    assert audits(db_session, "status_changed")


@pytest.mark.parametrize("status", ["resolved", "closed", "scheduled", "with_supplier", "with_msp", "open", "new"])
def test_no_other_status_can_be_set_in_place(client, db_session, status):
    ticket, _ = triaged_ticket(client, db_session)
    before = ticket.status
    response = client.post(f"/tickets/{ticket.number}/status/edit", data={"status": status})
    db_session.refresh(ticket)
    assert response.status_code == 400 and ticket.status == before


def test_a_finished_ticket_has_no_status_pencil(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    save(db_session, ticket, status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc), resolution_notes="x")
    assert "?edit=status" not in sla_box(page(client, ticket).text)
    assert '<option value="in_progress"' not in sla_box(page(client, ticket, edit="status").text)  # ?edit= for it opens nothing


def test_due_and_required_by_edit_in_place_under_the_existing_rules(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    day = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    form = sla_box(page(client, ticket, edit="due").text)
    assert f'action="/tickets/{ticket.number}/due"' in form and 'name="due_reason"' in form
    refused = client.post(f"/tickets/{ticket.number}/due", data={"due_on": day, "due_reason": ""})
    ok = client.post(f"/tickets/{ticket.number}/due", data={"due_on": day, "due_reason": "Room only free that morning"})
    assert refused.status_code in (200, 400) and ok.status_code == 200
    db_session.refresh(ticket)
    assert ticket.due_on.isoformat() == day
    assert "Required by" not in sla_box(page(client, ticket).text)  # an ordinary ticket never offers it


# ---- The Attachments box ---------------------------------------------------------------------------------------


def add_file(db_session, storage, ticket, name, data, uploader=None, message=None):
    attachment = store_attachment(db_session, storage, ticket=ticket, filename=name, data=data, message=message, uploaded_by=uploader)
    db_session.commit()
    db_session.refresh(attachment)
    return attachment


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def files_box(html):
    return html.split('id="box-files"')[1].split("</section>")[0]


def test_the_attachments_box_lists_every_file_newest_first_with_who_and_when(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    first = add_file(db_session, storage, ticket, "old.png", PNG, me)
    mail = Message(ticket_id=ticket.id, sender="Pat Requester <me@example.com>", body="see file", direction=MessageDirection.INBOUND)
    db_session.add(mail)
    db_session.commit()
    second_file = add_file(db_session, storage, ticket, "new.pdf", b"%PDF-1.7\n" + b"y" * 100, None, mail)
    box = files_box(page(client, ticket).text)

    assert box.index("new.pdf") < box.index("old.png")  # newest first
    assert "Added by Test User" in box or "Added by agent@example.com" in box
    assert "Added by Pat Requester" in box
    for attachment in (first, second_file):
        assert f'href="/attachments/{attachment.id}"' in box and f'href="/attachments/{attachment.id}?download=1"' in box
    assert "Delete" not in box and "delete" not in box  # never delete


def test_an_empty_attachments_box_says_so(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    assert "No files on this ticket." in files_box(page(client, ticket).text)


def test_view_shows_a_picture_and_download_saves_it(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    picture = add_file(db_session, storage, ticket, "shot.png", PNG)
    pdf = add_file(db_session, storage, ticket, "plan.pdf", b"%PDF-1.7\n" + b"z" * 100)

    assert client.get(f"/attachments/{picture.id}").headers["content-disposition"].startswith("inline")
    assert client.get(f"/attachments/{picture.id}?download=1").headers["content-disposition"].startswith("attachment")
    assert client.get(f"/attachments/{pdf.id}").headers["content-disposition"].startswith("attachment")


def test_rename_changes_only_the_shown_name_and_records_event_and_audit(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    file = add_file(db_session, storage, ticket, "IMG_0042.png", PNG)

    ok = client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "Error on the till screen.png"})
    db_session.refresh(file)

    assert ok.status_code == 200
    assert file.filename == "IMG_0042.png" and file.display_name == "Error on the till screen.png"  # the original is kept on the row
    [event] = events_of(db_session, ticket, "attachment_renamed")
    assert (event.from_value, event.to_value) == ("IMG_0042.png", "Error on the till screen.png") and "IMG_0042.png" in event.detail
    [entry] = audits(db_session, "renamed")
    assert entry.entity_type == "attachment" and entry.entity_id == file.id and entry.actor_email == "agent@example.com"
    box = files_box(page(client, ticket).text)
    assert "Error on the till screen.png" in box and "Arrived as IMG_0042.png" in box
    assert "Error on the till screen.png" in client.get(f"/attachments/{file.id}?download=1").headers["content-disposition"]
    assert db_session.get(Attachment, file.id).blob_key == file.blob_key  # the stored file itself is untouched


def test_renaming_back_to_the_original_clears_the_shown_name_and_the_same_name_records_nothing(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    file = add_file(db_session, storage, ticket, "a.png", PNG)
    client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "b.png"})
    client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "b.png"})
    assert len(events_of(db_session, ticket, "attachment_renamed")) == 1
    client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "a.png"})
    db_session.refresh(file)
    assert file.display_name is None and len(events_of(db_session, ticket, "attachment_renamed")) == 2


def test_rename_refuses_an_empty_or_runnable_name(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    file = add_file(db_session, storage, ticket, "a.png", PNG)
    assert client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "  "}).status_code == 400
    assert client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "run.exe"}).status_code == 400
    db_session.refresh(file)
    assert file.display_name is None and not events_of(db_session, ticket, "attachment_renamed")
    with pytest.raises(RenameRefused):
        rename_attachment(file, "x.bat")
    assert rename_attachment(file, "../../etc/passwd")[1] == "passwd"  # no directories in a name


def test_only_agents_who_work_the_ticket_rename_and_only_files_of_that_ticket(client, db_session, storage):
    ticket, desk = triaged_ticket(client, db_session)
    file = add_file(db_session, storage, ticket, "a.png", PNG)
    elsewhere = new_ticket(db_session, desk)
    foreign = add_file(db_session, storage, elsewhere, "b.png", PNG + b"1")
    assert client.post(f"/tickets/{ticket.number}/attachments/{foreign.id}/rename", data={"name": "mine now"}).status_code == 404
    assert "?edit=file-%d" % file.id in files_box(page(client, ticket).text)
    client.post("/auth/logout")
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")
    assert client.post(f"/tickets/{ticket.number}/attachments/{file.id}/rename", data={"name": "nosy"}).status_code in (302, 303, 403, 404)
    db_session.refresh(file)
    assert file.display_name is None
    assert "?edit=file-" not in files_box(client.get(f"/tickets/{ticket.number}").text)


def test_the_rename_form_opens_in_place(client, db_session, storage):
    ticket, _ = triaged_ticket(client, db_session)
    file = add_file(db_session, storage, ticket, "a.png", PNG)
    html = files_box(page(client, ticket, edit=f"file-{file.id}").text)
    assert f'action="/tickets/{ticket.number}/attachments/{file.id}/rename"' in html and "a.png" in html
    assert files_box(page(client, ticket, edit="file-99999").text).count("<form") == 0  # a file that is not here opens nothing


def test_the_end_user_box_is_its_own_box(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    box = page(client, ticket).text.split('id="box-enduser"')[1].split("</section>")[0]
    assert "End user" in box and "me@example.com" in box


def test_the_right_hand_boxes_come_in_the_agreed_order_without_tabs(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    side = page(client, ticket).text.split('<aside class="tp-side"')[1]
    order = [side.index(f'id="{box}"') for box in ("box-enduser", "box-triage", "box-sla", "box-followers", "box-files")]
    assert order == sorted(order)
    assert 'role="tab"' not in side and 'class="tabs"' not in side


def test_the_person_data_list_knows_the_new_column():
    from app.person_data import COLUMNS

    assert "attachment.display_name" in COLUMNS

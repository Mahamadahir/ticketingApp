"""4D: the resolve action (app/resolution.py): notes required, the SLA stops, one event and one audit
entry, the resolution email (admin template, preview step, cc rules), the split-original rule, access,
the backdated path in app/raising.py, and the new ticket fields."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app import mail, outbox
from app.config import settings
from app.email_templates import save_template
from app.models import AuditLog, Message, OutboundEmail, Ticket, TicketEvent, TicketStatus, User, UserRole
from app.resolution import ResolveError, auto_resolution_note, resolve_ticket
from app.sla import sla_view
from app.ticket_service import ClosingNeedsNotes, ResolutionRequired, set_status
from tests.conftest import create_team, login_as, set_role
from tests.test_email_preview import follow, recorded, ticket_for_email
from tests.test_raising import ago, desk, last_ticket, raise_it, FORMAT
from tests.test_splitting import split, subs_of


@pytest.fixture(autouse=True)
def utc_default_zone(db_session):
    """These tests type times as UTC. The default zone is London, an hour out in summer, so make the
    default UTC here (a person's own zone is tested in test_time_zones.py)."""
    from app.settings_store import DEFAULT_TIME_ZONE, set_setting

    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()


@pytest.fixture
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def resolve(client, ticket, notes="Replaced the roller", **extra):
    return client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": notes, **extra})


def preview(client, ticket, notes="Replaced the roller", **extra):
    return client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": notes, **extra})


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


# ---- The action -------------------------------------------------------------------------------------


def test_resolving_stores_the_notes_who_resolved_and_when_and_writes_the_event_and_audit(client, db_session):
    ticket = ticket_for_email(client, db_session)

    assert resolve(client, ticket).status_code == 200

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.resolution_notes == "Replaced the roller"
    assert ticket.resolved_by_id is not None and ticket.resolved_at is not None
    assert ticket.closed_at is None and ticket.reopen_count == 0
    [event] = db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "status_changed",
                                  TicketEvent.to_value == "resolved")
    ).all()
    assert event.actor_email == "agent@example.com"
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all()
    assert (audit.entity_id, audit.actor_email, audit.detail) == (ticket.id, "agent@example.com", "Replaced the roller")
    page = client.get(f"/tickets/{ticket.number}").text
    assert 'id="resolution-heading">Resolution</h2>' in page and "Replaced the roller" in page and "Resolved by Agent Ann" in page


@pytest.mark.parametrize("notes", ["", "   \r\n  "])
def test_notes_are_required(client, db_session, notes):
    ticket = ticket_for_email(client, db_session)
    before = recorded(db_session)

    for call in (resolve, preview):
        response = call(client, ticket, notes)
        assert response.status_code == 400 and "resolution notes" in response.text

    assert fresh(db_session, ticket).status == TicketStatus.OPEN and recorded(db_session) == before


def test_notes_have_a_length_limit(client, db_session):
    ticket = ticket_for_email(client, db_session)
    from app.forms import MAX_MESSAGE_LENGTH

    assert resolve(client, ticket, "x" * (MAX_MESSAGE_LENGTH + 1)).status_code == 400
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_the_status_buttons_no_longer_resolve(client, db_session):
    ticket = ticket_for_email(client, db_session)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'name="status" value="resolved"' not in page and "Preview resolution" in page
    response = client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"})
    assert response.status_code == 400 and "Resolve action" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_set_status_refuses_a_bare_resolved(db_session):
    ticket = ticket_for_email_direct(db_session)
    with pytest.raises(ResolutionRequired):
        set_status(db_session, ticket, TicketStatus.RESOLVED, "x@example.com")
    assert ticket.status == TicketStatus.OPEN


def another(db_session, ticket, subject):
    from tests.test_ticket_core import make_ticket

    return make_ticket(db_session, team_id=ticket.team_id, subject=subject, triaged=True)


def ticket_for_email_direct(db_session):
    from tests.test_ticket_core import make_ticket

    team = create_team(db_session, "Service Desk")
    return make_ticket(db_session, team_id=team.id, triaged=True)


def test_a_finished_untriaged_or_waiting_ticket_cannot_be_resolved(client, db_session):
    ticket = ticket_for_email(client, db_session)
    assert resolve(client, ticket).status_code == 200
    again = resolve(client, ticket, "Again")
    assert again.status_code == 400 and "already resolved or closed" in again.text
    assert fresh(db_session, ticket).resolution_notes == "Replaced the roller"

    waiting = another(db_session, ticket, "Waiting")
    waiting.status = TicketStatus.AWAITING_APPROVAL
    db_session.add(waiting)
    db_session.commit()
    assert "waiting for its approvers" in resolve(client, waiting).text
    assert fresh(db_session, waiting).status == TicketStatus.AWAITING_APPROVAL

    untriaged = another(db_session, ticket, "Untriaged")
    untriaged.triaged_at = None
    untriaged.status = TicketStatus.NEW
    db_session.add(untriaged)
    db_session.commit()
    assert "Triage the ticket" in resolve(client, untriaged).text
    assert fresh(db_session, untriaged).status == TicketStatus.NEW


def test_the_sla_clock_stops_on_resolve(client, db_session):
    ticket = ticket_for_email(client, db_session)
    assert sla_view(fresh(db_session, ticket), datetime.now(timezone.utc), 20).state in ("ok", "at_risk")

    resolve(client, ticket)

    ticket = fresh(db_session, ticket)
    view = sla_view(ticket, datetime.now(timezone.utc), 20)
    assert view.state == "met" and ticket.resolved_at is not None


def test_resolving_from_awaiting_user_closes_the_pause(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "awaiting_user"})
    assert fresh(db_session, ticket).sla_paused_at is not None

    resolve(client, ticket)

    assert fresh(db_session, ticket).sla_paused_at is None


def test_closed_at_is_set_on_close_and_cleared_when_it_leaves_closed(client, db_session):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)

    client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"})
    closed = fresh(db_session, ticket)
    assert closed.closed_at is not None and closed.resolved_at is not None

    client.post(f"/tickets/{ticket.number}/status", data={"status": "open"})
    reopened = fresh(db_session, ticket)
    assert reopened.closed_at is None and reopened.resolved_at is None


# ---- Access -----------------------------------------------------------------------------------------


def test_only_an_agent_who_can_work_the_ticket_may_resolve_or_preview(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post("/auth/logout")
    other = create_team(db_session, "Network")
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT, other.id)
    assert resolve(client, ticket).status_code == 403 and preview(client, ticket).status_code == 403

    client.post("/auth/logout")
    login_as(client, "walkin@example.com")  # the requester, signed in
    assert resolve(client, ticket).status_code == 403 and preview(client, ticket).status_code == 403
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


# ---- The email --------------------------------------------------------------------------------------


def test_a_preview_shows_the_email_and_records_nothing(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    before = recorded(db_session)

    page = preview(client, ticket).text

    assert "To:</b> walkin@example.com" in page and "boss@example.com" in page
    assert f"Subject:</b> [{ticket.reference}] Printer jam" in page
    assert "Replaced the roller" in page and "has been resolved" in page
    assert "stops its SLA clock" in page
    assert recorded(db_session) == before and fresh(db_session, ticket).status == TicketStatus.OPEN


def test_resolving_emails_the_requester_copying_the_followers_with_the_template_text(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")

    resolve(client, ticket)

    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()
    assert message.cc == "boss@example.com, other@example.com"
    assert "Replaced the roller" in message.body and ticket.reference in message.body
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)).all()
    built = mail.build_message_mail(db_session, row)
    assert (built.to, built.cc) == ("walkin@example.com", ["boss@example.com", "other@example.com"])
    assert built.subject == f"[{ticket.reference}] Printer jam" and "Replaced the roller" in built.body
    assert fresh(db_session, ticket).first_responded_at is not None  # an agent's email to the requester


def test_unticked_followers_are_left_off_and_a_stranger_is_refused_before_anything_changes(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")

    refused = resolve(client, ticket, cc_form="1", cc=["stranger@example.com"])
    assert refused.status_code == 400 and "Not a follower" in refused.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).all() == []

    page = preview(client, ticket, cc_form="1", cc=["boss@example.com"]).text
    assert "<b>Copied:</b> boss@example.com" in page and 'name="cc" value="boss@example.com"' in page

    resolve(client, ticket, cc_form="1", cc=["boss@example.com"])
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()
    assert message.cc == "boss@example.com"


def test_an_admin_edit_of_the_template_changes_the_email(client, db_session):
    ticket = ticket_for_email(client, db_session)
    save_template(db_session, "resolution", None, "Fixed: {resolution_notes} ({reference})", "admin@example.com")
    db_session.commit()

    assert "Fixed: Replaced the roller (" + ticket.reference + ")" in preview(client, ticket).text
    resolve(client, ticket)
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()
    assert message.body == f"Fixed: Replaced the roller ({ticket.reference})"


def test_the_requesters_own_braces_in_the_notes_stay_as_written(client, db_session):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket, "Reset {reference} and {subject}")
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()
    assert "Reset {reference} and {subject}" in message.body


# ---- Split requests ---------------------------------------------------------------------------------


def test_a_split_original_resolves_only_when_its_parts_are_finished_and_sends_the_one_email(client, db_session):
    original = ticket_for_email(client, db_session, "Two things")
    split(client, original, ("Keyboard", ""), ("Office", ""))
    first, second = subs_of(db_session, original)

    early = resolve(client, original)
    assert early.status_code == 400 and "sub-tickets first" in early.text
    assert preview(client, original).status_code == 400
    assert fresh(db_session, original).resolution_notes is None

    resolve(client, first, "Keyboard swapped")
    resolve(client, second, "Office set up")
    assert resolve(client, original, "All done").status_code == 200
    assert fresh(db_session, original).status == TicketStatus.RESOLVED
    page = preview(client, fresh(db_session, original), "x")  # already resolved now
    assert page.status_code == 400


def test_resolving_a_part_emails_under_the_originals_reference(client, db_session):
    original = ticket_for_email(client, db_session, "Two things")
    split(client, original, ("Keyboard", ""), ("Office", ""))
    part = subs_of(db_session, original)[0]

    page = preview(client, part).text

    assert f"Subject:</b> [{original.reference}] Two things" in page and f"About: {part.reference} Keyboard" in page


# ---- Backdated tickets (app/raising.py) -------------------------------------------------------------


def test_a_ticket_raised_already_resolved_needs_notes_and_uses_the_same_resolve_without_an_email(client, db_session, dev_mail):
    team, category, _ = desk(client, db_session)
    base = dict(
        subject="Fixed at the desk", for_email="walkin@example.com", category_id=category.id, impact="low",
        urgency="low", reported_at=ago(days=3, hours=2).strftime(FORMAT), resolved_at=ago(days=3, hours=1).strftime(FORMAT),
    )

    missing = raise_it(client, resolution_notes="", **base)
    assert missing.status_code == 400 and "resolution notes" in missing.text
    assert db_session.exec(select(Ticket)).all() == []

    assert raise_it(client, resolution_notes="Swapped the cable", **base).status_code == 204
    ticket = last_ticket(db_session)
    assert ticket.status == TicketStatus.RESOLVED and ticket.resolution_notes == "Swapped the cable"
    assert ticket.resolved_by_id is not None and ticket.is_backdated
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all() != []
    kinds = {r.kind for r in db_session.exec(select(OutboundEmail)).all()}
    assert outbox.NOTICE not in kinds and outbox.REPLY not in kinds  # logged after the fact: no resolution email


# ---- Automatic resolves (4H and 4I reuse the helper) ------------------------------------------------


def test_an_automatic_resolve_has_no_agent_uses_the_template_note_and_copies_every_follower(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    ticket = fresh(db_session, ticket)

    note = auto_resolution_note(db_session, ticket, 3)
    assert "after 3 days" in note
    resolve_ticket(db_session, ticket, None, note)
    db_session.commit()

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.RESOLVED and ticket.resolved_by_id is None
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.NOTICE)).all()
    built = mail.build_message_mail(db_session, row)
    assert built.cc == ["boss@example.com"] and "after 3 days" in built.body
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all()
    assert audit.actor_email == "system"


def test_the_helper_raises_resolve_error_and_changes_nothing(client, db_session):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    with pytest.raises(ResolveError):
        resolve_ticket(db_session, ticket, None, "  ")
    assert ticket.status == TicketStatus.OPEN and ticket.resolution_notes is None


def test_a_bad_copy_address_raises_before_the_helper_changes_anything(client, db_session):
    from app.messages import UnknownRecipient

    ticket = fresh(db_session, ticket_for_email(client, db_session))
    agent = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    with pytest.raises(UnknownRecipient):
        resolve_ticket(db_session, ticket, agent, "Done", cc_emails=["stranger@example.com"])
    assert ticket.status == TicketStatus.OPEN and ticket.resolution_notes is None


def test_a_second_resolve_of_a_stale_copy_is_refused_and_changes_nothing(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    agent = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    stale = db_session.get(Ticket, ticket.id)  # loaded as Open
    assert resolve(client, ticket, "First agent's notes").status_code == 200
    assert stale.status == TicketStatus.OPEN  # this session has not seen the change
    emails_before = len(db_session.exec(select(OutboundEmail)).all())

    # The helper re-reads the row under a lock, so it sees the first agent's resolve.
    with pytest.raises(ResolveError, match="already resolved"):
        resolve_ticket(db_session, stale, agent, "Second agent's notes")

    db_session.rollback()
    assert fresh(db_session, ticket).resolution_notes == "First agent's notes"
    assert len(db_session.exec(select(OutboundEmail)).all()) == emails_before
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all()) == 1


def test_reopening_clears_the_resolution_so_a_later_close_does_not_show_it(client, db_session):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket, "Old notes")

    client.post(f"/tickets/{ticket.number}/status", data={"status": "in_progress"})
    reopened = fresh(db_session, ticket)
    assert (reopened.resolution_notes, reopened.resolved_by_id, reopened.resolved_at) == (None, None, None)

    client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"})
    assert "<h3>Resolution</h3>" not in client.get(f"/tickets/{ticket.number}").text


def test_the_page_offers_resolve_only_when_resolving_would_work(client, db_session):
    original = ticket_for_email(client, db_session, "Two things")
    assert "Preview resolution" in client.get(f"/tickets/{original.number}").text
    split(client, original, ("Keyboard", ""), ("Office", ""))
    assert "Preview resolution" not in client.get(f"/tickets/{original.number}").text  # parts still open

    first, second = subs_of(db_session, original)
    resolve(client, first)
    resolve(client, second)
    assert "Preview resolution" in client.get(f"/tickets/{original.number}").text


def test_a_resolved_ticket_shows_resolved_as_its_current_status_without_a_way_to_set_it(client, db_session):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)

    page = client.get(f"/tickets/{ticket.number}").text

    assert '<button type="button" disabled class="button-active">resolved</button>' in page
    assert 'name="status" value="resolved"' not in page and "Preview resolution" not in page


def test_notes_without_a_resolved_time_are_refused_not_dropped(client, db_session):
    team, category, _ = desk(client, db_session)

    response = raise_it(
        client, for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low",
        resolution_notes="Fixed it",
    )

    assert response.status_code == 400 and "Resolved time" in response.text
    assert db_session.exec(select(Ticket)).all() == []


# ---- Closing directly needs notes (#122) -------------------------------------------------------------


def test_closing_an_open_ticket_needs_closing_notes(client, db_session):
    ticket = ticket_for_email(client, db_session)

    for data in ({}, {"closing_notes": "   "}):
        response = client.post(f"/tickets/{ticket.number}/close", data=data)
        assert response.status_code == 400 and "closing notes" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN
    # The status buttons no longer close a ticket that is not resolved: the Close action does, with the email (#131).
    response = client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"})
    assert response.status_code == 400 and "Close action" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.OPEN

    assert client.post(f"/tickets/{ticket.number}/close", data={"closing_notes": "Duplicate of INC-1"}).status_code == 200
    closed = fresh(db_session, ticket)
    assert closed.status == TicketStatus.CLOSED and closed.resolution_notes == "Duplicate of INC-1"
    assert closed.resolved_by_id is not None and closed.closed_at is not None
    entry = db_session.exec(select(AuditLog).where(AuditLog.action == "closed")).one()
    assert entry.detail == "Duplicate of INC-1"


def test_set_status_refuses_closing_without_notes_but_not_a_resolved_ticket(client, db_session):
    ticket = ticket_for_email(client, db_session)
    with pytest.raises(ClosingNeedsNotes):
        set_status(db_session, ticket, TicketStatus.CLOSED, "agent@example.com")
    db_session.rollback()


def test_set_status_closes_a_resolved_ticket_without_new_notes(client, db_session):
    ticket = ticket_for_email(client, db_session)
    resolve(client, ticket)
    ticket = fresh(db_session, ticket)
    assert set_status(db_session, ticket, TicketStatus.CLOSED, None, detail="auto-closed")  # already has notes


def test_pressing_closed_on_a_ticket_closed_by_a_decision_does_not_stamp_notes(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/close", data={"closing_notes": "Dup"})
    closed = fresh(db_session, ticket)
    closed.resolution_notes = None
    closed.resolved_by_id = None
    db_session.add(closed)
    db_session.commit()  # as a decline or withdrawal leaves it: Closed, no notes

    client.post(f"/tickets/{ticket.number}/status", data={"status": "closed"})
    again = fresh(db_session, ticket)
    assert again.resolution_notes is None and again.resolved_by_id is None

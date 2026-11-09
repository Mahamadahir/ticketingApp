"""MSP actions (issue #150): linked tickets go With MSP with their parent, the MSP is told when IT finishes with a ticket,
and Take back from MSP."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.config import settings
from app.models import AuditLog, Message, OutboundEmail, Ticket, TicketEvent, TicketStatus, TicketType
from app.msp import hand_off_due_tickets
from app.msp_actions import ids_digest
from app.settings_store import MSP_ADDRESS, set_setting
from tests.conftest import create_team
from tests.test_visibility import agent_in, make_ticket

MSP = "support@msp.example"
NOW = datetime(2026, 11, 4, 18, 30, tzinfo=timezone.utc)


@pytest.fixture
def mail_on(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")


def set_msp(db_session, address=MSP):
    set_setting(db_session, MSP_ADDRESS, address)
    db_session.commit()


def triaged(db_session, team, status=TicketStatus.OPEN, **fields):
    fields.setdefault("triaged_at", NOW - timedelta(days=1))
    fields.setdefault("created_at", NOW - timedelta(days=1))
    return make_ticket(db_session, team.id, status, **fields)


def events(db_session, ticket, kind):
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def mail_of(db_session, kind):
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == kind)).all()


def sent_to_msp(client, ticket, **extra):
    data = {"reason": "Needs an engineer", "scope": "thread", **extra}
    return client.post(f"/tickets/{ticket.number}/send-to-msp", data=data)


def delivered(db_session, ticket):
    """The worker has sent the ticket's handoff email (until it has, the MSP knows nothing of the ticket)."""
    for row in db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == "msp_handoff")).all():
        row.sent_at = NOW
        db_session.add(row)
    db_session.commit()


def with_msp(client, db_session, team, **fields):
    """A ticket really sent to the MSP through the button (so it has msp_sent_at and the handoff email, which has been sent)."""
    ticket = triaged(db_session, team, **fields)
    assert sent_to_msp(client, ticket).status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP
    delivered(db_session, ticket)
    return ticket


# ---- 1. Linked tickets go With MSP with their parent ------------------------------------------------------------


def parent_with_children(db_session, team, other_team=None):
    parent = triaged(db_session, team, type=TicketType.MAJOR_INCIDENT, subject="Network down")
    kids = {
        "open": triaged(db_session, team, linked_parent_id=parent.id, subject="Child open"),
        "progress": triaged(db_session, team, TicketStatus.IN_PROGRESS, linked_parent_id=parent.id, subject="Child in progress"),
        "resolved": triaged(db_session, team, TicketStatus.RESOLVED, linked_parent_id=parent.id, subject="Child resolved"),
        "closed": triaged(db_session, team, TicketStatus.CLOSED, linked_parent_id=parent.id, subject="Child closed"),
        "already": triaged(db_session, team, TicketStatus.WITH_MSP, linked_parent_id=parent.id, subject="Child already"),
    }
    if other_team is not None:
        kids["elsewhere"] = triaged(db_session, other_team, linked_parent_id=parent.id, subject="Child elsewhere")
    return parent, kids


def test_the_panel_offers_both_boxes_for_a_major_incident_with_linked_tickets(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, _ = parent_with_children(db_session, team)

    page = client.post(f"/tickets/{parent.number}/send-to-msp/preview", data={"reason": "Outage", "scope": "thread"}).text
    assert 'name="mark_linked"' in page and 'name="tell_linked_requesters"' in page
    assert "Also mark the 2 linked tickets as With MSP" in page  # the open and the in-progress child only
    assert "Child resolved" not in page and "Child already" not in page
    assert page.count("checked") >= 2  # both ticked by default
    assert "cannot be marked" not in page  # finished tickets and ones already With MSP are not "left alone", they are just not offered

    plain = triaged(db_session, team, subject="Plain")
    assert 'name="mark_linked"' not in client.post(
        f"/tickets/{plain.number}/send-to-msp/preview", data={"reason": "Outage", "scope": "thread"}
    ).text


def test_sending_the_parent_marks_the_eligible_linked_tickets_and_tells_their_requesters(client, db_session, mail_on):
    set_msp(db_session)
    team, other = create_team(db_session, "Support"), create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[team])
    parent, kids = parent_with_children(db_session, team, other)
    ids = ids_digest([kids["open"].id, kids["progress"].id])

    assert sent_to_msp(client, parent, mark_linked="1", tell_linked_requesters="1", linked_digest=ids).status_code == 200

    for name in ("open", "progress"):
        db_session.refresh(kids[name])
        child = kids[name]
        assert child.status == TicketStatus.WITH_MSP
        (event,) = events(db_session, child, "with_msp_through")
        assert event.to_value == parent.reference
        assert child.msp_sent_at is None  # status only: it was never emailed to the MSP
        told = [m for m in db_session.exec(select(Message).where(Message.ticket_id == child.id)).all() if "support partner" in m.body]
        assert len(told) == 1
        assert db_session.exec(select(AuditLog).where(AuditLog.entity_id == child.id, AuditLog.action == "with_msp_through")).first()
    for name, status in (("resolved", TicketStatus.RESOLVED), ("closed", TicketStatus.CLOSED), ("elsewhere", TicketStatus.OPEN)):
        db_session.refresh(kids[name])
        assert kids[name].status == status and not events(db_session, kids[name], "with_msp_through")
    # Nothing was sent to the MSP for the linked tickets: one handoff email, for the parent.
    (mail,) = mail_of(db_session, "msp_handoff")
    assert mail.ticket_id == parent.id


def test_the_second_box_off_tells_no_requester_and_the_first_box_off_marks_nothing(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = parent_with_children(db_session, team)
    sent_to_msp(client, parent, mark_linked="1", linked_digest=ids_digest([kids["open"].id, kids["progress"].id]))
    db_session.refresh(kids["open"])
    assert kids["open"].status == TicketStatus.WITH_MSP
    assert not [m for m in db_session.exec(select(Message).where(Message.ticket_id == kids["open"].id)).all() if "support partner" in m.body]

    parent2, kids2 = parent_with_children(db_session, team)
    sent_to_msp(client, parent2, linked_digest=ids_digest([kids2["open"].id, kids2["progress"].id]))
    db_session.refresh(kids2["open"])
    assert kids2["open"].status == TicketStatus.OPEN


def test_linked_tickets_that_changed_since_the_preview_refuse_the_whole_send(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = parent_with_children(db_session, team)
    response = sent_to_msp(client, parent, mark_linked="1", linked_digest=ids_digest([kids["open"].id]))  # the preview showed one, there are two
    assert response.status_code == 400 and "changed since the preview" in response.text
    db_session.refresh(parent)
    db_session.refresh(kids["open"])
    assert parent.status == TicketStatus.OPEN and kids["open"].status == TicketStatus.OPEN
    assert mail_of(db_session, "msp_handoff") == []


def test_the_automatic_handoff_leaves_linked_tickets_alone(db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    team.msp_handoff_included = True
    db_session.add(team)
    db_session.commit()
    parent, kids = parent_with_children(db_session, team)
    parent.resolution_due_at = NOW + timedelta(hours=4)  # due before the next business morning: the job takes it
    db_session.add(parent)
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW) == 1
    db_session.commit()
    db_session.refresh(parent)
    db_session.refresh(kids["open"])
    assert parent.status == TicketStatus.WITH_MSP
    assert kids["open"].status == TicketStatus.OPEN and not events(db_session, kids["open"], "with_msp_through")


# ---- 2. Telling the MSP when IT finishes with it ----------------------------------------------------------------


def resolve(client, ticket, **extra):
    return client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Fixed it", **extra})


def close(client, ticket, **extra):
    return client.post(f"/tickets/{ticket.number}/close", data={"closing_notes": "Not needed", **extra})


def test_resolving_a_ticket_with_the_msp_offers_and_sends_the_email(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team, msp_ticket_number="7000237")
    (handoff,) = mail_of(db_session, "msp_handoff")

    preview = client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "Fixed it"}).text
    assert 'name="tell_msp"' in preview and "checked" in preview and MSP in preview
    assert "MSP ticket #7000237" in preview and "resolved by IT, no action needed" in preview

    assert resolve(client, ticket, tell_msp="1").status_code == 200
    (release,) = mail_of(db_session, "msp_release")
    assert release.to_address == MSP
    assert release.subject == handoff.subject  # the same thread and subject form, so their system files it
    assert ticket.reference in release.subject
    assert release.body.startswith(f"{ticket.reference} (MSP ticket #7000237): resolved by IT, no action needed")
    assert "agent@example.com" in (release.cc or "")
    (told,) = events(db_session, ticket, "msp_told_no_longer_needed")
    assert told.to_value == "resolved"
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_id == ticket.id, AuditLog.action == "msp_told_no_longer_needed")).first()


def test_the_email_without_an_msp_number_leaves_it_out(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    resolve(client, ticket, tell_msp="1")
    (release,) = mail_of(db_session, "msp_release")
    assert release.body.startswith(f"{ticket.reference}: resolved by IT, no action needed")
    assert "MSP ticket" not in release.body


def test_unticking_the_box_sends_nothing_and_it_is_not_offered_again(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    resolve(client, ticket, msp_offered="1")  # the box was shown and unticked (the MSP resolved it themselves)
    assert mail_of(db_session, "msp_release") == []
    assert len(events(db_session, ticket, "msp_not_told")) == 1
    # Reopened and worked again, then closed: the decision stands until it goes to the MSP again.
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()
    assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/close/preview", data={"closing_notes": "x"}).text


def test_closing_offers_and_sends_the_email_too(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    assert 'name="tell_msp"' in client.post(f"/tickets/{ticket.number}/close/preview", data={"closing_notes": "Not needed"}).text
    assert close(client, ticket, tell_msp="1").status_code == 200
    (release,) = mail_of(db_session, "msp_release")
    assert "closed by IT, no action needed" in release.body


def test_nothing_is_offered_or_sent_when_mail_is_off(client, db_session, monkeypatch):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    monkeypatch.setattr(settings, "mail_sending", "dev")
    ticket = with_msp(client, db_session, team)
    monkeypatch.setattr(settings, "mail_sending", "off")
    assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text
    resolve(client, ticket, tell_msp="1")
    assert mail_of(db_session, "msp_release") == []


def test_nothing_is_offered_for_a_ticket_never_sent_to_the_msp(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    plain = triaged(db_session, team)
    by_hand = triaged(db_session, team, TicketStatus.WITH_MSP)  # set With MSP by hand: the MSP was never emailed
    for ticket in (plain, by_hand):
        assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text
        resolve(client, ticket, tell_msp="1")
    assert mail_of(db_session, "msp_release") == []


def test_a_ticket_sent_then_taken_back_by_status_is_still_offered_when_resolved(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "open"})  # the status buttons, not Take back
    assert 'name="tell_msp"' in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text


def test_no_usable_msp_address_offers_nothing(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    set_msp(db_session, "")
    assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text


def test_an_automatic_resolve_never_tells_the_msp(db_session, mail_on):
    from app.resolution import resolve_ticket

    set_msp(db_session)
    team = create_team(db_session, "Support")
    ticket = triaged(db_session, team, TicketStatus.WITH_MSP, msp_sent_at=NOW)
    resolve_ticket(db_session, ticket, None, "Resolved by the system", tell_msp=True)
    db_session.commit()
    assert mail_of(db_session, "msp_release") == []


def test_the_release_email_is_sent_by_the_mail_job(client, db_session, mail_on):
    from app.mail import BUILDERS

    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    resolve(client, ticket, tell_msp="1")  # the ticket is Resolved now: the handoff builder would refuse, this one does not
    (row,) = mail_of(db_session, "msp_release")
    built = BUILDERS["msp_release"](db_session, row)
    assert built.to == MSP and built.subject == row.subject and "no action needed" in built.body


def test_bulk_resolve_tells_the_msp_and_says_so_in_the_preview(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    plain = triaged(db_session, team, subject="Plain")
    page = client.post("/bulk-resolve/preview", data={"number": [ticket.number, plain.number], "resolution_notes": "Fixed"}).text
    assert page.count("tell the MSP it is no longer needed") == 1
    client.post("/bulk-resolve", data={"number": [ticket.number, plain.number], "resolution_notes": "Fixed", "msp_offered": "1", "tell_msp_for": [ticket.number]})
    (release,) = mail_of(db_session, "msp_release")
    assert release.ticket_id == ticket.id


# ---- 3. Take back from MSP --------------------------------------------------------------------------------------


def take_back(client, ticket, **extra):
    return client.post(f"/tickets/{ticket.number}/take-back-from-msp", data=extra)


def test_the_button_is_only_on_a_with_msp_ticket(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    plain = triaged(db_session, team, subject="Plain")
    assert "Take back from MSP" in client.get(f"/tickets/{ticket.number}").text
    assert "Take back from MSP" not in client.get(f"/tickets/{plain.number}").text
    assert take_back(client, plain).status_code == 400


def test_taking_back_an_assigned_ticket_goes_to_in_progress_and_an_unassigned_one_to_open(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    assigned = with_msp(client, db_session, team, assigned_agent_id=agent_id)
    loose = with_msp(client, db_session, team)

    page = client.post(f"/tickets/{assigned.number}/take-back-from-msp/preview").text
    assert "In progress" in page and 'name="tell_msp"' in page
    assert take_back(client, assigned, tell_msp="1").status_code == 200
    assert take_back(client, loose, msp_offered="1").status_code == 200
    db_session.refresh(assigned)
    db_session.refresh(loose)
    assert assigned.status == TicketStatus.IN_PROGRESS and loose.status == TicketStatus.OPEN
    (event,) = events(db_session, assigned, "taken_back_from_msp")
    assert event.to_value == "in_progress"
    assert db_session.exec(select(AuditLog).where(AuditLog.entity_id == assigned.id, AuditLog.action == "msp_taken_back")).first()
    # Ticked for one, left unticked for the other.
    (release,) = mail_of(db_session, "msp_release")
    assert release.ticket_id == assigned.id and "taken back by IT, no action needed" in release.body


def test_taking_back_then_resolving_does_not_tell_the_msp_twice(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    take_back(client, ticket, tell_msp="1")
    assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text
    resolve(client, ticket, tell_msp="1")
    assert len(mail_of(db_session, "msp_release")) == 1


def test_sending_again_after_a_take_back_offers_the_email_again(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    take_back(client, ticket, tell_msp="1")
    assert sent_to_msp(client, ticket).status_code == 200
    delivered(db_session, ticket)
    assert 'name="tell_msp"' in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text


def test_someone_who_cannot_work_the_ticket_cannot_take_it_back(client, db_session, mail_on):
    set_msp(db_session)
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[mine])
    ticket = triaged(db_session, other, TicketStatus.WITH_MSP, msp_sent_at=NOW)
    assert take_back(client, ticket).status_code in (403, 404)
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP


# ---- Follow-through for linked tickets --------------------------------------------------------------------------


def sent_parent(client, db_session, team):
    parent, kids = parent_with_children(db_session, team)
    sent_to_msp(client, parent, mark_linked="1", linked_digest=ids_digest([kids["open"].id, kids["progress"].id]))
    db_session.refresh(parent)
    delivered(db_session, parent)
    return parent, kids


@pytest.mark.parametrize("action", ["resolve", "close", "take_back"])
def test_finishing_the_parent_offers_to_take_its_linked_tickets_back(client, db_session, mail_on, action):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = sent_parent(client, db_session, team)
    # A linked ticket sent to the MSP on its own is not "through" the parent and is left alone.
    solo = triaged(db_session, team, linked_parent_id=parent.id, subject="Solo")
    sent_to_msp(client, solo)
    delivered(db_session, solo)

    ticked = {"take_back_linked": "1", "tell_msp": "1"}
    if action == "resolve":
        page = client.post(f"/tickets/{parent.number}/resolve/preview", data={"resolution_notes": "x"}).text
        resolve(client, parent, **ticked)
    elif action == "close":
        page = client.post(f"/tickets/{parent.number}/close/preview", data={"closing_notes": "x"}).text
        close(client, parent, **ticked)
    else:
        page = client.post(f"/tickets/{parent.number}/take-back-from-msp/preview").text
        take_back(client, parent, **ticked)
    assert 'name="take_back_linked"' in page and "2 linked tickets" in page and "Solo" not in page

    for name in ("open", "progress"):
        db_session.refresh(kids[name])
        assert kids[name].status == TicketStatus.OPEN
        assert events(db_session, kids[name], "taken_back_from_msp")
    db_session.refresh(solo)
    assert solo.status == TicketStatus.WITH_MSP
    assert len(events(db_session, parent, "linked_taken_back_from_msp")) == 1
    assert len(mail_of(db_session, "msp_release")) == 1  # one email, about the parent: nothing was sent for the children


def test_leaving_the_linked_box_unticked_leaves_them_with_the_msp(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = sent_parent(client, db_session, team)
    resolve(client, parent, tell_msp="1")
    db_session.refresh(kids["open"])
    assert kids["open"].status == TicketStatus.WITH_MSP


def test_a_linked_ticket_taken_back_on_its_own_is_not_offered_again(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = sent_parent(client, db_session, team)
    take_back(client, kids["open"])  # a linked ticket was never emailed to the MSP: nothing to tell
    assert mail_of(db_session, "msp_release") == []
    page = client.post(f"/tickets/{parent.number}/resolve/preview", data={"resolution_notes": "x"}).text
    assert "1 linked ticket " in page


def test_the_no_longer_needed_template_is_an_admin_template():
    from app.email_templates import REGISTRY

    defn = REGISTRY["msp_no_longer_needed"]
    assert "outcome" in defn.required and "msp_ticket_text" in defn.body_placeholders


# ---- Found in review --------------------------------------------------------------------------------------------


def test_a_form_that_never_showed_the_box_records_nothing_and_offers_again(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "open"})
    resolve(client, ticket)  # no tell_msp and no msp_offered: not an unticked box
    assert events(db_session, ticket, "msp_not_told") == [] and mail_of(db_session, "msp_release") == []


def test_nothing_is_offered_until_the_handoff_has_gone(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    sent_to_msp(client, ticket)  # queued, not yet sent by the worker: the MSP knows nothing of it
    assert 'name="tell_msp"' not in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text
    delivered(db_session, ticket)
    assert 'name="tell_msp"' in client.post(f"/tickets/{ticket.number}/resolve/preview", data={"resolution_notes": "x"}).text


def test_the_email_goes_to_the_address_the_handoff_went_to(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    set_msp(db_session, "new@msp.example")
    resolve(client, ticket, tell_msp="1")
    (release,) = mail_of(db_session, "msp_release")
    assert release.to_address == MSP


def test_a_taken_back_ticket_is_not_chased_for_an_acknowledgement(client, db_session, mail_on):
    from app.msp import alert_unacknowledged

    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team)
    take_back(client, ticket, tell_msp="1")
    assert alert_unacknowledged(db_session, NOW + timedelta(hours=2)) == 0


def test_bulk_resolve_can_leave_one_msp_ticket_unticked(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    a, b = with_msp(client, db_session, team), with_msp(client, db_session, team)
    client.post("/bulk-resolve", data={"number": [a.number, b.number], "resolution_notes": "Fixed", "msp_offered": "1", "tell_msp_for": [a.number]})
    (release,) = mail_of(db_session, "msp_release")
    assert release.ticket_id == a.id
    assert len(events(db_session, b, "msp_not_told")) == 1


def test_a_split_part_taken_back_locks_its_original_first(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    original = triaged(db_session, team)
    part = triaged(db_session, team, TicketStatus.WITH_MSP, split_from_id=original.id, msp_sent_at=NOW)
    assert take_back(client, part).status_code == 200
    db_session.refresh(part)
    assert part.status == TicketStatus.OPEN


# ---- Take back puts the ticket on hold (#155) ---------------------------------------------------------------------


def included(db_session, name="Support"):
    team = create_team(db_session, name)
    team.msp_handoff_included = True
    db_session.add(team)
    db_session.commit()
    return team


def test_a_taken_back_ticket_is_on_hold_until_released_and_the_handoff_skips_it(client, db_session, mail_on):
    set_msp(db_session)
    team = included(db_session)
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = with_msp(client, db_session, team, assigned_agent_id=agent_id)

    assert "on MSP hold" in client.post(f"/tickets/{ticket.number}/take-back-from-msp/preview").text
    assert take_back(client, ticket).status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS and ticket.msp_hold is True
    assert "on hold" in events(db_session, ticket, "msp_hold")[0].detail
    assert "On MSP hold" in client.get(f"/tickets/{ticket.number}").text

    ticket.resolution_due_at = NOW + timedelta(hours=4)  # due before the next business morning
    db_session.add(ticket)
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW) == 0  # held: not sent straight back
    assert client.post(f"/tickets/{ticket.number}/msp-hold/release").status_code == 200
    assert hand_off_due_tickets(db_session, NOW) == 1


def test_anyone_taking_back_sets_the_hold_not_only_the_assignee(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    colleague = agent_in(client, db_session, member_of=[team], email="colleague@example.com")
    client.get("/auth/logout")
    me = agent_in(client, db_session, member_of=[team])
    assert me != colleague
    ticket = with_msp(client, db_session, team, assigned_agent_id=colleague)
    assert take_back(client, ticket).status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == colleague and ticket.msp_hold is True


def test_linked_tickets_taken_back_with_the_parent_are_held_too(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    parent, kids = sent_parent(client, db_session, team)
    take_back(client, parent, take_back_linked="1")
    for name in ("open", "progress"):
        db_session.refresh(kids[name])
        assert kids[name].status == TicketStatus.OPEN and kids[name].msp_hold is True

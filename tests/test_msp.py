"""Send to MSP, the hold and the automatic handoff (phase 5A, issue #138)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.config import settings
from app.models import (
    AuditLog,
    Message,
    MessageDirection,
    MessageVisibility,
    OutboundEmail,
    Team,
    Ticket,
    TicketEscalator,
    TicketEvent,
    TicketFollower,
    TicketStatus,
    UserRole,
)
from app.msp import hand_off_due_tickets, handoff_window
from app.settings_store import MSP_ADDRESS, MSP_BANK_HOLIDAYS, MSP_HANDOFF_HOUR, set_setting
from app.time_zones import next_business_morning, parse_dates
from tests.conftest import create_team, login_as, set_role
from tests.test_escalation import categorised
from tests.test_visibility import agent_in, make_ticket

MSP = "support@msp.example"
UTC = timezone.utc
# Wednesday 4 November 2026, 18:30 in London (GMT in November): half an hour into the handoff.
NOW = datetime(2026, 11, 4, 18, 30, tzinfo=UTC)
START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
THURSDAY_MORNING = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


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


def message(db_session, ticket, body, direction, sender="Sam", author_user_id=None):
    public = direction != MessageDirection.INTERNAL
    row = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, author_user_id=author_user_id,
        visibility=MessageVisibility.PUBLIC if public else MessageVisibility.AGENTS_ONLY,
    )
    db_session.add(row)
    db_session.commit()
    return row


def send(client, ticket, reason="Needs an engineer", scope="thread"):
    return client.post(f"/tickets/{ticket.number}/send-to-msp", data={"reason": reason, "scope": scope})


def events(db_session, ticket, kind):
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def msp_mail(db_session):
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_handoff")).all()


# ---- Business mornings -----------------------------------------------------------------------------------


def test_next_business_morning_skips_weekends_and_holidays():
    friday_evening = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    assert next_business_morning(friday_evening, "Europe/London", 8) == datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert next_business_morning(START, "Europe/London", 8) == THURSDAY_MORNING
    monday_holiday = frozenset({date(2026, 11, 9)})
    assert next_business_morning(friday_evening, "Europe/London", 8, monday_holiday) == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    # Before the hour on a business day is that day's morning.
    assert next_business_morning(datetime(2026, 11, 4, 6, 0, tzinfo=UTC), "Europe/London", 8) == datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def test_parse_dates():
    assert parse_dates("2026-12-25, 2026-12-28 2027-01-01") == [date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1)]
    assert parse_dates("") == []
    with pytest.raises(ValueError):
        parse_dates("25 December")


# ---- Settings --------------------------------------------------------------------------------------------


def test_the_msp_settings_are_validated(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    assert client.post("/admin/settings", data={"key": MSP_ADDRESS, "value": "not an address"}).status_code == 400
    assert client.post("/admin/settings", data={"key": MSP_ADDRESS, "value": "Help@MSP.example"}, follow_redirects=False).status_code == 303
    assert "help@msp.example" in client.get("/admin/settings").text
    assert client.post("/admin/settings", data={"key": MSP_ADDRESS, "value": ""}, follow_redirects=False).status_code == 303
    assert client.post("/admin/settings", data={"key": MSP_HANDOFF_HOUR, "value": "24"}).status_code == 400
    assert client.post("/admin/settings", data={"key": MSP_BANK_HOLIDAYS, "value": "Christmas"}).status_code == 400
    ok = client.post("/admin/settings", data={"key": MSP_BANK_HOLIDAYS, "value": "2026-12-28 2026-12-25"}, follow_redirects=False)
    assert ok.status_code == 303
    assert "2026-12-25, 2026-12-28" in client.get("/admin/settings").text


def test_a_team_is_included_in_the_handoff_by_an_admin(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    team = create_team(db_session, "Support")
    assert team.msp_handoff_included is False
    client.post(f"/admin/teams/{team.id}/msp-handoff", data={"included": "1"})
    db_session.refresh(team)
    assert team.msp_handoff_included is True
    client.post(f"/admin/teams/{team.id}/msp-handoff", data={})
    db_session.refresh(team)
    assert team.msp_handoff_included is False
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "team")).all()]
    assert "msp_handoff_included" in actions and "msp_handoff_excluded" in actions


# ---- Send to MSP -----------------------------------------------------------------------------------------


def test_with_no_msp_address_sending_is_refused(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)

    response = send(client, ticket)
    assert response.status_code == 400 and "No MSP address is set" in response.text
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN and not ticket.is_escalated
    assert "Cannot send to the MSP" in client.get(f"/tickets/{ticket.number}").text


def test_sending_keeps_the_ticket_in_its_team_with_msp(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team, assigned_agent_id=agent_id, priority="p2")
    categorised(db_session, ticket, escalates_to=None, name="Printers")
    message(db_session, ticket, "The printer is jammed", MessageDirection.INBOUND, sender="requester@example.com")
    message(db_session, ticket, "Checked the rollers", MessageDirection.INTERNAL, sender="Alex")

    response = send(client, ticket, reason="Needs an engineer on site")
    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP
    assert ticket.team_id == team.id and ticket.assigned_agent_id == agent_id  # nobody's access changes
    assert ticket.is_escalated and ticket.escalation_reason == "Needs an engineer on site"
    assert ticket.msp_sent_at is not None and ticket.msp_hold is False

    (mail,) = msp_mail(db_session)
    assert mail.to_address == MSP and ticket.reference in mail.subject
    assert mail.body.startswith("End user: requester@example.com")  # opens with the end user's details
    assert "Requested by:" in mail.body and "agent@example.com" in mail.body
    assert "Priority: P2" in mail.body and "Category: Printers" in mail.body
    assert "Needs an engineer on site" in mail.body
    assert "The printer is jammed" in mail.body and "Checked the rollers" in mail.body and "internal note" in mail.body
    assert "agent@example.com" in (mail.cc or "")  # the sender is copied

    (told,) = [
        m for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
        if m.direction == MessageDirection.OUTBOUND
    ]
    assert "support partner" in told.body and told.visibility == MessageVisibility.PUBLIC
    assert len(db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "notice")).all()) == 1

    assert len(events(db_session, ticket, "sent_to_msp")) == 1
    assert any(e.to_value == "with_msp" for e in events(db_session, ticket, "status_changed"))
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "sent_to_msp", AuditLog.entity_id == ticket.id)).first()
    assert db_session.exec(select(TicketEscalator).where(TicketEscalator.ticket_id == ticket.id)).first()
    assert db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id, TicketFollower.is_internal.is_(True))).first()


def test_only_the_original_request_when_chosen(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    message(db_session, ticket, "First words from the user", MessageDirection.INBOUND, sender="requester@example.com")
    message(db_session, ticket, "A later chat with IT", MessageDirection.OUTBOUND, sender="Alex")
    message(db_session, ticket, "Secret internal note", MessageDirection.INTERNAL, sender="Alex")

    send(client, ticket, scope="original")
    (mail,) = msp_mail(db_session)
    assert "First words from the user" in mail.body
    assert "A later chat with IT" not in mail.body and "Secret internal note" not in mail.body


def test_events_are_never_sent_and_the_thread_is_the_default(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    message(db_session, ticket, "Hello", MessageDirection.INBOUND, sender="requester@example.com")
    message(db_session, ticket, "We are on it", MessageDirection.OUTBOUND, sender="Alex")
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="assigned", actor_email="x@example.com", detail="EVENTDETAIL"))
    db_session.commit()

    send(client, ticket)
    (mail,) = msp_mail(db_session)
    assert "Hello" in mail.body and "We are on it" in mail.body and "EVENTDETAIL" not in mail.body


def test_the_preview_changes_nothing_and_shows_the_email(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    message(db_session, ticket, "Hello from the user", MessageDirection.INBOUND, sender="requester@example.com")

    response = client.post(f"/tickets/{ticket.number}/send-to-msp/preview", data={"reason": "Because", "scope": "thread"})
    assert response.status_code == 200
    assert MSP in response.text and "Hello from the user" in response.text and "support partner" in response.text
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN and msp_mail(db_session) == []
    assert events(db_session, ticket, "sent_to_msp") == []
    assert client.post(f"/tickets/{ticket.number}/send-to-msp/preview", data={"reason": " "}).status_code == 400


def test_a_reason_is_needed_and_a_ticket_goes_once(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)

    assert send(client, ticket, reason="  ").status_code == 400
    assert send(client, ticket).status_code == 200
    again = send(client, ticket)
    assert again.status_code == 400 and "already with the MSP" in again.text
    assert len(msp_mail(db_session)) == 1 and len(events(db_session, ticket, "sent_to_msp")) == 1


def test_refusals(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    untriaged = make_ticket(db_session, team.id, TicketStatus.NEW)
    resolved = triaged(db_session, team, TicketStatus.RESOLVED)
    approval = triaged(db_session, team, TicketStatus.AWAITING_APPROVAL)
    assert "Triage" in send(client, untriaged).text
    assert "resolved or closed" in send(client, resolved).text
    assert "approval" in send(client, approval).text
    assert msp_mail(db_session) == []


def test_a_ticket_split_into_parts_sends_its_parts_not_the_original(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    original = triaged(db_session, team)
    triaged(db_session, team, split_from_id=original.id)
    assert "split" in send(client, original).text


def test_an_agent_outside_the_team_cannot_send(client, db_session, mail_on):
    set_msp(db_session)
    team, other = create_team(db_session, "Support"), create_team(db_session, "Finance")
    agent_in(client, db_session, member_of=[other])
    ticket = triaged(db_session, team)
    assert send(client, ticket).status_code in (403, 404)
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN


def test_with_mail_off_the_ticket_still_moves_and_the_preview_says_nothing_is_emailed(client, db_session):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    preview = client.post(f"/tickets/{ticket.number}/send-to-msp/preview", data={"reason": "x"})
    assert "nothing will be emailed" in preview.text
    send(client, ticket)
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP and msp_mail(db_session) == []


def test_the_email_waiting_to_go_is_dropped_if_the_ticket_is_taken_back(client, db_session, mail_on):
    from app.mail import deliver_mail

    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    send(client, ticket)
    db_session.refresh(ticket)
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()

    sent = []
    deliver_mail(db_session, send=sent.append)
    assert [m.to for m in sent if m.to == MSP] == []
    (row,) = msp_mail(db_session)
    assert row.sent_at is None and "no longer with the MSP" in row.last_error


def test_the_email_goes_with_the_text_that_was_previewed(client, db_session, mail_on):
    from app.mail import deliver_mail

    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    message(db_session, ticket, "Before", MessageDirection.INBOUND, sender="requester@example.com")
    send(client, ticket)
    message(db_session, ticket, "Written after sending", MessageDirection.INBOUND, sender="requester@example.com")

    sent = []
    deliver_mail(db_session, send=sent.append)
    (to_msp,) = [m for m in sent if m.to == MSP]
    assert "Before" in to_msp.body and "Written after sending" not in to_msp.body
    assert to_msp.cc == ["agent@example.com"]


# ---- Escalating to the MSP -------------------------------------------------------------------------------


def test_escalating_a_category_that_targets_the_msp_sends_it(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    categorised(db_session, ticket, escalates_to=None)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "use Send to MSP above" in page
    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Over to the partner"})
    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP and ticket.team_id == team.id and ticket.is_escalated
    assert len(msp_mail(db_session)) == 1


def test_escalating_to_the_msp_without_an_address_still_says_why_not(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    categorised(db_session, ticket, escalates_to=None)
    response = client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "x"})
    assert response.status_code == 400 and "No MSP address" in response.text


# ---- Hold ------------------------------------------------------------------------------------------------


def test_the_assigned_agent_holds_and_anyone_working_it_releases(client, db_session):
    team = create_team(db_session, "Support")
    mine = agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team, assigned_agent_id=mine)

    assert client.post(f"/tickets/{ticket.number}/msp-hold").status_code == 200
    db_session.refresh(ticket)
    assert ticket.msp_hold is True and len(events(db_session, ticket, "msp_hold")) == 1
    assert "on MSP hold" in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/msp-hold").status_code == 400  # already held

    # A colleague who is not the assignee can release it, so it is never stuck.
    client.get("/auth/logout")
    other = agent_in(client, db_session, member_of=[team], email="colleague@example.com")
    assert other != mine
    assert client.post(f"/tickets/{ticket.number}/msp-hold/release").status_code == 200
    db_session.refresh(ticket)
    assert ticket.msp_hold is False and len(events(db_session, ticket, "msp_hold_released")) == 1
    assert client.post(f"/tickets/{ticket.number}/msp-hold/release").status_code == 400


def test_only_the_assigned_agent_can_hold(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)  # unassigned
    response = client.post(f"/tickets/{ticket.number}/msp-hold")
    assert response.status_code == 400 and "assigned" in response.text
    db_session.refresh(ticket)
    assert ticket.msp_hold is False


def test_sending_by_hand_ends_a_hold(client, db_session, mail_on):
    set_msp(db_session)
    team = create_team(db_session, "Support")
    mine = agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team, assigned_agent_id=mine, msp_hold=True)
    send(client, ticket)
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP and ticket.msp_hold is False


# ---- The automatic handoff -------------------------------------------------------------------------------


def included_team(db_session, name="Support"):
    team = create_team(db_session, name)
    team.msp_handoff_included = True
    db_session.add(team)
    db_session.commit()
    return team


def at_risk(db_session, team, status=TicketStatus.OPEN, due=START + timedelta(hours=10), **fields):
    return triaged(db_session, team, status, resolution_due_at=due, **fields)


def test_the_window_runs_from_the_handoff_hour_to_the_next_business_morning(db_session):
    assert handoff_window(db_session, NOW) == (START, THURSDAY_MORNING)
    assert handoff_window(db_session, datetime(2026, 11, 5, 3, 0, tzinfo=UTC)) == (START, THURSDAY_MORNING)  # still the same night
    assert handoff_window(db_session, datetime(2026, 11, 4, 17, 59, tzinfo=UTC)) is None  # business hours
    assert handoff_window(db_session, datetime(2026, 11, 5, 8, 0, tzinfo=UTC)) is None
    # Friday evening's handoff covers the weekend, to Monday morning.
    friday = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
    assert handoff_window(db_session, friday) == (datetime(2026, 11, 6, 18, 0, tzinfo=UTC), datetime(2026, 11, 9, 8, 0, tzinfo=UTC))
    assert handoff_window(db_session, datetime(2026, 11, 7, 12, 0, tzinfo=UTC)) is not None  # Saturday


def test_the_handoff_follows_the_time_zone_and_the_hour_settings(db_session):
    from app.settings_store import DEFAULT_TIME_ZONE

    set_setting(db_session, DEFAULT_TIME_ZONE, "America/New_York")  # EST in November: 18:00 is 23:00 UTC
    set_setting(db_session, MSP_HANDOFF_HOUR, "17")
    db_session.commit()
    assert handoff_window(db_session, datetime(2026, 11, 4, 22, 30, tzinfo=UTC))[0] == datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
    assert handoff_window(db_session, datetime(2026, 11, 4, 21, 30, tzinfo=UTC)) is None


def test_bank_holidays_stretch_the_window(db_session):
    set_setting(db_session, MSP_BANK_HOLIDAYS, "2026-11-05")
    db_session.commit()
    assert handoff_window(db_session, NOW) == (START, datetime(2026, 11, 6, 8, 0, tzinfo=UTC))


def test_the_job_sends_what_is_due_before_the_next_morning(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    due = at_risk(db_session, team)
    due_late = at_risk(db_session, team, due=THURSDAY_MORNING + timedelta(hours=2))  # after the morning: the team will do it
    first_response = triaged(db_session, team, first_response_due_at=START + timedelta(hours=1))
    answered = triaged(db_session, team, first_response_due_at=START + timedelta(hours=1), first_responded_at=START - timedelta(hours=1))
    overdue = at_risk(db_session, team, due=START - timedelta(hours=5))
    no_sla = triaged(db_session, team)

    assert hand_off_due_tickets(db_session, NOW) == 3
    db_session.commit()
    status = lambda t: db_session.get(Ticket, t.id).status
    assert [status(t) for t in (due, first_response, overdue)] == [TicketStatus.WITH_MSP] * 3
    assert [status(t) for t in (due_late, answered, no_sla)] == [TicketStatus.OPEN] * 3

    sent_to = db_session.get(Ticket, due.id)
    assert sent_to.team_id == team.id and not sent_to.is_escalated  # nobody's access changes, and no escalator is made
    (mail,) = [m for m in msp_mail(db_session) if m.ticket_id == due.id]
    assert mail.to_address == MSP and "the automatic 18:00 handoff" in mail.body
    assert db_session.exec(select(TicketEscalator)).all() == []
    assert any(e.actor_email == "system" for e in events(db_session, due, "sent_to_msp"))
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "sent_to_msp", AuditLog.entity_id == due.id, AuditLog.actor_email == "system")).first()


def test_a_second_or_late_run_does_nothing_more(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    at_risk(db_session, team)
    assert hand_off_due_tickets(db_session, NOW) == 1
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW) == 0
    assert hand_off_due_tickets(db_session, datetime(2026, 11, 5, 6, 0, tzinfo=UTC)) == 0
    assert len(msp_mail(db_session)) == 1


def test_a_run_that_is_late_still_makes_the_handoff(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    at_risk(db_session, team)
    assert hand_off_due_tickets(db_session, datetime(2026, 11, 5, 5, 0, tzinfo=UTC)) == 1  # the worker was down at 18:00


def test_nothing_is_sent_in_business_hours(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    at_risk(db_session, team)
    assert hand_off_due_tickets(db_session, datetime(2026, 11, 4, 14, 0, tzinfo=UTC)) == 0
    assert hand_off_due_tickets(db_session, datetime(2026, 11, 5, 9, 0, tzinfo=UTC)) == 0  # a missed night is not made up by day
    assert msp_mail(db_session) == []


def test_who_is_left_out(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    off = create_team(db_session, "Not included")
    keep = [
        at_risk(db_session, off),  # the team is not included
        at_risk(db_session, team, msp_hold=True),  # held
        at_risk(db_session, team, TicketStatus.AWAITING_USER),
        at_risk(db_session, team, TicketStatus.WITH_SUPPLIER),
        at_risk(db_session, team, TicketStatus.SCHEDULED),
        at_risk(db_session, team, TicketStatus.WITH_MSP),
        at_risk(db_session, team, TicketStatus.RESOLVED),
        at_risk(db_session, team, triaged_at=None, status=TicketStatus.NEW),
    ]
    original = at_risk(db_session, team)
    part = at_risk(db_session, team, split_from_id=original.id)
    before = [(t.id, db_session.get(Ticket, t.id).status) for t in keep]

    assert hand_off_due_tickets(db_session, NOW) == 1  # the part only: a split original follows its parts
    db_session.commit()
    assert db_session.get(Ticket, part.id).status == TicketStatus.WITH_MSP
    # The original's status follows its parts (it shows With MSP); it was not sent itself.
    assert [m.ticket_id for m in msp_mail(db_session)] == [part.id]
    assert [(i, db_session.get(Ticket, i).status) for i, _ in before] == before


def test_a_ticket_in_a_sub_team_follows_its_own_teams_setting(db_session, mail_on):
    set_msp(db_session)
    parent = included_team(db_session, "Support")
    child = create_team(db_session, "Second line")
    child.parent_team_id = parent.id
    db_session.add(child)
    db_session.commit()
    ticket = at_risk(db_session, child)
    assert hand_off_due_tickets(db_session, NOW) == 0
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN


def test_with_no_address_the_job_does_nothing_and_records_why_once(db_session, mail_on):
    team = included_team(db_session)
    ticket = at_risk(db_session, team)
    assert hand_off_due_tickets(db_session, NOW) == 0
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW + timedelta(minutes=5)) == 0
    db_session.commit()
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN
    notes = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "msp_handoff")).all()
    assert len(notes) == 1 and "no MSP address" in notes[0].detail and "2026-11-04" in notes[0].detail
    # Set the address and the next run in the window makes the handoff.
    set_msp(db_session)
    assert hand_off_due_tickets(db_session, NOW + timedelta(minutes=10)) == 1


def test_a_ticket_that_arrives_during_the_night_and_is_due_before_morning_goes_at_the_next_run(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    late = at_risk(db_session, team, created_at=START + timedelta(hours=1), due=THURSDAY_MORNING - timedelta(hours=5))
    assert hand_off_due_tickets(db_session, START + timedelta(hours=2)) == 1
    assert db_session.get(Ticket, late.id).status == TicketStatus.WITH_MSP


def test_a_held_ticket_is_sent_once_released_if_the_window_is_still_open(db_session, mail_on):
    set_msp(db_session)
    team = included_team(db_session)
    ticket = at_risk(db_session, team, msp_hold=True)
    assert hand_off_due_tickets(db_session, NOW) == 0
    ticket = db_session.get(Ticket, ticket.id)
    ticket.msp_hold = False
    db_session.add(ticket)
    db_session.commit()
    assert hand_off_due_tickets(db_session, NOW + timedelta(minutes=5)) == 1


def test_the_job_is_registered_and_runs_through_the_runner(db_session, mail_on):
    from app.scheduled_jobs import SCHEDULED_JOBS, registry_errors

    assert "msp-handoff" in [job.name for job in SCHEDULED_JOBS]
    assert registry_errors(SCHEDULED_JOBS) == []
    # Called as the runner calls it: on a session, in business hours it finds nothing and does not raise.
    from app.msp import run_msp_handoff

    run_msp_handoff(db_session)


def test_the_job_does_nothing_with_mail_switched_off_and_says_so(db_session):
    set_msp(db_session)
    team = included_team(db_session)
    ticket = at_risk(db_session, team)
    assert settings.mail_sending == "off"
    assert hand_off_due_tickets(db_session, NOW) == 0
    db_session.commit()
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN
    (note,) = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "msp_handoff")).all()
    assert "MAIL_SENDING" in note.detail


def test_the_address_cannot_be_the_shared_mailbox(client, db_session, mail_on, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MSP)
    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    response = send(client, ticket)
    assert response.status_code == 400 and "shared mailbox" in response.text
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN


def test_a_thread_that_changed_since_the_preview_is_not_sent_unseen(client, db_session, mail_on):
    import re

    set_msp(db_session)
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team)
    message(db_session, ticket, "Hello", MessageDirection.INBOUND, sender="requester@example.com")
    shown = client.post(f"/tickets/{ticket.number}/send-to-msp/preview", data={"reason": "Because", "scope": "thread"}).text
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', shown).group(1)

    message(db_session, ticket, "A reply that arrived after the preview", MessageDirection.INBOUND, sender="requester@example.com")
    stale = client.post(f"/tickets/{ticket.number}/send-to-msp", data={"reason": "Because", "scope": "thread", "digest": digest})
    assert stale.status_code == 400 and "Preview it again" in stale.text
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN and msp_mail(db_session) == []

    fresh = client.post(f"/tickets/{ticket.number}/send-to-msp/preview", data={"reason": "Because", "scope": "thread"}).text
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', fresh).group(1)
    assert client.post(f"/tickets/{ticket.number}/send-to-msp", data={"reason": "Because", "scope": "thread", "digest": digest}).status_code == 200
    (mail,) = msp_mail(db_session)
    assert "A reply that arrived after the preview" in mail.body


def test_resolving_ends_a_hold(client, db_session):
    team = create_team(db_session, "Support")
    mine = agent_in(client, db_session, member_of=[team])
    ticket = triaged(db_session, team, assigned_agent_id=mine, msp_hold=True)
    from app.resolution import resolve_ticket
    from app.models import User

    resolve_ticket(db_session, ticket, db_session.get(User, mine), "Fixed", send_email=False)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED and ticket.msp_hold is False

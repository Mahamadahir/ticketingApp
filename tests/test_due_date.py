"""The Due date (issue #121): its default from Required by, who moves it and why, the SLA deadline in the
requester's time zone, the per-type lead times, Scheduled needing a date, and the morning job."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.due_today import morning_of, wake_scheduled_tickets
from app.models import AuditLog, Notification, RequestType, Ticket, TicketStatus, User, UserRole
from app.required_by import RequiredByError, default_due, prepare_scheduled, set_due, set_required_by
from app.settings_store import (
    DEFAULT_TIME_ZONE,
    DUE_DAYS_BEFORE_REQUIRED_BY,
    REGISTRY,
    REQUIRED_BY_LEAD_DAYS,
    SCHEDULED_MORNING_HOUR,
    set_setting,
)
from app.time_zones import end_of_day_utc
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_raising import desk, last_ticket, raise_it
from tests.test_request_types import make
from tests.test_triage import agent, events, new_ticket


@pytest.fixture(autouse=True)
def utc_default_zone(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()


def today():
    return datetime.now(timezone.utc).date()


def day(offset):
    return (today() + timedelta(days=offset)).isoformat()


def offering(db_session, name="Meeting room", **fields):
    request_type = make(db_session, name)
    request_type.offers_required_by = True
    for key, value in fields.items():
        setattr(request_type, key, value)
    db_session.add(request_type)
    db_session.commit()
    return request_type


def worked(db_session, team, request_type=None, **fields):
    """A triaged, open ticket in the team (so its agents work it)."""
    ticket = new_ticket(db_session, team)
    ticket.triaged_at, ticket.status, ticket.priority = datetime.now(timezone.utc), TicketStatus.OPEN, "p3"
    ticket.request_type_id = request_type.id if request_type else None
    for key, value in fields.items():
        setattr(ticket, key, value)
    db_session.add(ticket)
    db_session.commit()
    return ticket


def audit(db_session, action):
    db_session.expire_all()
    return [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == action)).all()]


# ---- The default and the deadline -----------------------------------------------------------------


def test_due_defaults_to_one_day_before_required_by_and_the_deadline_is_the_end_of_that_day(client, db_session):
    request_type = offering(db_session)
    login_as(client, "jo@example.com", "Jo")

    raise_it(client, request_type_id=request_type.id, required_by=day(10))

    ticket = last_ticket(db_session)
    assert ticket.due_on == today() + timedelta(days=9) and not ticket.due_set_by_hand
    assert ticket.resolution_due_at == end_of_day_utc(ticket.due_on, "UTC")
    [event] = events(db_session, ticket, "due_changed")
    assert (event.from_value, event.to_value, event.detail) == (None, day(9), "follows the Required by date")


def test_the_request_type_sets_its_own_number_of_days_and_a_blank_uses_the_setting(db_session):
    team = create_team(db_session, "Service Desk")
    plain, three = offering(db_session, "A"), offering(db_session, "B", due_days_before=3)
    ticket_a, ticket_b = worked(db_session, team, plain), worked(db_session, team, three)
    required = today() + timedelta(days=20)

    assert default_due(db_session, ticket_a, required, today()) == required - timedelta(days=1)  # the setting, 1
    set_setting(db_session, DUE_DAYS_BEFORE_REQUIRED_BY, "4")
    assert default_due(db_session, ticket_a, required, today()) == required - timedelta(days=4)
    assert default_due(db_session, ticket_b, required, today()) == required - timedelta(days=3)
    assert REGISTRY[DUE_DAYS_BEFORE_REQUIRED_BY].default == "1"


def test_the_default_is_never_before_today_or_after_required_by(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team, offering(db_session, due_days_before=5))

    assert default_due(db_session, ticket, today() + timedelta(days=2), today()) == today()  # not already late
    assert default_due(db_session, ticket, today(), today()) == today()
    assert default_due(db_session, ticket, today() - timedelta(days=3), today()) == today() - timedelta(days=3)  # backdated


def test_the_deadline_is_the_end_of_the_day_in_the_requesters_zone(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team, requester_email="kiwi@example.com")
    db_session.add(User(email="kiwi@example.com", display_name="Kiwi", time_zone="Pacific/Auckland"))
    db_session.commit()

    set_due(db_session, ticket, today() + timedelta(days=5), None, "agent@example.com")

    assert ticket.resolution_due_at == end_of_day_utc(today() + timedelta(days=5), "Pacific/Auckland")
    assert ticket.resolution_due_at != end_of_day_utc(today() + timedelta(days=5), "UTC")


def test_changing_required_by_recalculates_due_unless_an_agent_set_it_by_hand_and_then_clamps(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team, offering(db_session))
    set_required_by(db_session, ticket, today() + timedelta(days=10), "jo@example.com")
    assert ticket.due_on == today() + timedelta(days=9)
    set_required_by(db_session, ticket, today() + timedelta(days=14), "jo@example.com")
    assert ticket.due_on == today() + timedelta(days=13)  # follows

    set_due(db_session, ticket, today() + timedelta(days=14), "The room is free only that morning", "agent@example.com")
    assert ticket.due_on == today() + timedelta(days=14) and ticket.due_set_by_hand
    set_required_by(db_session, ticket, today() + timedelta(days=20), "jo@example.com")
    assert ticket.due_on == today() + timedelta(days=14)  # kept
    set_required_by(db_session, ticket, today() + timedelta(days=6), "jo@example.com")
    assert ticket.due_on == today() + timedelta(days=6)  # clamped so it is never after Required by


def test_clearing_required_by_clears_a_default_due_but_keeps_one_set_by_hand(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team, offering(db_session))
    set_required_by(db_session, ticket, today() + timedelta(days=10), "jo@example.com")
    set_required_by(db_session, ticket, None, "jo@example.com")
    assert ticket.due_on is None  # the P3 target applies again
    set_due(db_session, ticket, today() + timedelta(days=3), None, "agent@example.com")
    set_required_by(db_session, ticket, today() + timedelta(days=10), "jo@example.com")
    set_required_by(db_session, ticket, None, "jo@example.com")
    assert ticket.due_on == today() + timedelta(days=3)


# ---- Moving Due -----------------------------------------------------------------------------------


def test_an_agent_who_works_the_ticket_moves_due_with_a_reason_recorded_twice(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, offering(db_session), required_by=today() + timedelta(days=10), due_on=today() + timedelta(days=9))

    refused = client.post(f"/tickets/{ticket.number}/due", data={"due_on": day(10)})
    ok = client.post(f"/tickets/{ticket.number}/due", data={"due_on": day(10), "due_reason": "Room only free that morning"})

    assert (refused.status_code, ok.status_code) == (400, 200) and "Say why" in refused.text
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=10) and ticket.due_set_by_hand
    [event] = events(db_session, ticket, "due_changed")
    assert (event.from_value, event.to_value, event.detail, event.actor_email) == (
        day(9), day(10), "Room only free that morning", "agent@example.com",
    )
    assert audit(db_session, "due_changed") == [f"{day(9)} -> {day(10)}: Room only free that morning"]
    assert ticket.resolution_due_at == end_of_day_utc(ticket.due_on, "UTC")


def test_due_can_never_be_after_required_by_or_in_the_past_or_far_ahead(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, offering(db_session), required_by=today() + timedelta(days=10), due_on=today() + timedelta(days=9))
    go = lambda value: client.post(f"/tickets/{ticket.number}/due", data={"due_on": value, "due_reason": "why"})

    assert "after the Required by" in go(day(11)).text
    assert "past" in go(day(-1)).text
    assert go("30/10/2026").status_code == 400
    ticket_no_date = worked(db_session, team)
    far = client.post(f"/tickets/{ticket_no_date.number}/due", data={"due_on": "9999-12-31"})
    assert far.status_code == 400 and "too far" in far.text
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=9)


def test_a_ticket_with_no_required_by_can_still_be_given_a_due_date_without_a_reason(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team)

    assert client.post(f"/tickets/{ticket.number}/due", data={"due_on": day(4)}).status_code == 200

    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=4) and ticket.required_by is None
    assert ticket.resolution_due_at == end_of_day_utc(ticket.due_on, "UTC")


def test_a_blank_date_puts_due_back_to_the_default(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, offering(db_session), required_by=today() + timedelta(days=10), due_on=today() + timedelta(days=10), due_set_by_hand=True)

    assert client.post(f"/tickets/{ticket.number}/due", data={"due_on": "", "due_reason": "Back to normal"}).status_code == 200

    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=9) and not ticket.due_set_by_hand


def test_only_agents_who_work_the_ticket_can_move_due(client, db_session):
    team = create_team(db_session, "Service Desk")
    other = create_team(db_session, "Network")
    ticket = worked(db_session, team, due_on=today() + timedelta(days=5), requester_email="jo@example.com")
    data = {"due_on": day(6), "due_reason": "later"}

    login_as(client, "jo@example.com")
    assert client.post(f"/tickets/{ticket.number}/due", data=data).status_code == 403  # the requester
    client.post("/auth/logout")
    agent(client, db_session, member_of=[other], email="stranger@example.com")
    assert client.post(f"/tickets/{ticket.number}/due", data=data).status_code in (403, 404)
    assert 'action="/tickets/%d/due"' % 0 not in client.get("/tickets").text
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=5)

    client.post("/auth/logout")
    agent(client, db_session, member_of=[team])
    assert f'hx-post="/tickets/{ticket.number}/due"' in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/due", data=data).status_code == 200


def test_a_finished_ticket_keeps_its_due_date(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, status=TicketStatus.RESOLVED, due_on=today() + timedelta(days=5))

    assert client.post(f"/tickets/{ticket.number}/due", data={"due_on": day(6), "due_reason": "x"}).status_code == 403


def test_an_awaiting_user_wait_never_moves_due(db_session):
    from app.ticket_service import set_status

    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team, due_on=today() + timedelta(days=5))
    set_due(db_session, ticket, today() + timedelta(days=5), None, "a@example.com")
    deadline = ticket.resolution_due_at
    set_status(db_session, ticket, TicketStatus.AWAITING_USER, "a@example.com")
    ticket.sla_paused_at = datetime.now(timezone.utc) - timedelta(days=2)
    set_status(db_session, ticket, TicketStatus.OPEN, "a@example.com")

    assert ticket.sla_paused_seconds >= 2 * 86400 and ticket.resolution_due_at == deadline


# ---- Scheduled needs a date ----------------------------------------------------------------------


def test_scheduled_is_refused_without_a_date_by_the_status_buttons_the_dropdown_and_the_preview(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team)

    status = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled"})
    note = client.post(f"/tickets/{ticket.number}/note", data={"body": "booked", "next_status": "scheduled"})
    email = client.post(f"/tickets/{ticket.number}/email", data={"body": "booked", "next_status": "scheduled"})
    preview = client.post(f"/tickets/{ticket.number}/email/preview", data={"body": "booked", "next_status": "scheduled"})

    assert [r.status_code for r in (status, note, email, preview)] == [400, 400, 400, 400]
    assert all("Scheduled needs a date" in r.text for r in (status, note, email, preview))
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN
    from app.models import Message

    assert db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() == []  # no note, no email


def test_a_ticket_that_already_has_a_due_date_can_be_scheduled_with_no_date_typed(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, due_on=today() + timedelta(days=3))

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled"}).status_code == 200

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.SCHEDULED and ticket.due_on == today() + timedelta(days=3)


def test_the_forms_ask_for_the_date_and_the_preview_carries_it(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, due_on=today() + timedelta(days=3))

    page = client.get(f"/tickets/{ticket.number}").text
    assert page.count('name="due_on"') >= 4  # status buttons, Due form, email and note dropdowns
    preview = client.post(
        f"/tickets/{ticket.number}/email/preview",
        data={"body": "Booked", "next_status": "scheduled", "due_on": day(4), "due_reason": "moved"},
    )
    assert preview.status_code == 200 and f'name="due_on" value="{day(4)}"' in preview.text
    assert "pauses its SLA clock" not in preview.text
    sent = client.post(
        f"/tickets/{ticket.number}/email", data={"body": "Booked", "next_status": "scheduled", "due_on": day(4), "due_reason": "moved"}
    )
    assert sent.status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.SCHEDULED and ticket.due_on == today() + timedelta(days=4)


def test_moving_the_date_while_scheduling_needs_a_reason_and_a_past_date_is_refused(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, due_on=today() + timedelta(days=3))

    no_reason = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled", "due_on": day(5)})
    past = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled", "due_on": day(-1)})
    ok = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled", "due_on": day(5), "due_reason": "supplier date"})

    assert (no_reason.status_code, past.status_code, ok.status_code) == (400, 400, 200)
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=5) and ticket.status == TicketStatus.SCHEDULED


def test_set_status_itself_refuses_scheduled_with_no_due_date(db_session):
    from app.ticket_service import ScheduledNeedsDate, set_status

    ticket = worked(db_session, create_team_for(db_session))
    with pytest.raises(ScheduledNeedsDate):
        set_status(db_session, ticket, TicketStatus.SCHEDULED, "a@example.com")
    with pytest.raises(RequiredByError):
        prepare_scheduled(db_session, ticket, "", None, "a@example.com")


def create_team_for(db_session):
    return create_team(db_session, "Service Desk")


def test_reopening_to_scheduled_by_hand_needs_a_date_too(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc))

    refused = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled"})
    ok = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled", "due_on": day(3)})

    assert refused.status_code == 400 and ok.status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.SCHEDULED and ticket.due_on == today() + timedelta(days=3)


# ---- The morning job ------------------------------------------------------------------------------


def scheduled(db_session, team, due, zone=None, assignee=None, **fields):
    ticket = worked(db_session, team, status=TicketStatus.SCHEDULED, due_on=due, assigned_agent_id=assignee, **fields)
    if zone:
        db_session.add(User(email=ticket.requester_email, display_name="Req", time_zone=zone))
        db_session.commit()
    return ticket


def test_on_the_morning_of_the_due_date_it_goes_back_to_open_and_the_assignee_is_told(db_session):
    team = create_team(db_session, "Service Desk")
    assignee = User(email="ann@example.com", display_name="Ann")
    db_session.add(assignee)
    db_session.commit()
    set_role(db_session, assignee.id, UserRole.AGENT, team.id)
    ticket = scheduled(db_session, team, today(), assignee=assignee.id)
    other = scheduled(db_session, team, today() + timedelta(days=2))
    morning = morning_of(today(), "UTC", 7)

    assert wake_scheduled_tickets(db_session, morning - timedelta(minutes=1)) == 0  # not yet
    assert wake_scheduled_tickets(db_session, morning) == 1
    assert wake_scheduled_tickets(db_session, morning + timedelta(hours=1)) == 0  # nothing the second time

    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN
    assert db_session.get(Ticket, other.id).status == TicketStatus.SCHEDULED
    [change] = [e for e in events(db_session, ticket, "status_changed")]
    assert (change.from_value, change.to_value, change.actor_email, change.detail) == ("scheduled", "open", None, "due today")
    [entry] = db_session.exec(select(AuditLog).where(AuditLog.action == "due_today")).all()
    assert entry.actor_email == "system" and entry.entity_id == ticket.id
    [bell] = db_session.exec(select(Notification).where(Notification.ticket_id == ticket.id)).all()
    assert bell.user_id == assignee.id and bell.kind == "due_today" and bell.text.startswith(f"{ticket.reference} is due today")


def test_the_morning_is_in_the_requesters_time_zone(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = scheduled(db_session, team, today() + timedelta(days=1), zone="Pacific/Auckland", requester_email="kiwi@example.com")
    auckland_morning = morning_of(today() + timedelta(days=1), "Pacific/Auckland", 7)

    assert wake_scheduled_tickets(db_session, auckland_morning - timedelta(minutes=1)) == 0
    assert wake_scheduled_tickets(db_session, auckland_morning) == 1
    db_session.expire_all()
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN


def test_a_late_run_still_catches_an_overdue_ticket_and_the_hour_is_a_setting(db_session):
    team = create_team(db_session, "Service Desk")
    ticket = scheduled(db_session, team, today() - timedelta(days=3))
    set_setting(db_session, SCHEDULED_MORNING_HOUR, "9")
    assert REGISTRY[SCHEDULED_MORNING_HOUR].default == "7"

    assert wake_scheduled_tickets(db_session, datetime.now(timezone.utc)) == 1
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.OPEN
    assert morning_of(today(), "UTC", 9) - morning_of(today(), "UTC", 7) == timedelta(hours=2)


def test_the_job_only_touches_scheduled_tickets_and_leaves_unassigned_ones_unannounced(db_session):
    team = create_team(db_session, "Service Desk")
    in_progress = worked(db_session, team, status=TicketStatus.IN_PROGRESS, due_on=today())
    unassigned = scheduled(db_session, team, today())

    assert wake_scheduled_tickets(db_session, morning_of(today(), "UTC", 7)) == 1

    db_session.expire_all()
    assert db_session.get(Ticket, in_progress.id).status == TicketStatus.IN_PROGRESS
    assert db_session.get(Ticket, unassigned.id).status == TicketStatus.OPEN
    assert db_session.exec(select(Notification)).all() == []


def test_a_ticket_another_worker_holds_is_skipped(db_session):
    from sqlmodel import Session

    from app.database import engine

    team = create_team(db_session, "Service Desk")
    ticket = scheduled(db_session, team, today())
    with Session(engine) as other:
        other.exec(select(Ticket).where(Ticket.id == ticket.id).with_for_update()).all()  # held by someone else
        assert wake_scheduled_tickets(db_session, morning_of(today(), "UTC", 7)) == 0
        other.rollback()
    assert wake_scheduled_tickets(db_session, morning_of(today(), "UTC", 7)) == 1


def test_the_job_is_registered_as_a_recurring_job():
    from app.scheduled_jobs import SCHEDULED_JOBS

    assert "scheduled-due" in [job.name for job in SCHEDULED_JOBS]


# ---- Required by in the portal and the queue ---------------------------------------------------------


def test_the_requester_changes_required_by_after_triage_and_due_follows(client, db_session):
    request_type = offering(db_session)
    login_as(client, "jo@example.com", "Jo")
    raise_it(client, request_type_id=request_type.id, required_by=day(10))
    ticket = last_ticket(db_session)
    ticket.triaged_at, ticket.status = datetime.now(timezone.utc), TicketStatus.IN_PROGRESS
    db_session.add(ticket)
    db_session.commit()

    assert client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(20)}, follow_redirects=False).status_code == 303

    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=20) and ticket.due_on == today() + timedelta(days=19)


def queue_of(client, names):
    page = client.get("/tickets").text
    return [name for _, name in sorted((page.index(n), n) for n in names)]


def test_the_queue_lead_time_comes_from_the_request_type_else_the_setting(client, db_session):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])
    long_lead = offering(db_session, "Long", queue_lead_days=6)
    now = datetime.now(timezone.utc)

    def add(subject, request_type, due_in_days):
        ticket = worked(
            db_session, team, request_type, subject=subject, category_id=category.id, first_responded_at=now,
            due_on=today() + timedelta(days=due_in_days),
        )
        ticket.resolution_due_at = end_of_day_utc(ticket.due_on, "UTC")
        db_session.add(ticket)
        db_session.commit()

    add("A-plain", None, 4)  # a ticket with no request type: the setting
    add("B-type", long_lead, 7)  # 8 days out, less its own 6 days: ahead of A
    set_setting(db_session, REQUIRED_BY_LEAD_DAYS, "1")
    db_session.commit()
    assert queue_of(client, ("A-plain", "B-type")) == ["B-type", "A-plain"]

    long_lead.queue_lead_days = None  # blank: the setting (1 day) applies
    db_session.add(long_lead)
    db_session.commit()
    assert queue_of(client, ("A-plain", "B-type")) == ["A-plain", "B-type"]


def test_the_admin_sets_the_two_numbers_per_type_and_blank_means_the_setting(client, db_session):
    request_type = make(db_session, "Laptop")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    base = {"name": "Laptop", "description": "", "approval_group_id": request_type.approval_group_id, "pass_percent": 51, "is_active": "on"}

    client.post(f"/admin/request-types/{request_type.id}", data={**base, "due_days_before": "3", "queue_lead_days": "0"})
    db_session.expire_all()
    saved = db_session.get(RequestType, request_type.id)
    assert (saved.due_days_before, saved.queue_lead_days) == (3, 0)
    assert any("Due days before Required by None -> 3" in (d or "") for d in audit(db_session, "edited"))

    bad = client.post(f"/admin/request-types/{request_type.id}", data={**base, "due_days_before": "999"})
    assert bad.status_code == 400
    client.post(f"/admin/request-types/{request_type.id}", data={**base, "due_days_before": "", "queue_lead_days": ""})
    db_session.expire_all()
    saved = db_session.get(RequestType, request_type.id)
    assert (saved.due_days_before, saved.queue_lead_days) == (None, None)


def test_the_morning_hour_setting_takes_0_to_23(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)

    assert client.post("/admin/settings", data={"key": SCHEDULED_MORNING_HOUR, "value": "24"}).status_code == 400
    assert client.post("/admin/settings", data={"key": SCHEDULED_MORNING_HOUR, "value": "abc"}).status_code == 400
    assert client.post("/admin/settings", data={"key": SCHEDULED_MORNING_HOUR, "value": "0"}, follow_redirects=False).status_code in (200, 303)


# ---- The triage form ---------------------------------------------------------------------------------


def untriaged(db_session, request_type=None):
    team = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, team)
    ticket.request_type_id = request_type.id if request_type else None
    db_session.add(ticket)
    db_session.commit()
    return team, ticket


def test_the_triage_form_shows_required_by_only_for_a_kind_that_offers_it_and_due_for_any(client, db_session):
    plain = offering(db_session, "Plain")
    plain.offers_required_by = False
    db_session.add(plain)
    db_session.commit()
    _, ticket = untriaged(db_session, plain)
    agent(client, db_session)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'name="due_on"' in page and 'name="required_by"' not in page and 'hx-get="/tickets/triage-dates"' in page

    offers = offering(db_session, "Room")
    swapped = client.get("/tickets/triage-dates", params={"request_type_id": offers.id, "due_on": day(3)})
    assert 'name="required_by"' in swapped.text and f'value="{day(3)}"' in swapped.text
    none = client.get("/tickets/triage-dates", params={"request_type_id": plain.id})
    assert 'name="required_by"' not in none.text and 'name="due_on"' in none.text


def test_triaging_with_the_dates_sets_them_and_blank_dates_are_fine(client, db_session):
    request_type = offering(db_session)
    team, ticket = untriaged(db_session)
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id,
            "request_type_id": request_type.id, "required_by": day(10), "due_on": "",
        },
    )

    assert response.status_code == 200, response.text
    db_session.refresh(ticket)
    assert ticket.request_type_id == request_type.id and ticket.triaged_at is not None
    assert ticket.required_by == today() + timedelta(days=10) and ticket.due_on == today() + timedelta(days=9)
    assert [e.detail for e in events(db_session, ticket, "request_type")] == ["chosen at triage"]


def test_triaging_with_a_due_date_and_no_required_by(client, db_session):
    team, ticket = untriaged(db_session)
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id, "due_on": day(5)},
    )

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=5) and ticket.required_by is None
    assert ticket.resolution_due_at == end_of_day_utc(ticket.due_on, "UTC")


def test_an_emailed_request_with_no_date_triages_and_gets_its_date_later(client, db_session):
    request_type = offering(db_session)
    team, ticket = untriaged(db_session)
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])
    triage = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id,
            "request_type_id": request_type.id, "required_by": "", "due_on": "",
        },
    )
    assert triage.status_code == 200
    db_session.refresh(ticket)
    assert ticket.required_by is None and ticket.due_on is None

    assert client.post(f"/tickets/{ticket.number}/required-by", data={"required_by": day(8)}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=7)


def test_triage_refuses_a_due_date_after_required_by_and_changes_nothing(client, db_session):
    request_type = offering(db_session)
    team, ticket = untriaged(db_session)
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id,
            "request_type_id": request_type.id, "required_by": day(5), "due_on": day(6),
        },
    )

    assert response.status_code == 400 and "after the Required by" in response.text
    db_session.refresh(ticket)
    assert ticket.triaged_at is None and ticket.required_by is None  # the whole triage was rolled back


def test_a_kind_of_request_cannot_be_chosen_for_an_incident(client, db_session):
    request_type = offering(db_session)
    team, ticket = untriaged(db_session)
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id, "request_type_id": request_type.id},
    )

    assert response.status_code == 400

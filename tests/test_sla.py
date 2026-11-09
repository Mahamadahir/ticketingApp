from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import sla
from app.models import AuditLog, Ticket, TicketEvent, TicketStatus, UserRole
from app.settings_store import REGISTRY, SLA_AT_RISK_PERCENT, set_setting, sla_key
from app.sla import Level, Priority, priority_for, sla_view
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_triage import agent, events, new_ticket, triage

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def triaged_ticket(client, db_session, impact="high", urgency="high"):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    ticket = new_ticket(db_session, team)
    agent(client, db_session, member_of=[team])
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": impact, "urgency": urgency, "category_id": category.id},
    )
    assert response.status_code == 200
    db_session.refresh(ticket)
    return ticket, team


# ---- The priority grid ------------------------------------------------------------


@pytest.mark.parametrize(
    "impact,urgency,expected",
    [
        ("high", "high", "p1"),
        ("high", "medium", "p2"), ("medium", "high", "p2"),
        ("high", "low", "p3"), ("medium", "medium", "p3"), ("low", "high", "p3"),
        ("medium", "low", "p4"), ("low", "medium", "p4"), ("low", "low", "p4"),
    ],
)
def test_priority_comes_from_impact_and_urgency(impact, urgency, expected):
    assert priority_for(Level(impact), Level(urgency)).value == expected


# ---- Triage sets it -----------------------------------------------------------------


def test_triage_sets_priority_and_starts_the_deadlines_from_the_settings(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")

    assert (ticket.impact, ticket.urgency, ticket.priority) == ("high", "high", "p1")
    created = ticket.created_at if ticket.created_at.tzinfo else ticket.created_at.replace(tzinfo=timezone.utc)
    # The placeholder P1 targets: 15 minutes to respond, 240 to resolve.
    assert ticket.first_response_due_at == created + timedelta(minutes=15)
    assert ticket.resolution_due_at == created + timedelta(minutes=240)
    [event] = events(db_session, ticket, "triaged")
    assert "P1" in event.detail
    assert events(db_session, ticket, "priority_changed") == []  # the first priority rides on the triage event


def test_triage_needs_impact_and_urgency_and_refuses_made_up_levels(client, db_session):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    ticket = new_ticket(db_session, team)
    agent(client, db_session, member_of=[team])
    base = {"ticket_type": "incident", "category_id": category.id}

    assert client.post(f"/tickets/{ticket.number}/triage", data={**base, "urgency": "low"}).status_code == 422
    assert client.post(f"/tickets/{ticket.number}/triage", data={**base, "impact": "low"}).status_code == 422
    assert client.post(
        f"/tickets/{ticket.number}/triage", data={**base, "impact": "catastrophic", "urgency": "low"}
    ).status_code == 422
    db_session.refresh(ticket)
    assert ticket.triaged_at is None and ticket.priority is None


def test_a_blank_target_means_that_deadline_is_not_tracked(client, db_session):
    set_setting(db_session, sla_key("p1", "first_response"), "")
    db_session.commit()

    ticket, _ = triaged_ticket(client, db_session, "high", "high")

    assert ticket.first_response_due_at is None and ticket.resolution_due_at is not None


def test_the_targets_are_settings_not_constants(client, db_session):
    set_setting(db_session, sla_key("p4", "resolution"), "90")
    db_session.commit()

    ticket, _ = triaged_ticket(client, db_session, "low", "low")

    created = ticket.created_at if ticket.created_at.tzinfo else ticket.created_at.replace(tzinfo=timezone.utc)
    assert ticket.resolution_due_at == created + timedelta(minutes=90)


# ---- Changing the priority --------------------------------------------------------------


def test_changing_priority_moves_the_deadlines_and_records_who_and_what(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "low", "low")
    old_due = ticket.resolution_due_at

    response = client.post(f"/tickets/{ticket.number}/priority", data={"impact": "high", "urgency": "high"})

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.priority == "p1" and ticket.resolution_due_at < old_due
    [event] = events(db_session, ticket, "priority_changed")
    assert event.actor_email == "agent@example.com"
    assert event.from_value.startswith("P4") and event.to_value.startswith("P1")
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "priority_changed")).all()
    assert len(audit) == 1 and "P1" in audit[0].detail


def test_setting_the_same_priority_again_changes_and_records_nothing(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "low", "low")

    client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low"})

    assert events(db_session, ticket, "priority_changed") == []


def test_priority_cannot_be_set_on_an_untriaged_ticket_or_by_a_viewer(client, db_session):
    desk, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    fresh = new_ticket(db_session, desk)
    agent_id = agent(client, db_session, member_of=[desk])
    add_membership(db_session, agent_id, other.id, "viewer")

    assert client.post(f"/tickets/{fresh.number}/priority", data={"impact": "low", "urgency": "low"}).status_code == 400

    category = create_category(db_session, other, "Hardware")
    watched = new_ticket(db_session, other)
    # Triaged by someone else, in a team where this person is only a viewer.
    watched.triaged_at, watched.status, watched.category_id = NOW, TicketStatus.OPEN, category.id
    db_session.add(watched)
    db_session.commit()
    denied = client.post(f"/tickets/{watched.number}/priority", data={"impact": "high", "urgency": "high"})
    assert denied.status_code == 403
    db_session.refresh(watched)
    assert watched.priority is None


# ---- The clock ---------------------------------------------------------------------------


def clocked(priority="p3"):
    """A ticket created an hour before NOW with fixed targets, no database needed."""
    ticket = Ticket(subject="x", requester_email="a@example.com", status=TicketStatus.OPEN)
    ticket.created_at = NOW - timedelta(hours=1)
    ticket.priority = priority
    ticket.first_response_due_at = ticket.created_at + timedelta(hours=4)
    ticket.resolution_due_at = ticket.created_at + timedelta(hours=10)
    return ticket


def test_the_view_counts_down_to_the_first_response_then_to_resolution():
    ticket = clocked()
    view = sla_view(ticket, NOW, 25)
    assert (view.state, view.which) == ("ok", "first response") and "3h 0m" in view.text

    ticket.first_responded_at = NOW
    view = sla_view(ticket, NOW, 25)
    assert (view.state, view.which) == ("ok", "resolution") and "9h 0m" in view.text


def test_amber_when_the_threshold_share_of_time_is_left_and_red_once_overdue():
    ticket = clocked()
    ticket.first_responded_at = NOW - timedelta(minutes=30)  # watching the 10-hour resolution
    # 10 hours total; 25% is 2.5 hours. 2 hours left is at risk.
    assert sla_view(ticket, ticket.created_at + timedelta(hours=8), 25).state == "at_risk"
    assert sla_view(ticket, ticket.created_at + timedelta(hours=7), 25).state == "ok"
    late = sla_view(ticket, ticket.created_at + timedelta(hours=11), 25)
    assert late.state == "breached" and "overdue 1h 0m" in late.text


def test_awaiting_user_stops_the_clock_and_resuming_pushes_the_deadlines_out(db_session):
    ticket = clocked()
    ticket.priority = "p3"
    started = ticket.resolution_due_at

    sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, NOW)
    assert ticket.sla_paused_at == NOW
    assert sla_view(ticket, NOW + timedelta(days=30), 25).state == "paused"  # however long it waits

    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, TicketStatus.IN_PROGRESS, NOW + timedelta(hours=5))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 5 * 3600
    # Recomputed from created_at + the P3 target + the 5 hours spent waiting.
    assert ticket.resolution_due_at - ticket.created_at == timedelta(minutes=4320) + timedelta(hours=5)
    assert ticket.resolution_due_at > started


def test_two_pauses_add_up(db_session):
    ticket = clocked()
    for start in (NOW, NOW + timedelta(days=1)):
        sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, start)
        sla.on_status_change(
            db_session, ticket, TicketStatus.AWAITING_USER, TicketStatus.OPEN, start + timedelta(hours=2)
        )
    assert ticket.sla_paused_seconds == 4 * 3600


def test_resolving_while_awaiting_user_closes_the_pause_too(db_session):
    ticket = clocked()
    sla.on_status_change(db_session, ticket, TicketStatus.OPEN, TicketStatus.AWAITING_USER, NOW)
    sla.on_status_change(db_session, ticket, TicketStatus.AWAITING_USER, TicketStatus.RESOLVED, NOW + timedelta(hours=1))
    assert ticket.sla_paused_at is None and ticket.sla_paused_seconds == 3600


def test_a_finished_ticket_shows_whether_it_met_the_target():
    ticket = clocked()
    ticket.status = TicketStatus.RESOLVED
    ticket.resolved_at = ticket.resolution_due_at - timedelta(minutes=1)
    assert sla_view(ticket, NOW, 25).state == "met"
    ticket.resolved_at = ticket.resolution_due_at + timedelta(minutes=1)
    assert sla_view(ticket, NOW, 25).state == "breached"


def test_a_ticket_with_no_priority_has_no_sla():
    ticket = Ticket(subject="x", requester_email="a@example.com")
    assert sla_view(ticket, NOW, 25).state == "none"


def test_emailing_the_user_pauses_the_clock_and_their_reply_restarts_it(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")
    due_before = ticket.resolution_due_at

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Have you tried turning it off?"}).status_code == 200
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_USER
    assert ticket.sla_paused_at is not None and ticket.first_responded_at is not None

    ticket.sla_paused_at -= timedelta(hours=2)  # as if it had waited two hours
    db_session.add(ticket)
    db_session.commit()
    client.post("/auth/logout")
    login_as(client, ticket.requester_email)
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Yes, no change."}).status_code in (200, 303)

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS and ticket.sla_paused_at is None
    assert ticket.sla_paused_seconds >= 2 * 3600
    assert ticket.resolution_due_at >= due_before + timedelta(hours=2)


def test_a_note_or_a_portal_comment_is_not_a_first_response(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "internal thought"})
    db_session.refresh(ticket)
    assert ticket.first_responded_at is None


# ---- The queue ------------------------------------------------------------------------------


def references_in_list(client):
    page = client.get("/tickets").text
    found = [(page.index(t), t) for t in ("A-untriaged", "B-soon", "C-later", "D-paused", "E-nodeadline", "F-done")]
    return [name for _, name in sorted(found)]


def test_the_queue_pins_untriaged_then_sorts_by_time_to_breach_with_finished_last(client, db_session):
    desk = create_team(db_session, "Service Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])

    def make(subject, **fields):
        ticket = new_ticket(db_session, desk)
        ticket.subject = subject
        for key, value in fields.items():
            setattr(ticket, key, value)
        db_session.add(ticket)
        db_session.commit()
        return ticket

    base = dict(triaged_at=NOW, category_id=category.id, status=TicketStatus.OPEN)
    make("F-done", **{**base, "status": TicketStatus.RESOLVED}, priority="p1", resolved_at=NOW,
         resolution_due_at=NOW - timedelta(days=1))
    make("E-nodeadline", **base)
    make("D-paused", **base, priority="p1", sla_paused_at=NOW, resolution_due_at=NOW - timedelta(days=1))
    make("C-later", **base, priority="p4", first_responded_at=NOW, resolution_due_at=NOW + timedelta(days=5))
    make("B-soon", **base, priority="p3", first_responded_at=NOW, resolution_due_at=NOW + timedelta(hours=1))
    make("A-untriaged")

    assert references_in_list(client) == ["A-untriaged", "B-soon", "C-later", "D-paused", "E-nodeadline", "F-done"]


def test_priority_breaks_a_tie_on_the_deadline(client, db_session):
    desk = create_team(db_session, "Service Desk")
    category = create_category(db_session, desk, "Hardware")
    agent(client, db_session, member_of=[desk])
    for subject, priority in (("B-soon", "p3"), ("C-later", "p1")):
        ticket = new_ticket(db_session, desk)
        ticket.subject, ticket.priority, ticket.triaged_at = subject, priority, NOW
        ticket.category_id, ticket.status = category.id, TicketStatus.OPEN
        ticket.first_responded_at, ticket.resolution_due_at = NOW, NOW + timedelta(hours=3)
        db_session.add(ticket)
    db_session.commit()

    page = client.get("/tickets").text
    assert page.index("C-later") < page.index("B-soon")  # the P1 first


def test_the_list_and_ticket_pages_show_priority_and_the_countdown(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")

    listing = client.get("/tickets").text
    detail = client.get(f"/tickets/{ticket.number}").text

    # The queue words the same clock for scanning ("42m to first response"); the ticket page keeps its own sentence.
    assert "P1" in listing and "to first response" in listing
    assert "Priority: P1" in detail and "SLA: first response due in" in detail


def test_a_ticket_triaged_before_priorities_existed_still_lists_and_opens(client, db_session):
    team = create_team(db_session, "Service Desk")
    old = new_ticket(db_session, team)
    old.triaged_at, old.status = NOW, TicketStatus.OPEN
    db_session.add(old)
    db_session.commit()
    agent(client, db_session, member_of=[team])

    assert client.get("/tickets").status_code == 200
    detail = client.get(f"/tickets/{old.number}")
    assert detail.status_code == 200 and "Priority: Not set" in detail.text


def test_requesters_never_see_priority_or_sla(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")
    client.post("/auth/logout")
    login_as(client, ticket.requester_email)

    page = client.get(f"/portal/{ticket.number}").text

    assert "P1" not in page and "SLA" not in page and "first response" not in page


# ---- The settings -----------------------------------------------------------------------------


def super_admin(client, db_session):
    user_id = login_as(client, "boss@example.com")
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)


def test_every_priority_has_both_targets_as_placeholder_settings():
    for priority in ("p1", "p2", "p3", "p4"):
        for which in ("first_response", "resolution"):
            definition = REGISTRY[sla_key(priority, which)]
            assert definition.kind == "minutes" and definition.default.isdigit()
            assert "Placeholder" in definition.description


def test_target_settings_accept_whole_minutes_or_blank_and_nothing_else(client, db_session):
    super_admin(client, db_session)
    key = sla_key("p2", "resolution")

    def save(value):
        return client.post("/admin/settings", data={"key": key, "value": value}, follow_redirects=False).status_code

    assert save("120") == 303 and save("") == 303
    for bad in ("0", "-5", "1.5", "abc", "999999", "²"):
        assert save(bad) == 400, bad
    assert client.get("/admin/settings").status_code == 200


def test_the_at_risk_threshold_is_a_percentage_between_1_and_99(client, db_session):
    super_admin(client, db_session)

    def save(value):
        return client.post(
            "/admin/settings", data={"key": SLA_AT_RISK_PERCENT, "value": value}, follow_redirects=False
        ).status_code

    assert save("40") == 303
    for bad in ("", "0", "100", "x"):
        assert save(bad) == 400, bad
    assert sla.at_risk_percent(db_session) == 40


def test_agents_cannot_change_the_targets(client, db_session):
    user_id = login_as(client, "agent@example.com")
    set_role(db_session, user_id, UserRole.AGENT)
    response = client.post("/admin/settings", data={"key": sla_key("p1", "resolution"), "value": "1"})
    assert response.status_code == 403


def test_a_finished_ticket_keeps_its_sla_result_and_its_priority_cannot_be_changed(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "medium", "medium")
    ticket.status, ticket.resolved_at = TicketStatus.RESOLVED, ticket.resolution_due_at + timedelta(hours=1)
    db_session.add(ticket)
    db_session.commit()
    due = ticket.resolution_due_at

    response = client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low"})

    assert response.status_code == 400
    db_session.refresh(ticket)
    assert ticket.priority == "p3" and ticket.resolution_due_at == due
    assert "Set priority" not in client.get(f"/tickets/{ticket.number}").text


def test_a_finished_ticket_with_no_resolved_time_shows_no_countdown():
    ticket = clocked()
    ticket.status, ticket.resolved_at = TicketStatus.CLOSED, None
    assert sla_view(ticket, NOW + timedelta(days=9), 25).state == "none"


def test_pages_of_the_queue_never_repeat_or_skip_a_ticket(client, db_session):
    from app.pagination import PAGE_SIZE

    desk = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[desk])
    for number in range(PAGE_SIZE + 5):  # all untriaged, created in the same instant
        ticket = new_ticket(db_session, desk)
        ticket.created_at = ticket.updated_at = NOW
        ticket.subject = f"Same-{number:03d}-end"
        db_session.add(ticket)
    db_session.commit()

    import re
    seen = []
    for page in (1, 2):
        seen += re.findall(r"Same-\d{3}-end", client.get(f"/tickets?page={page}").text)
    assert len(seen) == len(set(seen)) == PAGE_SIZE + 5

def test_changing_a_target_leaves_existing_tickets_alone_but_applies_to_new_ones(client, db_session):
    ticket, team = triaged_ticket(client, db_session, "high", "high")
    before = (ticket.first_response_due_at, ticket.resolution_due_at)

    set_setting(db_session, sla_key("p1", "first_response"), "999")
    set_setting(db_session, sla_key("p1", "resolution"), "9999")
    db_session.commit()
    client.post(f"/tickets/{ticket.number}/note", data={"body": "any activity"})
    db_session.refresh(ticket)
    assert (ticket.first_response_due_at, ticket.resolution_due_at) == before

    newer = new_ticket(db_session, team)
    response = client.post(
        f"/tickets/{newer.number}/triage",
        data={"ticket_type": "incident", "impact": "high", "urgency": "high", "category_id": ticket.category_id},
    )
    assert response.status_code == 200
    db_session.refresh(newer)
    created = newer.created_at if newer.created_at.tzinfo else newer.created_at.replace(tzinfo=timezone.utc)
    assert newer.first_response_due_at == created + timedelta(minutes=999)
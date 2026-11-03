from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import sla
from app.backdating import BackdateError, backdate_ticket, parse_moment
from app.models import AuditLog, Ticket, TicketSource, TicketStatus
from tests.conftest import add_membership, create_category, create_team
from tests.test_sla import triaged_ticket as _triaged_ticket
from tests.test_triage import agent, events, new_ticket


@pytest.fixture(autouse=True)
def utc_default_zone(db_session):
    """These tests type times as UTC. The default zone is London, an hour out in summer, so make the
    default UTC here (a person's own zone is tested in test_time_zones.py)."""
    from app.settings_store import DEFAULT_TIME_ZONE, set_setting

    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()

FORMAT = "%Y-%m-%dT%H:%M"


def triaged_ticket(client, db_session, impact="high", urgency="high", source=TicketSource.AGENT):
    """Backdating is for tickets an agent raised on someone's behalf, so by default the ticket is
    raised that way, through the real raise form. The other sources have no creation path yet
    (email and Teams arrive with their intake), so those are set by hand to show they are refused."""
    if source == TicketSource.AGENT:
        team = create_team(db_session, "Service Desk")
        category = create_category(db_session, team, "Hardware")
        agent(client, db_session, member_of=[team])
        response = client.post(
            "/portal/new",
            headers={"HX-Request": "true"},
            data={
                "subject": "Printer on fire", "description": "They came to the desk",
                "for_email": "walkin@example.com", "ticket_type": "incident",
                "category_id": category.id, "impact": impact, "urgency": urgency,
            },
        )
        assert response.status_code == 204, response.text
        ticket = db_session.exec(select(Ticket).where(Ticket.requester_email == "walkin@example.com")).one()
        assert ticket.source == TicketSource.AGENT
        return ticket, team
    ticket, team = _triaged_ticket(client, db_session, impact, urgency)
    ticket.source = source
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket, team


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(second=0, microsecond=0)


def post(client, ticket, **fields):
    return client.post(f"/tickets/{ticket.number}/backdate", data=fields)


def resolve(client, ticket):
    assert client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Fixed it"}).status_code == 200


# ---- The dates ------------------------------------------------------------------------------


def test_the_reported_time_can_be_set_up_to_30_days_back(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    when = ago(days=3, hours=2)

    assert post(client, ticket, reported_at=when.strftime(FORMAT)).status_code == 200

    db_session.refresh(ticket)
    assert ticket.reported_at == when and ticket.is_backdated
    assert ticket.created_at > when  # the real entry time is untouched


def test_reported_and_resolved_are_both_set_for_a_ticket_logged_after_the_fix(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    resolve(client, ticket)
    reported, resolved = ago(days=5), ago(days=4)

    response = post(client, ticket, reported_at=reported.strftime(FORMAT), resolved_at=resolved.strftime(FORMAT))

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert (ticket.reported_at, ticket.resolved_at) == (reported, resolved)


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"reported_at": "{future}"}, "future"),
        ({"reported_at": "{too_old}"}, "30 days"),
        ({"reported_at": "yesterday"}, "YYYY-MM-DD"),
        ({"reported_at": "2026-13-45T99:99"}, "YYYY-MM-DD"),
    ],
)
def test_impossible_reported_times_are_refused_with_a_reason_and_change_nothing(client, db_session, fields, message):
    ticket, _ = triaged_ticket(client, db_session)
    before = ticket.reported_at
    values = {
        "future": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(FORMAT),
        "too_old": ago(days=31).strftime(FORMAT),
    }
    fields = {k: v.format(**values) for k, v in fields.items()}

    response = post(client, ticket, **fields)

    assert response.status_code == 400 and message in response.text
    db_session.refresh(ticket)
    assert ticket.reported_at == before and not ticket.is_backdated
    assert events(db_session, ticket, "backdated") == []


def test_the_resolved_time_has_its_own_rules(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)

    # A ticket that is not finished has no resolved time.
    assert post(client, ticket, resolved_at=ago(days=1).strftime(FORMAT)).status_code == 400
    resolve(client, ticket)
    db_session.refresh(ticket)
    assert post(client, ticket, resolved_at=ago(days=31).strftime(FORMAT)).status_code == 400
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(FORMAT)
    assert post(client, ticket, resolved_at=future).status_code == 400
    # Never before it was reported.
    assert post(client, ticket, reported_at=ago(days=2).strftime(FORMAT), resolved_at=ago(days=3).strftime(FORMAT)).status_code == 400
    db_session.refresh(ticket)
    assert not ticket.is_backdated


def test_a_resolved_time_before_the_existing_reported_time_is_refused(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    resolve(client, ticket)
    db_session.refresh(ticket)
    # Reported just now, so any earlier resolved time is before the report.
    assert post(client, ticket, resolved_at=ago(days=1).strftime(FORMAT)).status_code == 400


def test_blank_boxes_leave_the_dates_alone_and_the_same_dates_again_do_nothing(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)

    assert post(client, ticket, reported_at="", resolved_at="").status_code == 200
    db_session.refresh(ticket)
    assert not ticket.is_backdated and events(db_session, ticket, "backdated") == []

    when = ago(days=2).strftime(FORMAT)
    post(client, ticket, reported_at=when)
    post(client, ticket, reported_at=when)
    assert len(events(db_session, ticket, "backdated")) == 1


# ---- The record ------------------------------------------------------------------------------


def test_backdating_leaves_an_event_and_an_audit_entry_saying_who_and_what(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    when = ago(days=2)

    post(client, ticket, reported_at=when.strftime(FORMAT))

    [event] = events(db_session, ticket, "backdated")
    assert (event.actor_email, event.detail, event.to_value) == ("agent@example.com", "reported", when.strftime(FORMAT))
    assert event.from_value is not None
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "backdated")).all()
    assert len(audit) == 1 and when.strftime("%Y-%m-%d %H:%M") in audit[0].detail


def test_the_ticket_page_shows_the_dates_and_that_it_was_backdated(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    plain = client.get(f"/tickets/{ticket.number}").text
    assert "Reported:" in plain and "backdated</span>" not in plain

    post(client, ticket, reported_at=ago(days=2).strftime(FORMAT))
    page = client.get(f"/tickets/{ticket.number}").text

    assert "backdated</span>" in page and "Backdate (log after the fact)" in page


# ---- SLA ---------------------------------------------------------------------------------------


def test_a_backdated_ticket_has_no_deadlines_and_no_sla_and_stays_that_way(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")
    assert ticket.resolution_due_at is not None

    post(client, ticket, reported_at=ago(days=2).strftime(FORMAT))

    db_session.refresh(ticket)
    assert ticket.first_response_due_at is None and ticket.resolution_due_at is None
    assert sla.sla_view(ticket, datetime.now(timezone.utc), 25).state == "none"
    # Changing the priority later does not bring the clock back.
    client.post(f"/tickets/{ticket.number}/priority", data={"impact": "low", "urgency": "low"})
    db_session.refresh(ticket)
    assert ticket.priority == "p4" and ticket.resolution_due_at is None
    assert "SLA:" not in client.get(f"/tickets/{ticket.number}").text


def test_a_ticket_that_was_never_backdated_keeps_its_sla(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")
    assert post(client, ticket, reported_at="").status_code == 200
    db_session.refresh(ticket)
    assert ticket.resolution_due_at is not None and not ticket.is_backdated


# ---- Who may ----------------------------------------------------------------------------------


def test_a_viewer_cannot_backdate_and_the_form_is_not_offered(client, db_session):
    desk, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    category = create_category(db_session, other, "Hardware")
    watched = new_ticket(db_session, other)
    watched.triaged_at, watched.status, watched.category_id = datetime.now(timezone.utc), TicketStatus.OPEN, category.id
    watched.source = TicketSource.AGENT  # so only the viewer rule can be what refuses it
    db_session.add(watched)
    db_session.commit()
    me = agent(client, db_session, member_of=[desk])
    add_membership(db_session, me, other.id, "viewer")

    assert post(client, watched, reported_at=ago(days=2).strftime(FORMAT)).status_code == 403
    assert "Backdate (log after the fact)" not in client.get(f"/tickets/{watched.number}").text
    db_session.refresh(watched)
    assert not watched.is_backdated


@pytest.mark.parametrize("source", [TicketSource.EMAIL, TicketSource.PORTAL, TicketSource.TEAMS])
def test_a_ticket_the_requester_opened_themselves_cannot_be_backdated(client, db_session, source):
    ticket, _ = triaged_ticket(client, db_session, source=source)
    reported = ticket.reported_at

    response = post(client, ticket, reported_at=ago(days=2).strftime(FORMAT))

    assert response.status_code == 400 and "on someone's behalf" in response.text
    db_session.refresh(ticket)
    assert not ticket.is_backdated and ticket.reported_at == reported
    assert not events(db_session, ticket, "backdated")
    assert "Backdate (log after the fact)" not in client.get(f"/tickets/{ticket.number}").text


def test_the_form_is_offered_on_a_ticket_an_agent_raised_for_someone(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)

    assert "Backdate (log after the fact)" in client.get(f"/tickets/{ticket.number}").text


def test_the_dates_parse_only_in_the_expected_form():
    assert parse_moment("2026-10-01T14:30") == datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    assert parse_moment("  ") is None and parse_moment(None) is None
    assert parse_moment("2026-10-01T14:30:45") == datetime(2026, 10, 1, 14, 30, 45, tzinfo=timezone.utc)  # seconds are tolerated
    for bad in ("2026-10-01", "2026-10-01 14:30", "01/10/2026", "2026-10-01T14:30:00:00", "x"):
        with pytest.raises(BackdateError):
            parse_moment(bad)


def test_backdating_refuses_a_moment_exactly_in_the_future_and_accepts_now(db_session):
    from app.models import Ticket

    ticket = Ticket(subject="x", requester_email="a@example.com", source=TicketSource.AGENT)
    db_session.add(ticket)
    db_session.commit()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    assert backdate_ticket(db_session, ticket, "a@x", reported_at=now - timedelta(days=30), now=now)
    with pytest.raises(BackdateError):
        backdate_ticket(db_session, ticket, "a@x", reported_at=now + timedelta(minutes=1), now=now)
    with pytest.raises(BackdateError):
        backdate_ticket(db_session, ticket, "a@x", reported_at=now - timedelta(days=30, minutes=1), now=now)


# ---- Rules that another rule would otherwise hide ----------------------------------------------------


def test_an_unfinished_ticket_has_no_resolved_time_even_when_the_dates_are_otherwise_fine(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    ticket.reported_at = ago(days=3)  # so "before it was reported" cannot be what refuses this
    db_session.add(ticket)
    db_session.commit()

    response = post(client, ticket, resolved_at=ago(days=1).strftime(FORMAT))

    assert response.status_code == 400 and "resolved or closed" in response.text


def test_the_resolved_time_is_also_limited_to_30_days_back_for_an_old_ticket(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    resolve(client, ticket)
    db_session.refresh(ticket)
    ticket.reported_at = ago(days=60)  # an old ticket, so "before the report" does not apply
    db_session.add(ticket)
    db_session.commit()

    refused = post(client, ticket, resolved_at=ago(days=40).strftime(FORMAT))
    allowed = post(client, ticket, resolved_at=ago(days=20).strftime(FORMAT))

    assert refused.status_code == 400 and "30 days" in refused.text
    assert allowed.status_code == 200


def test_the_sla_view_ignores_a_backdated_ticket_even_if_deadlines_are_still_stored():
    from app.models import Ticket

    ticket = Ticket(subject="x", requester_email="a@example.com", status=TicketStatus.OPEN)
    ticket.priority, ticket.is_backdated = "p1", True
    ticket.first_response_due_at = ticket.resolution_due_at = datetime.now(timezone.utc) - timedelta(days=1)

    assert sla.sla_view(ticket, datetime.now(timezone.utc), 25).state == "none"

# ---- Review follow-ups ---------------------------------------------------------------------------------


def test_saving_the_prefilled_form_untouched_changes_nothing_even_with_seconds_stored(client, db_session):
    ticket, _ = triaged_ticket(client, db_session, "high", "high")
    resolve(client, ticket)
    db_session.refresh(ticket)
    assert ticket.reported_at.second or ticket.reported_at.microsecond  # stored with seconds, the form shows minutes
    due = ticket.resolution_due_at

    page = client.get(f"/tickets/{ticket.number}").text
    reported = ticket.reported_at.strftime(FORMAT)
    resolved = ticket.resolved_at.strftime(FORMAT)
    assert f'value="{reported}"' in page and f'value="{resolved}"' in page

    response = post(client, ticket, reported_at=reported, resolved_at=resolved)

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert not ticket.is_backdated and ticket.resolution_due_at == due
    assert events(db_session, ticket, "backdated") == []
    assert not db_session.exec(select(AuditLog).where(AuditLog.action == "backdated")).all()


def test_only_the_resolved_time_can_be_edited_on_a_ticket_reported_over_30_days_ago(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    resolve(client, ticket)
    db_session.refresh(ticket)
    ticket.reported_at = ago(days=45).replace(minute=0)
    db_session.add(ticket)
    db_session.commit()

    # The form sends the unchanged old reported time along with the new resolved time.
    response = post(
        client, ticket, reported_at=ticket.reported_at.strftime(FORMAT), resolved_at=ago(days=10).strftime(FORMAT)
    )

    assert response.status_code == 200
    db_session.refresh(ticket)
    assert ticket.resolved_at == ago(days=10) or abs((ticket.resolved_at - ago(days=10)).total_seconds()) < 120
    assert [e.detail for e in events(db_session, ticket, "backdated")] == ["resolved"]  # reported was not touched


def test_the_audit_entry_records_from_and_to(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    before = ticket.reported_at.strftime("%Y-%m-%d %H:%M")
    when = ago(days=2)

    post(client, ticket, reported_at=when.strftime(FORMAT))

    [entry] = db_session.exec(select(AuditLog).where(AuditLog.action == "backdated")).all()
    assert entry.detail == f"reported {before} -> {when:%Y-%m-%d %H:%M} UTC"


def test_a_change_to_one_date_does_not_log_the_other(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    resolve(client, ticket)
    db_session.refresh(ticket)

    post(client, ticket, reported_at=ago(days=2).strftime(FORMAT), resolved_at=ticket.resolved_at.strftime(FORMAT))

    assert [e.detail for e in events(db_session, ticket, "backdated")] == ["reported"]
    [entry] = db_session.exec(select(AuditLog).where(AuditLog.action == "backdated")).all()
    assert "resolved" not in entry.detail
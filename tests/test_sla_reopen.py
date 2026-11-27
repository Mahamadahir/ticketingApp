"""#355: resolving counts as the first response when there was none; a reopen restarts the resolution target from the
reopen time (also for a ticket whose Due date was its target), keeping the first resolution on record; the reports count
a reopened ticket once, for that first resolution."""

from datetime import date, datetime, timedelta, timezone

from app import sla
from app.models import TicketSource, TicketStatus
from app.reopening import reclose_ticket, reopen_ticket
from app.resolution import resolve_ticket
from app.sla import Level
from app.ticket_service import create_ticket, set_status
from tests.test_reports import at, make_ticket, notes, run, table, world  # noqa: F401  (world is a fixture)

P1_RESOLUTION = timedelta(minutes=240)  # the placeholder P1 targets (app/settings_store.py)
P1_FIRST_RESPONSE = timedelta(minutes=15)


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def triaged(db_session, impact=Level.HIGH, urgency=Level.HIGH, requester="jo@example.com"):
    ticket = create_ticket(db_session, subject="Forward my mail", requester_email=requester, source=TicketSource.PORTAL)
    ticket.triaged_at = datetime.now(timezone.utc)
    ticket.status = TicketStatus.OPEN
    if impact is not None:
        sla.apply_impact_urgency(db_session, ticket, impact, urgency)
    db_session.add(ticket)
    db_session.commit()
    return ticket


def resolve(db_session, ticket):
    resolve_ticket(db_session, ticket, None, "Done", send_email=False)
    db_session.commit()


def reopen(db_session, ticket):
    reopen_ticket(db_session, ticket, "agent@example.com", "Still broken", tell_assignee=False)
    db_session.commit()


def rows(ticket, now=None):
    now = now or datetime.now(timezone.utc)
    return {row.label: row for row in sla.sla_rows(ticket, now, 25)}


# ---- Rule 1: resolving counts as the first response ---------------------------------------------------------------


def test_resolving_a_ticket_with_no_first_response_sets_it_to_the_resolution_time(db_session):
    ticket = triaged(db_session)
    assert ticket.first_responded_at is None
    resolve(db_session, ticket)
    assert ticket.first_responded_at is not None and ticket.first_responded_at == ticket.resolved_at
    assert rows(ticket)["First response"].text.startswith("Met")


def test_automation_resolving_through_set_status_counts_the_same(db_session):
    # The mailbox-change job, auto-resolve and bulk resolve all end in set_status: the rule is on the status change.
    ticket = triaged(db_session)
    set_status(db_session, ticket, TicketStatus.RESOLVED, "system", by_resolution=True)
    assert ticket.first_responded_at == ticket.resolved_at


def test_closing_an_open_ticket_straight_away_counts_too_and_an_earlier_response_is_kept(db_session):
    closed = triaged(db_session)
    closed.resolution_notes = "Duplicate"
    set_status(db_session, closed, TicketStatus.CLOSED, "agent@example.com")
    assert closed.first_responded_at == closed.closed_at

    answered = triaged(db_session)
    earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
    sla.note_first_response(answered, earlier)
    resolve(db_session, answered)
    assert answered.first_responded_at == earlier


def test_reopening_does_not_clear_the_first_response(db_session):
    ticket = triaged(db_session)
    resolve(db_session, ticket)
    responded = ticket.first_responded_at
    reopen(db_session, ticket)
    assert ticket.first_responded_at == responded


# ---- Rule 2: a reopen restarts the resolution target ----------------------------------------------------------------


def test_reopening_restarts_the_resolution_target_from_the_reopen_time(db_session):
    ticket = triaged(db_session)
    original_due = ticket.resolution_due_at
    # Resolved long after its deadline would have passed, then reopened: the deadline restarts from now.
    ticket.created_at -= timedelta(days=3)
    ticket.first_response_due_at -= timedelta(days=3)
    ticket.resolution_due_at -= timedelta(days=3)
    db_session.add(ticket)
    resolve(db_session, ticket)
    reopen(db_session, ticket)

    assert ticket.resolution_restarted_at is not None
    assert aware(ticket.resolution_due_at) == aware(ticket.resolution_restarted_at) + P1_RESOLUTION
    assert aware(ticket.resolution_due_at) > datetime.now(timezone.utc)
    assert ticket.resolution_due_at != original_due
    assert sla.sla_view(ticket, datetime.now(timezone.utc), 25).state in ("ok", "met")
    resolution = rows(ticket)["Resolution"]
    assert resolution.tone == "green" and "restarted when reopened on" in resolution.text


def test_the_first_resolution_stays_on_record_as_met_or_missed(db_session):
    ticket = triaged(db_session)
    due = ticket.resolution_due_at
    resolve(db_session, ticket)
    first_at = ticket.resolved_at
    reopen(db_session, ticket)
    assert ticket.first_resolved_at == first_at
    assert ticket.first_resolution_due_at == due
    assert ticket.first_resolved_at <= ticket.first_resolution_due_at  # met

    # A second round does not overwrite the first resolution.
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    assert ticket.first_resolved_at == first_at and ticket.first_resolution_due_at == due


def test_a_ticket_with_a_due_date_that_has_passed_is_not_breached_on_reopen(db_session):
    ticket = triaged(db_session)
    ticket.due_on = date.today() - timedelta(days=1)
    sla.recalculate_deadlines(db_session, ticket)
    assert aware(ticket.resolution_due_at) < datetime.now(timezone.utc)  # the Due date was the target
    resolve(db_session, ticket)
    reopen(db_session, ticket)

    assert ticket.due_on is not None  # the date stays on the ticket
    assert aware(ticket.resolution_due_at) == aware(ticket.resolution_restarted_at) + P1_RESOLUTION
    resolution = rows(ticket)["Resolution"]
    assert resolution.tone == "green" and "restarted when reopened on" in resolution.text
    view = sla.sla_view(ticket, datetime.now(timezone.utc), 25)
    assert view.which != "due" and view.state != "breached"


def test_a_reopened_ticket_with_a_due_date_and_no_priority_has_no_resolution_target(db_session):
    ticket = triaged(db_session, impact=None, urgency=None)
    ticket.due_on = date.today() + timedelta(days=2)
    sla.recalculate_deadlines(db_session, ticket)
    assert ticket.resolution_due_at is not None
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    assert ticket.resolution_due_at is None and ticket.due_on is not None


def test_awaiting_user_after_a_reopen_pauses_from_the_reopen_only(db_session):
    ticket = triaged(db_session)
    # An hour paused before the resolution does not count against the restarted target.
    set_status(db_session, ticket, TicketStatus.AWAITING_USER, "agent@example.com")
    ticket.sla_paused_at -= timedelta(hours=1)
    set_status(db_session, ticket, TicketStatus.OPEN, "agent@example.com")
    assert ticket.sla_paused_seconds >= 3600
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    restarted = aware(ticket.resolution_restarted_at)
    assert aware(ticket.resolution_due_at) == restarted + P1_RESOLUTION

    set_status(db_session, ticket, TicketStatus.AWAITING_USER, "agent@example.com")
    ticket.sla_paused_at -= timedelta(hours=2)
    set_status(db_session, ticket, TicketStatus.IN_PROGRESS, "agent@example.com")
    since_reopen = ticket.sla_paused_seconds - ticket.resolution_restart_paused_seconds
    assert 7200 <= since_reopen < 7210  # the two hours waited since the reopen, not the hour before it
    assert aware(ticket.resolution_due_at) == restarted + P1_RESOLUTION + timedelta(seconds=since_reopen)


def test_reclose_and_reopen_again_restarts_again(db_session):
    ticket = triaged(db_session)
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    first_restart = aware(ticket.resolution_restarted_at)
    first_resolved = ticket.first_resolved_at
    reclose_ticket(db_session, ticket, "agent@example.com", "said_thanks")
    db_session.commit()
    assert ticket.status == TicketStatus.CLOSED

    # A closed ticket is not reopened by hand, but a decline set back to an open status is a reopen of the same kind.
    set_status(db_session, ticket, TicketStatus.OPEN, "agent@example.com", by_approval=True)
    assert aware(ticket.resolution_restarted_at) >= first_restart
    assert aware(ticket.resolution_due_at) == aware(ticket.resolution_restarted_at) + P1_RESOLUTION
    assert ticket.first_resolved_at == first_resolved


def test_second_reopen_after_a_second_resolution_restarts_from_the_second_reopen(db_session):
    ticket = triaged(db_session)
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    ticket.resolution_restarted_at -= timedelta(hours=10)  # as if the first reopen was long ago
    sla.recalculate_deadlines(db_session, ticket)
    old_due = ticket.resolution_due_at
    resolve(db_session, ticket)
    reopen(db_session, ticket)
    assert aware(ticket.resolution_due_at) == aware(ticket.resolution_restarted_at) + P1_RESOLUTION
    assert ticket.resolution_due_at > old_due


def test_a_ticket_never_reopened_behaves_as_before_including_the_due_date_target(db_session):
    plain = triaged(db_session)
    created = aware(plain.created_at)
    assert aware(plain.resolution_due_at) == created + P1_RESOLUTION
    assert aware(plain.first_response_due_at) == created + P1_FIRST_RESPONSE
    assert plain.resolution_restarted_at is None and plain.first_resolved_at is None

    dated = triaged(db_session)
    dated.due_on = date.today() + timedelta(days=3)
    sla.recalculate_deadlines(db_session, dated)
    assert sla.due_date_is_target(dated)
    assert aware(dated.resolution_due_at) > created + P1_RESOLUTION  # the end of the Due day
    resolve(db_session, dated)
    assert dated.first_resolved_at is None
    assert "restarted" not in rows(dated)["Resolution"].text


# ---- Rule 3: the reports count a reopened ticket once ---------------------------------------------------------------


def test_sla_compliance_counts_a_reopened_ticket_once_for_its_first_resolution(db_session, world):  # noqa: F811
    desk, _, admin = world
    # First resolved in time, reopened, restarted target, still open: counts as one met resolution (not undecided).
    make_ticket(db_session, desk, at("2026-10-05"), priority="p1", status=TicketStatus.IN_PROGRESS,
                first_response_due_at=at("2026-10-05", 10, 15), first_responded_at=at("2026-10-05", 10, 10),
                resolution_due_at=at("2026-11-30"), resolution_restarted_at=at("2026-11-07"),
                first_resolved_at=at("2026-10-05", 13), first_resolution_due_at=at("2026-10-05", 14))
    # First resolved late, reopened, resolved again in time against the restarted target: one breached resolution.
    make_ticket(db_session, desk, at("2026-10-06"), priority="p1", status=TicketStatus.RESOLVED,
                first_response_due_at=at("2026-10-06", 10, 15), first_responded_at=at("2026-10-06", 10, 10),
                resolution_due_at=at("2026-10-30", 16), resolution_restarted_at=at("2026-10-30", 12),
                resolved_at=at("2026-10-30", 14),
                first_resolved_at=at("2026-10-06", 16), first_resolution_due_at=at("2026-10-06", 14))

    report = run(db_session, admin, "sla")

    assert table(report)[("Desk", "P1")] == [2, 0, 100.0, 1, 1, 50.0]
    assert notes(report)["Resolution not yet decided (clock running or paused)"] == "0"


def test_sla_compliance_judges_a_reopened_ticket_with_no_reply_by_its_first_resolution(db_session, world):  # noqa: F811
    desk, _, admin = world
    # Resolved with no email before the first-response deadline, then reopened: met, whatever happens after.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p4", status=TicketStatus.OPEN,
                first_response_due_at=at("2026-10-05", 12), resolution_due_at=at("2026-11-30"),
                first_resolved_at=at("2026-10-05", 11), first_resolution_due_at=at("2026-10-06"),
                resolution_restarted_at=at("2026-10-20"))
    report = run(db_session, admin, "sla")
    assert table(report)[("Desk", "P4")] == [1, 0, 100.0, 1, 0, 100.0]


def test_times_count_a_reopened_ticket_once_for_its_first_resolution(db_session, world):  # noqa: F811
    desk, _, admin = world
    # First resolved after 180 minutes less 60 paused = 120; reopened and open again. Counted once, at 120.
    make_ticket(db_session, desk, at("2026-10-05"), priority="p1", status=TicketStatus.IN_PROGRESS,
                first_responded_at=at("2026-10-05", 10, 10), sla_paused_seconds=3600 + 7200,
                first_resolved_at=at("2026-10-05", 13), first_resolution_paused_seconds=3600,
                resolution_restarted_at=at("2026-10-20"))
    # Resolved, reopened and resolved again: still the first resolution (240 minutes), not the second.
    make_ticket(db_session, desk, at("2026-10-06"), priority="p1", status=TicketStatus.RESOLVED,
                first_responded_at=at("2026-10-06", 10, 10), resolved_at=at("2026-10-20"),
                first_resolved_at=at("2026-10-06", 14), first_resolution_paused_seconds=0,
                resolution_restarted_at=at("2026-10-19"))

    report = run(db_session, admin, "times")

    rows_ = table(report)
    assert rows_[("Desk", "P1")][3:] == [2, 180.0, 180.0]  # resolution: tickets, mean (120 and 240), median
    assert "counted once" in report.description


def test_a_reopened_ticket_whose_first_resolution_had_no_target_is_not_judged_by_the_restarted_one(db_session, world):  # noqa: F811
    desk, _, admin = world
    make_ticket(db_session, desk, at("2026-10-05"), priority="p1", status=TicketStatus.IN_PROGRESS,
                first_response_due_at=at("2026-10-05", 10, 15), first_responded_at=at("2026-10-05", 10, 10),
                resolution_due_at=at("2026-11-30"), resolution_restarted_at=at("2026-11-07"),
                first_resolved_at=at("2026-10-05", 13), first_resolution_due_at=None)
    report = run(db_session, admin, "sla")
    assert table(report)[("Desk", "P1")][3:5] == [0, 0]

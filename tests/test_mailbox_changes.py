"""Scheduled mailbox changes (#307): the row, the job, the ticket's life, the failures, the agent and requester actions.
Exchange is the FakeExecutor, told to fail in each of the ways the interface allows."""

import threading
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import mailbox_changes
from app.config import Settings, settings, unsafe_setting_errors
from app.database import engine
from app.due_today import wake_scheduled_tickets
from app.mailbox_changes import MailboxChangeError, create_mailbox_change, run_mailbox_changes
from app.mailbox_executor import (
    OUTCOME_MISMATCH, OUTCOME_NO_ANSWER, OUTCOME_PERMANENT, OUTCOME_RETRYABLE, ExecutorResult, FakeExecutor, get_executor, set_executor,
)
from app.messages import requester_visible_messages
from app.models import (
    AuditLog, MailboxChange, Message, MessageDirection, MessageVisibility, Notification, OutboundEmail, RelatedTicket, Team,
    TeamMembership, Ticket, TicketEvent, TicketStatus, TicketType, User, UserRole,
)
from app.outbox import MAILBOX_CHANGE
from app.settings_store import (
    DEFAULT_TIME_ZONE, MAILBOX_CHANGE_MAX_ATTEMPTS, MAILBOX_CHANGE_RETRY_MINUTES, set_setting,
)
from tests.conftest import create_team, login_as, set_role
from tests.test_resolution import dev_mail  # noqa: F401  (a fixture)
from tests.test_triage import events, new_ticket

OWNER = "alex@example.com"
TARGET = "jane@example.com"


@pytest.fixture(autouse=True)
def fake_exchange(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()
    executor = FakeExecutor()
    set_executor(executor)
    yield executor
    set_executor(None)


def now():
    return datetime.now(timezone.utc)


def team_with_agent(db_session, email="agent@example.com"):
    team = create_team(db_session, "Service Desk")
    agent = User(email=email, display_name="Agent Smith", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.flush()
    db_session.add(TeamMembership(user_id=agent.id, team_id=team.id))
    db_session.commit()
    return team, agent


def request_ticket(db_session, team, requester=OWNER):
    """A triaged, open Service Request, as a portal request whose type triages itself leaves it."""
    ticket = new_ticket(db_session, team, TicketType.SERVICE_REQUEST, requester=requester)
    ticket.triaged_at, ticket.status, ticket.priority = now(), TicketStatus.OPEN, "p3"
    ticket.assigned_agent_id = None
    db_session.add(ticket)
    db_session.commit()
    return ticket


def schedule(db_session, ticket=None, team=None, starts_in=timedelta(hours=2), lasts=timedelta(hours=8), **extra):
    team = team or create_team(db_session, f"Desk {uuid4().hex[:6]}")
    ticket = ticket or request_ticket(db_session, team)
    start = now() + starts_in
    change = create_mailbox_change(
        db_session, ticket, kind="forwarding", owner_address=OWNER, target_address=TARGET, starts_at=start, ends_at=start + lasts,
        created_by=OWNER, owner_object_id="OID-OWNER", target_object_id="OID-TARGET", **extra,
    )
    db_session.commit()
    return ticket, change


def run(db_session, at):
    db_session.expire_all()
    with Session(engine) as session:
        return run_mailbox_changes(session, at)


def reload(db_session, *rows):
    db_session.expire_all()
    out = [db_session.get(type(row), row.id) for row in rows]
    return out[0] if len(out) == 1 else out


def mails(db_session, kind=None):
    db_session.expire_all()
    query = select(OutboundEmail).order_by(OutboundEmail.id)
    if kind:
        query = query.where(OutboundEmail.kind == kind)
    return db_session.exec(query).all()


def notes(db_session, ticket, visible):
    db_session.expire_all()
    rows = db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()
    return [m for m in rows if (m.direction != MessageDirection.INTERNAL) == visible]


def audit(db_session, change):
    db_session.expire_all()
    return [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "mailbox_change", AuditLog.entity_id == change.id).order_by(AuditLog.id)).all()]


# ---- The ticket waits in Scheduled, then the change is made --------------------------------------------------------


def test_a_future_change_waits_scheduled_and_is_applied_by_the_first_run_after_its_start(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    ticket = reload(db_session, ticket)

    assert ticket.status == TicketStatus.SCHEDULED
    assert ticket.due_on == change.starts_at.date() and not ticket.due_set_by_hand
    assert ticket.sla_starts_at == change.starts_at  # the SLA clock starts at the start time, not at creation
    assert ticket.resolution_due_at is not None

    assert run(db_session, change.starts_at - timedelta(minutes=1)) == 0  # not before the start
    assert fake_exchange.calls == [] and reload(db_session, change).state == "scheduled"
    assert reload(db_session, ticket).status == TicketStatus.SCHEDULED

    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    change, ticket = reload(db_session, change, ticket)
    assert change.state == "active" and change.applied_at is not None and change.attempts == 0
    assert fake_exchange.holds(change)
    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.resolution_notes.startswith("Forwarding to Jane Smith is on until") or ticket.resolution_notes.startswith("Forwarding to jane@example.com is on until")
    assert "UTC" in ticket.resolution_notes


def test_both_people_get_the_started_emails_and_the_requester_sees_a_note(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    run(db_session, change.starts_at + timedelta(minutes=1))

    to_requester = mails(db_session, "notice")
    to_target = mails(db_session, MAILBOX_CHANGE)
    assert [m.to_address for m in to_requester] == [OWNER] and [m.to_address for m in to_target] == [TARGET]
    assert to_target[0].subject == f"Mailbox access from {OWNER}" or "Mailbox access from" in to_target[0].subject
    assert "A copy stays in your mailbox." in notes(db_session, ticket, visible=True)[-1].body
    assert any("A copy stays" in m.body for m in requester_visible_messages(db_session, reload(db_session, ticket)))


def test_keep_copy_off_says_so_in_the_requesters_email(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session, keep_copy=False)
    run(db_session, change.starts_at + timedelta(minutes=1))
    assert "Your mailbox is **not** keeping a copy." in notes(db_session, ticket, visible=True)[-1].body


def test_the_scheduled_morning_job_leaves_a_ticket_with_a_pending_change_alone(db_session):
    ticket, change = schedule(db_session, starts_in=timedelta(hours=30))
    far_future = now() + timedelta(days=3)  # past the Due morning

    assert wake_scheduled_tickets(db_session, far_future) == 0
    assert reload(db_session, ticket).status == TicketStatus.SCHEDULED

    # A Scheduled ticket with no mailbox change is still woken: the skip is only for a pending change.
    other = request_ticket(db_session, create_team(db_session, "Other"))
    other.due_on, other.status = (now() - timedelta(days=1)).date(), TicketStatus.SCHEDULED
    db_session.add(other)
    db_session.commit()
    assert wake_scheduled_tickets(db_session, far_future) == 1
    assert reload(db_session, other).status == TicketStatus.OPEN


# ---- The end ---------------------------------------------------------------------------------------------------------


def test_the_change_is_removed_by_the_first_run_after_its_end_with_a_note_on_the_closed_ticket(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    run(db_session, change.starts_at + timedelta(minutes=1))
    ticket = reload(db_session, ticket)
    ticket.status = TicketStatus.CLOSED  # auto-close has run in the meantime
    ticket.closed_at = now()
    db_session.add(ticket)
    db_session.commit()
    before = len(mails(db_session))

    assert run(db_session, change.ends_at - timedelta(minutes=1)) == 0
    assert fake_exchange.holds(change)
    assert run(db_session, change.ends_at + timedelta(minutes=1)) == 1

    change, ticket = reload(db_session, change, ticket)
    assert change.state == "ended" and change.removed_at is not None and not fake_exchange.holds(change)
    assert ticket.status == TicketStatus.CLOSED  # a note must not reopen it
    assert "has stopped" in notes(db_session, ticket, visible=True)[-1].body
    assert len(events(db_session, ticket, "mailbox_change_ended")) == 1
    new = mails(db_session)[before:]
    assert sorted(m.to_address for m in new) == sorted([OWNER, TARGET]) and {m.kind for m in new} == {"notice", MAILBOX_CHANGE}


def test_running_the_job_twice_changes_nothing_the_second_time(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    at = change.starts_at + timedelta(minutes=1)
    assert run(db_session, at) == 1
    assert run(db_session, at) == 0
    assert [c[0] for c in fake_exchange.calls] == ["apply"]  # nothing applied twice
    assert len(mails(db_session, MAILBOX_CHANGE)) == 1


def test_a_change_whose_end_passed_before_it_could_start_is_never_made(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session, starts_in=timedelta(hours=1), lasts=timedelta(hours=1))
    assert run(db_session, change.ends_at + timedelta(minutes=5)) == 0
    assert fake_exchange.calls == []
    assert reload(db_session, change).state == "cancelled"


def test_a_change_waits_while_its_request_is_awaiting_approval(db_session, fake_exchange):
    team = create_team(db_session, "Desk")
    ticket = request_ticket(db_session, team)
    ticket.status = TicketStatus.AWAITING_APPROVAL
    db_session.add(ticket)
    db_session.commit()
    start = now() + timedelta(hours=1)
    change = create_mailbox_change(
        db_session, ticket, kind="send_as", owner_address=OWNER, target_address=TARGET, starts_at=start, ends_at=start + timedelta(hours=3), created_by=OWNER,
    )
    db_session.commit()
    assert reload(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL  # not Scheduled until approved

    assert run(db_session, start + timedelta(minutes=5)) == 0
    assert fake_exchange.calls == [] and reload(db_session, change).state == "scheduled"


# ---- Retries and failures -----------------------------------------------------------------------------------------


def test_a_retryable_failure_is_retried_quietly_and_then_succeeds(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    fake_exchange.script("apply", OUTCOME_RETRYABLE, error="Throttled")
    start = change.starts_at + timedelta(minutes=1)

    run(db_session, start)
    change = reload(db_session, change)
    assert change.state == "scheduled" and change.attempts == 1 and change.last_error_category == "retryable"
    assert change.next_attempt_at == start + timedelta(minutes=10)  # the placeholder 10 minutes times the attempts so far
    assert reload(db_session, ticket).status == TicketStatus.SCHEDULED and mails(db_session) == []  # nothing is shown meanwhile
    assert notes(db_session, ticket, visible=True) == [] and notes(db_session, ticket, visible=False) == []

    assert run(db_session, start + timedelta(minutes=9)) == 0  # not due again yet
    assert run(db_session, start + timedelta(minutes=11)) == 1
    change = reload(db_session, change)
    assert change.state == "active" and change.attempts == 0 and change.last_error is None
    assert reload(db_session, ticket).status == TicketStatus.RESOLVED


def test_the_backoff_grows_with_the_attempts_and_the_settings_rule(db_session, fake_exchange):
    set_setting(db_session, MAILBOX_CHANGE_RETRY_MINUTES, "7")
    set_setting(db_session, MAILBOX_CHANGE_MAX_ATTEMPTS, "4")
    db_session.commit()
    ticket, change = schedule(db_session)
    fake_exchange.script("apply", OUTCOME_RETRYABLE, times=2)
    at = change.starts_at + timedelta(minutes=1)
    run(db_session, at)
    assert reload(db_session, change).next_attempt_at == at + timedelta(minutes=7)
    at2 = at + timedelta(minutes=8)
    run(db_session, at2)
    assert reload(db_session, change).next_attempt_at == at2 + timedelta(minutes=14)


def test_retryable_until_the_attempts_run_out_is_a_final_start_failure(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    fake_exchange.script("apply", OUTCOME_RETRYABLE, times=5, error="Exchange: ServiceUnavailable for alex@example.com")
    at = change.starts_at + timedelta(minutes=1)
    for attempt in range(3):  # the placeholder is 3
        assert reload(db_session, change).state == "scheduled"
        run(db_session, at)
        at += timedelta(minutes=40)
    change = reload(db_session, change)
    assert change.state == "failed" and change.attempts == 3
    assert [c[0] for c in fake_exchange.calls] == ["apply"] * 3


def test_a_permanent_failure_is_final_at_once_with_no_retries(db_session, fake_exchange, dev_mail):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    ticket = reload(db_session, ticket)
    ticket.assigned_agent_id = agent.id
    db_session.add(ticket)
    db_session.commit()
    fake_exchange.script("apply", OUTCOME_PERMANENT, error="Couldn't find object 'jane@example.com'. Please make sure that it was spelled correctly")

    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    change, ticket = reload(db_session, change, ticket)
    assert change.state == "failed" and change.attempts == 1 and len(fake_exchange.calls) == 1
    assert change.last_error_category == "permanent" and "Couldn't find object" in change.last_error
    assert ticket.status == TicketStatus.OPEN and ticket.assigned_agent_id is None and ticket.team_id == team.id
    assert ticket.resolved_at is None


def test_a_mismatch_is_final_and_the_start_failure_reaches_the_team_not_the_target(db_session, fake_exchange, dev_mail):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    fake_exchange.script("apply", OUTCOME_MISMATCH)

    run(db_session, change.starts_at + timedelta(minutes=1))
    change, ticket = reload(db_session, change, ticket)

    assert change.state == "failed" and change.last_error_category == "mismatch" and change.attempts == 1
    assert ticket.status == TicketStatus.OPEN and ticket.assigned_agent_id is None and ticket.triaged_at is not None
    # an internal note with what was attempted, the raw error, the job id and the read-back
    [internal] = notes(db_session, ticket, visible=False)
    for part in ("could not be made", "forwarding", OWNER, TARGET, "Attempts: 1", "Error category: mismatch", "The read-back does not show the change", "Job id: fake-", "Read-back:"):
        assert part in internal.body
    # the requester's note and email say nothing was changed and never the Exchange text
    [visible] = notes(db_session, ticket, visible=True)
    assert "nothing has been changed" in visible.body and ticket.reference in visible.body
    assert "read-back" not in visible.body.lower() and "fake" not in visible.body.lower()
    assert [m.to_address for m in mails(db_session)] == [OWNER]  # the target is told nothing
    assert all("read-back" not in (m.body or "").lower() for m in mails(db_session))
    # the team has the bell
    bell = db_session.exec(select(Notification).where(Notification.kind == "mailbox_change_failed")).all()
    assert [n.user_id for n in bell] == [agent.id] and bell[0].ticket_id == ticket.id and bell[0].teams_wanted


def test_no_answer_with_the_change_already_present_counts_as_success_and_nothing_is_applied_twice(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    fake_exchange.script("apply", OUTCOME_NO_ANSWER, effect=True)  # the call worked, its answer was lost

    run(db_session, change.starts_at + timedelta(minutes=1))

    change = reload(db_session, change)
    assert change.state == "active"
    assert [c[0] for c in fake_exchange.calls] == ["apply", "read"]
    assert reload(db_session, ticket).status == TicketStatus.RESOLVED


def test_no_answer_with_the_change_absent_is_retried_as_retryable(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    fake_exchange.script("apply", OUTCOME_NO_ANSWER)  # it did not happen

    at = change.starts_at + timedelta(minutes=1)
    run(db_session, at)

    change = reload(db_session, change)
    assert change.state == "scheduled" and change.attempts == 1 and change.next_attempt_at == at + timedelta(minutes=10)
    assert [c[0] for c in fake_exchange.calls] == ["apply", "read"]
    run(db_session, at + timedelta(minutes=11))
    assert reload(db_session, change).state == "active"


def test_a_failed_removal_raises_a_linked_high_priority_incident_and_tells_the_requester_nothing(db_session, fake_exchange, dev_mail):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    run(db_session, change.starts_at + timedelta(minutes=1))
    mails_before = len(mails(db_session))
    fake_exchange.script("remove", OUTCOME_PERMANENT, error="Access to the mailbox was denied")

    run(db_session, change.ends_at + timedelta(minutes=1))

    change = reload(db_session, change)
    assert change.state == "failed" and change.applied_at is not None and change.incident_ticket_id is not None
    incident = db_session.get(Ticket, change.incident_ticket_id)
    assert incident.type == TicketType.INCIDENT and incident.priority == "p2" and incident.team_id == team.id
    assert incident.status == TicketStatus.OPEN and incident.assigned_agent_id is None and incident.triaged_at is not None
    link = db_session.exec(select(RelatedTicket)).one()
    assert (link.ticket_id, link.related_ticket_id) == (incident.id, ticket.id)  # the new incident follows the request
    [internal] = notes(db_session, incident, visible=False)
    assert "could not be removed" in internal.body and "Access to the mailbox was denied" in internal.body and ticket.reference in internal.body
    assert len(mails(db_session)) == mails_before  # the requester (and the target) are told nothing
    assert notes(db_session, ticket, visible=True)[-1].body.find("stopped") == -1
    bell = db_session.exec(select(Notification).where(Notification.kind == "mailbox_change_failed")).all()
    assert [n.ticket_id for n in bell] == [incident.id] and bell[0].teams_wanted and bell[0].user_id == agent.id


# ---- Agent actions -------------------------------------------------------------------------------------------------


def failed_start(db_session, fake_exchange, outcome=OUTCOME_PERMANENT):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    fake_exchange.script("apply", outcome)
    run(db_session, change.starts_at + timedelta(minutes=1))
    return team, agent, ticket, change


def agent_client(client, db_session, team, email="agent@example.com"):
    login_as(client, email)
    return client


def test_the_failed_ticket_offers_try_again_done_by_hand_and_cancel(client, db_session, fake_exchange, dev_mail):
    team, agent, ticket, change = failed_start(db_session, fake_exchange)
    agent_client(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}")

    assert page.status_code == 200
    assert "?action=mbx_retry" in page.text and "?action=mbx_done" in page.text and "?action=mbx_cancel" in page.text
    for key in ("mbx_retry", "mbx_done", "mbx_cancel"):
        assert client.get(f"/tickets/{ticket.number}?action={key}").status_code == 200
    assert "Exchange message is in the internal note" in client.get(f"/tickets/{ticket.number}?action=mbx_retry").text


def test_a_healthy_ticket_offers_none_of_them(client, db_session, fake_exchange):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    agent_client(client, db_session, team)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "mbx_retry" not in page and "mbx_done" not in page and "mbx_cancel" not in page
    assert client.post(f"/tickets/{ticket.number}/mailbox/try-again").status_code in (400, 200)
    assert reload(db_session, change).state == "scheduled"


def test_try_again_makes_one_more_attempt_and_a_second_failure_is_final_straight_away(client, db_session, fake_exchange, dev_mail):
    team, agent, ticket, change = failed_start(db_session, fake_exchange)
    client_ = agent_client(client, db_session, team)
    told = len(mails(db_session))

    assert client_.post(f"/tickets/{ticket.number}/mailbox/try-again").status_code == 200
    change = reload(db_session, change)
    assert change.state == "scheduled" and change.attempts == 2 and change.next_attempt_at is None  # one attempt left of the three
    assert len(events(db_session, ticket, "mailbox_change_try_again")) == 1 and "try_again" in audit(db_session, change)

    fake_exchange.script("apply", OUTCOME_RETRYABLE)
    run(db_session, change.starts_at + timedelta(minutes=30))
    assert reload(db_session, change).state == "failed"  # the one attempt failed, so it is final again
    assert len(mails(db_session)) == told  # and the requester is not emailed a second time

    client_.post(f"/tickets/{ticket.number}/mailbox/try-again")
    run(db_session, change.starts_at + timedelta(minutes=30))
    assert reload(db_session, change).state == "active" and reload(db_session, ticket).status == TicketStatus.RESOLVED


def test_done_by_hand_after_a_failed_start_makes_it_active_and_the_removal_still_runs(client, db_session, fake_exchange, dev_mail):
    team, agent, ticket, change = failed_start(db_session, fake_exchange)
    agent_client(client, db_session, team)

    assert client.post(f"/tickets/{ticket.number}/mailbox/done-by-hand").status_code == 200

    change, ticket = reload(db_session, change, ticket)
    assert change.state == "active" and change.applied_at is not None
    assert ticket.status == TicketStatus.RESOLVED and "Forwarding to" in ticket.resolution_notes
    assert len(events(db_session, ticket, "mailbox_change_done_by_hand")) == 1 and "done_by_hand" in audit(db_session, change)
    assert sorted(m.to_address for m in mails(db_session)[-2:]) == sorted([OWNER, TARGET])  # both told it is on
    fake_exchange.held.add(mailbox_key(change))  # the agent made it in the Exchange admin centre
    run(db_session, change.ends_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "ended" and not fake_exchange.holds(change)  # the end-time removal still ran


def mailbox_key(change):
    from app.mailbox_executor import _key

    return _key(change)


def test_done_by_hand_after_a_failed_removal_ends_it_and_tells_both_people(client, db_session, fake_exchange, dev_mail):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    run(db_session, change.starts_at + timedelta(minutes=1))
    fake_exchange.script("remove", OUTCOME_MISMATCH)
    run(db_session, change.ends_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "failed"
    before = len(mails(db_session))
    agent_client(client, db_session, team)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "mbx_retry" in page and "mbx_done" in page and "mbx_cancel" not in page  # Exchange may still hold it: no Cancel
    assert client.post(f"/tickets/{ticket.number}/mailbox/done-by-hand").status_code == 200

    change = reload(db_session, change)
    assert change.state == "ended" and change.removed_at is not None
    assert len(mails(db_session)) == before + 2
    assert client.post(f"/tickets/{ticket.number}/mailbox/cancel").status_code == 400  # and it is refused on the server too


def test_cancel_drops_a_failed_start_resolves_the_request_and_tells_the_requester(client, db_session, fake_exchange, dev_mail):
    team, agent, ticket, change = failed_start(db_session, fake_exchange)
    agent_client(client, db_session, team)
    before = len(mails(db_session))

    assert client.post(f"/tickets/{ticket.number}/mailbox/cancel").status_code == 200

    change, ticket = reload(db_session, change, ticket)
    assert change.state == "cancelled" and "cancelled" in audit(db_session, change)
    assert ticket.status == TicketStatus.RESOLVED and "cancelled by IT Support" in ticket.resolution_notes
    assert len(events(db_session, ticket, "mailbox_change_cancelled")) == 1
    new = mails(db_session)[before:]
    assert [m.to_address for m in new] == [OWNER] and "cancelled" in notes(db_session, ticket, visible=True)[-1].body
    run(db_session, change.ends_at + timedelta(minutes=1))
    assert fake_exchange.calls == [("apply", "forwarding", OWNER, TARGET)]  # nothing more is ever done to a cancelled change


def test_a_person_who_cannot_work_the_ticket_cannot_use_the_actions(client, db_session, fake_exchange, dev_mail):
    team, agent, ticket, change = failed_start(db_session, fake_exchange)
    login_as(client, "outsider@example.com")  # an end user: not an agent
    assert client.post(f"/tickets/{ticket.number}/mailbox/done-by-hand").status_code in (302, 303, 401, 403, 404)
    assert reload(db_session, change).state == "failed"


# ---- Requester actions ---------------------------------------------------------------------------------------------


def test_the_requester_can_cancel_before_the_start(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    mailbox_changes.cancel_by_requester(db_session, change, OWNER)
    db_session.commit()
    change, ticket = reload(db_session, change, ticket)
    assert change.state == "cancelled" and ticket.status == TicketStatus.RESOLVED and "cancelled" in ticket.resolution_notes
    assert run(db_session, change.starts_at + timedelta(hours=1)) == 0 and fake_exchange.calls == []


def test_the_requester_cannot_cancel_once_the_start_time_has_come(db_session, fake_exchange):
    ticket, change = schedule(db_session)
    with pytest.raises(MailboxChangeError):
        mailbox_changes.cancel_by_requester(db_session, change, OWNER, now=change.starts_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "scheduled"


def test_stop_now_brings_the_end_forward_and_the_next_run_removes_it(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    run(db_session, change.starts_at + timedelta(minutes=1))
    stop_at = change.starts_at + timedelta(minutes=30)

    mailbox_changes.stop_now(db_session, change, OWNER, now=stop_at)
    db_session.commit()
    change = reload(db_session, change)
    assert change.ends_at == stop_at and change.state == "active"
    assert len(events(db_session, ticket, "mailbox_change_stopped_early")) == 1 and "stop_now" in audit(db_session, change)

    run(db_session, stop_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "ended" and not fake_exchange.holds(change)


def test_stop_now_is_refused_unless_the_change_is_on(db_session, fake_exchange):
    ticket, change = schedule(db_session)
    with pytest.raises(MailboxChangeError):
        mailbox_changes.stop_now(db_session, change, OWNER)


# ---- Locking and crashes -------------------------------------------------------------------------------------------


def test_a_row_another_run_holds_is_skipped_and_two_runs_never_take_the_same_change(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    at = change.starts_at + timedelta(minutes=1)

    holder = Session(engine)
    holder.exec(select(MailboxChange).where(MailboxChange.id == change.id).with_for_update()).one()  # a run is working on it
    try:
        assert run(db_session, at) == 0  # SKIP LOCKED: this run takes nothing and does not wait
        assert fake_exchange.calls == []
    finally:
        holder.rollback()
        holder.close()
    assert run(db_session, at) == 1 and len(fake_exchange.calls) == 1


def test_runs_at_the_same_moment_apply_each_change_once(db_session, fake_exchange, dev_mail):
    changes = [schedule(db_session)[1] for _ in range(4)]
    at = max(c.starts_at for c in changes) + timedelta(minutes=1)
    totals = []

    def worker():
        with Session(engine) as session:
            totals.append(run_mailbox_changes(session, at))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(totals) == 4
    assert sorted(c[0] for c in fake_exchange.calls) == ["apply"] * 4
    assert all(reload(db_session, c).state == "active" for c in changes)


def test_a_crash_between_claim_and_result_is_recovered_once_the_lease_has_passed(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    at = change.starts_at + timedelta(minutes=1)
    with Session(engine) as session:  # the worker claimed it and died before recording anything
        work = mailbox_changes._claim_next(session, at, set())
        assert work is not None and work.operation == "apply"
    change = reload(db_session, change)
    assert change.state == "applying" and change.attempts == 1
    assert run(db_session, at + timedelta(minutes=1)) == 0  # the lease (15 minutes) has not run out
    assert run(db_session, at + timedelta(minutes=16)) == 1
    change = reload(db_session, change)
    assert change.state == "active" and fake_exchange.holds(change)


def test_a_result_that_arrives_after_an_agent_settled_the_change_is_dropped(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    at = change.starts_at + timedelta(minutes=1)
    with Session(engine) as session:
        work = mailbox_changes._claim_next(session, at, set())
    change = reload(db_session, change)
    change.state = "cancelled"
    db_session.add(change)
    db_session.commit()
    with Session(engine) as session:
        mailbox_changes._record_result(session, work, ExecutorResult("succeeded", found=True), at)
        session.commit()
    assert reload(db_session, change).state == "cancelled" and reload(db_session, ticket).status == TicketStatus.SCHEDULED


# ---- Executor choice -----------------------------------------------------------------------------------------------


def test_fake_set_outside_dev_is_a_startup_error_but_unset_and_azure_are_not():
    base = dict(env="staging", app_role="worker", session_secret_key="a-real-secret-value-0123456789", mail_sending="off")
    assert any("MAILBOX_EXECUTOR=fake" in e for e in unsafe_setting_errors(Settings(mailbox_executor="fake", **base)))
    # azure on the worker needs the Automation settings (app/mailbox_executor_azure.py); the apps that only create changes do not
    assert any("MAILBOX_AUTOMATION_RESOURCE_ID" in e for e in unsafe_setting_errors(Settings(mailbox_executor="azure", **base)))
    assert not any("MAILBOX_" in e for e in unsafe_setting_errors(Settings(mailbox_executor="azure", **{**base, "app_role": "web"})))
    # unset means the feature is off: the worker still starts (staging deploys on every push to main)
    assert not any("MAILBOX_EXECUTOR" in e for e in unsafe_setting_errors(Settings(mailbox_executor="", **base)))
    assert not any("MAILBOX_EXECUTOR" in e for e in unsafe_setting_errors(Settings(mailbox_executor="fake", **{**base, "env": "dev"})))
    assert any("MAILBOX_EXECUTOR must be" in e for e in unsafe_setting_errors(Settings(mailbox_executor="bogus", **{**base, "env": "dev"})))
    assert not any("MAILBOX_EXECUTOR" in e for e in unsafe_setting_errors(Settings(mailbox_executor="fake", **{**base, "app_role": "web"})))
    assert Settings(_env_file=None).mailbox_executor == ""


def test_unset_means_off_the_job_does_nothing_and_a_change_cannot_be_created(db_session, fake_exchange, monkeypatch):
    ticket, change = schedule(db_session)  # made while an executor was in place
    set_executor(None)
    monkeypatch.setattr(settings, "mailbox_executor", "")
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 0
    assert reload(db_session, change).state == "scheduled" and reload(db_session, change).attempts == 0
    with pytest.raises(MailboxChangeError, match="not switched on"):
        schedule(db_session)


def test_an_unconfigured_azure_executor_fails_the_run_and_an_idle_run_does_not_ask_for_it(db_session, monkeypatch):
    ticket, change = schedule(db_session)
    set_executor(None)
    monkeypatch.setattr(settings, "mailbox_executor", "azure")
    assert run(db_session, change.starts_at - timedelta(minutes=5)) == 0  # nothing due, so the missing executor is not asked for
    with pytest.raises(Exception, match="MAILBOX_AUTOMATION_RESOURCE_ID"):
        run(db_session, change.starts_at + timedelta(minutes=1))
    assert reload(db_session, change).attempts == 0  # nothing was claimed, so no attempt was used up


def test_a_run_takes_a_bounded_number_of_changes_and_the_rest_wait_for_the_next(db_session, fake_exchange, dev_mail, monkeypatch):
    monkeypatch.setattr(mailbox_changes, "MAX_PER_RUN", 2)
    changes = [schedule(db_session)[1] for _ in range(3)]
    at = max(c.starts_at for c in changes) + timedelta(minutes=1)
    assert run(db_session, at) == 2
    assert run(db_session, at) == 1


def test_a_change_is_held_for_the_long_lease_and_the_job_lease_is_at_least_as_long(db_session, fake_exchange):
    from app.scheduled_jobs import SCHEDULED_JOBS

    ticket, change = schedule(db_session)
    at = change.starts_at + timedelta(minutes=1)
    with Session(engine) as session:
        mailbox_changes._claim_next(session, at, set())
    assert reload(db_session, change).next_attempt_at == at + timedelta(minutes=15)  # longer than a ten-minute automation call
    [job] = [j for j in SCHEDULED_JOBS if j.name == "mailbox-changes"]
    assert job.min_lease_minutes >= 15


def test_the_job_is_registered_with_a_five_minute_interval_setting():
    from app.scheduled_jobs import SCHEDULED_JOBS

    [job] = [j for j in SCHEDULED_JOBS if j.name == "mailbox-changes"]
    assert job.every_seconds == 300 and job.interval_setting == "mailbox_changes_poll_minutes"


# ---- Person data: the export and anonymisation ---------------------------------------------------------------------


def test_the_subject_access_export_lists_a_change_for_the_owner_and_for_the_other_person(db_session, fake_exchange, dev_mail):
    import io
    import json
    import zipfile

    from app.storage import get_storage
    from app.subject_access import build_export

    ticket, change = schedule(db_session)
    run(db_session, change.starts_at + timedelta(minutes=1))
    admin = User(email="dpo@example.com", display_name="DPO", role=UserRole.SUPER_ADMIN)
    db_session.add(admin)
    db_session.commit()

    for address in (OWNER, TARGET):
        archive = zipfile.ZipFile(io.BytesIO(build_export(db_session, get_storage(), address, admin).file.read()))
        [row] = json.loads(archive.read("mailbox_changes.json"))
        assert row["kind"] == "forwarding" and row["state"] == "active"
        # only the asker's own side is named; the other person's address is not theirs to be given
        assert (row["mailbox"], row["shared_with"]) == ((OWNER, "another person") if address == OWNER else ("another person's", TARGET))
        assert "last_error" not in row and "job" not in " ".join(row)  # the team's working notes are not part of the export


def test_a_person_with_an_unfinished_change_is_not_anonymised_until_it_has_ended(db_session, fake_exchange, dev_mail):
    from app.anonymisation import Refused, anonymise
    from app.person_data import find_person

    ticket, change = schedule(db_session)
    admin = User(email="dpo@example.com", display_name="DPO", role=UserRole.SUPER_ADMIN)
    db_session.add(admin)
    db_session.commit()
    for address in (OWNER, TARGET):
        with pytest.raises(Refused, match="mailbox change that has not ended"):
            anonymise(db_session, find_person(db_session, address), admin, n=1, close_open=True)
        db_session.rollback()

    run(db_session, change.starts_at + timedelta(minutes=1))
    run(db_session, change.ends_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "ended"
    fake_exchange.held.clear()
    anonymise(db_session, find_person(db_session, TARGET), admin, n=7, close_open=True)
    db_session.commit()
    change = reload(db_session, change)
    assert change.target_address == "anonymised-7@invalid" and change.target_object_id is None
    assert change.owner_address == OWNER and change.owner_object_id == "oid-owner"  # the owner is not the person asked about


def test_a_second_final_removal_failure_goes_to_the_same_incident(client, db_session, fake_exchange, dev_mail):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    run(db_session, change.starts_at + timedelta(minutes=1))
    fake_exchange.script("remove", OUTCOME_PERMANENT, times=2)
    run(db_session, change.ends_at + timedelta(minutes=1))
    first = reload(db_session, change).incident_ticket_id
    agent_client(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/mailbox/try-again")
    run(db_session, change.ends_at + timedelta(minutes=30))

    change = reload(db_session, change)
    assert change.state == "failed" and change.incident_ticket_id == first
    assert len(db_session.exec(select(Ticket).where(Ticket.type == TicketType.INCIDENT)).all()) == 1
    assert len(notes(db_session, db_session.get(Ticket, first), visible=False)) == 2


def test_a_row_recovered_from_applying_after_its_end_is_not_cancelled_but_applied_and_removed(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    with Session(engine) as session:  # claimed, the change was made in Exchange, the worker died
        mailbox_changes._claim_next(session, change.starts_at + timedelta(minutes=1), set())
    fake_exchange.held.add(mailbox_key(reload(db_session, change)))
    late = change.ends_at + timedelta(minutes=30)

    run(db_session, late)  # applied again (idempotent) ...
    assert reload(db_session, change).state == "active"
    run(db_session, late + timedelta(minutes=1))  # ... and removed
    change = reload(db_session, change)
    assert change.state == "ended" and not fake_exchange.holds(change)


def test_a_result_that_cannot_be_recorded_hands_the_change_to_the_team_and_does_not_stop_the_run(db_session, fake_exchange, dev_mail, monkeypatch):
    team, agent = team_with_agent(db_session)
    ticket, change = schedule(db_session, ticket=request_ticket(db_session, team), team=team)
    other_ticket, other = schedule(db_session)
    calls = []
    real = mailbox_changes._on_applied

    def broken(session, ch, tk, at, actor=None):
        calls.append(ch.id)
        if len(calls) == 1:
            raise RuntimeError("template broke")
        return real(session, ch, tk, at, actor)

    monkeypatch.setattr(mailbox_changes, "_on_applied", broken)
    assert run(db_session, max(change.starts_at, other.starts_at) + timedelta(minutes=1)) == 2

    failed, fine = reload(db_session, change, other) if calls[0] == change.id else reload(db_session, other, change)
    assert failed.state == "failed" and failed.last_error_category == "recording_failed" and failed.applied_at is not None
    assert fine.state == "active"
    assert "recording_failed" in audit(db_session, failed)


def test_a_final_start_failure_returns_a_ticket_an_agent_put_on_hold_to_open(db_session, fake_exchange, dev_mail):
    ticket, change = schedule(db_session)
    ticket = reload(db_session, ticket)
    ticket.status = TicketStatus.WITH_SUPPLIER
    db_session.add(ticket)
    db_session.commit()
    fake_exchange.script("apply", OUTCOME_PERMANENT)
    run(db_session, change.starts_at + timedelta(minutes=1))
    assert reload(db_session, ticket).status == TicketStatus.OPEN

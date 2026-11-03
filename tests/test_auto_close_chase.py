"""4H: auto-close and chase (app/auto_close_chase.py): two recurring jobs, both off until staging."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import mail, outbox, scheduled_jobs
from app import auto_close_chase as auto_close_chase_module
from app.auto_close_chase import run_auto_close, run_chase
from app.config import settings
from app.database import engine
from app.email_templates import definition
from app.models import AuditLog, OutboundEmail, Ticket, TicketEvent, TicketStatus
from app.reopening import reopen_ticket
from app.scheduled_jobs import run_due_jobs
from app.settings_store import (
    AUTO_CLOSE_AFTER_DAYS,
    AUTO_CLOSE_ENABLED,
    AUTOMATIC_EMAIL_CAP,
    CHASE_AFTER_DAYS,
    CHASE_ENABLED,
    CHASE_RESOLVE_AFTER_DAYS,
    REGISTRY,
    get_flag,
    set_setting,
)
from app.splitting import apply_requester_reply
from app.resolution import resolve_ticket
from app.teams import Unsendable
from app.ticket_service import set_status
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)
from tests.test_splitting import split, subs_of, world


def configure(db_session, close=True, chase=True, close_days=7, chase_days=3, resolve_days=3):
    for key, value in (
        (AUTO_CLOSE_ENABLED, "on" if close else "off"),
        (CHASE_ENABLED, "on" if chase else "off"),
        (AUTO_CLOSE_AFTER_DAYS, str(close_days)),
        (CHASE_AFTER_DAYS, str(chase_days)),
        (CHASE_RESOLVE_AFTER_DAYS, str(resolve_days)),
    ):
        set_setting(db_session, key, value)
    db_session.commit()


def later(days=0.0):
    return datetime.now(timezone.utc) + timedelta(days=days)


def close_job(db_session, days=0.0):
    run_auto_close(db_session, later(days))
    db_session.commit()


def chase_job(db_session, days=0.0):
    run_chase(db_session, later(days))
    db_session.commit()


def make(db_session, status=TicketStatus.OPEN, requester="sam@example.com", **fields):
    ticket = Ticket(subject="Printer", requester_email=requester, triaged_at=datetime.now(timezone.utc), **fields)
    db_session.add(ticket)
    db_session.commit()
    move(db_session, ticket, status)
    return ticket


def move(db_session, ticket, status):
    set_status(db_session, ticket, status, "agent@example.com")
    db_session.commit()


def waiting(db_session, **fields):
    return make(db_session, TicketStatus.AWAITING_USER, **fields)


def resolved(db_session, **fields):
    ticket = make(db_session, **fields)
    resolve_ticket(db_session, ticket, None, "Fixed it", send_email=False)
    db_session.commit()
    return ticket


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def chases(db_session):
    return rows(db_session, outbox.CHASE)


def mark_sent(db_session, *kinds, at=None):
    """As the mail job does once the emails have gone; `at` is when, on the same simulated clock as the jobs."""
    for row in rows(db_session, *kinds):
        row.sent_at = at or datetime.now(timezone.utc)
        db_session.add(row)
    db_session.commit()


def events(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


# ---- Defaults, registration and the switches ---------------------------------------------------------


def test_both_automations_are_off_by_default_with_placeholder_days(db_session):
    assert REGISTRY[AUTO_CLOSE_ENABLED].default == "off" and REGISTRY[CHASE_ENABLED].default == "off"
    assert not get_flag(db_session, AUTO_CLOSE_ENABLED) and not get_flag(db_session, CHASE_ENABLED)
    assert [REGISTRY[k].default for k in (AUTO_CLOSE_AFTER_DAYS, CHASE_AFTER_DAYS, CHASE_RESOLVE_AFTER_DAYS)] == ["7", "3", "3"]


def test_the_jobs_are_registered_and_the_chase_template_exists():
    assert {"auto-close", "chase"} <= {job.name for job in scheduled_jobs.SCHEDULED_JOBS}
    assert definition("chase").key == "chase"


def test_with_the_switches_off_nothing_happens(db_session, dev_mail):
    old_resolved = resolved(db_session)
    quiet = waiting(db_session)
    configure(db_session, close=False, chase=False)
    close_job(db_session, 60)
    chase_job(db_session, 60)
    assert fresh(db_session, old_resolved).status == TicketStatus.RESOLVED
    assert fresh(db_session, quiet).status == TicketStatus.AWAITING_USER and chases(db_session) == []


def test_each_switch_is_independent(db_session, dev_mail):
    old_resolved = resolved(db_session)
    quiet = waiting(db_session)
    configure(db_session, close=True, chase=False)
    close_job(db_session, 8)
    chase_job(db_session, 8)
    assert fresh(db_session, old_resolved).status == TicketStatus.CLOSED
    assert chases(db_session) == []
    assert fresh(db_session, quiet).chased_at is None


def test_the_jobs_run_on_the_scheduler(db_session, dev_mail):
    ticket = resolved(db_session)
    ticket.resolved_at = later(-8)  # the registered job reads the real clock
    db_session.add(ticket)
    configure(db_session)
    job = next(j for j in scheduled_jobs.SCHEDULED_JOBS if j.name == "auto-close")
    assert run_due_jobs([job], "w1") == ["auto-close"]
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED


# ---- Auto-close ---------------------------------------------------------------------------------------


def test_a_ticket_resolved_for_n_days_is_closed_with_an_event(db_session):
    ticket = resolved(db_session)
    configure(db_session, close_days=7)

    close_job(db_session, 6.9)
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
    close_job(db_session, 7.1)

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.CLOSED and ticket.closed_at is not None
    [event] = events(db_session, ticket, "auto_closed")
    assert event.actor_email is None and "7 days" in event.detail
    assert [e.detail for e in events(db_session, ticket, "status_changed") if e.to_value == "closed"] == ["auto-closed"]
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "auto_closed")).all()
    assert audit.actor_email == "system"


def test_days_to_close_is_a_setting(db_session):
    ticket = resolved(db_session)
    configure(db_session, close_days=14)
    close_job(db_session, 10)
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
    close_job(db_session, 15)
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED


def test_run_twice_or_late_closes_once(db_session):
    ticket = resolved(db_session)
    configure(db_session)
    close_job(db_session, 90)  # very late
    close_job(db_session, 90)
    close_job(db_session, 91)
    assert len(events(db_session, ticket, "auto_closed")) == 1
    assert len([e for e in events(db_session, ticket, "status_changed") if e.to_value == "closed"]) == 1


def test_open_and_reopened_tickets_are_not_closed(db_session):
    open_ticket = make(db_session)
    reopened = resolved(db_session)
    reopen_ticket(db_session, reopened, "sam@example.com", "Still broken")
    db_session.commit()
    configure(db_session)
    close_job(db_session, 90)
    assert fresh(db_session, open_ticket).status == TicketStatus.OPEN
    assert fresh(db_session, reopened).status != TicketStatus.CLOSED


def test_the_clock_restarts_when_a_reopened_ticket_is_resolved_again(db_session):
    ticket = resolved(db_session)
    reopen_ticket(db_session, ticket, "sam@example.com", "Still broken")
    resolve_ticket(db_session, ticket, None, "Fixed again", send_email=False)
    db_session.commit()
    configure(db_session)
    close_job(db_session, 6)
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
    close_job(db_session, 8)
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED


def test_a_ticket_someone_is_working_on_is_left_to_the_next_run(db_session):
    ticket = resolved(db_session)
    configure(db_session)
    with Session(engine) as other:
        other.exec(select(Ticket).where(Ticket.id == ticket.id).with_for_update()).all()  # held, as an agent's save would
        close_job(db_session, 30)  # does not wait
        assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
        other.rollback()
    close_job(db_session, 30)
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED


def test_a_resolved_split_family_closes_each_ticket(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    for part in (first, second):
        resolve_ticket(db_session, fresh(db_session, part), None, "Done", send_email=False)
    resolve_ticket(db_session, fresh(db_session, original), None, "All done", send_email=False)
    db_session.commit()
    configure(db_session)
    close_job(db_session, 8)
    assert {fresh(db_session, t).status for t in (original, first, second)} == {TicketStatus.CLOSED}


# ---- Chase --------------------------------------------------------------------------------------------


def test_a_quiet_awaiting_user_ticket_is_chased_after_x_days(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session, chase_days=3)

    chase_job(db_session, 2.9)
    assert chases(db_session) == [] and fresh(db_session, ticket).chased_at is None
    chase_job(db_session, 3.1)

    ticket = fresh(db_session, ticket)
    [row] = chases(db_session)
    assert (row.to_address, row.ticket_id) == ("sam@example.com", ticket.id)
    assert ticket.chased_at is not None and ticket.chase_count == 1
    [event] = events(db_session, ticket, "chased")
    assert event.actor_email is None
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "chased")).all()
    assert audit.actor_email == "system"


def test_chase_after_days_is_a_setting(db_session, dev_mail):
    waiting(db_session)
    configure(db_session, chase_days=6)
    chase_job(db_session, 5)
    assert chases(db_session) == []
    chase_job(db_session, 6.1)
    assert len(chases(db_session)) == 1


def test_run_twice_or_late_chases_once(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 40)  # very late
    chase_job(db_session, 40)
    chase_job(db_session, 41)
    assert len(chases(db_session)) == 1 and fresh(db_session, ticket).chase_count == 1


def test_the_wait_is_counted_from_entering_awaiting_user_not_from_the_ticket_age(db_session, dev_mail):
    ticket = make(db_session, TicketStatus.IN_PROGRESS)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    ticket.created_at = old
    db_session.add(ticket)
    db_session.commit()
    move(db_session, ticket, TicketStatus.AWAITING_USER)
    configure(db_session)
    chase_job(db_session, 1)
    assert chases(db_session) == []


@pytest.mark.parametrize("status", [
    TicketStatus.WITH_SUPPLIER, TicketStatus.WITH_MSP, TicketStatus.SCHEDULED, TicketStatus.OPEN, TicketStatus.IN_PROGRESS,
    TicketStatus.AWAITING_APPROVAL, TicketStatus.RESOLVED, TicketStatus.CLOSED,
])
def test_only_awaiting_user_is_ever_chased(db_session, dev_mail, status):
    ticket = make(db_session)
    if status == TicketStatus.AWAITING_APPROVAL:
        ticket.status = status  # set by hand: only the approval flow moves a ticket there
        db_session.add(ticket)
        db_session.commit()
    elif status == TicketStatus.RESOLVED:
        resolve_ticket(db_session, ticket, None, "Fixed", send_email=False)
        db_session.commit()
    elif status != TicketStatus.OPEN:
        if status == TicketStatus.SCHEDULED:
            ticket.due_on = datetime.now(timezone.utc).date() + timedelta(days=3)  # Scheduled needs a date
        move(db_session, ticket, status)
    configure(db_session, close=False)
    chase_job(db_session, 60)
    assert chases(db_session) == [] and fresh(db_session, ticket).chased_at is None


def test_a_ticket_moved_to_with_supplier_after_being_chased_is_not_auto_resolved(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)
    mark_sent(db_session, outbox.CHASE)
    move(db_session, ticket, TicketStatus.WITH_SUPPLIER)
    chase_job(db_session, 30)
    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.WITH_SUPPLIER and ticket.chased_at is None


def test_nothing_is_chased_while_mail_sending_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "off")
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 30)
    assert chases(db_session) == [] and fresh(db_session, ticket).chased_at is None


def test_the_chase_email_uses_the_template_and_the_tickets_own_subject(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session, resolve_days=5)
    chase_job(db_session, 3.1)
    [row] = chases(db_session)

    built = mail.build_chase_mail(db_session, row)

    assert built.to == "sam@example.com" and built.automatic
    assert built.subject.startswith(f"[{ticket.reference}]") and "Is this still an issue?" in built.body
    assert "next 5 days" in built.body


def test_a_reply_cancels_the_chase_and_a_queued_chase_is_not_sent(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)
    [row] = chases(db_session)

    apply_requester_reply(db_session, fresh(db_session, ticket), "sam@example.com", "It is fixed now, thanks")
    db_session.commit()

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS and ticket.chased_at is None and ticket.chase_count == 0
    with pytest.raises(Unsendable):
        mail.build_chase_mail(db_session, row)
    mark_sent(db_session, outbox.CHASE)
    chase_job(db_session, 40)
    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS  # never auto-resolved


def test_a_ticket_that_goes_back_to_awaiting_user_starts_a_fresh_wait(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)
    move(db_session, fresh(db_session, ticket), TicketStatus.IN_PROGRESS)
    move(db_session, fresh(db_session, ticket), TicketStatus.AWAITING_USER)
    ticket = fresh(db_session, ticket)
    assert ticket.chased_at is None and ticket.chase_count == 0
    chase_job(db_session, 2)  # the new wait is not old enough yet
    assert len(chases(db_session)) == 1
    chase_job(db_session, 3.2)
    assert len(chases(db_session)) == 2


# ---- Chase cap -----------------------------------------------------------------------------------------


def test_chase_emails_count_toward_the_automatic_email_cap(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    db_session.commit()
    chase_job(db_session, 3.1)
    [row] = chases(db_session)
    assert mail.cap_release(db_session, row, later(3.1)) is None  # nothing else sent: it may go

    db_session.add(OutboundEmail(
        kind=outbox.NOTICE, ticket_id=ticket.id, to_address="sam@example.com", sent_at=later(3), attempts=1,
    ))
    db_session.commit()
    assert mail.cap_release(db_session, row, later(3.1)) is not None  # held until the allowance frees up


def test_a_sent_chase_uses_up_the_allowance_for_a_notice(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    db_session.commit()
    chase_job(db_session, 3.1)
    mark_sent(db_session, outbox.CHASE, at=later(3.1))
    notice = OutboundEmail(kind=outbox.NOTICE, ticket_id=ticket.id, to_address="sam@example.com")
    assert mail.cap_release(db_session, notice, later(3.2)) is not None


# ---- Auto-resolve after the chase ---------------------------------------------------------------------


def chased(db_session, resolve_days=3):
    ticket = waiting(db_session)
    configure(db_session, resolve_days=resolve_days)
    chase_job(db_session, 3.1)
    mark_sent(db_session, outbox.CHASE, at=later(3.1))  # the job counts from when it went
    return ticket


def test_still_no_reply_y_days_after_the_chase_resolves_the_ticket_automatically(db_session, dev_mail):
    ticket = chased(db_session, resolve_days=3)

    chase_job(db_session, 5.9)
    assert fresh(db_session, ticket).status == TicketStatus.AWAITING_USER
    chase_job(db_session, 6.2)

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.RESOLVED and ticket.resolved_by_id is None
    assert "resolved automatically" in ticket.resolution_notes and "days" in ticket.resolution_notes
    assert ticket.chased_at is None  # the chase is over
    [email] = rows(db_session, outbox.NOTICE)  # the resolution email, an automatic notice
    assert email.ticket_id == ticket.id
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all()
    assert audit.actor_email == "system"


def test_run_twice_or_late_resolves_once(db_session, dev_mail):
    ticket = chased(db_session)
    chase_job(db_session, 90)
    chase_job(db_session, 90)
    chase_job(db_session, 91)
    assert len(rows(db_session, outbox.NOTICE)) == 1
    assert len([e for e in events(db_session, ticket, "status_changed") if e.to_value == "resolved"]) == 1
    assert len(chases(db_session)) == 1  # the resolved ticket is not chased again


def test_days_until_auto_resolve_is_a_setting(db_session, dev_mail):
    ticket = chased(db_session, resolve_days=6)
    chase_job(db_session, 8)
    assert fresh(db_session, ticket).status == TicketStatus.AWAITING_USER
    chase_job(db_session, 10)
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_the_clock_waits_for_a_chase_email_that_has_not_gone_yet(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)  # queued, held by the cap or by mail being down: never sent
    chase_job(db_session, 60)
    assert fresh(db_session, ticket).status == TicketStatus.AWAITING_USER
    mark_sent(db_session, outbox.CHASE)
    chase_job(db_session, 60)
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_a_chase_that_was_given_up_on_does_not_resolve_the_ticket(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)
    [row] = chases(db_session)
    mail.give_up(row, "no address")
    db_session.add(row)
    db_session.commit()
    chase_job(db_session, 60)
    assert fresh(db_session, ticket).status == TicketStatus.AWAITING_USER


def test_a_reply_after_the_chase_stops_the_auto_resolve(db_session, dev_mail):
    ticket = chased(db_session)
    apply_requester_reply(db_session, fresh(db_session, ticket), "sam@example.com", "Yes still broken")
    db_session.commit()
    chase_job(db_session, 90)
    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS


def test_the_auto_resolved_ticket_can_be_reopened_and_is_then_not_chased_until_it_waits_again(db_session, dev_mail):
    ticket = chased(db_session)
    chase_job(db_session, 7)
    reopen_ticket(db_session, fresh(db_session, ticket), "sam@example.com", "Still broken")
    db_session.commit()
    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.OPEN and ticket.chased_at is None and ticket.chase_count == 0
    chase_job(db_session, 60)
    assert len(chases(db_session)) == 1  # only the first one


def test_the_unresolvable_is_skipped_not_fatal(db_session, dev_mail):
    ticket = chased(db_session)
    other = chased_other(db_session)
    ticket.triaged_at = None  # check_can_resolve refuses an untriaged ticket
    db_session.add(ticket)
    db_session.commit()
    chase_job(db_session, 60)
    assert fresh(db_session, ticket).status == TicketStatus.AWAITING_USER
    assert fresh(db_session, other).status == TicketStatus.RESOLVED


def chased_other(db_session):
    other = waiting(db_session, requester="kim@example.com")
    chase_job(db_session, 3.1)
    mark_sent(db_session, outbox.CHASE, at=later(3.1))
    return other


# ---- Split originals -----------------------------------------------------------------------------------


def test_a_split_original_is_chased_through_its_parts_not_itself(client, db_session, dev_mail):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    move(db_session, fresh(db_session, first), TicketStatus.AWAITING_USER)
    move(db_session, fresh(db_session, second), TicketStatus.AWAITING_USER)
    assert fresh(db_session, original).status == TicketStatus.AWAITING_USER  # derived from its parts
    configure(db_session)

    chase_job(db_session, 3.1)

    assert sorted(r.ticket_id for r in chases(db_session)) == sorted([first.id, second.id])
    assert fresh(db_session, original).chased_at is None
    built = mail.build_chase_mail(db_session, chases(db_session)[0])
    assert built.subject.startswith(f"[{original.reference}]")  # one conversation for the requester
    assert "About:" in built.body


def test_a_part_that_replied_is_not_chased_but_its_sibling_is(client, db_session, dev_mail):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    for part in (first, second):
        move(db_session, fresh(db_session, part), TicketStatus.AWAITING_USER)
    apply_requester_reply(db_session, fresh(db_session, first), original.requester_email, "Part one is fine")
    db_session.commit()
    configure(db_session)
    chase_job(db_session, 3.1)
    assert [r.ticket_id for r in chases(db_session)] == [second.id]


# ---- Review fixes ---------------------------------------------------------------------------------------


def test_a_part_is_not_closed_while_its_original_is_still_resolved(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    for part in (first, second):
        resolve_ticket(db_session, fresh(db_session, part), None, "Done", send_email=False)
        db_session.commit()
    resolve_ticket(db_session, fresh(db_session, original), None, "All done", send_email=False)
    db_session.commit()
    for part in (first, second):
        part = fresh(db_session, part)
        part.resolved_at = later(-10)
        db_session.add(part)
    db_session.commit()
    configure(db_session)

    close_job(db_session)  # the original was resolved a moment ago

    assert {fresh(db_session, t).status for t in (original, first, second)} == {TicketStatus.RESOLVED}


def test_an_older_chase_that_never_went_is_replaced_by_the_new_one(db_session, dev_mail):
    ticket = waiting(db_session)
    configure(db_session)
    chase_job(db_session, 3.1)  # held, never sent
    apply_requester_reply(db_session, fresh(db_session, ticket), "sam@example.com", "hello")
    db_session.commit()
    move(db_session, fresh(db_session, ticket), TicketStatus.AWAITING_USER)
    chase_job(db_session, 7)
    old, new = chases(db_session)
    assert old.attempts >= 1 and old.last_error == "Replaced by a newer chase" and new.attempts == 0


def test_one_ticket_that_fails_does_not_stop_the_others(db_session, dev_mail, monkeypatch):
    bad = chased(db_session)
    good = chased_other(db_session)
    real = auto_close_chase_module.resolve_ticket

    def flaky(session, ticket, *args, **kwargs):
        if ticket.id == bad.id:
            raise RuntimeError("boom")
        return real(session, ticket, *args, **kwargs)

    monkeypatch.setattr(auto_close_chase_module, "resolve_ticket", flaky)
    chase_job(db_session, 60)
    assert fresh(db_session, bad).status == TicketStatus.AWAITING_USER
    assert fresh(db_session, good).status == TicketStatus.RESOLVED

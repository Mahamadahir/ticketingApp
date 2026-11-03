"""4C-3: approval reminders, a recurring job that chases approvers who have not answered."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import mail, outbox, scheduled_jobs
from app.approval_groups import remove_member
from app.approval_reminders import send_approval_reminders
from app.approvals import withdraw
from app.database import engine
from app.models import ApprovalGroup, ApprovalVote, AuditLog, Notification, OutboundEmail, Ticket, TicketApproval, User, UserRole
from app.scheduled_jobs import run_due_jobs
from app.settings_store import (
    APPROVAL_ESCALATE_AFTER_DAYS,
    APPROVAL_MAX_REMINDERS,
    APPROVAL_REMIND_AFTER_DAYS,
    APPROVAL_REMIND_EVERY_DAYS,
    APPROVAL_REMINDERS_ENABLED,
    REGISTRY,
    get_flag,
    set_setting,
)
from tests.conftest import login_as, set_role
from tests.test_approvals import answer, many, raised_and_triaged, request_type
from tests.test_outbound_mail import dev_mail, opt_out_of_approval_emails, rows  # noqa: F401  (dev_mail is a fixture)


def configure(db_session, enabled=True, after=2, every=2, maximum=3, escalate=7):
    for key, value in (
        (APPROVAL_REMINDERS_ENABLED, "on" if enabled else "off"),
        (APPROVAL_REMIND_AFTER_DAYS, str(after)),
        (APPROVAL_REMIND_EVERY_DAYS, str(every)),
        (APPROVAL_MAX_REMINDERS, str(maximum)),
        (APPROVAL_ESCALATE_AFTER_DAYS, str(escalate)),
    ):
        set_setting(db_session, key, value)
    db_session.commit()


def run(db_session, days=0.0, hours=0.0, delivered=True):
    """One run of the job, as if it were this long after the request was made. By default the mail job then
    sends what was queued (delivered=False leaves it waiting)."""
    send_approval_reminders(db_session, datetime.now(timezone.utc) + timedelta(days=days, hours=hours))
    db_session.commit()
    if delivered:
        mark_sent(db_session, outbox.APPROVAL_REMINDER)


def reminders(db_session):
    return rows(db_session, outbox.APPROVAL_REMINDER)


def to_addresses(db_session):
    return sorted(r.to_address for r in reminders(db_session))


def mark_sent(db_session, *kinds):
    """As the mail job does once the emails have gone."""
    for row in rows(db_session, *kinds):
        row.sent_at = datetime.now(timezone.utc)
        db_session.add(row)
    db_session.commit()


def waiting_request(client, db_session, names=("a", "b", "c"), pass_percent=100):
    """A request waiting on these approvers, whose approval emails have gone."""
    emails = many(client, db_session, names)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, pass_percent))
    mark_sent(db_session, outbox.APPROVAL)
    return emails, ticket


def vote_of(db_session, email):
    db_session.expire_all()
    return db_session.exec(select(ApprovalVote).join(User, User.id == ApprovalVote.voter_id).where(User.email == email)).one()


def test_reminders_are_off_until_switched_on(db_session):
    assert REGISTRY[APPROVAL_REMINDERS_ENABLED].default == "off"
    assert get_flag(db_session, APPROVAL_REMINDERS_ENABLED) is False


def test_the_job_is_registered_on_the_scheduler():
    assert "approval-reminders" in [job.name for job in scheduled_jobs.SCHEDULED_JOBS]


def test_nothing_is_sent_while_the_switch_is_off(client, db_session, dev_mail):
    waiting_request(client, db_session)
    configure(db_session, enabled=False)
    run(db_session, days=30)
    assert reminders(db_session) == []


def test_nothing_is_sent_before_the_first_delay(client, db_session, dev_mail):
    waiting_request(client, db_session)
    configure(db_session, after=2)
    run(db_session, days=1.9)
    assert reminders(db_session) == []
    run(db_session, days=2.1)
    assert len(reminders(db_session)) == 3


def test_remind_after_days_is_a_setting(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=5)
    run(db_session, days=3)
    assert reminders(db_session) == []
    run(db_session, days=5.1)
    assert len(reminders(db_session)) == 1


def test_a_member_who_voted_is_never_reminded(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session)
    answer(client, ticket, "a@example.com", "approved")
    configure(db_session)
    run(db_session, days=3)
    assert to_addresses(db_session) == ["b@example.com", "c@example.com"]


def test_running_twice_sends_once(client, db_session, dev_mail):
    waiting_request(client, db_session)
    configure(db_session)
    run(db_session, days=3)
    run(db_session, days=3)
    run(db_session, days=3.5)
    assert len(reminders(db_session)) == 3
    assert vote_of(db_session, "a@example.com").reminder_count == 1


def test_running_late_sends_one_reminder_not_one_per_missed_slot(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=2, every=2, maximum=5)
    run(db_session, days=40)
    assert len(reminders(db_session)) == 1
    run(db_session, days=40)
    assert len(reminders(db_session)) == 1


def test_a_repeat_is_sent_every_y_days(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=1, every=3, maximum=5)
    run(db_session, days=1.1)
    assert len(reminders(db_session)) == 1
    run(db_session, days=3)  # only 1.9 days after the first
    assert len(reminders(db_session)) == 1
    run(db_session, days=4.2)
    assert len(reminders(db_session)) == 2
    run(db_session, days=8)
    assert len(reminders(db_session)) == 3


def test_at_most_the_maximum_number_of_reminders(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=1, every=1, maximum=2)
    for day in (1.1, 2.3, 3.5, 4.7, 30):
        run(db_session, days=day)
    assert len(reminders(db_session)) == 2
    assert vote_of(db_session, "a@example.com").reminder_count == 2


@pytest.mark.parametrize("how", ["approved", "declined", "waived", "withdrawn"])
def test_reminders_stop_when_the_approval_is_settled(client, db_session, dev_mail, how):
    emails, ticket = waiting_request(client, db_session, ("a", "b"), pass_percent=100)
    configure(db_session)
    if how == "approved":
        answer(client, ticket, "a@example.com", "approved")
        answer(client, ticket, "b@example.com", "approved")
    elif how == "declined":
        answer(client, ticket, "a@example.com", "declined")
    elif how == "waived":
        client.post("/auth/logout")
        set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
        assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 200
    else:
        client.post("/auth/logout")
        login_as(client, "jo@example.com")
        assert client.post(f"/portal/{ticket.number}/withdraw", follow_redirects=False).status_code == 303
    run(db_session, days=30)
    assert reminders(db_session) == []


def test_an_approver_who_switched_approval_emails_off_is_skipped_and_not_counted(client, db_session, dev_mail):
    waiting_request(client, db_session)
    opt_out_of_approval_emails(db_session, "b@example.com")
    configure(db_session)
    run(db_session, days=3)
    assert to_addresses(db_session) == ["a@example.com", "c@example.com"]
    assert vote_of(db_session, "b@example.com").reminder_count == 0


def test_a_reminder_asks_again_when_it_is_sent(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a", "b"))
    configure(db_session)
    run(db_session, days=3)
    row = next(r for r in reminders(db_session) if r.to_address == "a@example.com")
    opt_out_of_approval_emails(db_session, "a@example.com")
    with pytest.raises(mail.Unsendable, match="turned approval emails off"):
        mail.build_approval_reminder_mail(db_session, row)


def test_a_member_taken_out_of_the_group_is_not_reminded(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session, ("a", "b"))
    approval = db_session.exec(select(TicketApproval)).one()
    group = db_session.get(ApprovalGroup, approval.group_id)
    remove_member(db_session, group, vote_of(db_session, "a@example.com").voter_id)
    db_session.commit()
    configure(db_session)
    run(db_session, days=3)
    assert to_addresses(db_session) == ["b@example.com"]


def test_nothing_is_queued_while_mail_is_off(client, db_session, monkeypatch):
    from app.config import settings

    waiting_request(client, db_session, ("a",))
    monkeypatch.setattr(settings, "mail_sending", "off")
    configure(db_session)
    run(db_session, days=3)
    assert reminders(db_session) == []
    assert vote_of(db_session, "a@example.com").reminder_count == 0  # not used up: it goes once mail is on


def test_the_reminder_email_uses_its_template_and_a_working_link(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session, ("a",))
    configure(db_session)
    run(db_session, days=3)
    [row] = reminders(db_session)
    built = mail.build_approval_reminder_mail(db_session, row)
    assert built.subject == f"[{ticket.reference}] Reminder, approval needed: {ticket.subject}"
    assert "This is a reminder" in built.body and "https://tickets.example.org/approvals/" in built.body
    assert "https://" not in built.record and "[answer link left out]" in built.record
    assert built.automatic is True


def test_a_reminder_counts_toward_the_automatic_email_cap_per_approver(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a", "b"))
    configure(db_session, after=1, every=1, maximum=10)
    set_setting(db_session, "automatic_email_cap_per_ticket", "2")
    db_session.commit()
    run(db_session, days=1.1)
    now = datetime.now(timezone.utc)
    mine = [r for r in reminders(db_session) if r.to_address == "a@example.com"][0]
    assert mail.cap_release(db_session, mine, now) is None
    for _ in range(2):
        extra = OutboundEmail(kind=outbox.APPROVAL_REMINDER, ticket_id=mine.ticket_id, vote_id=mine.vote_id, to_address="a@example.com", sent_at=now)
        db_session.add(extra)
    db_session.commit()
    assert mail.cap_release(db_session, mine, now) is not None  # a's allowance is used up
    theirs = [r for r in reminders(db_session) if r.to_address == "b@example.com"][0]
    assert mail.cap_release(db_session, theirs, now) is None  # b's is not


def test_the_super_admins_are_told_once_after_the_escalation_delay(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session, ("a", "b"))
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    configure(db_session, escalate=7)
    run(db_session, days=6)
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all() == []

    run(db_session, days=7.1)
    run(db_session, days=9)
    told = db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all()
    assert [db_session.get(User, n.user_id).email for n in told] == ["admin@example.com"]
    assert "2 had not answered" in told[0].text
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "approval_overdue")).one().entity_id == ticket.id
    assert db_session.exec(select(TicketApproval)).one().escalated_at is not None


def test_no_escalation_once_it_is_settled_or_when_switched_off(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session, ("a",))
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    configure(db_session, enabled=False)
    run(db_session, days=30)
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all() == []
    configure(db_session)
    answer(client, ticket, "a@example.com", "approved")
    run(db_session, days=30)
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all() == []


def test_the_scheduler_runs_it_without_committing_inside_the_job(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=1)
    for vote in db_session.exec(select(ApprovalVote)).all():
        vote.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.add(vote)
    db_session.commit()
    job = next(j for j in scheduled_jobs.SCHEDULED_JOBS if j.name == "approval-reminders")
    assert run_due_jobs([job], "w1") == ["approval-reminders"]
    assert len(reminders(db_session)) == 1
    assert run_due_jobs([job], "w1") == []


def test_nothing_piles_up_behind_an_email_that_has_not_gone(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, after=1, every=1, maximum=5)
    run(db_session, days=1.1, delivered=False)
    run(db_session, days=2.2, delivered=False)  # the first reminder is still queued
    assert len(reminders(db_session)) == 1
    assert vote_of(db_session, "a@example.com").reminder_count == 1
    mark_sent(db_session, outbox.APPROVAL_REMINDER)
    run(db_session, days=3.3)
    assert len(reminders(db_session)) == 2


def test_no_reminder_while_the_approval_email_itself_is_still_waiting(client, db_session, dev_mail):
    emails = many(client, db_session, ("a",))
    raised_and_triaged(client, db_session, request_type(db_session, emails))  # the approval email is not sent
    configure(db_session)
    run(db_session, days=30)
    assert reminders(db_session) == []


def test_a_deactivated_approver_is_not_chased_or_counted(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a", "b"))
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    leaver = db_session.exec(select(User).where(User.email == "a@example.com")).one()
    leaver.is_active = False
    db_session.add(leaver)
    db_session.commit()
    configure(db_session, escalate=1)
    run(db_session, days=3)
    assert to_addresses(db_session) == ["b@example.com"]
    [told] = db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all()
    assert "1 had not answered" in told.text


def test_escalation_is_not_used_up_when_there_was_nobody_to_tell(client, db_session, dev_mail):
    waiting_request(client, db_session, ("a",))
    configure(db_session, escalate=1)
    run(db_session, days=3)
    assert db_session.exec(select(TicketApproval)).one().escalated_at is None
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    run(db_session, days=3)
    assert len(db_session.exec(select(Notification).where(Notification.kind == "approval_overdue")).all()) == 1


def test_a_ticket_someone_is_working_on_is_left_to_the_next_run_without_waiting(client, db_session, dev_mail):
    emails, ticket = waiting_request(client, db_session, ("a",))
    configure(db_session)
    with Session(engine) as other:  # another transaction holding the ticket, as an agent's edit would
        other.exec(select(Ticket).where(Ticket.id == ticket.id).with_for_update()).one()
        run(db_session, days=3)  # would hang if it waited
        assert reminders(db_session) == []
        other.rollback()
    run(db_session, days=3)
    assert len(reminders(db_session)) == 1

"""#161: approvals with inactive approvers. A group with nobody active waits (not skips) and tells the super
admins; an approver who becomes inactive has their unanswered vote dropped and the outcome worked out again."""
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app import entra_check
from app.approvals import (
    APPROVED,
    DECLINED,
    NOT_NEEDED,
    PENDING,
    SUPERSEDED,
    WAIVED,
    ApprovalError,
    current_approval,
    decide,
    drop_votes_of,
    is_waiting_for_approvers,
)
from app.approval_reminders import send_approval_reminders
from app.models import ApprovalVote, AuditLog, Notification, RequestType, Setting, Ticket, TicketApproval, TicketStatus, User, UserRole
from app.settings_store import APPROVAL_REMINDERS_ENABLED, set_setting
from tests.conftest import login_as, set_role
from tests.test_raising import raise_it
from tests.test_approvals import answer, approvals, events, many, person, raised_and_triaged, request_type, status


def super_admin(client, db_session, email="admin@example.com"):
    client.post("/auth/logout")
    set_role(db_session, login_as(client, email, "Admin"), UserRole.SUPER_ADMIN)
    client.post("/auth/logout")
    return db_session.exec(select(User).where(User.email == email)).one()


def set_active(db_session, email, active):
    user = db_session.exec(select(User).where(User.email == email)).one()
    user.is_active = active
    db_session.add(user)
    db_session.commit()
    return user


def deactivate_in_admin(client, db_session, admin, email):
    user = db_session.exec(select(User).where(User.email == email)).one()
    client.post("/auth/logout")
    login_as(client, admin.email)
    client.post(f"/admin/users/{user.id}/active", data={"is_active": "false"}, follow_redirects=False)
    db_session.expire_all()
    return user


def bells(db_session):
    db_session.expire_all()
    return db_session.exec(select(Notification).where(Notification.kind == "approval_no_active_approvers")).all()


def raised_again(client, db_session, kind, requester):
    """Another request of this type, triaged by the desk's agent (the desk already exists)."""
    from app.models import Category

    client.post("/auth/logout")
    login_as(client, requester, "Another Requester")
    assert raise_it(client, subject="Another starter", request_type_id=kind.id).status_code == 204
    ticket = db_session.exec(select(Ticket).order_by(Ticket.id.desc())).first()
    client.post("/auth/logout")
    login_as(client, "agent@example.com", "Agent Ann")
    category = db_session.exec(select(Category)).first()
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def waiting_ticket(client, db_session, names=("a", "b"), pass_percent=100):
    """A request whose group's members all went inactive before it was triaged."""
    admin = super_admin(client, db_session)
    emails = many(client, db_session, names)
    kind = request_type(db_session, emails, pass_percent)
    for email in emails:
        set_active(db_session, email, False)
    ticket = raised_and_triaged(client, db_session, kind)
    return admin, emails, kind, ticket


# ---- A. Nobody active can approve --------------------------------------------------------------------


def test_a_group_with_nobody_active_makes_the_request_wait_and_tells_the_super_admins(client, db_session):
    admin, _, kind, ticket = waiting_ticket(client, db_session)

    assert ticket.status == TicketStatus.AWAITING_APPROVAL
    assert db_session.exec(select(TicketApproval)).all() == []
    assert is_waiting_for_approvers(db_session, ticket)
    [bell] = bells(db_session)
    assert bell.user_id == admin.id and bell.ticket_id == ticket.id
    assert "Nobody active can approve New starter requests; update the New starter approval group" in bell.text
    [waiting] = events(db_session, ticket, "approval_waiting")
    assert waiting.detail == "no active approvers in New starter"
    client.post("/auth/logout")
    login_as(client, admin.email)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Waiting: no active approvers in New starter" in page and "Waive approval" in page


def test_the_bell_is_once_per_group_until_it_has_an_active_member_again(client, db_session):
    admin, emails, kind, ticket = waiting_ticket(client, db_session)
    second = raised_again(client, db_session, kind, "kim@example.com")
    assert second.status == TicketStatus.AWAITING_APPROVAL and len(bells(db_session)) == 1  # same group, still told once
    assert len(events(db_session, second, "approval_waiting")) == 1  # but its own page still says so

    set_active(db_session, emails[0], True)
    send_approval_reminders(db_session)  # the safety net starts both rounds and forgets the mark
    db_session.commit()
    assert db_session.exec(select(Setting).where(Setting.key.like("approval_group_no_active_approvers:%"))).all() == []

    set_active(db_session, emails[0], False)
    third = raised_again(client, db_session, kind, "lee@example.com")
    assert third.status == TicketStatus.AWAITING_APPROVAL and len(bells(db_session)) == 2  # a new episode


def test_a_super_admin_can_waive_a_request_waiting_for_approvers(client, db_session):
    admin, _, _, ticket = waiting_ticket(client, db_session)
    client.post("/auth/logout")
    login_as(client, admin.email)
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 200
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["waived"]


def test_adding_an_active_member_starts_the_round_at_once(client, db_session):
    admin, emails, kind, ticket = waiting_ticket(client, db_session)
    person(client, db_session, "new@example.com")
    client.post("/auth/logout")
    login_as(client, admin.email)
    assert client.post(f"/admin/approval-groups/{kind.approval_group_id}/members", data={"email": "new@example.com"}, follow_redirects=False).status_code == 303

    db_session.expire_all()
    assert approvals(db_session, ticket) == {"new@example.com": PENDING}
    assert current_approval(db_session, ticket).voters == 1
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_reactivating_a_member_starts_the_round_at_once(client, db_session):
    admin, emails, _, ticket = waiting_ticket(client, db_session)
    user = db_session.exec(select(User).where(User.email == emails[0])).one()
    client.post("/auth/logout")
    login_as(client, admin.email)
    client.post(f"/admin/users/{user.id}/active", data={"is_active": "true"}, follow_redirects=False)

    assert approvals(db_session, ticket) == {emails[0]: PENDING}  # the voters are the active members only


def test_the_reminder_job_starts_a_waiting_round_even_with_reminders_off(client, db_session):
    _, emails, _, ticket = waiting_ticket(client, db_session)
    set_active(db_session, emails[1], True)
    set_setting(db_session, APPROVAL_REMINDERS_ENABLED, "off")
    db_session.commit()
    send_approval_reminders(db_session)
    db_session.commit()
    assert approvals(db_session, ticket) == {emails[1]: PENDING}


def test_a_request_type_with_no_group_at_all_still_skips_approval(client, db_session):
    emails = many(client, db_session, ("a",))
    kind = request_type(db_session, emails)
    kind.approval_group_id = None
    db_session.add(kind)
    db_session.commit()
    ticket = raised_and_triaged(client, db_session, kind)
    assert ticket.status == TicketStatus.OPEN and db_session.exec(select(TicketApproval)).all() == [] and bells(db_session) == []


# ---- B. An approver becomes inactive mid-vote ----------------------------------------------------------


def three_voters(client, db_session, pass_percent=100):
    admin = super_admin(client, db_session)
    emails = many(client, db_session, ("a", "b", "c"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, pass_percent))
    return admin, emails, ticket


def test_a_dropped_vote_is_marked_not_needed_with_an_event_and_an_audit_entry_and_a_cast_vote_still_counts(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    answer(client, ticket, a, "approved")
    deactivate_in_admin(client, db_session, admin, c)

    assert approvals(db_session, ticket) == {a: APPROVED, b: PENDING, c: NOT_NEEDED}
    approval = current_approval(db_session, ticket)
    assert approval.voters == 2 and approval.status == PENDING  # 2 of 2 must approve; not decided yet
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    [event] = events(db_session, ticket, "approval_vote_removed")
    assert event.detail == "C's vote removed (account inactive)" and event.to_value == c
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "approval_vote_removed")).all()


def test_dropping_a_vote_can_decide_the_request_as_approved(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    answer(client, ticket, a, "approved")
    deactivate_in_admin(client, db_session, admin, b)
    deactivate_in_admin(client, db_session, admin, c)

    assert status(db_session, ticket) == TicketStatus.OPEN  # 1 of 1 now
    assert current_approval(db_session, ticket).status == APPROVED
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["approved"]


def test_dropping_a_vote_can_decide_the_request_as_declined(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session, pass_percent=60)
    answer(client, ticket, a, "declined")  # 3 voters need 2: one decline leaves it open
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    deactivate_in_admin(client, db_session, admin, c)  # now 2 voters need 2, and a declined

    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert current_approval(db_session, ticket).status == DECLINED


def test_a_request_that_passes_after_a_drop_still_makes_its_subrequests(client, db_session, monkeypatch):
    calls = []
    import app.subrequests as subrequests

    monkeypatch.setattr(subrequests, "after_approval", lambda session, ticket: calls.append(ticket.id))
    admin, (a, b, c), ticket = three_voters(client, db_session)
    answer(client, ticket, a, "approved")
    deactivate_in_admin(client, db_session, admin, b)
    deactivate_in_admin(client, db_session, admin, c)
    assert calls == [ticket.id]


def test_with_no_voters_left_the_round_ends_and_the_request_waits_and_the_super_admins_are_told(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    for email in (a, b, c):
        deactivate_in_admin(client, db_session, admin, email)

    approval = current_approval(db_session, ticket)
    assert approval.status == SUPERSEDED and approval.voters == 0
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL and is_waiting_for_approvers(db_session, ticket)
    assert len(bells(db_session)) == 1 and bells(db_session)[0].ticket_id == ticket.id
    client.post("/auth/logout")
    login_as(client, admin.email)
    assert "Waiting: no active approvers in New starter" in client.get(f"/tickets/{ticket.number}").text
    # reactivating one of them starts a fresh round
    user = db_session.exec(select(User).where(User.email == a)).one()
    client.post(f"/admin/users/{user.id}/active", data={"is_active": "true"}, follow_redirects=False)
    assert approvals(db_session, ticket) == {a: PENDING}


def test_the_nightly_entra_check_drops_the_vote_too(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session, pass_percent=50)
    person_b = db_session.exec(select(User).where(User.email == b)).one()
    entra_check.deactivate(db_session, person_b, "account disabled in Entra")
    db_session.commit()

    assert approvals(db_session, ticket)[b] == NOT_NEEDED
    assert current_approval(db_session, ticket).voters == 2


def test_an_inactive_voter_cannot_answer_even_from_an_old_link(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    vote = db_session.exec(select(ApprovalVote).join(User, User.id == ApprovalVote.voter_id).where(User.email == a)).one()
    set_active(db_session, a, False)  # a switch that did not go through the admin page or the check
    try:
        decide(db_session, vote, APPROVED, "")
        raised = False
    except ApprovalError:
        raised = True
    db_session.rollback()
    assert raised


def test_reminders_are_not_sent_to_inactive_voters_and_their_votes_are_dropped(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    set_setting(db_session, APPROVAL_REMINDERS_ENABLED, "on")
    db_session.commit()
    set_active(db_session, b, False)  # not through the admin page: the job is the safety net
    send_approval_reminders(db_session, datetime.now(timezone.utc) + timedelta(days=30))
    db_session.commit()

    assert approvals(db_session, ticket)[b] == NOT_NEEDED and current_approval(db_session, ticket).voters == 2
    from app.models import OutboundEmail

    sent_to = {r.to_address for r in db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "approval_reminder")).all()}
    assert b not in sent_to


def test_dropping_votes_of_someone_with_nothing_pending_does_nothing(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    answer(client, ticket, a, "approved")
    user = set_active(db_session, a, False)
    assert drop_votes_of(db_session, user) == 0
    assert approvals(db_session, ticket)[a] == APPROVED and current_approval(db_session, ticket).voters == 3


def test_the_requester_can_withdraw_a_request_that_waits_for_approvers_and_sees_no_rule(client, db_session):
    admin, _, _, ticket = waiting_ticket(client, db_session)
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert "Withdraw" in page and "must approve" not in page

    assert client.post(f"/portal/{ticket.number}/withdraw", follow_redirects=False).status_code == 303
    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert [e.actor_email for e in events(db_session, ticket, "approval_withdrawn")] == ["jo@example.com"]


def test_a_superseded_round_shows_no_rule_to_the_requester(client, db_session):
    admin, (a, b, c), ticket = three_voters(client, db_session)
    for email in (a, b, c):
        deactivate_in_admin(client, db_session, admin, email)
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert "0 of 0" not in client.get(f"/portal/{ticket.number}").text
    login_as(client, admin.email)
    assert "0 of 0" not in client.get(f"/tickets/{ticket.number}").text

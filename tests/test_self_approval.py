"""#167: the per-request-type switch that lets an approver approve their own request."""
from sqlmodel import select

from app.approvals import APPROVED, NOT_NEEDED, PENDING, SELF_APPROVED_COMMENT, current_approval, eligible_voters
from app.models import ApprovalGroup, ApprovalVote, AuditLog, Notification, RequestType, Ticket, TicketStatus
from tests.conftest import login_as
from tests.test_approval_inactive import set_active, super_admin
from tests.test_approvals import answer, approvals, events, many, person, raised_and_triaged, request_type, second_agent, status
from tests.test_request_types import form
from tests.test_withdraw import withdraw_as

REQUESTER = "jo@example.com"


def self_type(db_session, others, pass_percent, in_group=True, on=True):
    """A request type whose group is the requester Jo (when in_group) plus the others, with the switch set."""
    members = ([REQUESTER] if in_group else []) + others
    kind = request_type(db_session, members, pass_percent)
    kind.approvers_may_self_approve = on
    db_session.add(kind)
    db_session.commit()
    return kind


def save_type(client, db_session, admin, kind, **extra):
    client.post("/auth/logout")
    login_as(client, admin.email)
    group = db_session.get(ApprovalGroup, kind.approval_group_id)
    data = {"name": kind.name, "description": "", "is_active": "on", **form(group, pass_percent=kind.pass_percent), **extra}
    response = client.post(f"/admin/request-types/{kind.id}", data=data, follow_redirects=False)
    db_session.expire_all()
    return response


def test_off_by_default_a_sole_approver_who_is_the_requester_waits(client, db_session):
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, [], 100, on=False))
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    assert current_approval(db_session, ticket) is None  # #161: waits for approvers
    assert events(db_session, ticket, "approval_self_approved") == []


def test_off_with_others_in_the_group_the_requester_is_not_a_voter(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 100, on=False))
    assert REQUESTER not in approvals(db_session, ticket)


def test_on_with_a_sole_approver_approves_at_once_and_makes_sub_requests(client, db_session, monkeypatch):
    calls = []
    import app.subrequests as subrequests

    monkeypatch.setattr(subrequests, "after_approval", lambda session, ticket: calls.append(ticket.id))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, [], 100))
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert calls == [ticket.id]
    approval = current_approval(db_session, ticket)
    assert approval.status == APPROVED and approval.voters == 1
    vote = db_session.exec(select(ApprovalVote).where(ApprovalVote.approval_id == approval.id)).one()
    assert (vote.decision, vote.comment) == (APPROVED, SELF_APPROVED_COMMENT) and vote.decided_at is not None
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_waiting")).all() == []


def test_the_event_and_audit_entry_name_the_request_type(client, db_session):
    person(client, db_session, REQUESTER)
    kind = self_type(db_session, [], 100)
    ticket = raised_and_triaged(client, db_session, kind)
    [event] = events(db_session, ticket, "approval_self_approved")
    assert event.actor_email == REQUESTER and event.to_value == APPROVED
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "approval_self_approved")).one()
    assert audit.actor_email == REQUESTER and audit.detail == kind.name and audit.entity_id == ticket.id


def test_on_with_three_approvers_at_60_percent_still_needs_one_more(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 60))
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    assert approvals(db_session, ticket) == {REQUESTER: APPROVED, emails[0]: PENDING, emails[1]: PENDING}
    assert len(db_session.exec(select(Notification).where(Notification.kind == "approval_waiting")).all()) == 2
    answer(client, ticket, emails[0], "approved")
    assert status(db_session, ticket) == TicketStatus.OPEN


def test_on_with_three_approvers_at_30_percent_is_approved_at_once(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 30))
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert approvals(db_session, ticket) == {REQUESTER: APPROVED, emails[0]: NOT_NEEDED, emails[1]: NOT_NEEDED}
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_waiting")).all() == []


def test_the_self_vote_cannot_be_changed_and_the_others_can_still_decline(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 100))
    answer(client, ticket, REQUESTER, "declined")
    assert approvals(db_session, ticket)[REQUESTER] == APPROVED
    answer(client, ticket, emails[0], "declined")
    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_the_requester_can_still_withdraw_while_the_others_have_not_answered(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 100))
    client.post("/auth/logout")
    login_as(client, REQUESTER)
    assert withdraw_as(client, ticket).status_code == 303
    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert approvals(db_session, ticket)[REQUESTER] == APPROVED  # their own vote is not rewritten


def test_on_but_the_requester_is_not_in_the_group_nothing_changes(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 50, in_group=False))
    assert approvals(db_session, ticket) == {emails[0]: PENDING, emails[1]: PENDING}
    assert events(db_session, ticket, "approval_self_approved") == []


def test_an_inactive_requester_is_never_a_voter(client, db_session):
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, ["x@example.com"], 50))
    set_active(db_session, REQUESTER, False)
    db_session.expire_all()
    assert REQUESTER not in [u.email for u in eligible_voters(db_session, db_session.get(Ticket, ticket.id))]


def test_a_restart_after_reopen_self_approves_again(client, db_session):
    emails = many(client, db_session, ("a",))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 100))
    answer(client, ticket, emails[0], "declined")
    assert status(db_session, ticket) == TicketStatus.CLOSED
    second_agent(client, db_session)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "open"})
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    assert approvals(db_session, ticket) == {REQUESTER: APPROVED, emails[0]: PENDING}
    assert len(events(db_session, ticket, "approval_self_approved")) == 2


def test_a_waiting_request_self_approves_when_the_switch_is_turned_on(client, db_session):
    admin = super_admin(client, db_session)
    person(client, db_session, REQUESTER)
    kind = self_type(db_session, [], 100, on=False)
    ticket = raised_and_triaged(client, db_session, kind)
    assert current_approval(db_session, ticket) is None
    assert save_type(client, db_session, admin, kind, approvers_may_self_approve="on").status_code == 303
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert current_approval(db_session, ticket).status == APPROVED


def test_the_admin_form_sets_the_switch_with_an_audit_entry(client, db_session):
    admin = super_admin(client, db_session)
    kind = self_type(db_session, ["x@example.com"], 50, on=False)
    assert save_type(client, db_session, admin, kind).status_code == 303
    assert db_session.get(RequestType, kind.id).approvers_may_self_approve is False
    assert not any("their own" in (a.detail or "") for a in db_session.exec(select(AuditLog)).all())
    assert save_type(client, db_session, admin, kind, approvers_may_self_approve="on").status_code == 303
    assert db_session.get(RequestType, kind.id).approvers_may_self_approve is True
    assert any("approvers may approve their own requests False -> True" in (a.detail or "") for a in db_session.exec(select(AuditLog)).all())
    assert "approvers_may_self_approve" in client.get(f"/admin/request-types?type={kind.id}").text


def test_the_portal_and_the_ticket_page_show_the_self_approval(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    person(client, db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, emails, 100))
    client.post("/auth/logout")
    login_as(client, REQUESTER)
    assert "Your own approval was recorded automatically" in client.get(f"/portal/{ticket.number}").text
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    assert SELF_APPROVED_COMMENT in client.get(f"/tickets/{ticket.number}").text


def test_a_request_not_raised_by_the_requester_in_the_portal_is_asked_as_usual(client, db_session):
    """An agent raising for an approver, or a mail from their address, must not approve in their name."""
    from app.models import TicketSource

    person(client, db_session, REQUESTER)
    kind = self_type(db_session, ["x@example.com"], 50)
    ticket = raised_and_triaged(client, db_session, kind)  # a portal request: self-approved
    assert events(db_session, ticket, "approval_self_approved")
    for source in (TicketSource.AGENT, TicketSource.EMAIL, TicketSource.TEAMS):
        ticket = db_session.get(Ticket, ticket.id)
        ticket.source = source
        db_session.add(ticket)
        db_session.flush()
        assert REQUESTER not in [u.email for u in eligible_voters(db_session, ticket)]
    db_session.rollback()

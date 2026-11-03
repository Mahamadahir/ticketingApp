"""4C-2: the requester withdraws a request that is waiting for approval."""
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from app import mail, outbox
from app.approvals import APPROVED, NOT_NEEDED, PENDING, WITHDRAWN, ApprovalError, current_approval, record_decision, withdraw
from app.database import engine
from app.models import ApprovalVote, AuditLog, Notification, Team, Ticket, TicketApproval, TicketStatus, User, UserRole
from app.teams import Unsendable
from tests.conftest import login_as, set_role
from tests.test_approvals import answer, approvals, events, link_for, many, person, raised_and_triaged, request_type, status
from tests.test_outbound_mail import dev_mail, opt_out_of_approval_emails, rows  # noqa: F401  (dev_mail is a fixture)


def withdraw_as(client, ticket, email="jo@example.com"):
    client.post("/auth/logout")
    login_as(client, email, "Jo Bloggs")
    return client.post(f"/portal/{ticket.number}/withdraw", follow_redirects=False)


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def mark_approval_emails_sent(db_session):
    """As the mail job does once the approval emails have gone."""
    for row in rows(db_session, outbox.APPROVAL):
        row.sent_at = datetime.now(timezone.utc)
        db_session.add(row)
    db_session.commit()


def vote_id_of(db_session, email):
    return db_session.exec(
        select(ApprovalVote).join(User, User.id == ApprovalVote.voter_id).where(User.email == email)
    ).one().id


def test_the_requester_withdraws_while_it_waits(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert "Withdraw this request" in client.get(f"/portal/{ticket.number}").text

    assert withdraw_as(client, ticket).status_code == 303

    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert current_approval(db_session, ticket).status == WITHDRAWN
    assert set(approvals(db_session, ticket).values()) == {NOT_NEEDED}
    [event] = events(db_session, ticket, "approval_withdrawn")
    assert event.actor_email == "jo@example.com"
    closed = [e for e in events(db_session, ticket, "status_changed") if e.to_value == "closed"]
    assert closed[-1].detail == "withdrawn"
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "approval_withdrawn")).one()
    assert audit.actor_email == "jo@example.com"
    assert "Withdraw this request" not in client.get(f"/portal/{ticket.number}").text


def test_nobody_but_the_requester_can_withdraw(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    person(client, db_session, "other@example.com")
    for who in ("a@example.com", "other@example.com"):  # an approver, a stranger
        assert withdraw_as(client, ticket, who).status_code == 404
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    with pytest.raises(ApprovalError, match="Only the person who made the request"):
        withdraw(db_session, fresh(db_session, ticket), "a@example.com")
    db_session.rollback()


def test_a_follower_cannot_withdraw(client, db_session):
    from app.followers import add_follower

    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    person(client, db_session, "friend@example.com")
    add_follower(db_session, fresh(db_session, ticket), "friend@example.com", "jo@example.com")
    db_session.commit()
    assert withdraw_as(client, ticket, "friend@example.com").status_code == 404
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_not_after_an_approval(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 50))
    assert answer(client, ticket, "a@example.com", "approved").status_code == 200
    assert status(db_session, ticket) == TicketStatus.OPEN

    assert withdraw_as(client, ticket).status_code == 303
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert current_approval(db_session, ticket).status == APPROVED
    assert events(db_session, ticket, "approval_withdrawn") == []
    with pytest.raises(ApprovalError, match="no longer waiting"):
        withdraw(db_session, fresh(db_session, ticket), "jo@example.com")
    db_session.rollback()


def test_not_after_a_decline(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    answer(client, ticket, "a@example.com", "declined")
    assert status(db_session, ticket) == TicketStatus.CLOSED
    withdraw_as(client, ticket)
    assert current_approval(db_session, ticket).status == "declined"
    assert events(db_session, ticket, "approval_withdrawn") == []


def test_withdrawing_twice_changes_nothing_the_second_time(client, db_session):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    withdraw_as(client, ticket)
    withdraw_as(client, ticket)
    assert len(events(db_session, ticket, "approval_withdrawn")) == 1


def test_a_voter_who_had_answered_keeps_their_vote(client, db_session):
    emails = many(client, db_session, ("a", "b", "c"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    answer(client, ticket, "a@example.com", "approved")
    withdraw_as(client, ticket)
    assert approvals(db_session, ticket) == {"a@example.com": APPROVED, "b@example.com": NOT_NEEDED, "c@example.com": NOT_NEEDED}


def test_the_emailed_link_no_longer_decides_anything(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    url = link_for(db_session, ticket, "a@example.com")
    withdraw_as(client, ticket)
    client.post("/auth/logout")

    page = client.get(url)
    assert page.status_code == 200 and "withdrawn this request" in page.text
    assert 'value="approved"' not in page.text
    refused = client.post(url, data={"decision": "approved", "comment": ""})
    assert "withdrawn by the requester" in refused.text
    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert current_approval(db_session, ticket).status == WITHDRAWN
    assert set(approvals(db_session, ticket).values()) == {NOT_NEEDED}
    assert events(db_session, ticket, "approval_decided") == []


def test_the_portal_answer_is_refused_too(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    withdraw_as(client, ticket)
    assert answer(client, ticket, "a@example.com", "approved").status_code == 400
    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_the_request_leaves_the_approvers_waiting_list(client, db_session):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    client.post("/auth/logout")
    login_as(client, "a@example.com")
    assert "Waiting for your approval (1)" in client.get("/portal").text
    withdraw_as(client, ticket)
    client.post("/auth/logout")
    login_as(client, "a@example.com")
    assert "Nothing is waiting" in client.get("/portal/approvals").text


def test_unanswered_approvers_are_emailed_per_preference_and_answered_ones_are_not(client, db_session, dev_mail):
    emails = many(client, db_session, ("a", "b", "c"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    opt_out_of_approval_emails(db_session, "c@example.com")
    mark_approval_emails_sent(db_session)
    answer(client, ticket, "a@example.com", "approved")
    withdraw_as(client, ticket)

    sent = rows(db_session, outbox.APPROVAL_WITHDRAWN)
    assert [r.to_address for r in sent] == ["b@example.com"]
    built = mail.build_approval_withdrawn_mail(db_session, sent[0])
    assert built.subject == f"[{ticket.reference}] Withdrawn: {ticket.subject}"
    assert "jo@example.com has withdrawn" in built.body and "http" not in built.body


def test_an_approver_whose_approval_email_never_went_is_not_told_of_a_withdrawal(client, db_session, dev_mail):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    withdraw_as(client, ticket)  # the approval email is still waiting in the queue
    assert rows(db_session, outbox.APPROVAL_WITHDRAWN) == []


def test_the_withdrawn_email_asks_again_when_it_goes(client, db_session, dev_mail):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    [asked] = rows(db_session, outbox.APPROVAL)
    not_yet = type(asked)(kind=outbox.APPROVAL_WITHDRAWN, ticket_id=ticket.id, vote_id=asked.vote_id, to_address="a@example.com")
    with pytest.raises(Unsendable, match="was not withdrawn"):
        mail.build_approval_withdrawn_mail(db_session, not_yet)
    mark_approval_emails_sent(db_session)
    withdraw_as(client, ticket)
    [row] = rows(db_session, outbox.APPROVAL_WITHDRAWN)
    opt_out_of_approval_emails(db_session, "a@example.com")
    with pytest.raises(Unsendable, match="turned approval emails off"):
        mail.build_approval_withdrawn_mail(db_session, row)


def test_the_original_approval_email_is_dropped_once_withdrawn(client, db_session, dev_mail):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    [row] = rows(db_session, outbox.APPROVAL)
    withdraw_as(client, ticket)
    with pytest.raises(Unsendable, match="no longer needs"):
        mail.build_approval_mail(db_session, row)


def test_an_agent_approver_who_can_open_the_ticket_gets_a_bell(client, db_session):
    person(client, db_session, "approver@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["approver@example.com"]))
    approver = db_session.exec(select(User).where(User.email == "approver@example.com")).one()
    set_role(db_session, approver.id, UserRole.AGENT, db_session.exec(select(Team)).first().id)
    withdraw_as(client, ticket)
    told = db_session.exec(select(Notification).where(Notification.kind == "approval_withdrawn")).all()
    assert [n.user_id for n in told] == [approver.id]
    assert "withdrawn" in told[0].text and told[0].ticket_id == ticket.id


def test_an_agent_who_switched_approval_notifications_off_gets_no_bell(client, db_session):
    person(client, db_session, "approver@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["approver@example.com"]))
    approver = db_session.exec(select(User).where(User.email == "approver@example.com")).one()
    set_role(db_session, approver.id, UserRole.AGENT, db_session.exec(select(Team)).first().id)
    opt_out_of_approval_emails(db_session, "approver@example.com")
    withdraw_as(client, ticket)
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_withdrawn")).all() == []


def test_an_end_user_approver_has_no_bell(client, db_session):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    withdraw_as(client, ticket)
    assert db_session.exec(select(Notification).where(Notification.kind == "approval_withdrawn")).all() == []


def test_a_withdrawal_racing_the_final_vote_gives_exactly_one_outcome(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 50))
    vote_id = vote_id_of(db_session, "a@example.com")
    outcome = {}

    holder = Session(engine)  # the withdrawal, holding its locks without having committed yet
    withdraw(holder, holder.get(Ticket, ticket.id), "jo@example.com")

    def vote():
        try:
            with Session(engine) as session:
                outcome["vote"] = record_decision(session, session.get(ApprovalVote, vote_id), APPROVED, "")
                session.commit()
        except ApprovalError as error:
            outcome["vote"] = str(error)

    thread = threading.Thread(target=vote)
    thread.start()
    time.sleep(1)
    assert thread.is_alive()  # the vote waits for the withdrawal rather than counting beside it
    holder.commit()
    holder.close()
    thread.join(timeout=30)

    assert "withdrawn" in outcome["vote"]
    assert status(db_session, ticket) == TicketStatus.CLOSED
    assert current_approval(db_session, ticket).status == WITHDRAWN
    assert events(db_session, ticket, "approval_outcome") == []


def test_a_final_vote_landing_first_makes_the_withdrawal_refuse(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 50))
    vote_id = vote_id_of(db_session, "a@example.com")
    holder = Session(engine)
    assert record_decision(holder, holder.get(ApprovalVote, vote_id), APPROVED, "") == APPROVED
    result = {}

    def take_back():
        with Session(engine) as session:
            try:
                withdraw(session, session.get(Ticket, ticket.id), "jo@example.com")
                session.commit()
                result["w"] = "withdrawn"
            except ApprovalError as error:
                result["w"] = str(error)

    thread = threading.Thread(target=take_back)
    thread.start()
    time.sleep(1)
    assert thread.is_alive()
    holder.commit()
    holder.close()
    thread.join(timeout=30)

    assert "no longer waiting" in result["w"]
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert current_approval(db_session, ticket).status == APPROVED


def test_a_vote_decided_since_the_session_loaded_it_is_not_overwritten(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    session = Session(engine)
    loaded = session.get(Ticket, ticket.id)
    approval = current_approval(session, loaded)
    from app.approvals import votes_of

    held = votes_of(session, approval)  # this session now holds both votes as pending
    answer(client, ticket, "a@example.com", "approved")  # committed by another session meanwhile
    assert [vote.decision for vote, _ in held] == [PENDING, PENDING]  # and has not noticed
    withdraw(session, loaded, "jo@example.com")
    session.commit()
    session.close()
    assert approvals(db_session, ticket)["a@example.com"] == APPROVED


def test_a_withdrawn_approval_frees_the_one_pending_slot(client, db_session):
    emails = many(client, db_session, ("a",))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    withdraw_as(client, ticket)
    assert db_session.exec(select(TicketApproval).where(TicketApproval.status == PENDING)).all() == []

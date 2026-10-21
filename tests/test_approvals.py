import threading
import time

import pytest
from sqlmodel import Session, select

from app import approval_links
from app.approval_groups import add_member, create_group
from app.approvals import (
    APPROVED,
    DECLINED,
    NOT_NEEDED,
    PENDING,
    WAIVED,
    ApprovalError,
    PercentError,
    check_percentages,
    current_approval,
    decide,
    describe_rule,
    outcome,
    votes_needed,
    voters_yet_to_vote,
)
from app.database import engine
from app.models import (
    ApprovalGroup,
    ApprovalGroupMember,
    ApprovalVote,
    AuditLog,
    RequestType,
    Ticket,
    TicketApproval,
    TicketEvent,
    TicketStatus,
    User,
    UserRole,
)
from app.request_types import create_request_type
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_raising import desk, raise_it
from tests.test_splitting import split


def person(client, db_session, email, name=None):
    client.post("/auth/logout")
    login_as(client, email, name or email.split("@")[0].title())
    client.post("/auth/logout")


def request_type(db_session, approvers, pass_percent=50, fail_percent=51, name="New starter"):
    """A request type whose group (named like it) has these members. The defaults let one approval pass
    the request (with two voters) and need everyone to decline; tests that care pass their own numbers."""
    group = create_group(db_session, name)
    for email in approvers or ["nobody-yet@example.com"]:
        if db_session.exec(select(User).where(User.email == email)).first() is None:
            db_session.add(User(email=email, display_name=email))
            db_session.flush()
        add_member(db_session, group, email)
    created = create_request_type(db_session, name, "", group.id, pass_percent, fail_percent)
    if not approvers:  # an active type whose group has since been emptied (saving one that way is refused)
        for row in db_session.exec(select(ApprovalGroupMember)).all():
            db_session.delete(row)
    db_session.commit()
    db_session.refresh(created)
    return created


def raised_and_triaged(client, db_session, kind, requester="jo@example.com"):
    """An end user raises a catalogue request and an agent triages it. Returns the ticket."""
    client.post("/auth/logout")
    login_as(client, requester, "Jo Bloggs")
    assert raise_it(client, subject="Starter on Monday", request_type_id=kind.id).status_code == 204
    ticket = db_session.exec(select(Ticket).order_by(Ticket.id.desc())).first()
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)
    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def approvals(db_session, ticket):
    """Each voter's decision on the ticket's current approval, by email."""
    db_session.expire_all()
    approval = current_approval(db_session, ticket)
    rows = db_session.exec(select(ApprovalVote).where(ApprovalVote.approval_id == approval.id).order_by(ApprovalVote.id)).all()
    return {db_session.get(User, r.voter_id).email: r.decision for r in rows}


def status(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id).status


def second_agent(client, db_session, email="agent2@example.com"):
    """Another agent in the desk's team (the team already exists once a ticket was triaged)."""
    from app.models import Team

    client.post("/auth/logout")
    team = db_session.exec(select(Team)).first()
    set_role(db_session, login_as(client, email, "Second Agent"), UserRole.AGENT, team.id)


def answer(client, ticket, email, decision, comment=""):
    client.post("/auth/logout")
    login_as(client, email)
    return client.post(f"/portal/{ticket.number}/approval", data={"decision": decision, "comment": comment})


def events(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def many(client, db_session, names=("a", "b", "c")):
    emails = [f"{n}@example.com" for n in names]
    for email in emails:
        person(client, db_session, email)
    return emails


# ---- Starting ---------------------------------------------------------------------------------------


def test_triaging_a_catalogue_request_with_a_group_waits_for_its_members(client, db_session):
    for email in ("boss@example.com", "lead@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["boss@example.com", "lead@example.com"])

    ticket = raised_and_triaged(client, db_session, kind)

    assert ticket.status == TicketStatus.AWAITING_APPROVAL and ticket.triaged_at is not None
    assert approvals(db_session, ticket) == {"boss@example.com": PENDING, "lead@example.com": PENDING}
    approval = current_approval(db_session, ticket)
    assert (approval.voters, approval.pass_percent, approval.fail_percent, approval.status) == (2, 50, 51, PENDING)
    assert approval.group_id == kind.approval_group_id
    [asked] = events(db_session, ticket, "approval_requested")
    assert asked.to_value == "boss@example.com, lead@example.com"
    assert asked.detail == "1 of 2 must approve (50%), 2 must decline (51%)"
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Waiting for approval</b> (1 of 2 must approve (50%), 2 must decline (51%))" in page
    assert "Approvals: pending" in page


def test_a_request_type_with_an_empty_group_opens_as_usual(client, db_session):
    kind = request_type(db_session, [])
    ticket = raised_and_triaged(client, db_session, kind)
    assert ticket.status == TicketStatus.OPEN and db_session.exec(select(TicketApproval)).all() == []


def test_the_requester_never_votes_on_their_own_request(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "jo@example.com")
    kind = request_type(db_session, ["boss@example.com", "jo@example.com"], 100, 1)

    ticket = raised_and_triaged(client, db_session, kind, requester="jo@example.com")

    assert approvals(db_session, ticket) == {"boss@example.com": PENDING}  # Jo left out
    assert current_approval(db_session, ticket).voters == 1  # and the percentages count only the others
    assert answer(client, ticket, "jo@example.com", "approved").status_code == 404


def test_when_the_only_member_is_the_requester_no_approval_is_needed(client, db_session):
    person(client, db_session, "jo@example.com")
    kind = request_type(db_session, ["jo@example.com"])
    ticket = raised_and_triaged(client, db_session, kind, requester="jo@example.com")
    assert ticket.status == TicketStatus.OPEN and db_session.exec(select(TicketApproval)).all() == []


def test_a_requester_vote_row_cannot_be_decided_even_if_one_existed(client, db_session):
    """Belt and braces: the service itself refuses the requester, whatever created the row."""
    person(client, db_session, "boss@example.com")
    person(client, db_session, "jo@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    approval = current_approval(db_session, ticket)
    jo = db_session.exec(select(User).where(User.email == "jo@example.com")).one()
    add_member(db_session, db_session.get(ApprovalGroup, approval.group_id), "jo@example.com")
    vote = ApprovalVote(approval_id=approval.id, voter_id=jo.id)
    db_session.add(vote)
    db_session.commit()

    with pytest.raises(ApprovalError, match="your own request"):
        decide(db_session, vote, APPROVED, "")


def test_an_agent_raising_and_classifying_a_request_also_waits(client, db_session):
    person(client, db_session, "boss@example.com")
    kind = request_type(db_session, ["boss@example.com"])
    team, category, _ = desk(client, db_session)

    raise_it(
        client, subject="Starter", for_email="walkin@example.com", request_type_id=kind.id,
        category_id=category.id, impact="low", urgency="low",
    )

    ticket = db_session.exec(select(Ticket)).one()
    assert ticket.status == TicketStatus.AWAITING_APPROVAL
    assert raise_it(
        client, subject="Already done", for_email="walkin@example.com", request_type_id=kind.id,
        category_id=category.id, impact="low", urgency="low", resolved_at="2026-10-14T09:00",
    ).status_code == 400  # a request that needs approval cannot be logged as already resolved


def test_retyped_away_at_triage_needs_no_approval(client, db_session):
    person(client, db_session, "boss@example.com")
    kind = request_type(db_session, ["boss@example.com"])
    login_as(client, "jo@example.com")
    raise_it(client, request_type_id=kind.id)
    ticket = db_session.exec(select(Ticket)).one()
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)

    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": category.id},
    )

    assert status(db_session, ticket) == TicketStatus.OPEN and db_session.exec(select(TicketApproval)).all() == []


def test_there_is_only_one_pending_approval_per_ticket(client, db_session):
    from sqlalchemy.exc import IntegrityError

    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    approval = current_approval(db_session, ticket)
    db_session.add(TicketApproval(
        ticket_id=ticket.id, group_id=approval.group_id, voters=1, pass_percent=50, fail_percent=51,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---- The percentages --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "approved,declined,voters,pass_percent,fail_percent,expected",
    [
        (0, 0, 3, 51, 50, None),
        (1, 0, 1, 51, 50, APPROVED),
        (2, 0, 3, 51, 50, APPROVED),  # 66.7% >= 51%
        (1, 1, 3, 51, 50, None),
        (1, 2, 3, 51, 50, DECLINED),
        (1, 1, 2, 51, 50, DECLINED),  # 50% decline reaches 50%; the 50% approve does not reach 51%
        (1, 0, 2, 51, 50, None),  # 50% < 51%: half is not a majority
        (1, 0, 2, 50, 51, APPROVED),  # exactly 50% reaches a 50% threshold
        (0, 1, 2, 50, 51, None),
        (0, 2, 2, 50, 51, DECLINED),
        (3, 0, 4, 75, 26, APPROVED),  # 75% exactly
        (2, 0, 4, 75, 26, None),
        (1, 0, 4, 75, 26, None),  # 25% < 26%
        (1, 1, 4, 75, 26, None),  # 25% each: neither reaches its threshold
        # 100 / 1: everyone must approve, any decline closes it
        (2, 0, 3, 100, 1, None),
        (3, 0, 3, 100, 1, APPROVED),
        (2, 1, 3, 100, 1, DECLINED),
        # 1 / 100: any one approval passes, only everyone declining fails
        (1, 0, 5, 1, 100, APPROVED),
        (0, 4, 5, 1, 100, None),
        (0, 5, 5, 1, 100, DECLINED),
        # a decline wins when both are reached together (only possible if the numbers were not validated)
        (1, 1, 2, 50, 50, DECLINED),
        (2, 2, 4, 50, 50, DECLINED),
        (0, 0, 0, 50, 51, None),  # nobody to vote: never settles by itself
        (1, 1, 2, 57, 51, DECLINED),  # everyone answered, neither share reached (57/51 allows that): declined
        (2, 1, 3, 100, 100, DECLINED),  # 67% approve < 100% and 33% decline < 100%, all answered
        (1, 0, 2, 57, 51, None),  # still waiting for the second
    ],
)
def test_the_outcome_rule(approved, declined, voters, pass_percent, fail_percent, expected):
    assert outcome(approved, declined, voters, pass_percent, fail_percent) == expected


def test_a_complete_vote_always_has_an_outcome():
    for voters in range(1, 9):
        for pass_percent in range(1, 101, 7):
            for fail_percent in range(max(1, 101 - pass_percent), 101, 7):
                check_percentages(pass_percent, fail_percent)
                for approved in range(voters + 1):
                    assert outcome(approved, voters - approved, voters, pass_percent, fail_percent) is not None, (
                        voters, pass_percent, fail_percent, approved,
                    )


@pytest.mark.parametrize("pass_percent,fail_percent", [(50, 50), (1, 99), (100, 0), (0, 100), (101, 50), (30, 30), (-5, 120)])
def test_the_percentages_must_add_up_to_more_than_100(pass_percent, fail_percent):
    with pytest.raises(PercentError):
        check_percentages(pass_percent, fail_percent)


@pytest.mark.parametrize("pass_percent,fail_percent", [(51, 50), (50, 51), (100, 1), (1, 100), (100, 100)])
def test_valid_percentages_are_accepted(pass_percent, fail_percent):
    check_percentages(pass_percent, fail_percent)


def test_votes_needed_rounds_up():
    assert [votes_needed(51, n) for n in (1, 2, 3, 4)] == [1, 2, 2, 3]
    assert votes_needed(100, 3) == 3 and votes_needed(1, 100) == 1 and votes_needed(50, 2) == 1


# ---- The outcome end to end -------------------------------------------------------------------------


def test_one_approval_can_be_enough_and_opens_the_request(client, db_session):
    for email in ("boss@example.com", "lead@example.com"):
        person(client, db_session, email)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"]))

    response = answer(client, ticket, "boss@example.com", "approved", "Fine by me")

    assert response.status_code == 200
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert approvals(db_session, ticket) == {"boss@example.com": APPROVED, "lead@example.com": NOT_NEEDED}
    approval = current_approval(db_session, ticket)
    assert approval.status == APPROVED and approval.settled_at is not None
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["approved"]
    assert events(db_session, ticket, "approval_decided")[0].detail == "Fine by me"
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert "Your request has been approved" in client.get(f"/portal/{ticket.number}").text


def test_a_decline_that_reaches_the_fail_percent_closes_it_and_tells_the_requester_why(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "lead@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"], 51, 50))

    answer(client, ticket, "lead@example.com", "declined", "No budget")

    assert status(db_session, ticket) == TicketStatus.CLOSED
    db_session.refresh(ticket)
    assert ticket.resolved_at is not None
    assert approvals(db_session, ticket) == {"boss@example.com": NOT_NEEDED, "lead@example.com": DECLINED}
    assert current_approval(db_session, ticket).status == DECLINED
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert "was not approved" in page and "Reason: No budget" in page


def test_two_of_three_pass_a_simple_majority_and_the_third_is_not_needed(client, db_session):
    emails = many(client, db_session)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 51, 50))

    answer(client, ticket, "a@example.com", "approved")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    answer(client, ticket, "b@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.OPEN
    assert approvals(db_session, ticket)["c@example.com"] == NOT_NEEDED


def test_two_declines_of_three_close_it_at_fifty_percent(client, db_session):
    emails = many(client, db_session)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 51, 50))

    answer(client, ticket, "a@example.com", "declined")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # 33% < 50%
    answer(client, ticket, "b@example.com", "declined")

    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_a_pass_of_100_needs_everyone(client, db_session):
    emails = many(client, db_session)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 1))
    answer(client, ticket, "a@example.com", "approved")
    answer(client, ticket, "b@example.com", "approved")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # 67% < 100%
    answer(client, ticket, "c@example.com", "approved")
    assert status(db_session, ticket) == TicketStatus.OPEN



def test_a_fail_of_1_closes_on_the_first_decline(client, db_session):
    emails = many(client, db_session)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 1))
    answer(client, ticket, "a@example.com", "declined")
    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_fifty_and_fifty_one_split_a_pair(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 50, 51))
    answer(client, ticket, "a@example.com", "declined")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # 50% < 51%
    answer(client, ticket, "b@example.com", "approved")  # 50% reaches pass 50%
    assert status(db_session, ticket) == TicketStatus.OPEN


def test_a_tie_in_a_pair_is_a_decline_at_51_and_50(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 51, 50))
    answer(client, ticket, "a@example.com", "approved")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # 50% < 51%
    answer(client, ticket, "b@example.com", "declined")  # 50% reaches 50%
    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_the_voters_are_fixed_when_it_starts(client, db_session):
    emails = many(client, db_session, ("a", "b", "c", "d"))
    person(client, db_session, "late@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails[:2], 100, 1))
    approval = current_approval(db_session, ticket)
    group = db_session.get(ApprovalGroup, approval.group_id)
    for email in ("c@example.com", "d@example.com"):
        add_member(db_session, group, email)  # four members now, but the approval has two voters
    db_session.commit()

    answer(client, ticket, "a@example.com", "approved")
    answer(client, ticket, "b@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.OPEN  # 2 of 2, not 2 of 4
    assert db_session.get(TicketApproval, approval.id).voters == 2
    assert answer(client, ticket, "c@example.com", "approved").status_code == 404  # not a voter on this one


def test_the_rule_is_copied_when_it_starts_so_later_edits_do_not_change_it(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    kind = request_type(db_session, emails, 100, 1)
    ticket = raised_and_triaged(client, db_session, kind)
    kind.pass_percent, kind.fail_percent = 50, 100
    db_session.add(kind)
    db_session.commit()

    answer(client, ticket, "a@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # still needs both


# ---- Answering ---------------------------------------------------------------------------------------


def test_each_voter_answers_once_and_only_while_it_is_waiting(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))

    assert answer(client, ticket, "a@example.com", "approved").status_code == 200
    again = answer(client, ticket, "a@example.com", "declined")
    assert again.status_code == 400 and "already answered" in again.text
    assert approvals(db_session, ticket)["a@example.com"] == APPROVED

    assert answer(client, ticket, "b@example.com", "maybe").status_code == 400
    assert answer(client, ticket, "b@example.com", "approved", "x" * 1001).status_code == 400
    assert answer(client, ticket, "b@example.com", "approved").status_code == 200
    assert status(db_session, ticket) == TicketStatus.OPEN


def test_nobody_else_can_answer_a_request(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))

    assert answer(client, ticket, "jo@example.com", "approved").status_code == 404  # the requester
    assert answer(client, ticket, "stranger@example.com", "approved").status_code == 404
    second_agent(client, db_session)
    assert client.post(f"/portal/{ticket.number}/approval", data={"decision": "approved"}).status_code == 404  # an agent
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_a_decision_is_audited(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    answer(client, ticket, "boss@example.com", "approved", "ok")
    entry = db_session.exec(select(AuditLog).where(AuditLog.action == "approval_approved")).one()
    assert (entry.actor_email, entry.entity_id, entry.detail) == ("boss@example.com", ticket.id, "ok")


def test_deciding_a_ticket_that_is_no_longer_waiting_is_refused(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    vote = db_session.exec(select(ApprovalVote)).one()
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(ApprovalError):
        decide(db_session, vote, APPROVED, "")


def test_two_members_voting_at_the_same_moment_are_counted_once_each(client, db_session, monkeypatch):
    """Both vote at once on a 2-voter approval that needs both. Without the row lock each would count only
    its own vote (the other is uncommitted) and the request would wait for ever. The barrier makes the
    two meet at the count; with the lock the second one is held outside it, so the first gives up waiting."""
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    ids = {
        user.email: vote.id
        for vote, user in db_session.exec(select(ApprovalVote, User).join(User, User.id == ApprovalVote.voter_id)).all()
    }
    from app import approvals as approvals_module

    barrier = threading.Barrier(2)
    real_votes_of = approvals_module.votes_of

    def meeting(session, approval):
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return real_votes_of(session, approval)

    monkeypatch.setattr(approvals_module, "votes_of", meeting)
    results, errors = {}, []

    def vote(email):
        try:
            with Session(engine) as session:
                voted = session.get(ApprovalVote, ids[email])
                results[email] = approvals_module.record_decision(session, voted, APPROVED, "")
                session.commit()
        except Exception as error:  # reported below, not swallowed
            errors.append(error)

    threads = [threading.Thread(target=vote, args=(email,)) for email in emails]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(results.values(), key=str) == [None, APPROVED]  # exactly one of them settled it
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["approved"]
    assert sorted(approvals(db_session, ticket).values()) == [APPROVED, APPROVED]


# ---- Helpers for later issues -----------------------------------------------------------------------


def test_voters_yet_to_vote_lists_the_pending_members_of_a_pending_approval(client, db_session):
    emails = many(client, db_session)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    approval = current_approval(db_session, ticket)
    assert [u.email for _, u in voters_yet_to_vote(db_session, approval)] == emails

    answer(client, ticket, "b@example.com", "approved")
    db_session.expire_all()
    assert [u.email for _, u in voters_yet_to_vote(db_session, approval)] == ["a@example.com", "c@example.com"]

    answer(client, ticket, "a@example.com", "declined")  # 33% reaches the 2% to fail: settled
    db_session.expire_all()
    assert voters_yet_to_vote(db_session, db_session.get(TicketApproval, approval.id)) == []


def test_describe_rule_is_for_people(client, db_session):
    approval = TicketApproval(ticket_id=1, group_id=1, voters=3, pass_percent=67, fail_percent=34)
    assert describe_rule(approval) == "3 of 3 must approve (67%), 2 must decline (34%)"


# ---- The hold ----------------------------------------------------------------------------------------


def test_the_status_cannot_be_changed_by_hand_while_waiting(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    second_agent(client, db_session)

    for target in ("in_progress", "resolved", "closed", "awaiting_user", "open"):
        response = client.post(f"/tickets/{ticket.number}/status", data={"status": target})
        assert response.status_code == 400 and "waiting for its approvers" in response.text, target
    page = client.get(f"/tickets/{ticket.number}").text
    assert 'name="status" value="resolved"' not in page  # no buttons to press
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_agents_can_still_work_the_ticket_while_it_waits(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": "Chasing the boss"}).status_code == 200
    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "Waiting for approval"}).status_code == 200
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # emailing does not move it either


def test_a_request_waiting_for_approval_cannot_be_split(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    response = split(client, ticket, ("One", ""), ("Two", ""))
    assert response.status_code == 400 and "waiting for approval" in response.text


def test_the_clock_keeps_running_while_waiting(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert ticket.sla_paused_at is None and ticket.resolution_due_at is not None


def test_a_super_admin_can_waive_the_approval_and_agents_cannot(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 403  # the agent

    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    assert "Waive approval" in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 200

    assert status(db_session, ticket) == TicketStatus.OPEN
    assert set(approvals(db_session, ticket).values()) == {NOT_NEEDED}
    assert current_approval(db_session, ticket).status == WAIVED
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["waived"]
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 400  # nothing left to waive


# ---- The portal --------------------------------------------------------------------------------------


def test_a_member_sees_and_opens_what_waits_for_them_in_the_portal(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "other@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    client.post("/auth/logout")
    login_as(client, "boss@example.com")

    assert "Waiting for your approval (1)" in client.get("/portal").text
    waiting = client.get("/portal/approvals").text
    assert ticket.reference in waiting and "jo@example.com" in waiting
    page = client.get(f"/portal/{ticket.number}").text
    assert "Starter on Monday" in page and "one of its approvers" in page
    assert 'name="decision" value="approved"' in page and 'name="decision" value="declined"' in page

    client.post("/auth/logout")
    login_as(client, "other@example.com")
    assert client.get(f"/portal/{ticket.number}").status_code == 404  # not theirs to see
    assert "Nothing is waiting" in client.get("/portal/approvals").text


def test_waiting_for_your_approval_follows_group_membership(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    group = db_session.get(ApprovalGroup, current_approval(db_session, ticket).group_id)
    from app.approval_groups import remove_member

    leaver = db_session.exec(select(User).where(User.email == "a@example.com")).one()
    remove_member(db_session, group, leaver.id)
    db_session.commit()
    client.post("/auth/logout")
    login_as(client, "a@example.com")

    assert "Nothing is waiting" in client.get("/portal/approvals").text  # no longer in the group
    client.post("/auth/logout")
    login_as(client, "b@example.com")
    assert ticket.reference in client.get("/portal/approvals").text


def test_once_it_is_settled_the_voter_no_longer_reads_the_ticket(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    answer(client, ticket, "boss@example.com", "approved")

    assert client.get(f"/portal/{ticket.number}").status_code == 404  # their answer is no longer wanted
    assert "Nothing is waiting" in client.get("/portal/approvals").text
    assert "You approved this request" in client.get(link_for(db_session, ticket, "boss@example.com")).text  # the link page still tells them


# ---- The emailed link --------------------------------------------------------------------------------


def vote_of(db_session, ticket, email):
    return db_session.exec(
        select(ApprovalVote)
        .join(User, User.id == ApprovalVote.voter_id)
        .join(TicketApproval, TicketApproval.id == ApprovalVote.approval_id)
        .where(TicketApproval.ticket_id == ticket.id, User.email == email)
    ).one()


def link_for(db_session, ticket, email):
    return f"/approvals/{approval_links.make_token(vote_of(db_session, ticket, email).id)}"


def test_opening_the_link_shows_the_request_and_decides_nothing(client, db_session):
    person(client, db_session, "boss@example.com", "Big Boss")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    url = link_for(db_session, ticket, "boss@example.com")
    client.post("/auth/logout")  # the link needs no sign-in

    for _ in range(3):  # a mail scanner, a link preview, the person
        response = client.get(url)
        assert response.status_code == 200
    page = response.text
    assert ticket.reference in page and "Starter on Monday" in page and "Big Boss" in page
    assert 'method="post"' in page and 'value="approved"' in page and 'value="declined"' in page
    assert response.headers["cache-control"] == "no-store" and response.headers["referrer-policy"] == "no-referrer"
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    assert approvals(db_session, ticket) == {"boss@example.com": PENDING}


def test_answering_from_the_link_decides_and_shows_the_result(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    url = link_for(db_session, ticket, "boss@example.com")
    client.post("/auth/logout")

    response = client.post(url, data={"decision": "approved", "comment": "Go ahead"})

    assert response.status_code == 200 and "Thank you" in response.text and "You approved this request" in response.text
    assert status(db_session, ticket) == TicketStatus.OPEN
    entry = db_session.exec(select(AuditLog).where(AuditLog.action == "approval_approved")).one()
    assert entry.actor_email == "boss@example.com"
    again = client.post(url, data={"decision": "declined"})
    assert "already answered" in again.text and status(db_session, ticket) == TicketStatus.OPEN
    assert "You approved this request" in client.get(url).text


def test_a_bad_or_expired_link_is_a_404_page_that_points_to_the_portal(client, db_session, monkeypatch):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    url = link_for(db_session, ticket, "boss@example.com")
    client.post("/auth/logout")

    for bad in ("/approvals/garbage", url[:-3] + "xyz", url + "x"):
        response = client.get(bad)
        assert response.status_code == 404 and "/portal/approvals" in response.text, bad
    assert client.post("/approvals/garbage", data={"decision": "approved"}).status_code == 404

    real = time.time()
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: real - 30 * 24 * 3600)
    old = f"/approvals/{approval_links.make_token(vote_of(db_session, ticket, 'boss@example.com').id)}"
    monkeypatch.undo()
    assert client.get(old).status_code == 404  # older than the 14 days the setting allows
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_every_member_has_their_own_link_and_it_answers_only_for_them(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    url_a, url_b = link_for(db_session, ticket, "a@example.com"), link_for(db_session, ticket, "b@example.com")
    assert url_a != url_b
    client.post("/auth/logout")

    client.post(url_a, data={"decision": "approved"})

    assert approvals(db_session, ticket) == {"a@example.com": APPROVED, "b@example.com": PENDING}
    assert "Asking: B" in client.get(url_b).text


def test_a_link_for_a_removed_member_no_longer_answers(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    url = link_for(db_session, ticket, "a@example.com")
    group = db_session.get(ApprovalGroup, current_approval(db_session, ticket).group_id)
    from app.approval_groups import remove_member

    remove_member(db_session, group, vote_of(db_session, ticket, "a@example.com").voter_id)
    db_session.commit()
    client.post("/auth/logout")

    by_link = client.post(url, data={"decision": "approved"})
    portal = answer(client, ticket, "a@example.com", "approved")

    assert "no longer an approver" in by_link.text
    assert portal.status_code == 400 and "no longer an approver" in portal.text
    assert approvals(db_session, ticket)["a@example.com"] == PENDING


def test_the_link_lifetime_is_a_setting(client, db_session):
    from app.settings_store import APPROVAL_LINK_MINUTES, REGISTRY, get_setting

    assert REGISTRY[APPROVAL_LINK_MINUTES].kind == "minutes"
    assert get_setting(db_session, APPROVAL_LINK_MINUTES) == "20160"
    assert approval_links.read_token(approval_links.make_token(7), 5) == 7
    assert approval_links.read_token(approval_links.make_token(7), 0) in (7, None)  # a zero lifetime is a race: both fine


def test_page_text_is_escaped(client, db_session):
    person(client, db_session, "boss@example.com")
    kind = request_type(db_session, ["boss@example.com"])
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    raise_it(client, subject="<b>bold</b>", description="<script>alert(1)</script>", request_type_id=kind.id)
    ticket = db_session.exec(select(Ticket)).one()
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)
    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id},
    )
    url = link_for(db_session, ticket, "boss@example.com")
    client.post("/auth/logout")

    page = client.get(url).text

    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<b>bold</b>" not in page


# ---- Settings ----------------------------------------------------------------------------------------


def test_the_default_percentages_are_settings(client, db_session):
    from app.settings_store import (
        APPROVAL_DEFAULT_FAIL_PERCENT,
        APPROVAL_DEFAULT_PASS_PERCENT,
        REGISTRY,
        get_setting,
    )

    for key in (APPROVAL_DEFAULT_PASS_PERCENT, APPROVAL_DEFAULT_FAIL_PERCENT):
        assert REGISTRY[key].kind == "percent"
    assert (get_setting(db_session, APPROVAL_DEFAULT_PASS_PERCENT), get_setting(db_session, APPROVAL_DEFAULT_FAIL_PERCENT)) == ("51", "50")
    check_percentages(int(REGISTRY[APPROVAL_DEFAULT_PASS_PERCENT].default), int(REGISTRY[APPROVAL_DEFAULT_FAIL_PERCENT].default))


# ---- Review fixes -----------------------------------------------------------------------------------


def test_someone_removed_from_the_group_can_no_longer_answer_but_still_counts_as_a_voter(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    group = db_session.get(ApprovalGroup, current_approval(db_session, ticket).group_id)
    from app.approval_groups import remove_member

    remove_member(db_session, group, vote_of(db_session, ticket, "a@example.com").voter_id)
    db_session.commit()

    answer(client, ticket, "b@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # 1 of 2 voters: a is still counted
    assert current_approval(db_session, ticket).voters == 2


def test_waiving_tells_the_requester(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    client.post(f"/tickets/{ticket.number}/approval/waive")
    client.post("/auth/logout")
    login_as(client, "jo@example.com")

    assert "going ahead" in client.get(f"/portal/{ticket.number}").text


def test_a_voter_whose_answer_was_not_needed_is_told_so(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails))
    answer(client, ticket, "a@example.com", "approved")  # one is enough: b is marked not needed

    late = answer(client, ticket, "b@example.com", "approved")

    assert late.status_code == 400 and "settled without your answer" in late.text
    assert "already answered" not in late.text


def test_the_waiting_count_is_right(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    from app.approvals import count_pending_for, pending_for

    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    assert count_pending_for(db_session, boss) == len(pending_for(db_session, boss)) == 1
    answer(client, ticket, "boss@example.com", "approved")
    assert count_pending_for(db_session, boss) == 0


def test_the_waiting_count_leaves_out_what_the_person_already_answered(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100, 2))
    from app.approvals import count_pending_for

    a = db_session.exec(select(User).where(User.email == "a@example.com")).one()
    b = db_session.exec(select(User).where(User.email == "b@example.com")).one()
    answer(client, ticket, "a@example.com", "approved")  # still waiting on b

    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    assert count_pending_for(db_session, a) == 0 and count_pending_for(db_session, b) == 1

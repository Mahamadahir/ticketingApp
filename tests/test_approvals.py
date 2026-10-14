import time

import pytest
from sqlmodel import select

from app import approval_links
from app.approvals import APPROVED, DECLINED, NOT_NEEDED, PENDING, ApprovalError, Tally, decide, outcome
from app.models import (
    AuditLog,
    Message,
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


def request_type(db_session, approvers, approvals_required=1, declines_required=1, name="New starter"):
    created = create_request_type(db_session, name, "", ",".join(approvers), approvals_required, declines_required)
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
    db_session.expire_all()
    rows = db_session.exec(select(TicketApproval).where(TicketApproval.ticket_id == ticket.id).order_by(TicketApproval.id)).all()
    return {db_session.get(User, r.approver_id).email: r.decision for r in rows}


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


# ---- Starting ---------------------------------------------------------------------------------------


def test_triaging_a_catalogue_request_with_approvers_waits_for_them(client, db_session):
    for email in ("boss@example.com", "lead@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["boss@example.com", "lead@example.com"])

    ticket = raised_and_triaged(client, db_session, kind)

    assert ticket.status == TicketStatus.AWAITING_APPROVAL and ticket.triaged_at is not None
    assert (ticket.approvals_required, ticket.declines_required) == (1, 1)
    assert approvals(db_session, ticket) == {"boss@example.com": PENDING, "lead@example.com": PENDING}
    [asked] = events(db_session, ticket, "approval_requested")
    assert asked.to_value == "boss@example.com, lead@example.com" and asked.detail == "1 of 2 must approve"
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Waiting for approval</b> (1 of 2 must approve)" in page and "Approvals (1 of 2 must approve)" in page


def test_a_request_type_without_approvers_opens_as_usual(client, db_session):
    kind = request_type(db_session, [])
    ticket = raised_and_triaged(client, db_session, kind)
    assert ticket.status == TicketStatus.OPEN and approvals(db_session, ticket) == {}


def test_the_requester_never_approves_their_own_request(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "jo@example.com")
    kind = request_type(db_session, ["boss@example.com", "jo@example.com"], approvals_required=2)

    ticket = raised_and_triaged(client, db_session, kind, requester="jo@example.com")

    assert approvals(db_session, ticket) == {"boss@example.com": PENDING}  # Jo left out
    assert ticket.approvals_required == 1  # and the rule cannot ask for more than there are


def test_when_the_only_approver_is_the_requester_no_approval_is_needed(client, db_session):
    person(client, db_session, "jo@example.com")
    kind = request_type(db_session, ["jo@example.com"])
    ticket = raised_and_triaged(client, db_session, kind, requester="jo@example.com")
    assert ticket.status == TicketStatus.OPEN and approvals(db_session, ticket) == {}


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

    assert status(db_session, ticket) == TicketStatus.OPEN and approvals(db_session, ticket) == {}


# ---- The outcome -------------------------------------------------------------------------------------


def test_one_approval_is_enough_by_default_and_opens_the_request(client, db_session):
    for email in ("boss@example.com", "lead@example.com"):
        person(client, db_session, email)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"]))

    response = answer(client, ticket, "boss@example.com", "approved", "Fine by me")

    assert response.status_code == 200
    assert status(db_session, ticket) == TicketStatus.OPEN
    assert approvals(db_session, ticket) == {"boss@example.com": APPROVED, "lead@example.com": NOT_NEEDED}
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["approved"]
    assert events(db_session, ticket, "approval_decided")[0].detail == "Fine by me"
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert "Your request has been approved" in client.get(f"/portal/{ticket.number}").text


def test_one_decline_closes_it_by_default_and_tells_the_requester_why(client, db_session):
    person(client, db_session, "boss@example.com")
    person(client, db_session, "lead@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"]))

    answer(client, ticket, "lead@example.com", "declined", "No budget")

    assert status(db_session, ticket) == TicketStatus.CLOSED
    db_session.refresh(ticket)
    assert ticket.resolved_at is not None
    assert approvals(db_session, ticket) == {"boss@example.com": NOT_NEEDED, "lead@example.com": DECLINED}
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/{ticket.number}").text
    assert "was not approved" in page and "Reason: No budget" in page


def test_a_threshold_of_two_needs_two_approvals(client, db_session):
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["a@example.com", "b@example.com", "c@example.com"], approvals_required=2, declines_required=3)
    ticket = raised_and_triaged(client, db_session, kind)
    assert "2 of 3 must approve" in client.get(f"/tickets/{ticket.number}").text

    answer(client, ticket, "a@example.com", "approved")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    answer(client, ticket, "b@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.OPEN
    assert approvals(db_session, ticket)["c@example.com"] == NOT_NEEDED


def test_a_decline_threshold_of_two_needs_two_declines(client, db_session):
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["a@example.com", "b@example.com", "c@example.com"], approvals_required=2, declines_required=2)
    ticket = raised_and_triaged(client, db_session, kind)

    answer(client, ticket, "a@example.com", "declined")
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # approvals still reachable: b and c
    answer(client, ticket, "b@example.com", "declined")

    assert status(db_session, ticket) == TicketStatus.CLOSED


def test_it_closes_early_when_the_approvals_can_no_longer_be_reached(client, db_session):
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["a@example.com", "b@example.com", "c@example.com"], approvals_required=3, declines_required=3)
    ticket = raised_and_triaged(client, db_session, kind)

    answer(client, ticket, "a@example.com", "declined")  # all three must approve, so one decline settles it

    assert status(db_session, ticket) == TicketStatus.CLOSED


@pytest.mark.parametrize(
    "counts,needed,declines,expected",
    [
        (Tally(0, 0, 3), 1, 1, None),
        (Tally(1, 0, 2), 1, 1, APPROVED),
        (Tally(1, 1, 1), 1, 1, APPROVED),  # approvals win a tie
        (Tally(0, 1, 2), 1, 1, DECLINED),
        (Tally(1, 0, 2), 2, 2, None),
        (Tally(0, 1, 1), 2, 2, DECLINED),  # 0 approved + 1 pending < 2: out of reach
        (Tally(2, 0, 0), 2, 2, APPROVED),
    ],
)
def test_the_rule(counts, needed, declines, expected):
    ticket = Ticket(subject="x", requester_email="a@example.com", approvals_required=needed, declines_required=declines)
    assert outcome(ticket, counts) == expected


def test_the_rule_is_copied_when_it_starts_so_later_edits_do_not_change_it(client, db_session):
    for email in ("a@example.com", "b@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["a@example.com", "b@example.com"], approvals_required=2)
    ticket = raised_and_triaged(client, db_session, kind)
    kind.approvals_required = 1
    db_session.add(kind)
    db_session.commit()

    answer(client, ticket, "a@example.com", "approved")

    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL  # still needs two


# ---- Answering ---------------------------------------------------------------------------------------


def test_each_approver_answers_once_and_only_while_it_is_waiting(client, db_session):
    for email in ("a@example.com", "b@example.com"):
        person(client, db_session, email)
    kind = request_type(db_session, ["a@example.com", "b@example.com"], approvals_required=2, declines_required=2)
    ticket = raised_and_triaged(client, db_session, kind)

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
    approval = db_session.exec(select(TicketApproval)).one()
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(ApprovalError):
        decide(db_session, approval, APPROVED, "")


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
    for email in ("a@example.com", "b@example.com"):
        person(client, db_session, email)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["a@example.com", "b@example.com"], approvals_required=2))
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 403  # the agent

    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    assert "Waive approval" in client.get(f"/tickets/{ticket.number}").text
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 200

    assert status(db_session, ticket) == TicketStatus.OPEN
    assert set(approvals(db_session, ticket).values()) == {NOT_NEEDED}
    assert [e.to_value for e in events(db_session, ticket, "approval_outcome")] == ["waived"]
    assert client.post(f"/tickets/{ticket.number}/approval/waive").status_code == 400  # nothing left to waive


# ---- The portal --------------------------------------------------------------------------------------


def test_an_approver_sees_and_opens_what_waits_for_them_in_the_portal(client, db_session):
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


def test_after_answering_the_approver_still_reads_it_but_cannot_answer_again(client, db_session):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    answer(client, ticket, "boss@example.com", "approved")

    page = client.get(f"/portal/{ticket.number}").text

    assert 'name="decision"' not in page
    assert "Nothing is waiting" in client.get("/portal/approvals").text


# ---- The emailed link --------------------------------------------------------------------------------


def link_for(db_session, ticket, email):
    approval = db_session.exec(
        select(TicketApproval).join(User, User.id == TicketApproval.approver_id).where(
            TicketApproval.ticket_id == ticket.id, User.email == email
        )
    ).one()
    return f"/approvals/{approval_links.make_token(approval.id)}"


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
    old = f"/approvals/{approval_links.make_token(1)}"
    monkeypatch.undo()
    assert client.get(old).status_code == 404  # older than the 14 days the setting allows
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


def test_a_link_for_one_approval_cannot_answer_another(client, db_session):
    for email in ("a@example.com", "b@example.com"):
        person(client, db_session, email)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["a@example.com", "b@example.com"], approvals_required=2))
    url = link_for(db_session, ticket, "a@example.com")
    client.post("/auth/logout")

    client.post(url, data={"decision": "approved"})

    assert approvals(db_session, ticket) == {"a@example.com": APPROVED, "b@example.com": PENDING}


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


# ---- The admin settings ------------------------------------------------------------------------------


def test_the_admin_sets_the_thresholds_and_they_are_checked(client, db_session):
    for email in ("a@example.com", "b@example.com"):
        person(client, db_session, email)
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)

    created = client.post(
        "/admin/request-types",
        data={"name": "Access", "approvers": "a@example.com, b@example.com", "approvals_required": 2, "declines_required": 1},
    )
    assert created.status_code == 200
    kind = db_session.exec(select(RequestType)).one()
    assert (kind.approvals_required, kind.declines_required) == (2, 1)

    too_many = client.post(f"/admin/request-types/{kind.id}", data={"name": "Access", "approvers": "a@example.com", "approvals_required": 2, "is_active": "on"})
    assert too_many.status_code == 400 and "only 1 are named" in too_many.text
    assert client.post(f"/admin/request-types/{kind.id}", data={"name": "Access", "approvers": "a@example.com, b@example.com", "approvals_required": 0}).status_code == 422
    client.post(f"/admin/request-types/{kind.id}", data={"name": "Access", "approvers": "a@example.com, b@example.com", "approvals_required": 1, "declines_required": 2, "is_active": "on"})
    db_session.expire_all()
    kind = db_session.get(RequestType, kind.id)
    assert (kind.approvals_required, kind.declines_required) == (1, 2)
    details = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "edited")).all()]
    assert details == ["needs 2 approvals / 1 declines -> 1 / 2"]
    assert 'name="approvals_required"' in client.get("/admin/request-types").text

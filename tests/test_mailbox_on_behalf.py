"""Requests about SOMEONE ELSE'S mailbox (#311): the form's "Whose mailbox?", the approval (the owner of the mailbox or an IT approver
from the request type's group, any one is enough), the IT approver's note, the owner always being told, the timing around the
approval, and the privacy of the subject access export. Exchange is the FakeExecutor; Graph is replaced where a directory answer is
needed (the fixtures are the forwarding form's, #309)."""

import html
import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app import approval_links
from app.approval_groups import add_member
from app.approvals import ApprovalError, record_decision, waive
from app.database import engine
from app.mailbox_changes import run_mailbox_changes
from app.mailbox_executor import FakeExecutor, set_executor
from app.models import (
    ApprovalVote, AuditLog, MailboxChange, Message, MessageDirection, OutboundEmail, Ticket, TicketApproval, TicketEvent, TicketStatus, User,
    UserRole,
)
from app.outbox import MAILBOX_CHANGE
from app.request_types import TypeDefaults, create_request_type
from app.settings_store import DEFAULT_TIME_ZONE, set_setting
from app.storage import get_storage
from app.subject_access import build_export
from tests.conftest import create_category, create_team
from tests.test_mailbox_access import mails
from tests.test_portal_mailbox import FRIEND_OID, OWNER, OWNER_OID, directory, later, now, person, raw_user, signed_in  # noqa: F401
from tests.test_request_types import group_of
from tests.test_resolution import dev_mail  # noqa: F401  (a fixture)

BEE = "bee@example.com"
BEE_OID = "oid-bee"
NOBODY = "nobody@example.com"
NOBODY_OID = "oid-nobody"
IT1, IT2 = "it1@example.com", "it2@example.com"
NOTE = "Spoke to J. Smith, line manager, by phone 09:10"


@pytest.fixture(autouse=True)
def fake_exchange(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()
    set_executor(FakeExecutor())
    yield
    set_executor(None)


@pytest.fixture
def world(db_session, directory):
    """Request types for forwarding and for Full access (the one form), each with the approval group and "any one approver is enough"; a
    third forwarding type with no group; the owner Bee (who has an account) and Nobody (who is in the directory but has none)."""
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    group = group_of(db_session, "Mailbox access approvers", (IT1, IT2))
    defaults = TypeDefaults(team.id, category.id, "low", "low")
    forwarding = create_request_type(db_session, "Forward my mail", "", group.id, 51, defaults=defaults, mailbox_action="forwarding", any_one_approver=True)
    access = create_request_type(db_session, "Mailbox access", "", group.id, 51, defaults=defaults, mailbox_action="full_access", any_one_approver=True)
    no_group = create_request_type(db_session, "Forward (own only)", "", None, 51, defaults=defaults, mailbox_action="forwarding")
    bee = person(db_session, BEE, "Bee Owner")
    db_session.commit()
    for row in (forwarding, access, no_group):
        db_session.refresh(row)
    directory[OWNER_OID] = raw_user(OWNER_OID, "Alex Owner", OWNER)
    directory[BEE_OID] = raw_user(BEE_OID, "Bee Owner", BEE)
    directory[NOBODY_OID] = raw_user(NOBODY_OID, "No Body", NOBODY)
    directory[FRIEND_OID] = raw_user(FRIEND_OID, "Jane Friend", "jane@example.com")
    return SimpleNamespace(team=team, category=category, group=group, forwarding=forwarding, access=access, no_group=no_group, bee=bee)


def form(kind, **extra):
    end_date, end_time = later(30)
    data = {
        "request_type_id": kind.id, "whose": "other", "owner_object_id": BEE_OID, "owner_name": "Bee Owner", "keep_copy": "on",
        "start_mode": "now", "end_date": end_date, "end_time": end_time, "full_access": "on", "confirm_access": "1",
    }
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def ask(client, kind, **extra):
    return client.post("/portal/mailbox-change", data=form(kind, **extra), follow_redirects=False)


def raised(client, db_session, world, kind=None, **extra):
    """The requester (Alex) asks for Bee's mailbox; returns the ticket."""
    signed_in(client, db_session)
    assert ask(client, kind or world.forwarding, **extra).status_code == 303
    db_session.expire_all()
    return db_session.exec(select(Ticket).order_by(Ticket.id.desc())).first()


def changes(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(MailboxChange).where(MailboxChange.ticket_id == ticket.id).order_by(MailboxChange.id)).all()


def approval_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(TicketApproval).where(TicketApproval.ticket_id == ticket.id)).one()


def votes(db_session, ticket):
    """{email: vote} of the ticket's approval."""
    approval = approval_of(db_session, ticket)
    rows = db_session.exec(select(ApprovalVote, User).join(User, User.id == ApprovalVote.voter_id).where(ApprovalVote.approval_id == approval.id)).all()
    return {user.email: vote for vote, user in rows}


def answer(db_session, ticket, email, decision, comment=""):
    vote = votes(db_session, ticket)[email]
    record_decision(db_session, vote, decision, comment)
    db_session.commit()
    db_session.expire_all()


def run(db_session, at):
    db_session.expire_all()
    with Session(engine) as session:
        return run_mailbox_changes(session, at)


def ticket_now(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def mail_to(db_session, address):
    return [m for m in mails(db_session) if m.kind == MAILBOX_CHANGE and m.to_address == address]


# ---- The form ---------------------------------------------------------------------------------------------------------------------


def test_the_form_offers_whose_mailbox_only_for_a_type_with_a_group(client, db_session, world):
    signed_in(client, db_session)
    page = client.get(f"/portal/new?request_type_id={world.forwarding.id}").text
    assert 'name="whose"' in page and 'value="other"' in page and 'id="mo-oid"' in page and "Someone else" in page
    assert 'name="owner_object_id"' in page and 'id="mb-oid"' in page  # the owner's picker and the other person's
    plain = client.get(f"/portal/new?request_type_id={world.no_group.id}").text
    assert 'name="whose"' not in plain and 'id="mo-oid"' not in plain and 'id="mb-oid"' in plain
    access = client.get(f"/portal/new?request_type_id={world.access.id}").text
    assert 'name="whose"' in access and 'data-warn="access"' in access


def test_someone_elses_mailbox_is_refused_for_a_type_without_a_group_whatever_the_form_says(client, db_session, world):
    signed_in(client, db_session)
    response = ask(client, world.no_group)
    assert response.status_code == 400 and "not available for this kind of request" in response.text
    assert db_session.exec(select(Ticket)).first() is None


def test_someone_elses_mailbox_needs_the_owner_to_be_chosen_and_found(client, db_session, world):
    signed_in(client, db_session)
    blank = ask(client, world.forwarding, owner_object_id="")
    assert blank.status_code == 400 and "Choose whose mailbox" in blank.text
    unknown = ask(client, world.forwarding, owner_object_id="oid-who")
    assert unknown.status_code == 400 and "could not be found" in unknown.text
    assert db_session.exec(select(Ticket)).first() is None
    # a refused owner is forgotten, so the person is asked to choose again
    assert 'value="oid-who"' not in unknown.text


def test_the_owner_cannot_be_the_person_the_mail_is_forwarded_to(client, db_session, world):
    signed_in(client, db_session)
    response = ask(client, world.forwarding, target_object_id=BEE_OID, target_name="Bee Owner")
    assert response.status_code == 400 and "person whose mailbox it is" in response.text
    assert db_session.exec(select(Ticket)).first() is None


def test_a_mailbox_cannot_be_asked_for_with_your_own_account_as_the_owner(client, db_session, world):
    signed_in(client, db_session)
    response = ask(client, world.forwarding, owner_object_id=OWNER_OID)
    assert response.status_code == 400
    assert db_session.exec(select(Ticket)).first() is None


# ---- The round --------------------------------------------------------------------------------------------------------------------


def test_the_round_is_the_group_plus_the_owner_and_nothing_starts_before_an_approval(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    assert ticket.status == TicketStatus.AWAITING_APPROVAL and ticket.triaged_at is not None
    approval = approval_of(db_session, ticket)
    assert approval.any_one and approval.voters == 3
    found = votes(db_session, ticket)
    assert set(found) == {IT1, IT2, BEE}
    assert found[BEE].extra_voter and not found[IT1].extra_voter and not found[IT2].extra_voter
    (change,) = changes(db_session, ticket)
    assert change.owner_address == BEE and change.target_address == OWNER and change.state == "scheduled"  # the target defaults to the requester
    assert run(db_session, change.starts_at + timedelta(hours=1)) == 0  # the row waits unclaimed
    assert ticket_now(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL
    assert [m.to_address for m in mails(db_session) if m.kind == "approval"].count(BEE) == 1  # B is asked by email like the group


def test_the_owner_approving_starts_the_change_and_needs_no_note(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.SCHEDULED
    (change,) = changes(db_session, ticket)
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    assert changes(db_session, ticket)[0].state == "active" and ticket_now(db_session, ticket).status == TicketStatus.RESOLVED
    approval = approval_of(db_session, ticket)
    assert approval.status == "approved"
    assert {v.decision for v in votes(db_session, ticket).values()} == {"approved", "not_needed"}


def test_an_it_approver_needs_a_note_and_it_is_recorded_on_the_ticket_and_in_the_audit_log(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    vote = votes(db_session, ticket)[IT1]
    with pytest.raises(ApprovalError, match="who you checked with"):
        record_decision(db_session, vote, "approved", "")
    db_session.rollback()
    assert ticket_now(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL
    assert votes(db_session, ticket)[IT1].decision == "pending"
    answer(db_session, ticket, IT1, "approved", NOTE)
    assert votes(db_session, ticket)[IT1].comment == NOTE
    assert ticket_now(db_session, ticket).status == TicketStatus.SCHEDULED
    event = db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "approval_decided")).one()
    assert event.detail == NOTE and event.actor_email == IT1
    audit = db_session.exec(select(AuditLog).where(AuditLog.entity_id == ticket.id, AuditLog.action == "approval_approved")).one()
    assert NOTE in audit.detail
    notes = [m for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction == MessageDirection.INTERNAL]
    assert any(NOTE in m.body for m in notes)


def test_the_note_is_required_on_the_emailed_link_too_and_a_decline_needs_none(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    token = approval_links.make_token(votes(db_session, ticket)[IT1].id)
    page = client.get(f"/approvals/{token}").text
    assert "required to approve" in page and "Bee Owner" in page and "mailbox" in page
    refused = client.post(f"/approvals/{token}", data={"decision": "approved", "comment": ""})
    assert "who you checked with" in refused.text and "recorded" not in refused.text
    assert ticket_now(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL
    ok = client.post(f"/approvals/{token}", data={"decision": "approved", "comment": NOTE})
    assert "Thank you" in ok.text
    assert ticket_now(db_session, ticket).status == TicketStatus.SCHEDULED
    # the owner's own page asks for no note
    other = raised(client, db_session, world, kind=world.access)
    owners_token = approval_links.make_token(votes(db_session, other)[BEE].id)
    assert "required to approve" not in client.get(f"/approvals/{owners_token}").text


def test_a_decline_from_either_cancels_the_change_and_the_requester_is_told(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, IT2, "declined", "")  # a decline needs no note
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.CLOSED
    (change,) = changes(db_session, ticket)
    assert change.state == "cancelled"
    assert run(db_session, change.starts_at + timedelta(hours=1)) == 0
    assert approval_of(db_session, ticket).status == "declined"
    told = [m for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction == MessageDirection.OUTBOUND]
    assert told  # the declined notice to the requester
    assert not mail_to(db_session, BEE)  # nothing started, so nothing to tell the owner about


def test_the_owner_declining_cancels_it_too(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "declined", "not now")
    assert ticket_now(db_session, ticket).status == TicketStatus.CLOSED and changes(db_session, ticket)[0].state == "cancelled"


def test_withdrawing_cancels_the_change(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    assert client.post(f"/portal/{ticket.number}/withdraw", follow_redirects=False).status_code == 303
    assert ticket_now(db_session, ticket).status == TicketStatus.CLOSED and changes(db_session, ticket)[0].state == "cancelled"


def test_an_owner_without_an_account_cannot_vote_and_the_page_says_so(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, owner_object_id=NOBODY_OID, owner_name="No Body")
    assert approval_of(db_session, ticket).voters == 2 and set(votes(db_session, ticket)) == {IT1, IT2}
    page = client.get(f"/portal/{ticket.number}").text
    assert "nobody@example.com does not have an account in this app" in page and "only the IT approvers decide" in page
    (change,) = changes(db_session, ticket)
    assert change.owner_address == NOBODY
    # one approver still settles it, and the owner (who has no account) is told by email at the start
    answer(db_session, ticket, IT1, "approved", NOTE)
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    assert len(mail_to(db_session, NOBODY)) == 1


def test_the_page_says_the_owner_or_an_it_approver_can_approve(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    page = client.get(f"/portal/{ticket.number}").text
    assert "Bee Owner, the owner of the mailbox, or an IT approver can approve this" in page and "Waiting for approval" in page


def test_the_requester_is_left_out_even_in_the_group_and_self_approval_is_refused_for_these_types(client, db_session, world, dev_mail):
    signed_in(client, db_session)
    add_member(db_session, world.group, OWNER)
    world.forwarding.approvers_may_self_approve = True  # an admin ticking it changes nothing for a mailbox type
    db_session.add(world.forwarding)
    db_session.commit()
    assert ask(client, world.forwarding).status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    assert ticket.status == TicketStatus.AWAITING_APPROVAL  # not self-approved
    assert set(votes(db_session, ticket)) == {IT1, IT2, BEE} and approval_of(db_session, ticket).voters == 3
    assert not [e for e in db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all() if e.event_type == "approval_self_approved"]


def test_a_vote_from_someone_taken_out_of_the_group_is_refused_but_the_extra_voter_is_not_a_member(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    from app.approval_groups import remove_member

    remove_member(db_session, world.group, votes(db_session, ticket)[IT1].voter_id)
    db_session.commit()
    with pytest.raises(ApprovalError, match="no longer an approver"):
        record_decision(db_session, votes(db_session, ticket)[IT1], "approved", NOTE)
    db_session.rollback()
    answer(db_session, ticket, BEE, "approved", "")  # B was never in the group, and may answer
    assert ticket_now(db_session, ticket).status == TicketStatus.SCHEDULED


# ---- Timing -----------------------------------------------------------------------------------------------------------------------


def test_approval_after_the_start_applies_at_the_next_run_and_the_sla_starts_at_the_approval(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    (change,) = changes(db_session, ticket)
    # the chosen start (Now) passed hours ago, by the clock of the approval
    change.starts_at = change.created_at - timedelta(hours=3)
    db_session.add(change)
    db_session.commit()
    approved_at = now()
    answer(db_session, ticket, BEE, "approved", "")
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.SCHEDULED
    assert ticket.sla_starts_at is not None and ticket.sla_starts_at.replace(tzinfo=approved_at.tzinfo) >= approved_at - timedelta(seconds=5)
    assert run(db_session, now() + timedelta(minutes=1)) == 1
    assert changes(db_session, ticket)[0].state == "active"


def test_approval_before_a_later_start_waits_for_the_start_and_the_sla_starts_then(client, db_session, world, dev_mail):
    start_date, start_time = later(5)
    ticket = raised(client, db_session, world, start_mode="later", start_date=start_date, start_time=start_time)
    (change,) = changes(db_session, ticket)
    answer(db_session, ticket, BEE, "approved", "")
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.SCHEDULED
    assert ticket.sla_starts_at.replace(tzinfo=change.starts_at.tzinfo) == change.starts_at.replace(tzinfo=change.starts_at.tzinfo)
    assert run(db_session, now()) == 0  # not yet
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1


def test_approval_after_the_end_cancels_it_and_tells_the_requester(client, db_session, world, dev_mail, fake_exchange):
    ticket = raised(client, db_session, world)
    (change,) = changes(db_session, ticket)
    change.starts_at, change.ends_at = now() - timedelta(hours=5), now() - timedelta(hours=1)
    db_session.add(change)
    db_session.commit()
    answer(db_session, ticket, IT1, "approved", NOTE)
    ticket = ticket_now(db_session, ticket)
    assert changes(db_session, ticket)[0].state == "cancelled" and ticket.status == TicketStatus.RESOLVED
    told = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction == MessageDirection.OUTBOUND]
    assert any("approved too late" in body for body in told)
    assert run(db_session, now() + timedelta(minutes=1)) == 0
    assert not mail_to(db_session, BEE)  # never started, so the owner is not told it started


# ---- The owner is always told ------------------------------------------------------------------------------------------------------


def finish(db_session, ticket):
    """Runs the start and then the end of the request's change."""
    (change,) = changes(db_session, ticket)
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    assert run(db_session, change.ends_at + timedelta(minutes=1)) == 1
    return changes(db_session, ticket)[0]


def owner_mails(db_session):
    rows = mail_to(db_session, BEE)
    assert [m.subject for m in rows] == ["A change was made to your mailbox", "A change to your mailbox has ended"]
    return rows


def test_the_owner_gets_the_start_and_end_emails_when_the_owner_approved(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    assert finish(db_session, ticket).state == "ended"
    started, stopped = owner_mails(db_session)
    assert "you approved it" in started.body and "Alex Owner" in started.body and "now going to Alex Owner" in started.body
    assert "A copy stays in your mailbox" in started.body
    assert "has stopped" in stopped.body and "you approved it" in stopped.body


def test_the_owner_gets_the_start_and_end_emails_when_an_it_approver_approved(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, IT1, "approved", NOTE)
    finish(db_session, ticket)
    started, stopped = owner_mails(db_session)
    assert "approved by it1@example.com (IT Support)" in started.body and "approved by it1@example.com (IT Support)" in stopped.body
    assert NOTE not in started.body and NOTE not in stopped.body  # who they checked with is for the team, not for the owner's email


def test_the_owner_gets_the_start_and_end_emails_when_a_super_admin_waived_the_approval(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    admin = User(email="admin@example.com", display_name="Admin", role=UserRole.SUPER_ADMIN)
    db_session.add(admin)
    db_session.commit()
    waive(db_session, ticket_now(db_session, ticket), admin)
    db_session.commit()
    finish(db_session, ticket)
    started, _ = owner_mails(db_session)
    assert "approved by an administrator" in started.body


def test_the_requester_is_told_in_the_owners_words_and_not_twice_when_the_mail_comes_to_them(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    run(db_session, changes(db_session, ticket)[0].starts_at + timedelta(minutes=1))
    db_session.expire_all()
    messages = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction == MessageDirection.OUTBOUND]
    assert any("Mail sent to Bee Owner is now going to you as well" in body and "A copy stays in Bee Owner's mailbox" in body for body in messages)
    assert not mail_to(db_session, OWNER)  # the requester is the target: their own notice says it, no second email


def test_a_chosen_target_is_told_as_well(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, target_object_id=FRIEND_OID, target_name="Jane Friend")
    (change,) = changes(db_session, ticket)
    assert change.owner_address == BEE and change.target_address == "jane@example.com"
    answer(db_session, ticket, BEE, "approved", "")
    run(db_session, change.starts_at + timedelta(minutes=1))
    assert len(mail_to(db_session, "jane@example.com")) == 1 and len(mail_to(db_session, BEE)) == 1
    db_session.expire_all()
    notes = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction == MessageDirection.OUTBOUND]
    assert any("Mail sent to Bee Owner is now going to jane@example.com as well" in body for body in notes)


def test_stopping_early_tells_the_owner(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    (change,) = changes(db_session, ticket)
    run(db_session, change.starts_at + timedelta(minutes=1))
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", follow_redirects=False).status_code == 303
    assert run(db_session, now() + timedelta(minutes=2)) == 1
    assert [m.subject for m in mail_to(db_session, BEE)] == ["A change was made to your mailbox", "A change to your mailbox has ended"]


# ---- Full access ------------------------------------------------------------------------------------------------------------------


def test_access_to_someone_elses_mailbox_is_for_the_requester_and_the_warning_names_the_owner(client, db_session, world, dev_mail):
    signed_in(client, db_session)
    first = ask(client, world.access, confirm_access=None, send_as="on")
    warning = html.unescape(first.text)
    assert first.status_code == 200 and "You will be able to read all mail already in Bee Owner's mailbox" in warning
    assert "You will be able to send email that appears to come from Bee Owner" in warning
    assert db_session.exec(select(Ticket)).first() is None
    assert ask(client, world.access, send_as="on").status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    full, send = changes(db_session, ticket)
    assert (full.kind, send.kind) == ("full_access", "send_as")
    assert {full.owner_address, send.owner_address} == {BEE} and {full.target_address, send.target_address} == {OWNER}
    assert ticket.status == TicketStatus.AWAITING_APPROVAL and approval_of(db_session, ticket).voters == 3
    page = client.get(f"/portal/{ticket.number}").text
    assert "Waiting for approval" in page and "Full access to Bee Owner" in page and "Send As on Bee Owner" in page
    assert "Cancel Full access" not in page  # a request waiting for approval is withdrawn, not cancelled
    answer(db_session, ticket, IT2, "approved", NOTE)
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.SCHEDULED  # scheduled once, whatever the number of changes
    assert run(db_session, full.starts_at + timedelta(minutes=1)) == 2
    assert ticket_now(db_session, ticket).status == TicketStatus.RESOLVED
    assert len(mail_to(db_session, BEE)) == 2  # one for each permission
    assert not mail_to(db_session, OWNER)


def test_the_requester_may_stop_the_access_early_and_it_is_removed(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, kind=world.access)
    answer(db_session, ticket, BEE, "approved", "")
    (change,) = changes(db_session, ticket)
    run(db_session, change.starts_at + timedelta(minutes=1))
    page = client.get(f"/portal/{ticket.number}").text
    assert "Remove Full access now" in page
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", data={"change_id": change.id}, follow_redirects=False).status_code == 303
    assert run(db_session, now() + timedelta(minutes=2)) == 1
    assert changes(db_session, ticket)[0].state == "ended"


def test_someone_else_cannot_stop_or_cancel_a_request_that_is_not_theirs(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    client.post("/auth/logout")
    signed_in(client, db_session, email=BEE, name="Bee Owner")
    for action in ("cancel", "stop"):
        assert client.post(f"/portal/{ticket.number}/mailbox/{action}", follow_redirects=False).status_code == 404


# ---- Your own mailbox ---------------------------------------------------------------------------------------------------------------


def test_your_own_mailbox_still_needs_no_approval_even_on_a_type_with_a_group(client, db_session, world, dev_mail):
    signed_in(client, db_session)
    response = ask(client, world.forwarding, whose="mine", owner_object_id="", target_object_id=FRIEND_OID, target_name="Jane Friend")
    assert response.status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    assert ticket.status == TicketStatus.SCHEDULED
    assert db_session.exec(select(TicketApproval)).first() is None and not [m for m in mails(db_session) if m.kind == "approval"]
    (change,) = changes(db_session, ticket)
    assert change.owner_address == OWNER and change.target_address == "jane@example.com"
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    assert not mail_to(db_session, BEE) and not mail_to(db_session, OWNER)


def test_a_posted_owner_is_ignored_for_your_own_mailbox(client, db_session, world, dev_mail):
    signed_in(client, db_session)
    assert ask(client, world.forwarding, whose="mine", target_object_id=FRIEND_OID, target_name="Jane Friend").status_code == 303
    db_session.expire_all()
    (change,) = db_session.exec(select(MailboxChange)).all()
    assert change.owner_address == OWNER  # Bee's id in the form changed nothing


# ---- The request type ---------------------------------------------------------------------------------------------------------------


def test_any_one_approver_settles_on_the_first_answer_whatever_the_number_of_voters():
    from app.approvals import describe_rule, outcome

    assert outcome(1, 0, 5, 90, True) == "approved" and outcome(0, 1, 5, 90, True) == "declined" and outcome(0, 0, 5, 90, True) is None
    assert outcome(1, 0, 5, 90) is None  # the percentage rule is untouched when the option is off
    assert describe_rule(SimpleNamespace(any_one=True, voters=3, pass_percent=51)) == "any one of 3 approvers can decide"


def test_without_any_one_approver_the_percentage_rules_a_mailbox_round_too(client, db_session, world, dev_mail):
    world.forwarding.any_one_approver = False
    world.forwarding.pass_percent = 100
    db_session.add(world.forwarding)
    db_session.commit()
    ticket = raised(client, db_session, world)
    assert not approval_of(db_session, ticket).any_one
    answer(db_session, ticket, BEE, "approved", "")
    assert ticket_now(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL  # 100% of 3 needs all three


def test_the_admin_editor_saves_the_group_and_any_one_approver_on_a_mailbox_type(client, db_session, world):
    from tests.test_request_types import admin

    admin(client, db_session)
    base = {
        "name": "Forward my mail", "pass_percent": 51, "is_active": "on", "mailbox_action": "forwarding", "approval_group_id": world.group.id,
        "default_team_id": world.team.id, "default_category_id": world.category.id, "default_impact": "low", "default_urgency": "low",
    }
    assert client.post(f"/admin/request-types/{world.forwarding.id}", data=base, follow_redirects=False).status_code == 303
    db_session.refresh(world.forwarding)
    assert world.forwarding.approval_group_id == world.group.id and not world.forwarding.any_one_approver
    assert client.post(f"/admin/request-types/{world.forwarding.id}", data={**base, "any_one_approver": "on"}, follow_redirects=False).status_code == 303
    db_session.refresh(world.forwarding)
    assert world.forwarding.any_one_approver
    details = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "request_type", AuditLog.action == "edited")).all()]
    assert any("any one approver is enough False -> True" in d for d in details)


# ---- Privacy: the subject access export ----------------------------------------------------------------------------------------------


def export_section(db_session, address, named):
    admin = db_session.exec(select(User).where(User.email == "dpo@example.com")).first()
    if admin is None:
        admin = User(email="dpo@example.com", display_name="DPO", role=UserRole.SUPER_ADMIN)
        db_session.add(admin)
        db_session.commit()
    archive = zipfile.ZipFile(io.BytesIO(build_export(db_session, get_storage(), address, admin, name_other_person=named).file.read()))
    return archive.read("mailbox_changes.json").decode("utf-8")


def test_the_export_names_nobody_else_for_the_requester_the_owner_or_the_target_unless_ticked(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, target_object_id=FRIEND_OID, target_name="Jane Friend")
    answer(db_session, ticket, IT1, "approved", NOTE)
    run(db_session, changes(db_session, ticket)[0].starts_at + timedelta(minutes=1))
    alex, bee, jane = OWNER, BEE, "jane@example.com"
    # asked about the requester: the owner and the target are another person unless ticked
    plain = export_section(db_session, alex, False)
    assert bee not in plain and jane not in plain and alex in plain
    rows = json.loads(plain)
    assert rows[0]["mailbox"] == "another person's" and rows[0]["shared_with"] == "another person" and rows[0]["asked_by"] == alex
    assert bee in export_section(db_session, alex, True) and jane in export_section(db_session, alex, True)
    # asked about the mailbox owner, who did not ask: the requester and the target are another person
    plain = export_section(db_session, bee, False)
    assert alex not in plain and jane not in plain and bee in plain
    rows = json.loads(plain)
    assert rows[0]["mailbox"] == bee and rows[0]["shared_with"] == "another person" and rows[0]["asked_by"] == "another person"
    assert alex in export_section(db_session, bee, True)
    # asked about the person the mail was forwarded to
    plain = export_section(db_session, jane, False)
    assert alex not in plain and bee not in plain and jane in plain
    rows = json.loads(plain)
    assert rows[0]["mailbox"] == "another person's" and rows[0]["shared_with"] == jane and rows[0]["asked_by"] == "another person"


def test_the_export_for_the_requester_who_is_also_the_target_names_only_the_owner_when_ticked(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    plain = export_section(db_session, OWNER, False)
    assert BEE not in plain
    row = json.loads(plain)[0]
    assert row["shared_with"] == OWNER and row["asked_by"] == OWNER and row["mailbox"] == "another person's"


def test_the_it_approvers_own_vote_is_exported_to_them_with_the_note(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, IT1, "approved", NOTE)
    admin = User(email="dpo@example.com", display_name="DPO", role=UserRole.SUPER_ADMIN)
    db_session.add(admin)
    db_session.commit()
    archive = zipfile.ZipFile(io.BytesIO(build_export(db_session, get_storage(), IT1, admin).file.read()))
    approvals = json.loads(archive.read("approvals.json"))
    assert approvals and approvals[0]["comment"] == NOTE
    other = zipfile.ZipFile(io.BytesIO(build_export(db_session, get_storage(), OWNER, admin).file.read()))
    assert NOTE not in other.read("approvals.json").decode() and IT1 not in other.read("mailbox_changes.json").decode()


# ---- Review findings ------------------------------------------------------------------------------------------------------------------


def test_the_approval_email_can_be_built_for_the_owner_who_is_not_in_the_group(client, db_session, world, dev_mail):
    from app.mail import build_approval_mail

    raised(client, db_session, world)
    row = [m for m in mails(db_session) if m.kind == "approval" and m.to_address == BEE][0]
    built = build_approval_mail(db_session, row)
    assert BEE in str(built.to) and "/approvals/" in built.body


def test_the_requesters_progress_line_follows_any_one_approver(client, db_session, world, dev_mail):
    from app.messages import _approval_progress

    world.forwarding.pass_percent = 100
    db_session.add(world.forwarding)
    db_session.commit()
    ticket = raised(client, db_session, world)
    assert _approval_progress(db_session, ticket_now(db_session, ticket)) == "Waiting for approval"


def test_cancel_is_refused_while_the_request_waits_for_approval(client, db_session, world, dev_mail):
    start_date, start_time = later(5)
    ticket = raised(client, db_session, world, start_mode="later", start_date=start_date, start_time=start_time)
    (change,) = changes(db_session, ticket)
    page = client.get(f"/portal/{ticket.number}").text
    assert "Cancel this request" not in page
    client.post(f"/portal/{ticket.number}/mailbox/cancel", data={"change_id": change.id}, follow_redirects=False)
    assert changes(db_session, ticket)[0].state == "scheduled" and ticket_now(db_session, ticket).status == TicketStatus.AWAITING_APPROVAL


def test_only_a_super_admin_changes_who_approves_a_mailbox_type(client, db_session, world):
    from app.models import UserPermission
    from app.permissions import MANAGE_REQUEST_TEMPLATES
    from tests.conftest import login_as, set_role

    agent_id = login_as(client, "desk@example.com")
    set_role(db_session, agent_id, UserRole.AGENT)
    db_session.add(UserPermission(user_id=agent_id, permission=MANAGE_REQUEST_TEMPLATES))
    db_session.commit()
    base = {
        "name": "Forward my mail", "pass_percent": 51, "is_active": "on", "mailbox_action": "forwarding", "approval_group_id": world.group.id,
        "default_team_id": world.team.id, "default_category_id": world.category.id, "default_impact": "low", "default_urgency": "low",
    }
    assert client.post(f"/admin/request-types/{world.forwarding.id}", data=base).status_code == 403  # any_one_approver switched off
    assert client.post(f"/admin/request-types/{world.forwarding.id}", data={**base, "any_one_approver": "on"}, follow_redirects=False).status_code == 303

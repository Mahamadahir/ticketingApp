"""Access to someone else's mailbox for a third person, and a declined mailbox request that is reopened (#325). The fixtures and helpers
are those of the #311 tests (tests/test_mailbox_on_behalf.py)."""

import html
import json
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import Message, MessageDirection, TeamMembership, Ticket, TicketApproval, TicketStatus, UserRole, MailboxChange
from app.settings_store import MAILBOX_TARGET_BLOCKLIST, set_setting
from tests.test_mailbox_on_behalf import (  # noqa: F401  (fixtures and helpers)
    BEE, BEE_OID, IT1, NOTE, answer, approval_of, ask, changes, export_section, fake_exchange, mail_to, raised, run, ticket_now, world,
)
from tests.test_portal_mailbox import FRIEND_OID, OWNER, OWNER_OID, directory, person, raw_user, signed_in  # noqa: F401
from tests.test_resolution import dev_mail  # noqa: F401  (a fixture)

JANE = "jane@example.com"


def outbound_notes(db_session, ticket):
    db_session.expire_all()
    rows = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    return [m.body for m in rows if m.direction == MessageDirection.OUTBOUND]


def test_access_to_someone_elses_mailbox_can_be_given_to_a_third_person_and_the_warning_names_them(client, db_session, world, dev_mail):
    signed_in(client, db_session)
    person(db_session, JANE, "Jane Friend")  # the grantee has an account, so emails use their name
    first = ask(client, world.access, confirm_access=None, send_as="on", target_object_id=FRIEND_OID, target_name="Jane Friend")
    warning = html.unescape(first.text)
    assert first.status_code == 200 and "Jane Friend will be able to read all mail already in Bee Owner's mailbox" in warning
    assert "Jane Friend will be able to send email that appears to come from Bee Owner" in warning and "You will be able" not in warning
    assert db_session.exec(select(Ticket)).first() is None
    assert ask(client, world.access, send_as="on", target_object_id=FRIEND_OID, target_name="Jane Friend").status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    full, send = changes(db_session, ticket)
    assert {full.owner_address, send.owner_address} == {BEE} and {full.target_address, send.target_address} == {JANE}
    assert ticket.subject == "Give Jane Friend access to Bee Owner's mailbox"
    answer(db_session, ticket, BEE, "approved", "")
    assert run(db_session, full.starts_at + timedelta(minutes=1)) == 2
    assert ticket_now(db_session, ticket).status == TicketStatus.RESOLVED
    # the grantee is told they have it; the owner is told who was given it; the requester (neither) reads it in the notes
    to_jane = mail_to(db_session, JANE)
    assert len(to_jane) == 2 and any("You now have Full access to Bee Owner's mailbox" in m.body for m in to_jane)
    owner_mail = mail_to(db_session, BEE)
    assert len(owner_mail) == 2 and any("Jane Friend now has Full access to your mailbox" in m.body for m in owner_mail)
    assert any("Jane Friend can now send email as you" in m.body for m in owner_mail)
    assert not mail_to(db_session, OWNER)
    assert any("Jane Friend now has Full access to Bee Owner's mailbox" in body for body in outbound_notes(db_session, ticket))


def test_without_a_chosen_person_the_access_is_still_for_the_requester(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, kind=world.access)
    (change,) = changes(db_session, ticket)
    assert change.target_address == OWNER and change.owner_address == BEE


def test_the_voters_are_told_who_the_access_is_for(client, db_session, world, dev_mail):
    from app import portal_mailbox

    person(db_session, JANE, "Jane Friend")
    ticket = raised(client, db_session, world, kind=world.access, target_object_id=FRIEND_OID, target_name="Jane Friend")
    assert "Full access for Jane Friend" in portal_mailbox.approval_summary(db_session, ticket)


def test_the_third_person_cannot_be_the_owner_of_the_mailbox(client, db_session, world):
    signed_in(client, db_session)
    response = ask(client, world.access, target_object_id=BEE_OID, target_name="Bee Owner")
    assert response.status_code == 400 and "whose mailbox it is" in response.text
    assert db_session.exec(select(Ticket)).first() is None


def test_the_owner_cannot_be_the_requester_for_access_either(client, db_session, world):
    signed_in(client, db_session)
    response = ask(client, world.access, owner_object_id=OWNER_OID, target_object_id=FRIEND_OID)
    assert response.status_code == 400 and "your own mailbox" in html.unescape(response.text)
    assert db_session.exec(select(Ticket)).first() is None


def test_the_third_person_is_checked_like_any_target(client, db_session, world, directory, monkeypatch):
    from app.config import settings

    signed_in(client, db_session)
    monkeypatch.setattr(settings, "graph_shared_mailbox", "itsupport@example.com")
    directory["sm"] = raw_user("sm", "Support Desk", "itsupport@example.com")
    directory["guest"] = raw_user("guest", "Gus Guest", "gus@other.com", kind="Guest")
    directory["off"] = raw_user("off", "Olly Off", "olly@example.com", enabled=False)
    directory["blocked"] = raw_user("blocked", "Bob Blocked", "blocked@example.com")
    set_setting(db_session, MAILBOX_TARGET_BLOCKLIST, "blocked@example.com")
    db_session.commit()
    for oid in ("sm", "guest", "off", "blocked", "no-such-person"):
        assert ask(client, world.access, target_object_id=oid).status_code == 400, oid
    assert db_session.exec(select(Ticket)).first() is None


def test_a_second_request_for_the_same_third_person_is_refused_and_the_owner_lock_is_taken(client, db_session, world, monkeypatch):
    from app import mailbox_changes, portal_mailbox

    taken = []
    real = mailbox_changes.lock_owner
    monkeypatch.setattr(portal_mailbox, "lock_owner", lambda session, address: (taken.append(address), real(session, address)))
    signed_in(client, db_session)
    assert ask(client, world.access, target_object_id=FRIEND_OID, target_name="Jane Friend").status_code == 303
    again = ask(client, world.access, target_object_id=FRIEND_OID, target_name="Jane Friend")
    assert again.status_code == 400 and "Jane Friend already has Full access on Bee Owner's mailbox" in html.unescape(again.text)
    assert taken == [BEE, BEE]
    assert ask(client, world.access).status_code == 303  # the same permission for the requester is a different change


def test_the_export_covers_the_requester_the_owner_and_the_grantee_of_an_access_request(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world, kind=world.access, target_object_id=FRIEND_OID, target_name="Jane Friend")
    answer(db_session, ticket, IT1, "approved", NOTE)
    run(db_session, changes(db_session, ticket)[0].starts_at + timedelta(minutes=1))
    alex, bee, jane = OWNER, BEE, JANE
    plain = export_section(db_session, alex, False)  # the requester: neither the owner nor the grantee is named
    assert bee not in plain and jane not in plain
    row = json.loads(plain)[0]
    assert row["mailbox"] == "another person's" and row["shared_with"] == "another person" and row["asked_by"] == alex
    named = export_section(db_session, alex, True)
    assert bee in named and jane in named
    plain = export_section(db_session, bee, False)  # the owner
    assert alex not in plain and jane not in plain
    row = json.loads(plain)[0]
    assert row["mailbox"] == bee and row["shared_with"] == "another person" and row["asked_by"] == "another person"
    named = export_section(db_session, bee, True)
    assert alex in named and jane in named
    plain = export_section(db_session, jane, False)  # the grantee
    assert alex not in plain and bee not in plain
    row = json.loads(plain)[0]
    assert row["mailbox"] == "another person's" and row["shared_with"] == jane and row["asked_by"] == "another person"
    named = export_section(db_session, jane, True)
    assert alex in named and bee in named


# ---- A declined request that is reopened ---------------------------------------------------------------------------------------------


def declined(client, db_session, world):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "declined", "")
    assert ticket_now(db_session, ticket).status == TicketStatus.CLOSED
    agent = person(db_session, "agent@example.com", "Agent Smith", role=UserRole.AGENT)
    db_session.add(TeamMembership(user_id=agent.id, team_id=world.team.id))
    db_session.commit()
    return ticket, agent


def reopen(db_session, ticket, agent):
    from app.ticket_service import set_status

    set_status(db_session, ticket_now(db_session, ticket), TicketStatus.OPEN, agent.email)
    db_session.commit()


def test_a_declined_request_reopened_keeps_its_changes_cancelled_and_does_not_ask_the_approvers_again(client, db_session, world, dev_mail):
    ticket, agent = declined(client, db_session, world)
    reopen(db_session, ticket, agent)
    ticket = ticket_now(db_session, ticket)
    assert ticket.status == TicketStatus.OPEN  # not sent back to Awaiting approval: nothing is left to approve
    (change,) = changes(db_session, ticket)
    assert change.state == "cancelled"
    assert approval_of(db_session, ticket).status == "declined"
    assert run(db_session, change.starts_at + timedelta(hours=1)) == 0
    db_session.expire_all()
    assert db_session.get(MailboxChange, change.id).state == "cancelled"


def test_the_portal_page_says_it_was_declined_with_no_controls_before_and_after_a_reopen(client, db_session, world, dev_mail):
    ticket, agent = declined(client, db_session, world)
    signed_in(client, db_session)
    page = client.get(f"/portal/{ticket.number}").text
    assert "This request was declined. Submit a new request." in page and "Declined" in page
    assert "/mailbox/cancel" not in page and "/mailbox/stop" not in page
    reopen(db_session, ticket, agent)
    page = client.get(f"/portal/{ticket.number}").text
    assert "This request was declined. Submit a new request." in page
    assert "/mailbox/cancel" not in page and "/mailbox/stop" not in page and "Waiting for approval" not in page


def test_the_agent_page_says_it_was_declined_after_a_reopen(client, db_session, world, dev_mail):
    from tests.conftest import login_as

    ticket, agent = declined(client, db_session, world)
    reopen(db_session, ticket, agent)
    login_as(client, agent.email)
    page = client.get(f"/tickets/{ticket.number}").text
    assert "This request was declined. Submit a new request." in page
    assert "mbx_retry" not in page and "mbx_cancel" not in page


def test_an_approved_request_does_not_say_it_was_declined(client, db_session, world, dev_mail):
    ticket = raised(client, db_session, world)
    answer(db_session, ticket, BEE, "approved", "")
    assert "was declined" not in client.get(f"/portal/{ticket.number}").text


def later_date(days):
    from datetime import datetime, timezone

    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def test_the_cancel_step_of_a_request_for_someone_elses_mailbox_names_their_mailbox(client, db_session, world, dev_mail):
    ticket = raised(
        client, db_session, world, start_mode="later", start_date=later_date(5), start_time="09:00", end_date=later_date(6), end_time="09:00",
    )
    answer(db_session, ticket, BEE, "approved", "")
    signed_in(client, db_session)
    page = html.unescape(client.get(f"/portal/{ticket.number}").text)
    assert "Nothing will be changed on Bee Owner's mailbox" in page and "Nothing will be changed on your mailbox" not in page


def test_two_people_forwarding_to_each_other_at_once_take_turns_on_both_mailboxes(client, db_session, world, directory, monkeypatch):
    from app import mailbox_changes, portal_mailbox

    taken = []
    real = mailbox_changes.lock_owner
    monkeypatch.setattr(portal_mailbox, "lock_owner", lambda session, address: (taken.append(address), real(session, address)))
    signed_in(client, db_session)
    assert ask(client, world.no_group, whose="mine", target_object_id=FRIEND_OID, target_name="Jane Friend").status_code == 303
    assert taken[:2] == sorted([OWNER, JANE])  # the loop check is under both locks, taken in a fixed order

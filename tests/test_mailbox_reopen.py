"""#363 (part of #305): a finished mailbox request is not reopened by its requester. The portal's Reopen becomes Make a new request,
filled in again from the finished request; the reopen POST, the emailed reopen link and a reply by email or in the portal do not
reopen it; an agent still reopens it, and the pages say the change has ended. Nothing here starts anything in Exchange."""

import re
from datetime import timedelta

import pytest
from sqlmodel import select

from app import mail, outbox
from app.config import settings
from app.inbound import REPLY_ADDED, ingest_mail
from app.main import build_app
from app.models import ActionToken, Message, MailboxChange, OutboundEmail, Ticket, TicketEvent, TicketStatus, User, UserRole
from app.resolution import resolve_ticket
from tests.conftest import add_membership, login_as, set_role
from tests.test_inbound_mail import mail as incoming
from tests.test_inbound_mail import mailbox, storage  # noqa: F401  (fixtures)
from tests.test_portal_mailbox import (  # noqa: F401  (fixtures and helpers)
    FRIEND_OID, OWNER, directory, fake_exchange, friend, now, only_change, post, signed_in, world,
)

BASE = "https://tickets.example.org"


@pytest.fixture(autouse=True)
def urls(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "web_base_url", BASE)


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def finished_request(client, db_session, world, *, send_email=False, **form):
    """A forwarding request made through the portal form, its change ended and the ticket resolved."""
    signed_in(client, db_session)
    assert post(client, world, **form).status_code == 303
    change = only_change(db_session)
    ticket = db_session.get(Ticket, change.ticket_id)
    change.state, change.applied_at, change.removed_at = "ended", now() - timedelta(hours=2), now() - timedelta(hours=1)
    db_session.add(change)
    actor = None
    if send_email:  # an agent's resolution is the one that emails the requester; the system's own is a notice
        actor = User(email="agent@example.com", display_name="Agent Ann", role=UserRole.AGENT)
        db_session.add(actor)
        db_session.flush()
    resolve_ticket(db_session, ticket, actor, "Forwarding has stopped.", send_email=send_email)
    db_session.commit()
    return fresh(db_session, ticket)


def ordinary_resolved(client, db_session):
    from tests.test_reopening import finish
    from tests.test_reply_notification import assigned_ticket

    ticket, _, _ = assigned_ticket(client, db_session)
    return finish(db_session, ticket)


# ---- The portal --------------------------------------------------------------------------------------------------


def test_a_resolved_mailbox_request_offers_make_a_new_request_not_reopen(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    page = client.get(f"/portal/{ticket.number}").text

    assert "Make a new request" in page
    assert f"/portal/new?request_type_id={world.type.id}&amp;again={ticket.number}" in page
    assert "This request has finished. To set it up again, make a new request." in page
    assert "Reopen this request" not in page and f"/portal/{ticket.number}/reopen" not in page
    assert "you can reopen it" not in page.lower()  # the status wording agrees (requester_status)


def test_my_requests_offers_the_same_link_not_a_reopen_button(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    page = client.get("/portal?tab=finished").text

    assert f"again={ticket.number}" in page and "Make a new request" in page
    assert f"/portal/{ticket.number}/reopen" not in page
    assert "you can reopen it" not in page.lower()


def test_an_ordinary_resolved_request_still_shows_reopen(client, db_session):
    ticket = ordinary_resolved(client, db_session)
    client.post("/auth/logout")
    login_as(client, "jo@example.com", "Jo")

    page = client.get(f"/portal/{ticket.number}").text

    assert "Reopen this request" in page and "Make a new request" not in page


def test_the_new_request_form_is_filled_in_with_the_same_person_and_no_times(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world, keep_copy=None, confirm_no_copy="1")

    page = client.get(f"/portal/new?request_type_id={world.type.id}&again={ticket.number}").text

    assert f'value="{FRIEND_OID}"' in page and "jane@example.com" in page
    assert re.search(r'name="keep_copy"(?![^>]*checked)', page)  # the copy setting is the old one: off
    assert re.search(r'name="end_date"[^>]*value=""|name="end_date"(?![^>]*value="\d)', page)
    assert 'value="now"' in page


def test_the_prefilled_copy_is_on_when_the_old_request_kept_one(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    page = client.get(f"/portal/new?again={ticket.number}").text  # the kind comes from the request, not the address

    assert re.search(r'name="keep_copy"[^>]*checked', page)
    assert f'value="{FRIEND_OID}"' in page


def test_someone_elses_request_number_gives_the_ordinary_form(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)
    client.post("/auth/logout")
    login_as(client, "mallory@example.com", "Mallory")

    page = client.get(f"/portal/new?request_type_id={world.type.id}&again={ticket.number}").text

    assert FRIEND_OID not in page and "Jane Friend" not in page


def test_an_ordinary_ticket_number_gives_the_ordinary_form(client, db_session):
    ticket = ordinary_resolved(client, db_session)
    client.post("/auth/logout")
    login_as(client, "jo@example.com", "Jo")

    assert client.get(f"/portal/new?again={ticket.number}").status_code == 200


def test_the_filled_in_form_checks_the_person_again_on_submit(client, db_session, world, friend, directory):
    ticket = finished_request(client, db_session, world)
    directory[FRIEND_OID]["accountEnabled"] = False  # they have left since

    page = client.get(f"/portal/new?again={ticket.number}").text
    refused = post(client, world)

    assert FRIEND_OID in page  # shown as chosen: the id is only checked on submit, as on any form
    assert refused.status_code == 400 and "switched off" in refused.text
    assert len(db_session.exec(select(MailboxChange)).all()) == 1  # nothing new was made


def test_the_filled_in_form_refuses_a_blocklisted_person_on_submit(client, db_session, world, friend, directory):
    from app.settings_store import MAILBOX_TARGET_BLOCKLIST, set_setting

    ticket = finished_request(client, db_session, world)
    set_setting(db_session, MAILBOX_TARGET_BLOCKLIST, "jane@example.com")
    db_session.commit()

    assert client.get(f"/portal/new?again={ticket.number}").status_code == 200
    refused = post(client, world)

    assert refused.status_code == 400 and len(db_session.exec(select(MailboxChange)).all()) == 1


# ---- The reopen POST ---------------------------------------------------------------------------------------------


def test_the_reopen_post_is_refused_for_a_mailbox_request(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    response = client.post(f"/portal/{ticket.number}/reopen", follow_redirects=False)

    assert response.status_code == 409
    assert "make a new request" in response.text and f"again={ticket.number}" in response.text
    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.RESOLVED, 0)


def test_the_reopen_post_still_reopens_an_ordinary_request(client, db_session):
    ticket = ordinary_resolved(client, db_session)
    client.post("/auth/logout")
    login_as(client, "jo@example.com", "Jo")

    assert client.post(f"/portal/{ticket.number}/reopen", follow_redirects=False).status_code == 303

    assert fresh(db_session, ticket).status != TicketStatus.RESOLVED


# ---- The emailed link --------------------------------------------------------------------------------------------


def resolution_mail(db_session, ticket):
    db_session.expire_all()
    [row] = db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)).all()
    built = mail.build_message_mail(db_session, row)
    db_session.commit()
    return row, built


def test_the_resolution_email_of_a_mailbox_request_has_a_new_request_link_and_no_reopen(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world, send_email=True)

    row, built = resolution_mail(db_session, ticket)

    assert f"Make a new request: {BASE}/portal/new?request_type_id={world.type.id}&again={ticket.number}" in built.body
    assert "/reopen/" not in built.body and "reopen" not in built.body.lower()
    assert "/feedback/" in built.body  # feedback is unchanged
    stored = db_session.get(Message, row.message_id).body
    assert "personal reopen link" not in stored and "new request is in the email" in stored  # what the portal timeline shows
    assert [t.kind for t in db_session.exec(select(ActionToken).where(ActionToken.ticket_id == ticket.id)).all()] == ["feedback"]


def test_an_ordinary_resolution_email_still_has_the_reopen_link(client, db_session):
    from tests.test_action_links import resolved_ticket

    ticket = resolved_ticket(client, db_session)

    _, built = resolution_mail(db_session, ticket)

    assert "/reopen/" in built.body and "Make a new request" not in built.body


def test_an_old_reopen_link_on_a_mailbox_request_shows_the_new_request_page_and_reopens_nothing(client, db_session, world, friend):
    from fastapi.testclient import TestClient

    from app import action_links

    ticket = finished_request(client, db_session, world)
    token = action_links.mint(db_session, ticket, action_links.REOPEN)  # a link sent before this change, say
    db_session.commit()

    with TestClient(build_app("public")) as public:
        shown = public.get(f"/reopen/{token}")
        posted = public.post(f"/reopen/{token}")

    for response in (shown, posted):
        assert response.status_code == 200
        assert "make a new request" in response.text and f"again={ticket.number}" in response.text
        assert "Reopen this ticket" not in response.text
    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.RESOLVED, 0)
    assert db_session.exec(select(ActionToken)).one().used_at is None


def test_a_doctored_link_post_cannot_reopen_through_the_helper(client, db_session, world, friend):
    from app import action_links

    ticket = finished_request(client, db_session, world)
    token = action_links.mint(db_session, ticket, action_links.REOPEN)
    db_session.commit()
    row = action_links.find(db_session, action_links.REOPEN, token)

    with pytest.raises(action_links.LinkError):
        action_links.reopen_from_link(db_session, row, ticket)


# ---- A reply ---------------------------------------------------------------------------------------------------


def test_a_reply_by_email_to_a_resolved_mailbox_request_is_recorded_and_does_not_reopen_it(client, db_session, world, friend, mailbox):
    ticket = finished_request(client, db_session, world)

    result = ingest_mail(db_session, incoming("r1", sender=OWNER, subject=f"Re: [{ticket.reference}] {ticket.subject}", body="Please turn it back on"))
    db_session.commit()

    assert result.outcome == REPLY_ADDED
    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.RESOLVED, 0)
    assert db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "reply_not_reopened")).one().detail == "Please turn it back on"
    assert db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "reopened")).all() == []
    assert "Please turn it back on" in [m.body for m in ticket.messages]  # kept on the ticket


def test_a_reply_by_email_to_an_ordinary_resolved_ticket_still_reopens_it(db_session, mailbox):
    from tests.test_inbound_mail import open_ticket, triage_by_hand
    from tests.test_reopening import finish

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)

    ingest_mail(db_session, incoming("r2", subject=f"Re: [{ticket.reference}] Printer jam", body="Still broken"))
    db_session.commit()

    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_a_portal_reply_to_a_resolved_mailbox_request_does_not_reopen_it_and_says_so(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)

    response = client.post(f"/portal/{ticket.number}/comment", data={"body": "Can you turn it back on?"})

    assert response.status_code == 200
    assert "has not reopened this request" in response.text and "make a new request" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


# ---- An agent ----------------------------------------------------------------------------------------------------


def test_an_agent_reopens_a_mailbox_request_and_both_pages_say_the_change_has_ended(client, db_session, world, friend):
    ticket = finished_request(client, db_session, world)
    change_before = only_change(db_session).state
    client.post("/auth/logout")
    agent_id = login_as(client, "agent@example.com", "Agent Ann")
    set_role(db_session, agent_id, UserRole.AGENT)
    add_membership(db_session, agent_id, world.team.id)

    response = client.post(f"/tickets/{ticket.number}/reopen", data={"reason": "Needs a look"}, follow_redirects=True)

    assert response.status_code == 200
    ticket = fresh(db_session, ticket)
    assert ticket.status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS) and ticket.reopen_count == 1
    assert only_change(db_session).state == change_before == "ended"  # nothing restarted
    agent_page = client.get(f"/tickets/{ticket.number}").text
    assert "This change has ended. Submit a new request to start it again." in agent_page
    client.post("/auth/logout")
    login_as(client, OWNER, "Alex Owner")
    portal_page = client.get(f"/portal/{ticket.number}").text
    assert "This change has ended. Submit a new request to start it again." in portal_page
    assert "Make a new request" not in portal_page  # reopened by an agent: the ordinary open request page


def test_the_ended_line_is_not_shown_while_the_change_is_still_on(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world).status_code == 303
    ticket = db_session.get(Ticket, only_change(db_session).ticket_id)

    assert "This change has ended" not in client.get(f"/portal/{ticket.number}").text

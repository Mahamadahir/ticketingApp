"""3D: email out of the shared mailbox. What is queued and when (app/outbox.py), what each email says
and which rules are asked again at send time (app/mail.py), how it is delivered, retried and capped,
the Graph call itself, the admin page, and the settings that switch it on."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import approval_links, mail, outbox
from app.config import Settings, settings, unsafe_setting_errors
from app.database import engine
from app.graph_client import GraphClient
from app.jobs import JOBS, mail_out
from app.messages import add_note, mail_subject, preview_email, record_notice
from app.models import (
    Message,
    OutboundEmail,
    Setting,
    Ticket,
    TicketApproval,
    TicketStatus,
    User,
    UserRole,
)
from app.settings_store import AUTOMATIC_EMAIL_CAP, TELL_REQUESTER_RAISED_FOR, set_setting
from app.teams import MAX_ATTEMPTS, Unsendable
from tests.conftest import login_as
from tests.test_approvals import person, raised_and_triaged, request_type
from tests.test_email_preview import follow, ticket_for_email
from tests.test_raising import raise_it
from tests.test_splitting import split, subs_of, world


@pytest.fixture
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def rows(db_session, *kinds):
    db_session.expire_all()
    query = select(OutboundEmail).order_by(OutboundEmail.id)
    if kinds:
        query = query.where(OutboundEmail.kind.in_(kinds))
    return db_session.exec(query).all()


def email(client, ticket, body="Please restart it", **extra):
    response = client.post(f"/tickets/{ticket.number}/email", data={"body": body, **extra})
    assert response.status_code == 200, response.text
    return response


def reply_row(db_session, ticket):
    [row] = [r for r in rows(db_session, outbox.REPLY) if r.ticket_id == ticket.id]
    return row


# ---- Queuing --------------------------------------------------------------------------------------


def test_with_sending_off_nothing_is_queued(client, db_session):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    record_notice(db_session, db_session.get(Ticket, ticket.id), "Done")
    db_session.commit()
    assert rows(db_session) == [] and settings.mail_sending == "off"


def test_an_agents_reply_queues_one_email_to_the_requester(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)

    row = reply_row(db_session, ticket)

    message = db_session.get(Message, row.message_id)
    assert (row.kind, row.to_address, row.sent_at, message.body) == ("reply", "walkin@example.com", None, "Please restart it")


def test_a_message_can_never_be_emailed_twice(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    row = reply_row(db_session, ticket)

    db_session.add(OutboundEmail(kind="reply", ticket_id=ticket.id, message_id=row.message_id, to_address="x@example.com"))
    with pytest.raises(Exception):  # the database refuses the second email
        db_session.commit()
    db_session.rollback()
    assert len(rows(db_session, "reply")) == 1


def test_an_automatic_notice_is_queued_as_a_notice(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    record_notice(db_session, db_session.get(Ticket, ticket.id), "Your request has been approved.")
    db_session.commit()
    [row] = rows(db_session, outbox.NOTICE)
    assert row.to_address == "walkin@example.com" and row.message_id is not None


def test_an_internal_note_is_never_queued(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    agent = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    add_note(db_session, db_session.get(Ticket, ticket.id), agent, "The user is a bit grumpy")
    db_session.commit()
    assert rows(db_session, "reply", "notice") == []  # only the raised-for-you email from the set-up


def test_raising_a_ticket_for_someone_queues_the_notice_only_if_they_are_to_be_told(client, db_session, dev_mail):
    first = ticket_for_email(client, db_session, subject="Told")
    assert [r.kind for r in rows(db_session)] == ["raised_for_you"]

    set_setting(db_session, TELL_REQUESTER_RAISED_FOR, "off")
    db_session.commit()
    raise_it(
        client, subject="Not told", for_email="other@example.com", category_id=first.category_id,
        impact="low", urgency="low",
    )
    assert len(rows(db_session)) == 1  # nothing new


def test_each_approver_gets_one_email(client, db_session, dev_mail):
    for address in ("boss@example.com", "lead@example.com"):
        person(client, db_session, address)
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"]))

    sent_to = sorted(r.to_address for r in rows(db_session, outbox.APPROVAL))

    assert sent_to == ["boss@example.com", "lead@example.com"]
    assert all(r.ticket_id == ticket.id and r.approval_id for r in rows(db_session, outbox.APPROVAL))


# ---- What an email says ---------------------------------------------------------------------------


def test_a_reply_goes_under_the_reference_and_copies_who_the_message_recorded(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session, subject="Printer jam")
    follow(client, ticket, "boss@example.com")
    email(client, ticket, body="Fixed it?")

    built = mail.build_message_mail(db_session, reply_row(db_session, ticket))

    assert built.subject == f"[{ticket.reference}] Printer jam"
    assert (built.to, built.cc, built.body, built.automatic) == ("walkin@example.com", ["boss@example.com"], "Fixed it?", False)


def test_the_preview_shows_the_same_subject_the_email_goes_out_under(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session, subject="Printer jam")
    assert preview_email(db_session, ticket, "Hello").subject == mail_subject(db_session, ticket) == f"[{ticket.reference}] Printer jam"


def test_a_part_of_a_split_request_goes_out_under_its_originals_reference_with_an_about_line(client, db_session, dev_mail):
    original, _ = world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Fix the screen", ""))
    part = subs_of(db_session, original)[0]
    email(client, part, body="On it")

    built = mail.build_message_mail(db_session, reply_row(db_session, part))

    assert built.subject == f"[{original.reference}] {original.subject}"
    assert built.body.startswith(f"About: {part.reference} {part.subject}\n\n") and built.body.endswith("On it")


def test_an_internal_note_cannot_be_built_into_an_email_even_if_a_row_points_at_it(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    agent = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    note = add_note(db_session, db_session.get(Ticket, ticket.id), agent, "Private")
    db_session.flush()
    row = OutboundEmail(kind="reply", ticket_id=ticket.id, message_id=note.id, to_address="walkin@example.com")

    with pytest.raises(Unsendable, match="may not see"):
        mail.build_message_mail(db_session, row)


def test_an_untriaged_ticket_cannot_be_emailed_yet_and_the_email_waits_for_triage(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    row = reply_row(db_session, ticket)
    t = db_session.get(Ticket, ticket.id)
    t.triaged_at = None
    db_session.add(t)
    db_session.commit()
    with pytest.raises(mail.NotReady, match="triaged"):
        mail.build_message_mail(db_session, row)

    now = datetime.now(timezone.utc)
    mail.deliver_mail(db_session, now=now)
    row = reply_row(db_session, ticket)
    assert row.sent_at is None and row.attempts == 0 and row.next_attempt_at == now + mail.NOT_READY_WAIT

    t = db_session.get(Ticket, ticket.id)  # triaged a few minutes later
    t.triaged_at = datetime.now(timezone.utc)
    db_session.add(t)
    db_session.commit()
    mail.deliver_mail(db_session, now=now + timedelta(minutes=11))
    assert reply_row(db_session, ticket).sent_at is not None


def test_the_raised_for_you_email_asks_again_and_a_later_opt_out_wins(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    [row] = rows(db_session, outbox.RAISED_FOR)
    built = mail.build_raised_for_mail(db_session, row)
    assert built.subject.startswith(f"[{ticket.reference}]") and ticket.reference in built.body and built.automatic

    set_setting(db_session, TELL_REQUESTER_RAISED_FOR, "off")
    db_session.commit()
    with pytest.raises(Unsendable, match="not to be told"):
        mail.build_raised_for_mail(db_session, row)


def test_an_approval_email_carries_a_working_link_and_keeps_none_on_the_row(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    [row] = rows(db_session, outbox.APPROVAL)

    built = mail.build_approval_mail(db_session, row)

    link = next(line for line in built.body.splitlines() if line.startswith("https://tickets.example.org/approvals/"))
    approval = db_session.get(TicketApproval, row.approval_id)
    assert approval_links.read_token(link.rsplit("/", 1)[1], 60) == approval.id
    assert built.subject == f"[{ticket.reference}] Approval needed: {ticket.subject}"
    assert link not in built.record and "link left out" in built.record


def test_an_approval_email_is_dropped_once_the_approval_is_decided(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    [row] = rows(db_session, outbox.APPROVAL)
    approval = db_session.get(TicketApproval, row.approval_id)
    approval.decision = "approved"
    db_session.add(approval)
    db_session.commit()
    with pytest.raises(Unsendable, match="no longer needs"):
        mail.build_approval_mail(db_session, row)


def test_an_approval_email_waits_for_the_address_without_using_its_tries_and_goes_once_it_is_set(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "")
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    now = datetime.now(timezone.utc)

    assert mail.deliver_mail(db_session, now=now) == (0, 0)
    [row] = rows(db_session, outbox.APPROVAL)
    assert row.sent_at is None and row.attempts == 0 and "PUBLIC_BASE_URL" in row.last_error
    assert row.next_attempt_at == now + mail.NOT_READY_WAIT

    for hours in (1, 5, 24):  # however long the address stays unset, the tries are not used up
        mail.deliver_mail(db_session, now=now + timedelta(hours=hours))
    assert rows(db_session, outbox.APPROVAL)[0].attempts == 0

    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")
    assert mail.deliver_mail(db_session, now=now + timedelta(hours=25)) == (1, 0)
    assert rows(db_session, outbox.APPROVAL)[0].sent_at is not None


def test_an_email_that_cannot_go_for_three_days_is_given_up_on(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "")
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))

    mail.deliver_mail(db_session, now=datetime.now(timezone.utc) + mail.NOT_READY_LIMIT + timedelta(hours=1))

    [row] = rows(db_session, outbox.APPROVAL)
    assert row.attempts == MAX_ATTEMPTS and "Gave up after 3 days" in row.last_error


def test_the_shared_mailbox_is_never_written_to(client, db_session, dev_mail, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "IT-Support@example.com")
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    row = reply_row(db_session, ticket)
    row.to_address = "it-support@example.com"
    with pytest.raises(Unsendable, match="shared mailbox"):
        mail.build_message_mail(db_session, row)


def test_a_part_of_a_split_quotes_its_own_words_not_its_originals(client, db_session, dev_mail):
    original = ticket_for_email(client, db_session, subject="Two things")
    split(client, original, ("Keyboard", ""), ("Office", ""))
    part = subs_of(db_session, original)[0]
    row = OutboundEmail(kind="raised_for_you", ticket_id=part.id, to_address=part.requester_email)

    built = mail.build_raised_for_mail(db_session, row)

    assert "It is smoking" not in built.body  # the original's first message
    assert f"Reference: {part.reference}" in built.body


def test_the_requesters_words_in_an_approval_email_are_labelled_and_cut(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    first = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).first()
    first.body = "Click here to approve: https://evil.example " + "x" * 3000
    db_session.add(first)
    db_session.commit()
    [row] = rows(db_session, outbox.APPROVAL)

    built = mail.build_approval_mail(db_session, row)

    assert "the requester's own words, not from IT" in built.body
    quote = built.body.split("---\n")[1]
    assert len(quote) < mail.MAX_QUOTED + 40 and "[cut short]" in quote
    assert built.body.index("not from IT") < built.body.index("/approvals/")  # said before the real link


# ---- Delivery -------------------------------------------------------------------------------------


def test_in_dev_mode_an_email_is_recorded_as_sent_with_its_text_and_nothing_leaves(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session, subject="Printer jam")
    email(client, ticket, body="Try turning it off")

    assert mail.deliver_mail(db_session) == (2, 0)  # the raised-for-you email and the reply
    assert mail.deliver_mail(db_session) == (0, 0)  # nothing left, nothing twice

    row = reply_row(db_session, ticket)
    assert row.sent_at is not None and row.subject == f"[{ticket.reference}] Printer jam" and row.body == "Try turning it off"


def test_with_sending_off_the_job_sends_nothing(client, db_session, dev_mail, monkeypatch):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    monkeypatch.setattr(settings, "mail_sending", "off")
    calls = []

    assert mail.deliver_mail(db_session, send=calls.append) == (0, 0) and calls == []
    assert all(r.sent_at is None for r in rows(db_session))


def test_graph_mode_without_its_settings_sends_nothing_and_says_so(client, db_session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "mail_sending", "graph")
    monkeypatch.setattr(settings, "graph_shared_mailbox", "")
    db_session.add(OutboundEmail(kind="reply", ticket_id=ticket_for_email(client, db_session).id, to_address="a@example.com"))
    db_session.commit()

    assert mail.deliver_mail(db_session) == (0, 0)
    assert "GRAPH_SHARED_MAILBOX" in caplog.text


def test_a_failed_send_waits_longer_each_time_and_never_keeps_the_address(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    now = datetime.now(timezone.utc)

    def refuse(_mail):
        raise httpx.ConnectError("could not reach walkin@example.com")

    sent, failed = mail.deliver_mail(db_session, send=refuse, now=now)

    assert (sent, failed) == (0, 2)
    for row in rows(db_session):
        assert row.attempts == 1 and row.sent_at is None and row.next_attempt_at > now
        assert row.last_error == "ConnectError" and "walkin" not in row.last_error


def _status_error(code):
    request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/users/x/sendMail")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize("code,permanent", [(400, True), (404, True), (401, False), (403, False), (429, False), (503, False)])
def test_graph_refusing_the_message_is_final_but_a_credential_or_busy_error_is_retried(client, db_session, dev_mail, code, permanent):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)

    def refuse(_mail):
        raise _status_error(code)

    mail.deliver_mail(db_session, send=refuse)

    row = reply_row(db_session, ticket)
    assert (row.attempts == MAX_ATTEMPTS) is permanent and row.last_error == f"Graph answered {code}"


def test_an_email_that_cannot_be_built_because_of_a_bug_does_not_block_the_ones_behind_it(
    client, db_session, dev_mail, monkeypatch
):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    real = mail.BUILDERS[outbox.RAISED_FOR]

    def broken(session, row):
        raise ValueError("a bug, not a refusal")

    monkeypatch.setitem(mail.BUILDERS, outbox.RAISED_FOR, broken)  # the oldest email is the broken one
    now = datetime.now(timezone.utc)

    assert mail.deliver_mail(db_session, now=now) == (1, 1)  # the reply behind it still went

    broken_row, reply = rows(db_session)
    assert broken_row.sent_at is None and broken_row.attempts == 1 and broken_row.next_attempt_at > now
    assert broken_row.last_error == "Could not be built: ValueError" and reply.sent_at is not None
    monkeypatch.setitem(mail.BUILDERS, outbox.RAISED_FOR, real)
    assert mail.deliver_mail(db_session, now=now + timedelta(hours=1)) == (1, 0)  # fixed, so it goes


def test_an_email_that_went_stays_recorded_when_the_job_dies_afterwards(client, db_session, dev_mail, monkeypatch):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    real = mail._next_due
    calls = []

    def dies_on_the_second_look(session, now):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the database went away")
        return real(session, now)

    monkeypatch.setattr(mail, "_next_due", dies_on_the_second_look)

    with pytest.raises(RuntimeError):
        mail.deliver_mail(db_session)
    db_session.rollback()  # the worker rolls back a job that raised

    first, second = rows(db_session)
    assert first.kind == "raised_for_you" and first.sent_at is not None and second.sent_at is None


def test_a_failed_email_keeps_no_text_it_never_sent(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Secret wording")

    def refuse(_mail):
        raise httpx.ConnectError("down")

    mail.deliver_mail(db_session, send=refuse)

    assert all(r.subject is None and r.body is None and r.cc is None for r in rows(db_session))
    mail.deliver_mail(db_session, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert reply_row(db_session, ticket).body == "Secret wording"


def test_the_job_is_registered_once_and_runs_the_sender(client, db_session, dev_mail):
    names = [job.name for job in JOBS]
    assert names.count("mail-out") == 1 and len(names) == len(set(names))
    ticket = ticket_for_email(client, db_session)
    email(client, ticket)
    mail_out(db_session)
    assert all(r.sent_at is not None for r in rows(db_session))


# ---- The cap on automatic mail ----------------------------------------------------------------------


def test_automatic_emails_to_one_ticket_are_held_at_the_cap_but_agent_replies_never_count(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)  # one raised-for-you email already queued
    t = db_session.get(Ticket, ticket.id)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "2")
    db_session.commit()
    email(client, ticket, body="From the agent")  # queued before the notices, so it is sent before them
    for n in range(3):
        record_notice(db_session, t, f"Notice {n}")
    db_session.commit()
    now = datetime.now(timezone.utc)

    assert mail.deliver_mail(db_session, now=now) == (3, 0)  # raised-for-you, the reply, one notice

    automatic = [r for r in rows(db_session) if r.kind in outbox.AUTOMATIC_TO_REQUESTER]
    assert sum(1 for r in automatic if r.sent_at) == 2  # the cap
    held = [r for r in automatic if not r.sent_at]
    assert len(held) == 2
    for row in held:  # held back, not lost: no try used, not given up, due when the day's count allows
        assert row.attempts == 0 and "Held back" in row.last_error and row.next_attempt_at > now
    assert reply_row(db_session, ticket).sent_at is not None

    assert mail.deliver_mail(db_session, now=now + timedelta(hours=1)) == (0, 0)  # still within the day
    assert mail.deliver_mail(db_session, now=now + timedelta(hours=25)) == (2, 0)  # a day on, both go
    assert all(r.sent_at for r in rows(db_session))


def test_the_cap_only_counts_the_last_day(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    t = db_session.get(Ticket, ticket.id)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    old = rows(db_session, outbox.RAISED_FOR)[0]
    old.sent_at = datetime.now(timezone.utc) - timedelta(hours=30)
    db_session.add(old)
    record_notice(db_session, t, "New")
    db_session.commit()

    assert mail.deliver_mail(db_session) == (1, 0)


# ---- The Graph call --------------------------------------------------------------------------------


def test_the_graph_call_sends_plain_text_from_the_shared_mailbox_with_the_headers(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "it-support@example.org")
    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    client.send_mail(
        "walkin@example.com", "[INC-5] Printer", "Hello", cc=["boss@example.com"],
        headers={"X-Ticket-Reference": "INC-5"},
    )

    assert captured["url"] == "https://graph.microsoft.com/v1.0/users/it-support@example.org/sendMail"
    message = captured["json"]["message"]
    assert message["body"] == {"contentType": "Text", "content": "Hello"}
    assert message["toRecipients"] == [{"emailAddress": {"address": "walkin@example.com"}}]
    assert message["ccRecipients"] == [{"emailAddress": {"address": "boss@example.com"}}]
    assert message["internetMessageHeaders"] == [{"name": "X-Ticket-Reference", "value": "INC-5"}]
    assert captured["json"]["saveToSentItems"] is True and captured["headers"]["Authorization"] == "Bearer token"


def test_automatic_mail_asks_other_mail_systems_not_to_auto_reply(monkeypatch):
    sent = {}
    monkeypatch.setattr(mail.graph_client, "send_mail", lambda to, subject, body, cc=None, headers=None: sent.update(headers=headers))

    mail.send_through_graph(mail.Mail("a@example.com", [], "s", "b", "b", automatic=True, reference="INC-1"))
    assert sent["headers"] == {"X-Ticket-Reference": "INC-1", "X-Auto-Response-Suppress": "All"}
    mail.send_through_graph(mail.Mail("a@example.com", [], "s", "b", "b", automatic=False, reference="INC-1"))
    assert sent["headers"] == {"X-Ticket-Reference": "INC-1"}


# ---- The admin page and the settings -----------------------------------------------------------------


def test_the_admin_page_lists_what_was_queued_and_hides_approval_links(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    mail.deliver_mail(db_session)
    client.post("/auth/logout")
    admin_id = login_as(client, "root@example.com", "Root")
    db_session.get(User, admin_id).role = UserRole.SUPER_ADMIN
    db_session.commit()

    page = client.get("/admin/outbound-mail")

    assert page.status_code == 200 and "Approval needed" in page.text and "boss@example.com" in page.text
    assert "/approvals/" not in page.text and "link left out" in page.text
    assert "sent " in page.text and "dev" in page.text


def test_only_a_super_admin_sees_the_page(client, db_session):
    login_as(client, "agent@example.com", "Agent")
    assert client.get("/admin/outbound-mail").status_code == 403


@pytest.mark.parametrize("value,ok", [("5", True), ("1000", True), ("0", False), ("1001", False), ("many", False)])
def test_the_cap_setting_takes_a_whole_number_from_1_to_1000(client, db_session, value, ok):
    admin_id = login_as(client, "root@example.com", "Root")
    db_session.get(User, admin_id).role = UserRole.SUPER_ADMIN
    db_session.commit()

    response = client.post("/admin/settings", data={"key": AUTOMATIC_EMAIL_CAP, "value": value}, follow_redirects=False)

    assert (response.status_code == 303) is ok


def test_the_sending_mode_must_be_a_known_one_and_dev_mode_stays_out_of_production():
    assert unsafe_setting_errors(Settings(env="dev", mail_sending="graph")) == []
    assert any("MAIL_SENDING must be one of" in e for e in unsafe_setting_errors(Settings(env="dev", mail_sending="yes")))
    assert unsafe_setting_errors(Settings(env="dev", mail_sending="dev")) == []
    production = unsafe_setting_errors(Settings(env="production", mail_sending="dev", session_secret_key="x" * 40, graph_webhook_client_state="y" * 30))
    assert any("only works in the dev environment" in e for e in production)

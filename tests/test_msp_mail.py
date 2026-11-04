"""Mail from the MSP and forwarding it to the user (phase 5B, issue #139): recognised in ingest_mail, recorded by the
visibility rule, never a requester or follower, forwarded with a preview in business hours and automatically out of
them, the files attached to the email to the MSP, and the handoff reporting data."""

import base64
import itertools
import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import mail as mailer
from app.config import settings
from app.followers import NeverFollowListed, add_follower
from app.graph_client import GraphClient
from app.inbound import IGNORED_MSP, IncomingAttachment, REPLY_ADDED, TICKET_CREATED, IncomingMail, ingest_mail, process_message
from app.messages import agent_timeline, requester_visible_messages
from app.models import (
    Attachment,
    AuditLog,
    InboundMail,
    Message,
    MessageDirection,
    MessageVisibility,
    Notification,
    OutboundEmail,
    Ticket,
    TicketEvent,
    TicketFollower,
    TicketStatus,
    User,
    UserRole,
)
from app.msp import (
    FORWARD_DISMISSED,
    FORWARD_FORWARDED,
    FORWARD_NOT_NEEDED,
    FORWARD_PENDING,
    MspError,
    attachment_plan,
    business_hours,
    compose_forward,
    dismiss_forward,
    forward_message,
    forward_waiting_messages,
    handoff_counts,
    is_business_time,
    receive_mail,
    send_to_msp,
)
from app.attachments import store_attachment
from app.settings_store import (
    MSP_ADDRESS,
    MSP_ATTACH_MAX_MB,
    MSP_BANK_HOLIDAYS,
    MSP_BUSINESS_END,
    MSP_BUSINESS_START,
    set_setting,
)
from app.storage import LocalStorage
from app.ticket_service import record_event
from app.time_zones import in_business_hours, parse_clock
from app.visibility import may_view_attachment
from tests.conftest import create_team, login_as, set_role
from tests.test_inbound_mail import FakeGraph, MAILBOX, graph_message, mail as plain_mail
from tests.test_visibility import agent_in

MSP = "support@msp.example"
UTC = timezone.utc
# Wednesday 4 November 2026 in London (GMT): 11:00 is business hours, 20:00 is not.
IN_HOURS = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
AFTER_HOURS = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def msp_world(db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)
    monkeypatch.setattr(settings, "mail_sending", "dev")
    set_setting(db_session, MSP_ADDRESS, MSP)
    db_session.commit()


def with_msp(db_session, team=None, status=TicketStatus.WITH_MSP, sent=True, **fields):
    fields.setdefault("triaged_at", IN_HOURS - timedelta(days=1))
    if sent:
        fields.setdefault("msp_sent_at", IN_HOURS - timedelta(hours=1))
    fields.setdefault("requester_email", "requester@example.com")
    ticket = Ticket(subject="Ticket", team_id=team.id if team else None, status=status, **fields)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


_ids = itertools.count(1)


def from_msp(ticket, message_id=None, to=("jo@example.com",), cc=(MAILBOX,), body="We have booked an engineer.", **fields):
    message_id = message_id or f"msp{next(_ids)}"
    fields = {
        "subject": f"Re: [{ticket.reference}] {ticket.subject}", "conversation_id": f"conv-{message_id}", **fields,
    }
    return IncomingMail(
        graph_message_id=message_id, sender=MSP, body=body, internet_message_id=f"{message_id}@msp.example",
        to=list(to), cc=list(cc), **fields,
    )


def take(db_session, mail, now, **kwargs):
    result = ingest_mail(db_session, mail, now=now, **kwargs)
    db_session.commit()
    return result


def messages_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()


def events_of(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def forwards(db_session):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_forward")).all()


# ---- Business hours ----------------------------------------------------------------------------------------


def test_clock_values_are_parsed_and_validated(client, db_session):
    assert parse_clock("09:00") == time(9, 0) and parse_clock("9:30") == time(9, 30) and parse_clock(" 17:30 ") == time(17, 30)
    assert parse_clock("24:00") is None and parse_clock("09:60") is None and parse_clock("nine") is None and parse_clock("") is None
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    assert client.post("/admin/settings", data={"key": MSP_BUSINESS_START, "value": "25:00"}).status_code == 400
    assert client.post("/admin/settings", data={"key": MSP_BUSINESS_END, "value": ""}).status_code == 400
    assert client.post("/admin/settings", data={"key": MSP_BUSINESS_END, "value": "18:15"}, follow_redirects=False).status_code == 303
    assert client.post("/admin/settings", data={"key": MSP_BUSINESS_START, "value": "8:05"}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert business_hours(db_session) == (time(8, 5), time(18, 15))


def test_business_hours_are_monday_to_friday_in_the_organisation_zone_without_holidays(db_session):
    assert business_hours(db_session) == (time(9, 0), time(17, 30))  # the placeholders
    assert is_business_time(db_session, IN_HOURS)
    assert not is_business_time(db_session, datetime(2026, 11, 4, 8, 59, tzinfo=UTC))
    assert is_business_time(db_session, datetime(2026, 11, 4, 17, 29, tzinfo=UTC))
    assert not is_business_time(db_session, datetime(2026, 11, 4, 17, 30, tzinfo=UTC))  # the end is not inside
    assert not is_business_time(db_session, SATURDAY)
    set_setting(db_session, MSP_BANK_HOLIDAYS, "2026-11-04")
    assert not is_business_time(db_session, IN_HOURS)
    # Summer time: 09:00 in London is 08:00 UTC.
    assert in_business_hours(datetime(2026, 7, 1, 8, 0, tzinfo=UTC), "Europe/London", time(9), time(17, 30))
    assert not in_business_hours(datetime(2026, 7, 1, 7, 59, tzinfo=UTC), "Europe/London", time(9), time(17, 30))
    # An end at or before the start leaves no business hours.
    assert not in_business_hours(IN_HOURS, "Europe/London", time(17), time(9))


# ---- Recognising the MSP -----------------------------------------------------------------------------------


def test_mail_the_user_was_on_is_an_ordinary_public_message_and_changes_no_status(db_session):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    result = take(db_session, from_msp(ticket), IN_HOURS)

    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    [message] = messages_of(db_session, ticket)
    assert (message.sender, message.body, message.msp_forward) == (MSP, "We have booked an engineer.", FORWARD_NOT_NEEDED)
    assert message.visibility == MessageVisibility.PUBLIC and message.direction == MessageDirection.OUTBOUND
    assert message.graph_message_id.startswith("msp")
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP
    assert [m.id for m in requester_visible_messages(db_session, ticket)] == [message.id]
    assert forwards(db_session) == []  # they already have it
    assert db_session.exec(select(OutboundEmail)).all() == []  # no automatic reply, no receipt
    [event] = events_of(db_session, ticket, "msp_mail_received")
    assert (event.to_value, event.actor_email) == (FORWARD_NOT_NEEDED, MSP)
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "msp_mail_received")).one().entity_id == ticket.id


def test_the_msp_is_never_the_requester_or_a_follower(db_session):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    take(db_session, from_msp(ticket, cc=(MAILBOX, "pat@example.com")), IN_HOURS)
    assert db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id)).all() == []
    db_session.refresh(ticket)
    assert ticket.requester_email == "jo@example.com"
    with pytest.raises(NeverFollowListed):
        add_follower(db_session, ticket, MSP, "agent@example.com")
    with pytest.raises(NeverFollowListed):
        add_follower(db_session, ticket, MSP.upper(), "agent@example.com")

    # Copied on the mail that opens a ticket does not make it a follower either.
    opened = take(db_session, plain_mail("m9", sender="jo@example.com", cc=[MSP, "pat@example.com"]), IN_HOURS)
    assert opened.outcome == TICKET_CREATED
    assert [f.email for f in db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == opened.ticket.id)).all()] == ["pat@example.com"]


def test_the_msp_is_never_copied_on_a_users_email(db_session):
    from app.never_follow import listed_among

    assert listed_among(db_session, [MSP, "pat@example.com"]) == {MSP}
    set_setting(db_session, MSP_ADDRESS, "")
    assert listed_among(db_session, [MSP]) == set()  # with no MSP set there is nothing to protect


def test_mail_from_the_msp_never_makes_a_ticket(db_session):
    unsent = with_msp(db_session, sent=False, status=TicketStatus.IN_PROGRESS)  # never sent to the MSP
    elsewhere = IncomingMail(
        graph_message_id="x1", sender=MSP, subject="Hello, a new question", body="Can you help?", internet_message_id="x1@msp.example",
        to=[MAILBOX],
    )
    stray = from_msp(unsent, "x2")
    for mail in (elsewhere, stray):
        assert take(db_session, mail, IN_HOURS).outcome == IGNORED_MSP
    assert db_session.exec(select(Ticket)).all() == [unsent]
    assert messages_of(db_session, unsent) == []
    assert [r.outcome for r in db_session.exec(select(InboundMail).order_by(InboundMail.id)).all()] == [IGNORED_MSP, IGNORED_MSP]
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "msp_mail_ignored")).all()) == 2


def test_it_is_found_like_any_reply_by_the_conversation_or_the_reference(db_session):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    take(db_session, from_msp(ticket, "a"), IN_HOURS)  # by the reference in the subject
    again = from_msp(ticket, "b", subject="No reference here", conversation_id="conv-a")  # by the thread
    assert take(db_session, again, IN_HOURS).ticket.id == ticket.id
    assert len(messages_of(db_session, ticket)) == 2


def test_only_the_new_text_is_recorded_through_graph(db_session):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    graph = FakeGraph()
    graph.messages["g1"] = graph_message(
        "g1", sender=MSP, subject=f"RE: [{ticket.reference}] Printer", body="Booked for Thursday.",
        toRecipients=[{"emailAddress": {"address": "jo@example.com"}}],
        ccRecipients=[{"emailAddress": {"address": MAILBOX}}],
        body_full={},
    )
    graph.messages["g1"]["body"] = {"contentType": "text", "content": "Booked for Thursday.\n\n-----Original Message-----\nthe whole earlier thread"}
    result = process_message(db_session, "g1", graph)
    db_session.commit()
    assert result.outcome == REPLY_ADDED
    [message] = messages_of(db_session, ticket)
    assert message.body == "Booked for Thursday." and message.msp_forward == FORWARD_NOT_NEEDED


def test_a_status_other_than_with_msp_is_left_alone_and_the_assigned_agent_is_told(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = with_msp(db_session, team, status=TicketStatus.IN_PROGRESS, requester_email="jo@example.com", assigned_agent_id=agent_id)
    take(db_session, from_msp(ticket), IN_HOURS)
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS
    [told] = db_session.exec(select(Notification).where(Notification.user_id == agent_id)).all()
    assert told.ticket_id == ticket.id and MSP in told.text


def test_the_requesters_reply_while_with_the_msp_changes_nothing(db_session):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    result = take(db_session, plain_mail("r1", sender="jo@example.com", subject=f"Re: [{ticket.reference}] x", body="Any news?"), IN_HOURS)
    assert result.outcome == REPLY_ADDED
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.WITH_MSP


def test_a_finished_ticket_still_records_a_late_msp_message_without_opening_anything(db_session):
    ticket = with_msp(db_session, status=TicketStatus.CLOSED, requester_email="jo@example.com")
    result = take(db_session, from_msp(ticket), IN_HOURS)
    assert result.outcome == REPLY_ADDED
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.CLOSED and len(db_session.exec(select(Ticket)).all()) == 1


# ---- Left off To and Cc: business hours -------------------------------------------------------------------


def left_off(db_session, now=IN_HOURS, **fields):
    ticket = with_msp(db_session, requester_email="jo@example.com", **fields)
    take(db_session, from_msp(ticket, to=("someone.else@msp.example",)), now)
    [message] = messages_of(db_session, ticket)
    return ticket, message


def test_in_business_hours_it_waits_for_an_agent_and_the_user_cannot_see_it(db_session):
    ticket, message = left_off(db_session)
    assert (message.msp_forward, message.visibility) == (FORWARD_PENDING, MessageVisibility.AGENTS_ONLY)
    assert requester_visible_messages(db_session, ticket) == []
    assert forwards(db_session) == []
    assert [i.message.id for i in agent_timeline(db_session, ticket) if i.message] == [message.id]
    [event] = events_of(db_session, ticket, "msp_mail_received")
    assert event.to_value == FORWARD_PENDING


def test_the_agent_previews_then_forwards_and_it_becomes_visible(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket, message = left_off(db_session, team=team)
    page = client.get(f"/tickets/{ticket.number}")
    assert "The MSP replied without copying the user" in page.text and "We have booked an engineer." in page.text

    preview = client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}/preview")
    assert preview.status_code == 200 and "jo@example.com" in preview.text and "We have booked an engineer." in preview.text
    assert forwards(db_session) == []  # the preview changes nothing

    digest = compose_forward(db_session, ticket, message).digest
    done = client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}", data={"digest": digest})
    assert done.status_code == 200
    [row] = forwards(db_session)
    assert row.to_address == "jo@example.com" and "We have booked an engineer." in row.body and row.sent_at is None
    assert row.subject == f"[{ticket.reference}] {ticket.subject}"
    db_session.refresh(message)
    assert (message.msp_forward, message.visibility) == (FORWARD_FORWARDED, MessageVisibility.PUBLIC)
    assert [m.id for m in requester_visible_messages(db_session, ticket)] == [message.id]
    [event] = events_of(db_session, ticket, "msp_forwarded")
    assert (event.to_value, event.actor_email) == ("agent", "agent@example.com")
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "msp_forwarded")).one().actor_email == "agent@example.com"
    # A second click forwards nothing more.
    assert client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}", data={"digest": digest}).status_code == 400
    assert len(forwards(db_session)) == 1


def test_the_forward_goes_out_with_the_previewed_text_and_only_while_it_is_still_forwarded(db_session):
    ticket, message = left_off(db_session)
    forward_message(db_session, ticket, message, None)
    db_session.commit()
    sent = []
    assert mailer.deliver_mail(db_session, send=sent.append) == (1, 0)
    [mail] = sent
    assert (mail.to, mail.cc, mail.subject, mail.files) == ("jo@example.com", [], f"[{ticket.reference}] {ticket.subject}", [])
    assert "support@msp.example" in mail.body and "We have booked an engineer." in mail.body and ticket.reference in mail.body

    other, other_message = left_off(db_session)  # a second ticket whose message is withdrawn before it goes
    forward_message(db_session, other, other_message, None)
    other_message.msp_forward = FORWARD_DISMISSED
    db_session.add(other_message)
    db_session.commit()
    assert mailer.deliver_mail(db_session, send=sent.append) == (0, 1)
    assert len(sent) == 1


def test_a_message_that_changed_since_the_preview_is_not_forwarded_unseen(db_session):
    ticket, message = left_off(db_session)
    digest = compose_forward(db_session, ticket, message).digest
    message.body = "Different words"
    db_session.add(message)
    db_session.commit()
    with pytest.raises(MspError):
        forward_message(db_session, ticket, message, None, expect_digest=digest)
    db_session.rollback()
    assert forwards(db_session) == []


def test_not_forwarding_keeps_it_agents_only_and_is_recorded(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket, message = left_off(db_session, team=team)
    assert client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}/dismiss").status_code == 200
    db_session.refresh(message)
    assert (message.msp_forward, message.visibility) == (FORWARD_DISMISSED, MessageVisibility.AGENTS_ONLY)
    assert requester_visible_messages(db_session, ticket) == [] and forwards(db_session) == []
    assert len(events_of(db_session, ticket, "msp_forward_dismissed")) == 1
    assert client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}/dismiss").status_code == 400
    assert client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}", data={}).status_code == 400
    assert "The MSP replied without copying the user" not in client.get(f"/tickets/{ticket.number}").text


def test_an_agent_outside_the_team_cannot_forward_and_a_message_of_another_ticket_is_not_found(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[mine])
    ticket, message = left_off(db_session, team=other)
    assert client.post(f"/tickets/{ticket.number}/msp-forward/{message.id}").status_code in (403, 404)
    here, _ = left_off(db_session, team=mine)
    assert client.post(f"/tickets/{here.number}/msp-forward/{message.id}").status_code == 404
    db_session.refresh(message)
    assert message.msp_forward == FORWARD_PENDING


# ---- Left off To and Cc: out of hours -----------------------------------------------------------------------


def test_out_of_hours_it_is_forwarded_at_once_with_no_preview(db_session):
    ticket, message = left_off(db_session, now=AFTER_HOURS)
    assert (message.msp_forward, message.visibility) == (FORWARD_FORWARDED, MessageVisibility.PUBLIC)
    [row] = forwards(db_session)
    assert row.to_address == "jo@example.com"
    [event] = events_of(db_session, ticket, "msp_forwarded")
    assert (event.to_value, event.actor_email) == ("automatic", "system")
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "msp_forwarded")).one().detail == "automatic"


def test_a_weekend_or_a_bank_holiday_counts_as_out_of_hours(db_session):
    _, weekend = left_off(db_session, now=SATURDAY)
    assert weekend.msp_forward == FORWARD_FORWARDED
    set_setting(db_session, MSP_BANK_HOLIDAYS, "2026-11-04")
    db_session.commit()
    _, holiday = left_off(db_session, now=IN_HOURS)
    assert holiday.msp_forward == FORWARD_FORWARDED


def test_the_hours_are_the_settings(db_session):
    set_setting(db_session, MSP_BUSINESS_START, "12:00")
    db_session.commit()
    _, early = left_off(db_session, now=IN_HOURS)  # 11:00 is now before the start
    assert early.msp_forward == FORWARD_FORWARDED


def test_with_mail_off_nothing_is_forwarded_and_it_waits_for_an_agent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "off")
    ticket, message = left_off(db_session, now=AFTER_HOURS)
    assert message.msp_forward == FORWARD_PENDING and forwards(db_session) == []
    assert forward_waiting_messages(db_session, AFTER_HOURS) == 0
    with pytest.raises(MspError):
        forward_message(db_session, ticket, message, None)


def test_what_waited_for_an_agent_goes_when_business_hours_end(db_session):
    ticket, message = left_off(db_session)
    assert forward_waiting_messages(db_session, IN_HOURS) == 0  # still business hours
    assert forward_waiting_messages(db_session, AFTER_HOURS) == 1
    db_session.commit()
    db_session.refresh(message)
    assert message.msp_forward == FORWARD_FORWARDED and len(forwards(db_session)) == 1
    assert forward_waiting_messages(db_session, AFTER_HOURS) == 0  # a second run finds nothing
    assert [e.to_value for e in events_of(db_session, ticket, "msp_forwarded")] == ["automatic"]


def test_the_forwarding_job_is_registered_and_runs_through_the_runner(db_session):
    from app.scheduled_jobs import SCHEDULED_JOBS

    job = {j.name: j for j in SCHEDULED_JOBS}["msp-forwards"]
    assert job.every_seconds == 300
    job.run(db_session)  # nothing waiting: runs and does nothing


# ---- Reporting data ----------------------------------------------------------------------------------------


def test_handoffs_and_forwards_are_counted_by_how_they_happened(db_session):
    first, a = left_off(db_session)
    second, b = left_off(db_session, now=AFTER_HOURS)
    third, c = left_off(db_session)
    agent = User(email="agent@example.com", display_name="Agent", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.commit()
    forward_message(db_session, first, a, agent)
    dismiss_forward(db_session, third, c, agent)
    record_event(db_session, first, "sent_to_msp", "system", to_value="automatic")
    record_event(db_session, first, "sent_to_msp", "agent@example.com", to_value="thread")
    record_event(db_session, first, "sent_to_msp", "agent@example.com", to_value="original")
    db_session.commit()
    counts = handoff_counts(db_session, datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) + timedelta(hours=1))
    assert counts == {
        "sent_automatic": 1, "sent_manual": 2, "forwarded_automatic": 1, "forwarded_manual": 1, "forward_dismissed": 1,
    }
    assert handoff_counts(db_session, datetime.now(UTC) + timedelta(hours=1), datetime.now(UTC) + timedelta(hours=2))["sent_manual"] == 0


# ---- Attaching the files -----------------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path, monkeypatch):
    local = LocalStorage(tmp_path)
    monkeypatch.setattr(mailer, "get_storage", lambda: local)
    return local


def png(size):
    return b"\x89PNG\r\n\x1a\n" + b"\x01" * size


def test_files_that_fit_are_attached_and_the_rest_are_listed_as_not_attached(db_session, storage):
    ticket = with_msp(db_session, sent=False, status=TicketStatus.OPEN)
    store_attachment(db_session, storage, ticket=ticket, filename="jam.png", data=png(600_000))
    store_attachment(db_session, storage, ticket=ticket, filename="huge.png", data=png(2_500_000))  # more than is left
    store_attachment(db_session, storage, ticket=ticket, filename="log.png", data=png(100_000))
    store_attachment(db_session, storage, ticket=ticket, filename="logo.png", data=png(5000), inline=True)
    db_session.commit()
    text, ids = attachment_plan(db_session, ticket)
    assert len(ids) == 2
    assert "Attached to this email:\n- jam.png" in text and "- log.png" in text
    assert "too large to attach" in text and "- huge.png" in text
    assert text.index("huge.png") > text.index("too large to attach") and "logo.png" not in text


def test_the_setting_lowers_the_total_and_graphs_limit_caps_it(db_session, storage):
    ticket = with_msp(db_session, sent=False, status=TicketStatus.OPEN)
    store_attachment(db_session, storage, ticket=ticket, filename="a.png", data=png(1_500_000))
    db_session.commit()
    set_setting(db_session, MSP_ATTACH_MAX_MB, "1")
    assert attachment_plan(db_session, ticket)[1] == []
    set_setting(db_session, MSP_ATTACH_MAX_MB, "1000")  # no more than Graph takes in one send
    assert len(attachment_plan(db_session, ticket)[1]) == 1
    store_attachment(db_session, storage, ticket=ticket, filename="b.png", data=png(1_900_000))
    assert len(attachment_plan(db_session, ticket)[1]) == 1  # 3 MB in all, not 3.4


def test_the_handoff_email_carries_exactly_the_previewed_files(client, db_session, storage):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = with_msp(db_session, team, status=TicketStatus.OPEN, sent=False, requester_email="jo@example.com")
    store_attachment(db_session, storage, ticket=ticket, filename="jam.png", data=png(1000))
    db_session.commit()
    agent = db_session.get(User, agent_id)
    preview = send_to_msp(db_session, ticket, agent, "Needs an engineer")
    db_session.commit()
    store_attachment(db_session, storage, ticket=ticket, filename="later.png", data=png(1000))  # added after it was queued
    db_session.commit()
    assert "Attached to this email:\n- jam.png" in preview.body

    sent = []
    assert mailer.deliver_mail(db_session, send=sent.append)[0] >= 1
    [handoff] = [m for m in sent if m.to == MSP]
    assert [(name, data) for name, _, data in handoff.files] == [("jam.png", png(1000))]
    assert "later.png" not in handoff.body


def test_a_file_that_cannot_be_read_is_named_and_does_not_stop_the_email(client, db_session, storage):

    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = with_msp(db_session, team, status=TicketStatus.OPEN, sent=False, requester_email="jo@example.com")
    store_attachment(db_session, storage, ticket=ticket, filename="jam.png", data=png(1000))
    db_session.commit()
    send_to_msp(db_session, ticket, db_session.get(User, agent_id), "Needs an engineer")
    db_session.commit()
    attachment = db_session.exec(select(Attachment)).one()
    attachment.blob_key = "missing/blob"
    db_session.add(attachment)
    db_session.commit()
    sent = []
    mailer.deliver_mail(db_session, send=sent.append)
    [handoff] = [m for m in sent if m.to == MSP]
    assert handoff.files == [] and "These files could not be attached: jam.png" in handoff.body


def test_graph_gets_the_files_inline_as_base64(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "it-support@example.org")
    graph = GraphClient()
    monkeypatch.setattr(graph, "_get_access_token", lambda: "token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    graph.send_mail("msp@example.com", "[INC-5] x", "Hello", attachments=[("jam.png", "image/png", b"abc")])
    [file] = captured["json"]["message"]["attachments"]
    assert file == {
        "@odata.type": "#microsoft.graph.fileAttachment", "name": "jam.png", "contentType": "image/png",
        "contentBytes": base64.b64encode(b"abc").decode(),
    }
    json.dumps(captured["json"])  # serialisable
    captured.clear()
    graph.send_mail("msp@example.com", "[INC-5] x", "Hello")
    assert "attachments" not in captured["json"]["message"]


def test_files_the_msp_sends_stay_hidden_from_the_user_until_forwarded(db_session, storage):
    ticket = with_msp(db_session, requester_email="jo@example.com")
    result = take(
        db_session, from_msp(ticket, to=("else@msp.example",)), IN_HOURS, storage=storage,
        attachments=[IncomingAttachment("quote.pdf", data=b"%PDF-1.7\n" + b"x" * 40)],
    )
    file = db_session.exec(select(Attachment)).one()
    user = User(email="jo@example.com", display_name="Jo", role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    assert not may_view_attachment(db_session, user, file, ticket, {})
    forward_message(db_session, ticket, result.message, None)
    db_session.commit()
    assert may_view_attachment(db_session, user, file, ticket, {})


def test_receive_mail_is_one_call_that_never_changes_the_status(db_session):
    ticket = with_msp(db_session, status=TicketStatus.AWAITING_USER, requester_email="jo@example.com")
    receive_mail(db_session, ticket, sender=MSP, body="Hi", recipients=["JO@example.com"], now=IN_HOURS)
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_USER

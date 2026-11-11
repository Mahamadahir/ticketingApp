"""The MSP's mail as it really arrives (issue #149). The fixtures are shaped like the anonymised samples taken from a real
tenant: Mimecast in front of Exchange Online, so Exchange's own verdict is the header named Authentication-Results and
dkim=fail is normal; the MSP's relay adds List-Unsubscribe; the MSP never replies in-thread and has its own subject and
ticket number. Headers are Graph's internetMessageHeaders list; a mail goes through parse_graph_message like the real thing."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.config import settings
from app.inbound import (
    IGNORED_AUTOMATIC,
    IGNORED_MSP,
    REPLY_ADDED,
    TICKET_CREATED,
    TRUST_FAIL,
    TRUST_PASS,
    TRUST_UNVERIFIED,
    ingest_mail,
    msp_trust,
    parse_graph_message,
)
from app.messages import requester_visible_messages
from app.models import (
    AuditLog,
    Message,
    MessageDirection,
    MessageVisibility,
    MspUnmatchedMail,
    Notification,
    OutboundEmail,
    Ticket,
    TicketEvent,
    TicketStatus,
    UserRole,
)
from app.msp import (
    FORWARD_INTERNAL,
    FORWARD_PENDING,
    alert_unacknowledged,
    compose_forward,
    forward_waiting_messages,
    parse_subject,
)
from app.settings_store import (
    MSP_ACK_ALERT_MINUTES,
    MSP_ADDRESS,
    MSP_SUBJECT_PATTERN,
    set_setting,
)
from tests.conftest import create_team, login_as, set_role
from tests.test_inbound_mail import MAILBOX
from tests.test_visibility import agent_in

MSP = "support@msp.example"
UTC = timezone.utc
IN_HOURS = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)  # a Wednesday, GMT
AFTER_HOURS = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)

# Headers as in the samples: Exchange's verdict (mx.microsoft.com) is the one plain Authentication-Results.
GENUINE = (
    "mx.microsoft.com 1; spf=pass (sender IP is 198.51.100.24) smtp.mailfrom=em1.msp.example; dkim=fail (body hash did not "
    "verify) header.d=msp.example;dmarc=pass action=none header.from=msp.example;compauth=pass reason=100"
)
FORGED = (
    "mx.microsoft.com 1; spf=fail (sender IP is 203.0.113.66) smtp.mailfrom=msp.example; dkim=none (message not signed) "
    "header.d=none;dmarc=fail action=oreject header.from=msp.example;compauth=fail reason=000"
)
SOFTPASS = GENUINE.replace("compauth=pass reason=100", "compauth=softpass reason=202")
UPSTREAM = (
    'relay.mimecast.com; dkim=pass header.d=msp.example header.s=s1 header.b=QwrmEOpQ; spf=pass (relay.mimecast.com: domain of '
    '"bounce.xyz@em1.msp.example" designates 198.51.100.24 as permitted sender) smtp.mailfrom="bounce.xyz@em1.msp.example"; '
    "dmarc=pass (policy=reject) header.from=msp.example"
)
ARC = "i=2; mx.microsoft.com 1; spf=pass (sender ip is 198.51.100.24); dmarc=pass; dkim=fail; arc=pass (0 oda=0 ltdi=0 93)"

ACK_HTML = (
    '<html><body><div class="mc-ip-hide"><b>** EXTERNAL MESSAGE **</b><hr></div>'
    '<p style="font-size:11px;color:#999999">--REPLY above this line to respond--</p><h1>IT Support:</h1>'
    "<p>Your issue: <strong>[INC-1042] Printer offline on floor 3</strong>, is in our system as <strong>Ticket #7000237</strong>. "
    "Your ticket has been escalated to your support team and we will contact you as soon as possible to resolve this issue.</p>"
    "<p>Thank you in advance.</p><p>MSP Support</p></body></html>"
)

_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def msp_world(db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)
    monkeypatch.setattr(settings, "mail_sending", "dev")
    set_setting(db_session, MSP_ADDRESS, MSP)
    db_session.commit()


def headers(*pairs):
    return [{"name": name, "value": value} for name, value in pairs]


def msp_headers(auth=GENUINE, extra=()):
    """What Graph returns for the MSP's acknowledgement: list-unsubscribe, no In-Reply-To or References."""
    return headers(
        ("Message-ID", "<IP-0A0A018DJfKHmj9n00000723@IP-0A0A018D>"),
        ("List-Unsubscribe", "<mailto:unsub@relay.example-smtp.com>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
        ("Return-Path", "bounce.xyz@em1.msp.example"),
        ("Authentication-Results", auth),
        ("Authentication-Results-Original", UPSTREAM),
        ("ARC-Authentication-Results", ARC),
        ("X-MS-Exchange-Organization-MessageDirectionality", "Incoming"),
        *extra,
    )


def graph_raw(subject, header_list, body=None, html=False, sender=MSP, to=("itsupport@corp.example",)):
    n = next(_ids)
    body = body if body is not None else f"We are looking at it. ({n})"
    return {
        "id": f"AAMk-real-{n}",
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
        "internetMessageId": f"<real{n}@msp.example>",
        "conversationId": f"conv-real-{n}",
        "body": {"contentType": "html" if html else "text", "content": body},
        "internetMessageHeaders": header_list,
    }


def take(db_session, raw, now=IN_HOURS):
    mail = parse_graph_message(raw)
    result = ingest_mail(db_session, mail, now=now)
    db_session.commit()
    return result


def acknowledgement(ref="INC-1042", subject=None, auth=GENUINE, body=None):
    """The MSP's "has been received" mail for our ticket `ref`: the subject and the HTML-only body both carry our reference."""
    subject = subject or f"Ticket#7000237/[{ref}] Printer offline on floor 3 -- has been received"
    body = body if body is not None else ACK_HTML.replace("INC-1042", ref)
    return graph_raw(subject, msp_headers(auth), body=body, html=True)


def update(number="7000237", title="Corp/Printer floor 3 driver fault", auth=GENUINE, **kwargs):
    return graph_raw(f"Ticket#{number}/{title} -- has been updated", msp_headers(auth), **kwargs)


def sent_ticket(db_session, subject="Printer offline on floor 3", team=None, **fields):
    fields.setdefault("triaged_at", IN_HOURS - timedelta(days=1))
    fields.setdefault("msp_sent_at", IN_HOURS - timedelta(minutes=5))
    ticket = Ticket(
        subject=subject, status=TicketStatus.WITH_MSP, requester_email="jo@example.com", team_id=team.id if team else None, **fields
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def handoff_email(db_session, ticket, subject=None, sent_at=None):
    row = OutboundEmail(
        kind="msp_handoff", ticket_id=ticket.id, to_address=MSP, subject=subject or f"[{ticket.reference}] {ticket.subject}",
        sent_at=sent_at or IN_HOURS - timedelta(minutes=5), body="x",
    )
    db_session.add(row)
    db_session.commit()
    return row


def messages_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()


def unmatched(db_session):
    db_session.expire_all()
    return db_session.exec(select(MspUnmatchedMail).order_by(MspUnmatchedMail.id)).all()


# ---- Trust: Microsoft's compauth verdict only ---------------------------------------------------------------------


def test_a_genuine_external_mail_through_mimecast_is_trusted_despite_dkim_fail():
    raw = graph_raw(
        "Testing Headers",
        headers(
            ("Message-ID", "<CAexample0001@mail.gmail.example>"),
            (
                "Authentication-Results",
                "mx.microsoft.com 1; spf=pass (sender IP is 192.0.2.10) smtp.mailfrom=gmail.example; dkim=fail (body hash did "
                "not verify) header.d=gmail.example;dmarc=pass action=none header.from=gmail.example;compauth=pass reason=100",
            ),
            ("Authentication-Results-Original", "relay.mimecast.com; dkim=pass header.d=gmail.example; dmarc=pass (policy=none)"),
            ("ARC-Authentication-Results", "i=3; mx.microsoft.com 1; spf=pass; dmarc=pass; dkim=fail; arc=pass"),
            ("X-MS-Exchange-SkipListedInternetSender", "ip=[192.0.2.10];domain=mail.gmail.example"),
        ),
        sender="pat.person@gmail.example", to=("someone@corp.example",),
    )
    assert msp_trust(parse_graph_message(raw)) == TRUST_PASS


def test_compauth_fail_is_not_believed_and_a_later_plain_header_never_rescues_it():
    raw = graph_raw(
        "Ticket#7000237/Corp/Printer floor 3 -- has been updated",
        headers(("Authentication-Results", FORGED), ("Authentication-Results", GENUINE)),  # a forger's own, lower down
    )
    mail = parse_graph_message(raw)
    assert len(mail.auth_results) == 2 and msp_trust(mail) == TRUST_FAIL


def test_only_the_receiving_servers_own_verdict_counts():
    foreign_first = graph_raw("s", headers(("Authentication-Results", "evil.example; compauth=pass"), ("Authentication-Results", FORGED)))
    assert msp_trust(parse_graph_message(foreign_first)) == TRUST_FAIL  # the first mx.microsoft.com one decides
    only_renamed = graph_raw("s", headers(("Authentication-Results-Original", "mx.microsoft.com 1; compauth=pass"), ("ARC-Authentication-Results", ARC)))
    assert msp_trust(parse_graph_message(only_renamed)) == TRUST_UNVERIFIED  # renamed and ARC headers are never read
    assert msp_trust(parse_graph_message(graph_raw("s", []))) == TRUST_UNVERIFIED
    softpass = graph_raw("s", headers(("Authentication-Results", SOFTPASS)))
    assert msp_trust(parse_graph_message(softpass)) == TRUST_UNVERIFIED
    # dmarc=fail or dkim=fail alone no longer decide: compauth does
    dmarc_only = graph_raw("s", headers(("Authentication-Results", "mx.microsoft.com 1; dmarc=fail; dkim=fail; compauth=pass reason=100")))
    assert msp_trust(parse_graph_message(dmarc_only)) == TRUST_PASS


def test_a_forged_mail_is_set_aside_and_audited_and_not_listed(db_session):
    ticket = sent_ticket(db_session)
    result = take(db_session, update(auth=FORGED), AFTER_HOURS)
    assert result.outcome == IGNORED_MSP and messages_of(db_session, ticket) == []
    assert unmatched(db_session) == []
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "msp_mail_ignored")).first() is not None


# ---- The acknowledgement: not dropped, number learned, stored internal ---------------------------------------------


def test_the_acknowledgement_is_kept_despite_list_unsubscribe_and_teaches_the_number(db_session):
    ticket = sent_ticket(db_session)
    result = take(db_session, acknowledgement(ticket.reference))  # carries List-Unsubscribe, HTML only, no In-Reply-To
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id  # matched by our [INC-n] reference

    db_session.refresh(ticket)
    assert ticket.msp_ticket_number == "7000237" and ticket.status == TicketStatus.WITH_MSP
    [message] = messages_of(db_session, ticket)
    assert (message.msp_forward, message.visibility) == (FORWARD_INTERNAL, MessageVisibility.AGENTS_ONLY)
    assert requester_visible_messages(db_session, ticket) == []  # never shown to the user
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_forward")).all() == []
    event = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "msp_acknowledged")).one()
    assert event.to_value == "7000237"
    assert db_session.exec(select(Notification)).all() == []  # an acknowledgement tells nobody


def test_the_acknowledgement_is_matched_by_our_reference_in_the_body_when_the_title_lacks_it(db_session):
    ticket = sent_ticket(db_session)
    raw = acknowledgement(ticket.reference, "Ticket#7000237/Printer floor three -- has been received")  # title rewritten, reference only in the body
    assert take(db_session, raw).ticket.id == ticket.id


def test_the_acknowledgement_is_matched_by_the_exact_title_when_the_reference_is_missing(db_session):
    ticket = sent_ticket(db_session)
    other = sent_ticket(db_session, subject="Something else")
    handoff_email(db_session, ticket, subject="Printer offline on floor 3")
    handoff_email(db_session, other)
    raw = acknowledgement(subject="Ticket#7000237/Printer offline on floor 3 -- has been received", body="<p>Your issue is in our system.</p>")
    result = take(db_session, raw)
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    db_session.refresh(ticket)
    assert ticket.msp_ticket_number == "7000237"


def test_a_title_that_matches_two_tickets_or_an_old_handoff_is_listed_not_guessed(db_session):
    first, second = sent_ticket(db_session), sent_ticket(db_session)
    handoff_email(db_session, first, subject="Printer offline on floor 3")
    handoff_email(db_session, second, subject="Printer offline on floor 3")
    raw = acknowledgement(subject="Ticket#7000237/Printer offline on floor 3 -- has been received", body="<p>no reference</p>")
    assert take(db_session, raw).outcome == IGNORED_MSP
    [item] = unmatched(db_session)
    assert item.msp_number == "7000237" and item.is_acknowledgement and item.verified and item.status == "open"

    old = sent_ticket(db_session)
    handoff_email(db_session, old, subject="An old one", sent_at=IN_HOURS - timedelta(days=30))
    raw = acknowledgement(subject="Ticket#7000300/An old one -- has been received", body="<p>none</p>")
    assert take(db_session, raw).outcome == IGNORED_MSP and len(unmatched(db_session)) == 2


def test_a_later_update_with_a_rewritten_title_is_matched_by_the_number(db_session):
    ticket = sent_ticket(db_session)
    take(db_session, acknowledgement(ticket.reference))
    result = take(db_session, update(title="Corp/Printer floor 3 driver fault"))  # no INC reference, no thread
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    last = messages_of(db_session, ticket)[-1]
    assert last.msp_forward == FORWARD_PENDING  # the user was not on it: agents only until forwarded


def test_the_number_is_unique_a_second_ticket_cannot_take_it(db_session):
    first, second = sent_ticket(db_session), sent_ticket(db_session, subject="Other")
    take(db_session, acknowledgement(first.reference))
    raw = acknowledgement(subject=f"Ticket#7000237/[{second.reference}] Other -- has been received", body="<p>x</p>")
    result = take(db_session, raw)
    assert result.ticket.id == first.id  # by number: the ticket that has it
    db_session.refresh(second)
    assert second.msp_ticket_number is None


def test_a_mail_that_is_not_verified_is_recorded_but_never_forwarded_automatically(db_session):
    ticket = sent_ticket(db_session)
    raw = graph_raw(f"Re: [{ticket.reference}] {ticket.subject}", msp_headers(SOFTPASS), body="An engineer is booked")
    result = take(db_session, raw, AFTER_HOURS)
    assert result.outcome == REPLY_ADDED
    [message] = messages_of(db_session, ticket)
    assert message.msp_forward == FORWARD_PENDING and message.msp_unverified is True
    assert forward_waiting_messages(db_session, AFTER_HOURS) == 0  # the job leaves it too
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_forward")).all() == []
    assert compose_forward(db_session, ticket, message).to == "jo@example.com"  # an agent can still preview and forward it


def test_an_unverified_acknowledgement_is_recorded_but_does_not_teach_the_number(db_session):
    ticket = sent_ticket(db_session)
    assert take(db_session, acknowledgement(ticket.reference, auth=SOFTPASS)).outcome == REPLY_ADDED
    db_session.refresh(ticket)
    assert ticket.msp_ticket_number is None
    assert db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "msp_acknowledged")).all() == []


def test_a_verified_mail_out_of_hours_is_still_forwarded_automatically(db_session):
    ticket = sent_ticket(db_session)
    take(db_session, graph_raw(f"Re: [{ticket.reference}] x", msp_headers(GENUINE), body="Booked"), AFTER_HOURS)
    assert len(db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_forward")).all()) == 1


# ---- The pattern ---------------------------------------------------------------------------------------------------


def test_the_default_pattern_reads_the_real_subjects(db_session):
    ack = parse_subject(db_session, "Ticket#7000237/[INC-1042] Printer offline on floor 3 -- has been received")
    assert (ack.number, ack.title, ack.is_acknowledgement) == ("7000237", "[INC-1042] Printer offline on floor 3", True)
    spaced = parse_subject(db_session, "RE: Ticket #7000237/Corp/Printer floor 3 driver fault -- has been updated")
    assert (spaced.number, spaced.title, spaced.is_acknowledgement) == ("7000237", "Corp/Printer floor 3 driver fault", False)
    assert parse_subject(db_session, "Lunch on Friday") is None


def test_the_pattern_is_validated_as_a_regex_with_named_groups(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    for bad in ("(unclosed", r"Ticket#(\d+)/(.+)", r"Ticket#(?P<number>\d+)"):
        assert client.post("/admin/settings", data={"key": MSP_SUBJECT_PATTERN, "value": bad}).status_code == 400
    good = r"MSP-(?P<number>\d+): (?P<title>.+)"
    assert client.post("/admin/settings", data={"key": MSP_SUBJECT_PATTERN, "value": good}, follow_redirects=False).status_code == 303
    assert parse_subject(db_session, "MSP-55: Printer").number == "55"
    set_setting(db_session, MSP_SUBJECT_PATTERN, "(broken")  # stored anyway: the default is used, mail still read
    db_session.commit()
    assert parse_subject(db_session, "Ticket#9/x -- has been updated").number == "9"


def test_the_alert_setting_accepts_zero_and_nothing_else_odd(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    assert client.post("/admin/settings", data={"key": MSP_ACK_ALERT_MINUTES, "value": "0"}, follow_redirects=False).status_code == 303
    assert client.post("/admin/settings", data={"key": MSP_ACK_ALERT_MINUTES, "value": "-1"}).status_code == 400


# ---- The unmatched list --------------------------------------------------------------------------------------------


def test_unmatched_mail_is_listed_then_linked_by_an_agent(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = sent_ticket(db_session, team=team)
    assert take(db_session, acknowledgement(subject="Ticket#7000237/Something we cannot place -- has been received", body="<p>x</p>")).outcome == IGNORED_MSP
    page = client.get("/msp-mail")
    assert page.status_code == 200 and "Something we cannot place" in page.text and "7000237" in page.text
    [item] = unmatched(db_session)

    response = client.post(f"/msp-mail/{item.id}/link", data={"ticket": ticket.reference}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/tickets/{ticket.number}"
    db_session.expire_all()
    db_session.refresh(ticket)
    assert ticket.msp_ticket_number == "7000237"
    assert (item.status, item.ticket_id) == ("linked", ticket.id)
    [message] = messages_of(db_session, ticket)
    assert message.msp_forward == FORWARD_INTERNAL
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "msp_mail_linked")).first() is not None
    assert "Something we cannot place" not in client.get("/msp-mail").text
    assert client.post(f"/msp-mail/{item.id}/link", data={"ticket": ticket.reference}).status_code == 400  # only once


def test_an_agent_can_dismiss(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    take(db_session, update(number="7000999", title="Corp/Nobody knows"))
    [item] = unmatched(db_session)
    assert client.post(f"/msp-mail/{item.id}/dismiss", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(MspUnmatchedMail, item.id).status == "dismissed"
    assert "Nobody knows" not in client.get("/msp-mail").text



def test_linking_needs_access_to_the_ticket_and_refuses_a_number_another_ticket_has(client, db_session):
    mine, theirs = create_team(db_session, "Mine"), create_team(db_session, "Theirs")
    agent_in(client, db_session, member_of=[mine])
    foreign = sent_ticket(db_session, team=theirs)
    owner = sent_ticket(db_session, team=mine, subject="Has the number", msp_ticket_number="7000999")
    free = sent_ticket(db_session, team=mine, subject="Free")
    take(db_session, update(number="7000998", title="Corp/Unplaced"))
    [item] = unmatched(db_session)
    assert client.post(f"/msp-mail/{item.id}/link", data={"ticket": foreign.reference}).status_code == 403
    item.msp_number = "7000999"
    db_session.add(item)
    db_session.commit()
    assert client.post(f"/msp-mail/{item.id}/link", data={"ticket": free.reference}).status_code == 400
    assert owner.msp_ticket_number == "7000999"
    assert client.post(f"/msp-mail/{item.id}/link", data={"ticket": "INC-99999"}).status_code == 400


# ---- The no-acknowledgement alert ----------------------------------------------------------------------------------


def alerts(db_session):
    db_session.expire_all()
    return db_session.exec(select(Notification).where(Notification.kind == "msp_unacknowledged")).all()


def test_the_alert_fires_once_per_handoff_to_the_assignee(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = sent_ticket(db_session, team=team, assigned_agent_id=agent_id)
    handoff_email(db_session, ticket, sent_at=IN_HOURS - timedelta(minutes=29))
    assert alert_unacknowledged(db_session, IN_HOURS) == 0  # not yet: 30 minutes is the placeholder
    db_session.commit()
    assert alert_unacknowledged(db_session, IN_HOURS + timedelta(minutes=2)) == 1
    db_session.commit()
    [bell] = alerts(db_session)
    assert bell.user_id == agent_id and "hasn't confirmed" in bell.text
    assert alert_unacknowledged(db_session, IN_HOURS + timedelta(hours=2)) == 0  # once per handoff
    db_session.commit()
    assert len(alerts(db_session)) == 1

    handoff_email(db_session, ticket, sent_at=IN_HOURS + timedelta(hours=3))  # sent to the MSP again: a new handoff
    assert alert_unacknowledged(db_session, IN_HOURS + timedelta(hours=4)) == 1
    db_session.commit()
    assert len(alerts(db_session)) == 2


def test_the_alert_goes_to_the_teams_members_when_nobody_is_assigned(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = sent_ticket(db_session, team=team)
    handoff_email(db_session, ticket, sent_at=IN_HOURS - timedelta(hours=1))
    assert alert_unacknowledged(db_session, IN_HOURS) == 1
    db_session.commit()
    assert [n.user_id for n in alerts(db_session)] == [agent_id]


def test_the_alert_does_not_fire_once_acknowledged(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = sent_ticket(db_session, team=team, assigned_agent_id=agent_id)
    handoff_email(db_session, ticket, sent_at=IN_HOURS - timedelta(hours=1))
    take(db_session, acknowledgement(ticket.reference), IN_HOURS - timedelta(minutes=10))  # the event is after the handoff was sent
    assert alert_unacknowledged(db_session, IN_HOURS) == 0
    assert alerts(db_session) == []


def test_zero_turns_the_alert_off_and_a_finished_ticket_is_left_alone(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = sent_ticket(db_session, team=team, assigned_agent_id=agent_id)
    handoff_email(db_session, ticket, sent_at=IN_HOURS - timedelta(hours=1))
    set_setting(db_session, MSP_ACK_ALERT_MINUTES, "0")
    db_session.commit()
    assert alert_unacknowledged(db_session, IN_HOURS) == 0
    set_setting(db_session, MSP_ACK_ALERT_MINUTES, "30")
    ticket.status = TicketStatus.CLOSED
    db_session.add(ticket)
    db_session.commit()
    assert alert_unacknowledged(db_session, IN_HOURS) == 0
    assert alerts(db_session) == []


# ---- Found in code review ------------------------------------------------------------------------------------------


def test_an_unverified_mail_the_user_was_on_is_still_agents_only_until_an_agent_forwards_it(db_session):
    ticket = sent_ticket(db_session)
    raw = graph_raw(f"Re: [{ticket.reference}] x", msp_headers(SOFTPASS), body="Pay us", to=("jo@example.com",))
    take(db_session, raw)
    [message] = messages_of(db_session, ticket)
    assert (message.msp_forward, message.visibility) == (FORWARD_PENDING, MessageVisibility.AGENTS_ONLY)
    assert requester_visible_messages(db_session, ticket) == []


def test_a_repeated_acknowledgement_found_by_the_number_is_still_internal(db_session):
    ticket = sent_ticket(db_session)
    take(db_session, acknowledgement(ticket.reference))
    take(db_session, acknowledgement(ticket.reference), AFTER_HOURS)  # the MSP sends it again, matched by the number
    assert [m.msp_forward for m in messages_of(db_session, ticket)] == [FORWARD_INTERNAL, FORWARD_INTERNAL]
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "msp_forward")).all() == []


def test_the_pattern_setting_may_be_long_and_blank_means_the_default(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    long = r"(?P<number>\d+)/(?P<title>.+)" + "|" + "|".join(f"alt{i}(?P<n{i}>x)" for i in range(25))
    assert 200 < len(long) < 500
    assert client.post("/admin/settings", data={"key": MSP_SUBJECT_PATTERN, "value": long}, follow_redirects=False).status_code == 303
    assert client.post("/admin/settings", data={"key": MSP_SUBJECT_PATTERN, "value": ""}, follow_redirects=False).status_code == 303
    assert parse_subject(db_session, "Ticket#9/x -- has been updated").number == "9"


def test_the_alert_is_tried_again_when_nobody_could_be_told(client, db_session):
    team = create_team(db_session, "Support")
    ticket = sent_ticket(db_session, team=team)  # nobody in the team yet
    handoff_email(db_session, ticket, sent_at=IN_HOURS - timedelta(hours=1))
    assert alert_unacknowledged(db_session, IN_HOURS) == 0
    db_session.commit()
    agent_in(client, db_session, member_of=[team])
    assert alert_unacknowledged(db_session, IN_HOURS) == 1


# ---- Real auto-replies from the MSP are still dropped (#155) -------------------------------------------------------


def test_an_msp_out_of_office_is_dropped_but_its_other_automatic_looking_headers_are_not(db_session):
    ticket = sent_ticket(db_session)
    for extra in (
        (("Auto-Submitted", "auto-replied"),),
        (("Auto-Submitted", 'Auto-Replied; owner-email="x@msp.example"'),),
        (("X-Autoreply", "yes"),),
        (("X-Autorespond", "yes"),),
        (("Return-Path", "<>"),),
    ):
        raw = acknowledgement(ticket.reference)
        raw["internetMessageHeaders"] = [h for h in raw["internetMessageHeaders"] if h["name"] != "Return-Path"] + headers(*extra)
        assert take(db_session, raw).outcome == IGNORED_AUTOMATIC
    assert messages_of(db_session, ticket) == []


def test_msp_mail_marked_auto_generated_or_suppress_or_bulk_is_still_accepted(db_session):
    ticket = sent_ticket(db_session)
    raw = acknowledgement(
        ticket.reference,
    )
    raw["internetMessageHeaders"] += headers(
        ("Auto-Submitted", "auto-generated"), ("X-Auto-Response-Suppress", "All"), ("Precedence", "bulk"), ("List-Id", "<x.msp.example>"),
    )
    assert take(db_session, raw).outcome == REPLY_ADDED


def test_the_title_window_is_measured_from_the_time_the_mail_is_taken_not_the_wall_clock(db_session):
    """The handoff was sent 5 minutes before IN_HOURS (a fixed past date); the window must follow `now`, not today's date."""
    ticket = sent_ticket(db_session)
    handoff_email(db_session, ticket, subject="Printer offline on floor 3")
    raw = lambda: acknowledgement(subject="Ticket#7000237/Printer offline on floor 3 -- has been received", body="<p>no reference</p>")
    assert take(db_session, raw(), IN_HOURS + timedelta(days=6)).ticket.id == ticket.id  # inside 7 days of the handoff

    second = sent_ticket(db_session, subject="Second")
    handoff_email(db_session, second, subject="Second")
    late = graph_raw("Ticket#7000999/Second -- has been received", msp_headers(), body="<p>none</p>", html=True)
    assert take(db_session, late, IN_HOURS + timedelta(days=8)).outcome == IGNORED_MSP  # outside it

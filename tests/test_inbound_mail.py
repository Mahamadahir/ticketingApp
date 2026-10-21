"""3B: inbound mail. A mail in the shared mailbox becomes a ticket or a reply (app/inbound.py): read from a
simulated Graph, handled once, never from ourselves or a machine, matched to its ticket only for the people
allowed to write on it, with its files, and driven by the worker job from the notifications the webhook stored.
Nothing here touches the network: Graph is a fake, and the one test of the real client replaces httpx."""

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import inbound
from app.attachments import store_attachment
from app.config import settings
from app.settings_store import INBOUND_MAX_ATTACHMENTS, INBOUND_MAX_CC, INBOUND_MAX_PER_SENDER_HOUR, set_setting
from app.database import engine
from app.graph_client import GraphClient
from app.inbound import (
    DUPLICATE,
    GONE,
    IGNORED_AUTOMATIC,
    IGNORED_DRAFT,
    IGNORED_FLOOD,
    IGNORED_NO_SENDER,
    IGNORED_OWN,
    REPLY_ADDED,
    TICKET_CREATED,
    IncomingMail,
    ingest_mail,
    parse_graph_message,
    process_message,
    process_notifications,
)
from app.jobs import JOBS, mail_in
from app.messages import agent_timeline, record_inbound, requester_visible_messages
from app.models import (
    Attachment,
    AuditLog,
    GraphNotification,
    InboundMail,
    Message,
    MessageDirection,
    Ticket,
    TicketEvent,
    TicketFollower,
    TicketSource,
    TicketStatus,
)
from app.storage import LocalStorage
from app.teams import MAX_ATTEMPTS
from tests.test_splitting import split, subs_of, world

MAILBOX = "support@corp.example"
PDF = b"%PDF-1.7\n" + b"x" * 32
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 8000


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path)


class FakeGraph:
    """The three Graph calls the intake makes, answering from dictionaries. Counts what was asked."""

    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.files: dict[str, list[tuple[dict, bytes]]] = {}
        self.broken: Exception | None = None
        self.fetched: list[str] = []
        self.downloaded: list[str] = []

    def add(self, message_id="m1", **fields):
        self.messages[message_id] = graph_message(message_id, **fields)
        return message_id

    def attach(self, message_id, name, data, inline=False, **extra):
        item = {
            "id": f"{message_id}-{len(self.files.get(message_id, []))}", "name": name, "size": len(data),
            "isInline": inline, "@odata.type": "#microsoft.graph.fileAttachment", **extra,
        }
        self.files.setdefault(message_id, []).append((item, data))
        self.messages[message_id]["hasAttachments"] = True

    def get_message(self, message_id):
        self.fetched.append(message_id)
        if self.broken:
            raise self.broken
        return self.messages.get(message_id)

    def get_attachments(self, message_id):
        return [item for item, _ in self.files.get(message_id, [])]

    def get_attachment_bytes(self, message_id, attachment_id):
        self.downloaded.append(attachment_id)
        for item, data in self.files.get(message_id, []):
            if item["id"] == attachment_id:
                return data
        return None


def graph_message(message_id="m1", sender="jo@example.com", subject="Printer jam", body="It is stuck", **fields):
    message = {
        "id": message_id,
        "internetMessageId": f"<{message_id}@mail.example.com>",
        "conversationId": f"conv-{message_id}",
        "subject": subject,
        "from": {"emailAddress": {"name": "Jo", "address": sender}},
        "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
        "ccRecipients": [],
        "uniqueBody": {"contentType": "text", "content": body},
        "internetMessageHeaders": [],
        "hasAttachments": False,
        "isDraft": False,
        "receivedDateTime": "2026-10-20T10:00:00Z",
    }
    message.update(fields)
    return message


def headers(**named):
    return [{"name": name.replace("_", "-"), "value": value} for name, value in named.items()]


def mail(message_id="m1", sender="jo@example.com", **fields) -> IncomingMail:
    fields.setdefault("conversation_id", f"conv-{message_id}")
    return IncomingMail(graph_message_id=message_id, sender=sender, subject=fields.pop("subject", "Printer jam"),
                        body=fields.pop("body", "It is stuck"), internet_message_id=f"{message_id}@mail.example.com", **fields)


def tickets(db_session):
    db_session.expire_all()
    return db_session.exec(select(Ticket).order_by(Ticket.id)).all()


def messages_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()


def events_of(db_session, ticket, kind=None):
    db_session.expire_all()
    query = select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    if kind:
        query = query.where(TicketEvent.event_type == kind)
    return db_session.exec(query).all()


def open_ticket(db_session, **fields):
    """A ticket made by mail, then committed."""
    result = ingest_mail(db_session, mail(**fields))
    db_session.commit()
    assert result.outcome == TICKET_CREATED
    return result.ticket


def triage_by_hand(db_session, ticket, status=TicketStatus.IN_PROGRESS):
    ticket.triaged_at = datetime.now(timezone.utc)
    ticket.status = status
    db_session.add(ticket)
    db_session.commit()


# ---- A new ticket --------------------------------------------------------------------------------


def test_a_new_mail_opens_an_untriaged_email_ticket_with_its_first_message(db_session):
    graph = FakeGraph()
    graph.add("m1", sender="Jo.Bloggs@Example.com", subject="  Printer\n jam ", body="It is stuck\x00 again")

    result = process_message(db_session, "m1", graph)
    db_session.commit()

    assert result.outcome == TICKET_CREATED
    [ticket] = tickets(db_session)
    assert (ticket.source, ticket.requester_email, ticket.subject) == (TicketSource.EMAIL, "jo.bloggs@example.com", "Printer jam")
    assert ticket.status == TicketStatus.NEW and ticket.triaged_at is None
    [message] = messages_of(db_session, ticket)
    assert (message.direction, message.sender, message.body) == (MessageDirection.INBOUND, "jo.bloggs@example.com", "It is stuck again")
    assert message.graph_message_id == "m1"
    [record] = db_session.exec(select(InboundMail)).all()
    assert (record.graph_message_id, record.outcome, record.ticket_id, record.message_id) == ("m1", TICKET_CREATED, ticket.id, message.id)
    assert record.conversation_id == "conv-m1" and record.internet_message_id == "m1@mail.example.com"
    assert [e.event_type for e in events_of(db_session, ticket)] == ["created"]
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "created_by_email")).one().actor_email == "jo.bloggs@example.com"


def test_everyone_copied_on_the_opening_mail_follows_the_ticket_but_not_the_mailbox_or_the_sender(db_session):
    result = ingest_mail(
        db_session,
        mail(cc=["Pat@example.com", MAILBOX, "jo@example.com", "not an address", "sam@example.com", "pat@example.com"]),
    )
    db_session.commit()

    followers = db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == result.ticket.id)).all()
    assert sorted(f.email for f in followers) == ["pat@example.com", "sam@example.com"]
    assert {f.added_by_email for f in followers} == {"jo@example.com"} and not any(f.is_internal for f in followers)


def test_the_cc_line_is_limited_so_one_mail_cannot_fill_the_follower_list(db_session):
    set_setting(db_session, INBOUND_MAX_CC, "2")
    result = ingest_mail(db_session, mail(cc=[f"p{i}@example.com" for i in range(6)]))
    db_session.commit()
    assert len(db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == result.ticket.id)).all()) == 2


def test_a_mail_with_no_subject_or_text_still_opens_a_ticket(db_session):
    result = ingest_mail(db_session, mail(subject="  ", body="   "))
    db_session.commit()
    assert result.ticket.subject == "(no subject)"
    assert messages_of(db_session, result.ticket)[0].body == "(no message text)"


def test_a_long_subject_and_body_are_cut_not_refused(db_session):
    result = ingest_mail(db_session, mail(subject="s" * 500, body="b" * 30_000))
    db_session.commit()
    assert len(result.ticket.subject) == 200
    body = messages_of(db_session, result.ticket)[0].body
    assert len(body) <= 10_000 and body.endswith("[cut short]")


# ---- Handled once --------------------------------------------------------------------------------


def test_the_same_graph_message_is_handled_once_and_not_fetched_again(db_session):
    graph = FakeGraph()
    graph.add("m1")
    assert process_message(db_session, "m1", graph).outcome == TICKET_CREATED
    db_session.commit()

    assert process_message(db_session, "m1", graph).outcome == DUPLICATE
    assert graph.fetched == ["m1"]  # the second call did not even ask Graph
    assert len(tickets(db_session)) == 1 and len(db_session.exec(select(Message)).all()) == 1


def test_the_same_email_under_another_graph_id_is_a_duplicate(db_session):
    # Delivered to the mailbox twice (or moved and back): two Graph ids, one Message-ID header.
    graph = FakeGraph()
    graph.add("a", internetMessageId="<same@mail.example.com>")
    graph.add("b", internetMessageId="<SAME@mail.example.com>")
    assert process_message(db_session, "a", graph).outcome == TICKET_CREATED
    db_session.commit()
    assert process_message(db_session, "b", graph).outcome == DUPLICATE
    db_session.commit()
    assert len(tickets(db_session)) == 1


def test_a_failure_after_the_claim_leaves_no_half_handled_mail(db_session, monkeypatch):
    # The record that a mail was handled, the ticket and the message stand or fall together.
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(inbound, "record_inbound", boom)
    graph = FakeGraph()
    graph.add("m1")
    with pytest.raises(RuntimeError):
        process_message(db_session, "m1", graph)
    db_session.rollback()
    assert tickets(db_session) == [] and db_session.exec(select(InboundMail)).all() == []

    monkeypatch.undo()
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)
    assert process_message(db_session, "m1", graph).outcome == TICKET_CREATED  # a retry works


def test_a_message_graph_no_longer_has_is_gone_and_leaves_nothing(db_session):
    assert process_message(db_session, "missing", FakeGraph()).outcome == GONE
    assert db_session.exec(select(InboundMail)).all() == []


# ---- Not from a person ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"internetMessageHeaders": headers(Auto_Submitted="auto-replied")},
        {"internetMessageHeaders": headers(Auto_Submitted="auto-generated")},
        {"internetMessageHeaders": headers(X_Auto_Response_Suppress="DR, NDR, RN, NRN, OOF, AutoReply")},
        {"internetMessageHeaders": headers(X_Auto_Response_Suppress="All")},
        {"internetMessageHeaders": headers(Precedence="bulk")},
        {"internetMessageHeaders": headers(Precedence="Auto_Reply")},
        {"internetMessageHeaders": headers(X_Autoreply="yes")},
        {"internetMessageHeaders": headers(List_Id="<news.example.com>")},
        {"internetMessageHeaders": headers(Return_Path="<>")},
        {"internetMessageHeaders": headers(Content_Type="multipart/report; report-type=delivery-status")},
        {"from": {"emailAddress": {"address": "MAILER-DAEMON@example.com"}}},
        {"from": {"emailAddress": {"address": "postmaster@example.com"}}},
    ],
)
def test_automatic_mail_never_becomes_a_ticket(db_session, extra):
    graph = FakeGraph()
    graph.add("m1", **extra)
    result = process_message(db_session, "m1", graph)
    db_session.commit()
    assert result.outcome == IGNORED_AUTOMATIC
    assert tickets(db_session) == [] and db_session.exec(select(Message)).all() == []
    assert db_session.exec(select(InboundMail)).one().outcome == IGNORED_AUTOMATIC  # recorded, so not looked at again


def test_an_automatic_reply_to_a_ticket_does_not_wake_it(db_session):
    ticket = open_ticket(db_session, message_id="first")
    triage_by_hand(db_session, ticket, TicketStatus.AWAITING_USER)
    result = ingest_mail(
        db_session,
        mail("ooo", subject=f"Automatic reply: [{ticket.reference}] Printer jam", headers={"auto-submitted": "auto-replied"}),
    )
    db_session.commit()
    assert result.outcome == IGNORED_AUTOMATIC
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_USER and len(messages_of(db_session, ticket)) == 1


def test_a_person_who_says_auto_submitted_no_is_a_person(db_session):
    graph = FakeGraph()
    graph.add("m1", internetMessageHeaders=headers(Auto_Submitted="no"))
    assert process_message(db_session, "m1", graph).outcome == TICKET_CREATED


def test_our_own_mail_is_never_a_ticket_so_a_loop_cannot_start(db_session):
    graph = FakeGraph()
    graph.add("m1", sender=MAILBOX.upper(), subject="[INC-1] Anything")
    result = process_message(db_session, "m1", graph)
    assert result.outcome == IGNORED_OWN and tickets(db_session) == []


def test_a_draft_and_a_mail_with_no_usable_sender_are_set_aside(db_session):
    graph = FakeGraph()
    graph.add("d", isDraft=True)
    graph.add("n", **{"from": None, "sender": None})
    graph.add("bad", sender="not-an-address")
    assert process_message(db_session, "d", graph).outcome == IGNORED_DRAFT
    assert process_message(db_session, "n", graph).outcome == IGNORED_NO_SENDER
    assert process_message(db_session, "bad", graph).outcome == IGNORED_NO_SENDER
    assert tickets(db_session) == []


def test_a_sender_who_floods_the_mailbox_is_set_aside_after_the_limit(db_session):
    set_setting(db_session, INBOUND_MAX_PER_SENDER_HOUR, "3")
    outcomes = [ingest_mail(db_session, mail(f"f{i}", subject=f"Help {i}")).outcome for i in range(5)]
    assert outcomes == [TICKET_CREATED] * 3 + [IGNORED_FLOOD] * 2
    # Someone else is not affected, and raising the setting lets the same sender through again.
    assert ingest_mail(db_session, mail("other", sender="kim@example.com")).outcome == TICKET_CREATED
    set_setting(db_session, INBOUND_MAX_PER_SENDER_HOUR, "100")
    assert ingest_mail(db_session, mail("again")).outcome == TICKET_CREATED


def test_the_limits_default_to_the_placeholders_and_a_bad_stored_value_falls_back(db_session):
    from app.settings_store import count_setting

    assert [count_setting(db_session, k) for k in (INBOUND_MAX_PER_SENDER_HOUR, INBOUND_MAX_ATTACHMENTS, INBOUND_MAX_CC)] == [30, 20, 10]
    set_setting(db_session, INBOUND_MAX_CC, "lots")
    assert count_setting(db_session, INBOUND_MAX_CC) == 10


# ---- Which ticket ---------------------------------------------------------------------------------


def test_a_reply_with_the_reference_in_the_subject_joins_the_ticket(db_session):
    ticket = open_ticket(db_session)
    graph = FakeGraph()
    graph.add("r1", subject=f"RE: [{ticket.reference}] Printer jam", body="Still jammed", conversationId="unrelated")

    result = process_message(db_session, "r1", graph)
    db_session.commit()

    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    assert len(tickets(db_session)) == 1
    first, reply = messages_of(db_session, ticket)
    assert (reply.body, reply.sender, reply.direction, reply.graph_message_id) == (
        "Still jammed", "jo@example.com", MessageDirection.INBOUND, "r1",
    )
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "replied_by_email")).one().entity_id == ticket.id
    assert db_session.exec(select(InboundMail).where(InboundMail.graph_message_id == "r1")).one().ticket_id == ticket.id


def test_only_the_number_matters_so_an_old_prefix_still_finds_the_ticket(db_session):
    ticket = open_ticket(db_session)
    result = ingest_mail(db_session, mail("r1", subject=f"Re: [sr-{ticket.number}] Printer jam"))
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id


@pytest.mark.parametrize("subject", ["Re: INC-1 no brackets", "Re: [INC-0] zero", "Re: [FOO-1] unknown prefix", "Re: [INC-] none"])
def test_only_a_bracketed_known_reference_counts(db_session, subject):
    open_ticket(db_session)  # number 1
    assert ingest_mail(db_session, mail("r1", subject=subject)).outcome == TICKET_CREATED


def test_a_reference_to_a_ticket_that_does_not_exist_opens_a_new_one(db_session):
    result = ingest_mail(db_session, mail(subject="Re: [INC-9999] Ghost"))
    assert result.outcome == TICKET_CREATED


def test_a_reply_is_matched_by_the_conversation_before_the_subject(db_session):
    first = open_ticket(db_session, message_id="a", subject="One")
    other = open_ticket(db_session, message_id="b", subject="Two")
    graph = FakeGraph()
    # The subject names the second ticket, but the thread is the first ticket's.
    graph.add("r1", subject=f"[{other.reference}] Two", conversationId="conv-a")
    assert process_message(db_session, "r1", graph).ticket.id == first.id


def test_a_reply_is_matched_by_the_message_it_answers(db_session):
    ticket = open_ticket(db_session, message_id="a")
    graph = FakeGraph()
    graph.add(
        "r1", subject="Different subject", conversationId="new-conv",
        internetMessageHeaders=headers(In_Reply_To="<A@mail.example.com>", References="<zzz@x> <a@mail.example.com>"),
    )
    result = process_message(db_session, "r1", graph)
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id


def test_anyone_else_naming_only_the_reference_gets_a_ticket_of_their_own(db_session):
    # The reference is printed on every email, and is no proof of being in the thread: a subject-only match
    # from an unknown sender must not let them write on the ticket or become a follower.
    ticket = open_ticket(db_session)
    result = ingest_mail(db_session, mail("x1", sender="stranger@evil.example", subject=f"[{ticket.reference}] Printer jam"))
    db_session.commit()

    assert result.outcome == TICKET_CREATED and result.ticket.id != ticket.id
    assert len(messages_of(db_session, ticket)) == 1  # nothing added to the real one
    assert followers_of(db_session, ticket) == []
    [event] = events_of(db_session, result.ticket, "mail_reference_not_used")
    assert event.to_value == ticket.tag
    assert result.ticket.requester_email == "stranger@evil.example"


def followers_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id)).all()


def test_someone_midway_through_the_thread_is_added_as_a_follower_and_their_mail_is_a_reply(db_session):
    ticket = open_ticket(db_session, message_id="a", cc=["pat@example.com"])
    result = ingest_mail(db_session, mail("x1", sender="Kim@Example.com", conversation_id="conv-a", subject="Re: Printer jam", body="Same here"))
    db_session.commit()

    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    assert len(tickets(db_session)) == 1
    assert result.message.sender == "kim@example.com"
    assert sorted(f.email for f in followers_of(db_session, ticket)) == ["kim@example.com", "pat@example.com"]
    [event] = events_of(db_session, ticket, "follower_added_from_mail")
    assert event.to_value == "kim@example.com"
    kinds = {i.message.body: i.sender_kind for i in agent_timeline(db_session, ticket) if i.message is not None}
    assert kinds == {"It is stuck": "requester", "Same here": "follower"}
    assert [m.body for m in requester_visible_messages(db_session, ticket)] == ["It is stuck", "Same here"]


def test_the_message_it_answers_is_also_proof_of_the_thread(db_session):
    ticket = open_ticket(db_session, message_id="a")
    graph = FakeGraph()
    graph.add("r1", sender="kim@example.com", subject="Different", conversationId="other",
              internetMessageHeaders=headers(In_Reply_To="<a@mail.example.com>"))
    result = process_message(db_session, "r1", graph)
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id
    assert [f.email for f in followers_of(db_session, ticket)] == ["kim@example.com"]


def test_a_colleagues_reply_all_resumes_a_waiting_ticket_and_tells_the_assignee_once(client, db_session):
    from app.models import Notification
    from tests.test_reply_notification import assigned_ticket

    ticket, team, ann_id = assigned_ticket(client, db_session, TicketStatus.AWAITING_USER)
    db_session.add(InboundMail(graph_message_id="g0", internet_message_id="g0@x", conversation_id="conv-t", ticket_id=ticket.id, outcome=REPLY_ADDED))
    db_session.commit()

    result = ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-t", subject="Re: it"))
    db_session.commit()
    ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-t", subject="Re: it"))  # replay
    db_session.commit()

    db_session.expire_all()
    assert result.outcome == REPLY_ADDED
    assert db_session.get(Ticket, ticket.id).status == TicketStatus.IN_PROGRESS
    [told] = db_session.exec(select(Notification)).all()
    assert (told.kind, told.actor_email) == ("replied", "kim@example.com")
    assert told.text == f"{ticket.tag} Printer jam: Ticket updated by kim@example.com"
    assert len(followers_of(db_session, ticket)) == 1 and len(messages_of(db_session, ticket)) == 1


def test_replaying_the_same_mail_adds_nothing_more(db_session):
    ticket = open_ticket(db_session, message_id="a")
    assert ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-a")).outcome == REPLY_ADDED
    db_session.commit()
    assert ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-a")).outcome == DUPLICATE
    db_session.commit()
    assert len(messages_of(db_session, ticket)) == 2 and len(followers_of(db_session, ticket)) == 1
    assert len(events_of(db_session, ticket, "follower_added_from_mail")) == 1


def test_an_unknown_sender_on_a_resolved_ticket_is_only_recorded(db_session):
    ticket = open_ticket(db_session, message_id="a")
    triage_by_hand(db_session, ticket, TicketStatus.RESOLVED)
    result = ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-a"))
    db_session.commit()
    db_session.refresh(ticket)
    assert result.outcome == REPLY_ADDED and ticket.status == TicketStatus.RESOLVED
    assert len(messages_of(db_session, ticket)) == 2 and len(followers_of(db_session, ticket)) == 1


def test_the_shared_mailbox_the_requester_and_a_repeat_sender_are_never_added_as_followers(db_session):
    ticket = open_ticket(db_session, message_id="a")
    ingest_mail(db_session, mail("x1", sender="Jo@Example.com", conversation_id="conv-a"))  # the requester
    ingest_mail(db_session, mail("x2", sender="kim@example.com", conversation_id="conv-a"))
    ingest_mail(db_session, mail("x3", sender="KIM@example.com", conversation_id="conv-a"))  # already following
    db_session.commit()
    assert [f.email for f in followers_of(db_session, ticket)] == ["kim@example.com"]
    assert len(messages_of(db_session, ticket)) == 4
    # The mailbox's own mail is dropped before it gets this far, and the guard holds even if it were not.
    assert inbound._join_thread(db_session, ticket, MAILBOX) is False
    assert [f.email for f in followers_of(db_session, ticket)] == ["kim@example.com"]


def test_when_the_follower_list_is_full_a_stranger_in_the_thread_gets_a_ticket_of_their_own(db_session, monkeypatch):
    from app import followers

    monkeypatch.setattr(followers, "MAX_EXTERNAL_FOLLOWERS", 1)
    ticket = open_ticket(db_session, message_id="a", cc=["pat@example.com"])
    result = ingest_mail(db_session, mail("x1", sender="kim@example.com", conversation_id="conv-a"))
    db_session.commit()
    assert result.outcome == TICKET_CREATED and result.ticket.id != ticket.id
    assert [f.email for f in followers_of(db_session, ticket)] == ["pat@example.com"]
    assert len(messages_of(db_session, ticket)) == 1


def test_an_agent_who_is_only_an_internal_follower_cannot_reply_by_mail(db_session):
    from app.followers import add_follower
    from app.models import User, UserRole

    ticket = open_ticket(db_session)
    agent = User(email="agent@corp.example", display_name="Agent", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.flush()
    add_follower(db_session, ticket, "x", "system", internal_user=agent)
    db_session.commit()
    result = ingest_mail(db_session, mail("x1", sender="agent@corp.example", subject=f"[{ticket.reference}] x"))
    assert result.outcome == TICKET_CREATED  # agents answer from the ticket page
    # Not even in the thread: an internal follower is already on the list, so there is nobody to add.
    result = ingest_mail(db_session, mail("x2", sender="agent@corp.example", conversation_id="conv-m1"))
    assert result.outcome == TICKET_CREATED


def test_a_follower_replying_is_recorded_under_their_address_and_shown_as_a_follower(db_session):
    ticket = open_ticket(db_session, cc=["pat@example.com"])
    result = ingest_mail(db_session, mail("r1", sender="Pat@Example.com", subject=f"Re: [{ticket.reference}] Printer jam", body="Same here"))
    db_session.commit()

    assert result.outcome == REPLY_ADDED and result.message.sender == "pat@example.com"
    timeline = agent_timeline(db_session, ticket)
    kinds = {i.message.body: i.sender_kind for i in timeline if i.message is not None}
    assert kinds == {"It is stuck": "requester", "Same here": "follower"}


def test_a_reply_from_someone_with_an_account_is_linked_to_it(db_session):
    from app.models import User, UserRole

    user = User(email="jo@example.com", display_name="Jo", role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    ticket = open_ticket(db_session)
    ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x"))
    db_session.commit()
    assert [m.author_user_id for m in messages_of(db_session, ticket)] == [user.id, user.id]


# ---- What a reply does ---------------------------------------------------------------------------


def test_a_reply_hands_a_ticket_waiting_on_the_requester_back_to_us(db_session):
    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket, TicketStatus.AWAITING_USER)
    ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x"))
    db_session.commit()
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.IN_PROGRESS
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).one().detail == "awaiting_user -> in_progress"
    [event] = events_of(db_session, ticket, "status_changed")
    assert event.actor_email == "jo@example.com"


@pytest.mark.parametrize(
    "status",
    [TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WITH_MSP, TicketStatus.WITH_SUPPLIER, TicketStatus.RESOLVED, TicketStatus.CLOSED],
)
def test_a_reply_changes_no_other_status_but_is_recorded(db_session, status):
    ticket = open_ticket(db_session)
    if status != TicketStatus.NEW:
        triage_by_hand(db_session, ticket, status)
    before = ticket.updated_at
    result = ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x", body="One more thing"))
    db_session.commit()
    db_session.refresh(ticket)
    assert result.outcome == REPLY_ADDED and ticket.status == status
    assert ticket.updated_at > before and len(messages_of(db_session, ticket)) == 2


def test_the_requester_sees_their_reply_through_the_one_visible_list(db_session):
    ticket = open_ticket(db_session)
    ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x", body="Me again"))
    db_session.commit()
    assert [m.body for m in requester_visible_messages(db_session, ticket)] == ["It is stuck", "Me again"]


def test_a_reply_on_a_split_request_refreshes_every_open_part(client, db_session):
    original, desk = world(client, db_session)
    split(client, original, ("Keyboard", "E key"), ("Office", ""))
    first, second = subs_of(db_session, original)
    for part in (first, second):
        part.status = TicketStatus.AWAITING_USER
        db_session.add(part)
    db_session.commit()

    result = ingest_mail(db_session, mail("r1", sender=original.requester_email, subject=f"Re: [{original.reference}] {original.subject}"))
    db_session.commit()

    assert result.outcome == REPLY_ADDED and result.ticket.id == original.id
    db_session.expire_all()
    assert {db_session.get(Ticket, t.id).status for t in (first, second)} == {TicketStatus.IN_PROGRESS}


def test_the_reply_notification_is_asked_for_with_the_senders_address(db_session, monkeypatch):
    # A reply on an existing ticket passes reply_by so the assignee is notified; a new ticket does not.
    seen = {}

    def spy(session, ticket, *, sender, body, author_user_id=None, reply_by=None):
        seen.update(sender=sender, reply_by=reply_by)
        return record_inbound(session, ticket, sender=sender, body=body, author_user_id=author_user_id)

    ticket = open_ticket(db_session)
    monkeypatch.setattr(inbound, "record_inbound", spy)
    ingest_mail(db_session, mail("r1", subject=f"[{ticket.reference}] x"))
    assert seen == {"sender": "jo@example.com", "reply_by": "jo@example.com"}

    # The message a ticket is opened with tells nobody.
    seen.clear()
    ingest_mail(db_session, mail("n1", sender="kim@example.com", subject="Brand new"))
    assert seen == {"sender": "kim@example.com", "reply_by": None}


# ---- Files ---------------------------------------------------------------------------------------


def test_files_are_stored_on_the_ticket_through_the_attachment_rules(db_session, storage):
    graph = FakeGraph()
    graph.add("m1")
    graph.attach("m1", "report.pdf", PDF)
    graph.attach("m1", "..\\evil.exe", b"MZ" + b"\x00" * 50)
    graph.attach("m1", "logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, inline=True)  # too small: a signature picture
    graph.attach("m1", "photo.png", BIG_PNG, inline=True)

    result = process_message(db_session, "m1", graph, storage)
    db_session.commit()

    db_session.expire_all()
    files = db_session.exec(select(Attachment).order_by(Attachment.id)).all()
    assert [(f.filename, f.content_type, f.is_inline, f.message_id) for f in files] == [
        ("report.pdf", "application/pdf", False, result.message.id),
        ("photo.png", "image/png", True, result.message.id),
    ]
    assert storage.read(files[0].blob_key) == PDF
    [skipped] = events_of(db_session, result.ticket, "attachment_skipped")
    assert skipped.to_value == "evil.exe" and ".exe" in skipped.detail


def test_a_file_over_the_size_limit_is_noted_and_never_downloaded(db_session, storage, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_bytes", 100)
    graph = FakeGraph()
    graph.add("m1")
    graph.attach("m1", "big.pdf", PDF + b"y" * 500)
    graph.attach("m1", "ok.pdf", PDF)
    graph.files["m1"][0][0]["size"] = 532  # what Graph says it is
    result = process_message(db_session, "m1", graph, storage)
    db_session.commit()
    assert graph.downloaded == ["m1-1"]
    assert [a.filename for a in db_session.exec(select(Attachment)).all()] == ["ok.pdf"]
    [skipped] = events_of(db_session, result.ticket, "attachment_skipped")
    assert skipped.to_value == "big.pdf" and "limit" in skipped.detail


def test_a_file_that_lies_about_its_size_is_caught_after_download(db_session, storage, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_bytes", 100)
    graph = FakeGraph()
    graph.add("m1")
    graph.attach("m1", "liar.pdf", PDF + b"y" * 500)
    graph.files["m1"][0][0]["size"] = 10
    result = process_message(db_session, "m1", graph, storage)
    db_session.commit()
    assert db_session.exec(select(Attachment)).all() == []
    assert len(events_of(db_session, result.ticket, "attachment_skipped")) == 1


def test_only_so_many_files_are_kept_per_mail_and_attached_emails_are_skipped(db_session, storage):
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "2")
    graph = FakeGraph()
    graph.add("m1")
    for i in range(3):
        graph.attach("m1", f"f{i}.pdf", PDF + bytes([i]))
    graph.attach("m1", "other mail", b"", **{"@odata.type": "#microsoft.graph.itemAttachment"})
    result = process_message(db_session, "m1", graph, storage)
    db_session.commit()
    assert len(db_session.exec(select(Attachment)).all()) == 2
    assert sorted(e.to_value for e in events_of(db_session, result.ticket, "attachment_skipped")) == ["f2.pdf", "other mail"]


def test_a_reply_keeps_its_files_on_the_reply(db_session, storage):
    ticket = open_ticket(db_session)
    graph = FakeGraph()
    graph.add("r1", subject=f"[{ticket.reference}] x")
    graph.attach("r1", "screen.pdf", PDF)
    result = process_message(db_session, "r1", graph, storage)
    db_session.commit()
    [file] = db_session.exec(select(Attachment)).all()
    assert (file.ticket_id, file.message_id) == (ticket.id, result.message.id)


def test_files_on_an_automatic_reply_are_not_even_fetched(db_session, storage):
    graph = FakeGraph()
    graph.add("m1", internetMessageHeaders=headers(Auto_Submitted="auto-replied"))
    graph.attach("m1", "report.pdf", PDF)
    process_message(db_session, "m1", graph, storage)
    assert graph.downloaded == [] and db_session.exec(select(Attachment)).all() == []


# ---- Reading Graph's answer ----------------------------------------------------------------------


def test_a_graph_message_is_read_into_the_fields_the_intake_uses():
    raw = graph_message(
        "AAMk=", sender="Jo@Example.com", ccRecipients=[{"emailAddress": {"address": "Pat@x.org"}}, {"nope": 1}],
        internetMessageHeaders=[
            {"name": "In-Reply-To", "value": "<One@x>"},
            {"name": "References", "value": "<Zero@x> <One@x>"},
            {"name": "Auto-Submitted", "value": "no"},
            {"name": "auto-submitted", "value": "auto-replied"},
        ],
    )
    parsed = parse_graph_message(raw)
    assert (parsed.graph_message_id, parsed.sender, parsed.cc) == ("AAMk=", "jo@example.com", ["pat@x.org"])
    assert parsed.internet_message_id == "aamk=@mail.example.com"
    assert parsed.in_reply_to == ["one@x", "one@x", "zero@x"]
    assert parsed.headers["auto-submitted"] == "no"  # the first of a repeated header


def test_odd_shapes_from_graph_become_empty_not_errors():
    parsed = parse_graph_message({"id": "x", "from": "nonsense", "uniqueBody": None, "internetMessageHeaders": "no", "ccRecipients": None})
    assert (parsed.sender, parsed.body, parsed.cc, parsed.headers) == ("", "", [], {})


def test_the_body_falls_back_to_the_full_body_when_there_is_no_unique_body():
    parsed = parse_graph_message({"id": "x", "body": {"contentType": "text", "content": "whole"}})
    assert parsed.body == "whole"


def test_the_real_client_asks_graph_for_plain_text_and_the_headers(monkeypatch):
    sent = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        sent.update(url=url, params=params, headers=headers)
        return httpx.Response(200, json={"id": "m1"}, request=httpx.Request("GET", url))

    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", fake_get)
    assert client.get_message("AA/BB=") == {"id": "m1"}
    assert sent["url"].endswith("/users/support@corp.example/messages/AA%2FBB%3D")
    assert sent["headers"]["Prefer"] == 'outlook.body-content-type="text"' and sent["headers"]["Authorization"] == "Bearer token"
    assert "uniqueBody" in sent["params"]["$select"] and "internetMessageHeaders" in sent["params"]["$select"]


def test_the_real_client_treats_404_as_gone_and_other_errors_as_errors(monkeypatch):
    status = {"code": 404}

    def fake_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(status["code"], json={}, request=httpx.Request("GET", url))

    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", fake_get)
    assert client.get_message("m") is None and client.get_attachments("m") == [] and client.get_attachment_bytes("m", "a") is None
    status["code"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        client.get_message("m")


def test_the_real_client_reads_attachment_bytes_raw(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        return httpx.Response(200, content=b"abc", request=httpx.Request("GET", url))

    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", fake_get)
    assert client.get_attachment_bytes("m1", "a=") == b"abc" and seen["url"].endswith("/attachments/a%3D/$value")


# ---- The worker job ------------------------------------------------------------------------------


def stored(db_session, resource_id="m1", change_type="created", lifecycle_event=None, **fields):
    row = GraphNotification(resource_id=resource_id, change_type=change_type, lifecycle_event=lifecycle_event, **fields)
    db_session.add(row)
    db_session.commit()
    return row.id


def row_of(db_session, row_id):
    db_session.expire_all()
    return db_session.get(GraphNotification, row_id)


def test_a_stored_notification_becomes_a_ticket_and_is_marked_done(db_session, storage):
    graph = FakeGraph()
    graph.add("m1")
    row_id = stored(db_session)
    assert process_notifications(db_session, graph, storage) == (1, 0)
    row = row_of(db_session, row_id)
    assert row.processed_at is not None and row.last_error is None
    assert len(tickets(db_session)) == 1
    assert process_notifications(db_session, graph, storage) == (0, 0)  # nothing left to do


def test_notifications_that_are_not_new_mail_or_belong_to_3c_are_left_alone(db_session):
    graph = FakeGraph()
    updated = stored(db_session, "m1", change_type="updated")
    deleted = stored(db_session, "m1", change_type="deleted")
    lifecycle = stored(db_session, None, change_type=None, lifecycle_event="reauthorizationRequired")
    assert process_notifications(db_session, graph) == (2, 0)
    assert graph.fetched == []  # a read flag changing does not make Graph fetch anything
    assert row_of(db_session, updated).processed_at and row_of(db_session, deleted).processed_at
    assert row_of(db_session, lifecycle).processed_at is None  # 3C's, untouched


def test_the_same_mail_notified_twice_is_still_one_ticket(db_session, storage):
    graph = FakeGraph()
    graph.add("m1")
    stored(db_session)
    stored(db_session)
    assert process_notifications(db_session, graph, storage) == (2, 0)
    assert len(tickets(db_session)) == 1 and graph.fetched == ["m1"]


def test_a_graph_failure_backs_off_then_works_and_one_bad_mail_does_not_block_the_next(db_session, storage):
    graph = FakeGraph()
    graph.add("m1")
    graph.add("m2", subject="Second")
    graph.broken = httpx.ConnectError("secret-host.internal")
    first = stored(db_session, "m1")
    graph.fetched.clear()
    now = datetime.now(timezone.utc)

    assert process_notifications(db_session, graph, storage, now=now) == (0, 1)
    row = row_of(db_session, first)
    assert row.attempts == 1 and row.processed_at is None and row.next_attempt_at > now
    assert row.last_error == "ConnectError" and "secret-host" not in row.last_error
    assert process_notifications(db_session, graph, storage, now=now) == (0, 0)  # waiting out the backoff

    graph.broken = None
    second = stored(db_session, "m2")
    assert process_notifications(db_session, graph, storage, now=now + timedelta(hours=2)) == (2, 0)
    assert row_of(db_session, first).processed_at and row_of(db_session, second).processed_at
    assert {t.subject for t in tickets(db_session)} == {"Printer jam", "Second"}


def test_a_notification_that_never_works_is_given_up_on_with_the_reason(db_session):
    graph = FakeGraph()
    graph.broken = httpx.HTTPStatusError("x", request=httpx.Request("GET", "u"), response=httpx.Response(500))
    row_id = stored(db_session)
    now = datetime.now(timezone.utc)
    for round_ in range(MAX_ATTEMPTS):
        process_notifications(db_session, graph, now=now + timedelta(days=round_))
    row = row_of(db_session, row_id)
    assert row.processed_at is not None and row.attempts == MAX_ATTEMPTS
    assert row.last_error == f"Gave up after {MAX_ATTEMPTS} tries: Graph answered 500"


def test_a_message_that_vanished_is_done_not_retried(db_session):
    row_id = stored(db_session, "deleted-meanwhile")
    assert process_notifications(db_session, FakeGraph()) == (1, 0)
    assert row_of(db_session, row_id).processed_at is not None


def test_the_job_is_registered_and_does_nothing_until_switched_on(db_session, monkeypatch):
    assert "mail-in" in [job.name for job in JOBS]
    called = []
    monkeypatch.setattr("app.jobs.process_notifications", lambda session: called.append(1))
    mail_in(db_session)
    assert called == []  # INBOUND_ENABLED is off
    monkeypatch.setattr(settings, "inbound_enabled", True)
    mail_in(db_session)
    assert called == []  # on, but no Graph credentials
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "c")
    monkeypatch.setattr(settings, "graph_client_secret", "s")
    mail_in(db_session)
    assert called == [1]


# ---- The dev helper runs the same intake ---------------------------------------------------------


def test_the_dev_helper_opens_a_ticket_and_a_reference_makes_it_a_reply(client, db_session):
    first = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "a@x.org", "subject": "Printer", "body": "Jammed", "cc": "b@x.org, c@x.org"},
    ).json()
    assert first["outcome"] == TICKET_CREATED and first["reference"].startswith("INC-")

    again = client.post(
        "/webhooks/dev/simulate-inbound",
        data={"requester_email": "a@x.org", "subject": f"Re: [{first['reference']}] Printer", "body": "Still"},
    ).json()
    assert again["outcome"] == REPLY_ADDED and again["number"] == first["number"]
    [ticket] = tickets(db_session)
    assert len(messages_of(db_session, ticket)) == 2
    assert sorted(f.email for f in db_session.exec(select(TicketFollower)).all()) == ["b@x.org", "c@x.org"]


def test_the_dev_helper_applies_the_same_filters(client, db_session):
    body = client.post(
        "/webhooks/dev/simulate-inbound", data={"requester_email": MAILBOX, "subject": "Loop", "body": "x"}
    ).json()
    assert body == {"outcome": IGNORED_OWN} and tickets(db_session) == []


def test_a_stranger_joining_a_thread_does_not_take_the_thread_over(db_session):
    # The thread stays with the first mail's ticket, so a stranger who joins it changes nothing about it.
    ticket = open_ticket(db_session, message_id="a")
    ingest_mail(db_session, mail("x1", sender="stranger@evil.example", conversation_id="conv-a"))
    db_session.commit()
    result = ingest_mail(db_session, mail("r1", conversation_id="conv-a", subject="no reference"))
    assert result.outcome == REPLY_ADDED and result.ticket.id == ticket.id


def test_spaces_and_joiners_survive_but_control_characters_do_not(db_session):
    raw = graph_message("p1", subject="Hello\u00a0world \u200d", body="a\u00a0b\x00\x07c\u2028d")
    result = ingest_mail(db_session, parse_graph_message(raw))
    assert result.ticket.subject == "Hello world \u200d"  # the subject's white space is normalised to single spaces
    assert messages_of(db_session, result.ticket)[0].body == "a\u00a0bc\u2028d"


def test_a_flooding_senders_files_are_not_downloaded(db_session, storage):
    set_setting(db_session, INBOUND_MAX_PER_SENDER_HOUR, "1")
    graph = FakeGraph()
    graph.add("m1")
    graph.add("m2")
    graph.attach("m2", "report.pdf", PDF)
    assert process_message(db_session, "m1", graph, storage).outcome == TICKET_CREATED
    assert process_message(db_session, "m2", graph, storage).outcome == IGNORED_FLOOD
    assert graph.downloaded == []

"""#350: ticket emails go as replies in the ticket's mail conversation (Graph `reply`, Mail.Send only). Which earlier email a new one
replies to (app/mail_threading.py), the fresh-send fallbacks, how the Sent Items id of what we sent is learned, and the Graph calls.
Graph is simulated; how Exchange really quotes, threads and shows these is Azure-only (docs/build-plan.md, staging checklist)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import email_reactions, mail, mail_threading, outbox
from app.config import settings
from app.graph_client import GraphClient
from app.messages import record_inbound
from app.models import InboundMail, MessageVisibility, OutboundEmail, Ticket
from tests.test_email_preview import follow, ticket_for_email
from tests.test_outbound_mail import dev_mail, email, rows  # noqa: F401  (dev_mail is a fixture)

REQUESTER = "walkin@example.com"
MAILBOX = "support@corp.example"


class SimGraph:
    """The Graph calls the sender makes: sendMail, reply and the newest Sent Items. Every send is saved to a Sent Items list with an
    id, the way Exchange does (a reply's copy gets an "RE: " in front, as Outlook does)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.items: list[dict] = []
        self.gone: set[str] = set()
        self.refuse_with: int = 404

    def _save(self, to, subject, cc):
        item = {
            "id": f"sent-{len(self.items) + 1}", "subject": subject, "sentDateTime": datetime.now(timezone.utc).isoformat(),
            "toRecipients": [{"emailAddress": {"address": a}} for a in [to]],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
        }
        self.items.insert(0, item)

    def send_mail(self, to_address, subject, body, cc=None, headers=None, attachments=None):
        self.calls.append({"how": "send", "to": to_address, "cc": cc or [], "subject": subject, "body": body, "headers": headers})
        self._save(to_address, subject, cc)

    def reply_to_message(self, message_id, to_address, subject, comment, cc=None, headers=None, attachments=None):
        if message_id in self.gone:
            raise httpx.HTTPStatusError(
                "gone", request=httpx.Request("POST", "https://graph.example/reply"), response=httpx.Response(self.refuse_with)
            )
        self.calls.append({
            "how": "reply", "to_id": message_id, "to": to_address, "cc": cc or [], "subject": subject, "comment": comment, "headers": headers,
        })
        self._save(to_address, "RE: " + subject, cc)

    def recent_sent_items(self, limit=15):
        return self.items[:limit]


@pytest.fixture
def sim(monkeypatch):
    graph = SimGraph()
    monkeypatch.setattr(mail, "graph_client", graph)
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)
    return graph


def deliver(db_session):
    return mail.deliver_mail(db_session, send=mail.send_through_graph, recent_sent=mail.graph_client.recent_sent_items)


def inbound_mail(db_session, ticket, graph_id, sender=REQUESTER, to=MAILBOX, cc="", msp=None, minutes_ago=30):
    """A mail that reached the shared mailbox and became a message on the ticket, the way app/inbound.py records one."""
    ticket = db_session.get(Ticket, ticket.id)
    message = record_inbound(db_session, ticket, sender=sender, body="It is still jammed", channel="email")
    message.graph_message_id = graph_id
    message.msp_forward = msp
    db_session.add(message)
    db_session.flush()
    db_session.add(InboundMail(
        graph_message_id=graph_id, internet_message_id=f"<{graph_id}@x>", sender=sender, to_addresses=to, cc_addresses=cc or None,
        outcome="reply_added", ticket_id=ticket.id, message_id=message.id,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    ))
    db_session.commit()
    return message


def without_raised_for(db_session):
    """Leaves only the agent's email in the outbox: two emails of different kinds seconds apart cannot be told apart in Sent Items
    by the reactions job's match (by design), and these tests are about that match."""
    for row in rows(db_session, outbox.RAISED_FOR):
        db_session.delete(row)
    db_session.commit()


def only_reply_rows(db_session, ticket):
    return [r for r in rows(db_session, outbox.REPLY) if r.ticket_id == ticket.id]


# ---- What to reply to --------------------------------------------------------------------------------


def test_the_first_email_on_a_portal_ticket_is_fresh_and_later_ones_reply_to_it(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, "First")
    assert deliver(db_session)[1] == 0
    first = sim.calls[0]
    assert first["how"] == "send"  # nothing to reply to: this starts the conversation
    assert rows(db_session)[0].graph_message_id == "sent-1"

    email(client, ticket, "Second")
    deliver(db_session)
    second = sim.calls[-1]
    assert second["how"] == "reply" and second["to_id"] == sim.items[1]["id"]  # to the first email's Sent Items copy
    email(client, ticket, "Third")
    deliver(db_session)
    third = sim.calls[-1]
    assert third["how"] == "reply" and third["to_id"] == sim.items[1]["id"]  # the latest one is the reply just sent
    sent = rows(db_session)
    assert [r.graph_message_id for r in sent] == [f"sent-{i}" for i in range(1, len(sent) + 1)]
    assert sent[-1].reply_to_graph_id == sent[-2].graph_message_id and sent[-1].thread_note is None


def test_on_an_email_raised_ticket_the_first_email_replies_to_the_requesters_message(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")
    email(client, ticket, "Hello")

    deliver(db_session)

    first = sim.calls[0]  # the "raised for you" email, the first of the two sent
    assert first["how"] == "reply" and first["to_id"] == "in-1" and first["to"] == REQUESTER
    assert all(c["how"] == "reply" for c in sim.calls)


def test_the_text_goes_in_comment_and_the_recipients_are_exactly_the_outbox(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1", cc="boss@example.com")
    follow(client, ticket, "boss@example.com")
    email(client, ticket, "Please restart it")

    deliver(db_session)

    row = only_reply_rows(db_session, ticket)[0]
    [call] = [c for c in sim.calls if c["how"] == "reply" and "Please restart it" in c["comment"]]
    assert call["to"] == row.to_address == REQUESTER
    assert call["cc"] == [a.strip() for a in row.cc.split(",")] == ["boss@example.com"]
    assert call["comment"] == row.body and call["subject"] == row.subject
    assert call["headers"]["X-Ticket-Reference"] == ticket.reference


def test_a_follower_who_was_never_on_an_email_gets_a_fresh_one_and_one_who_was_gets_a_reply(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")  # the requester wrote to the mailbox alone
    follow(client, ticket, "late@example.com")
    email(client, ticket, "First answer")
    deliver(db_session)

    answer = [c for c in sim.calls if "First answer" in (c.get("comment") or c.get("body") or "")][0]
    assert answer["how"] == "send" and answer["cc"] == ["late@example.com"]  # the follower was not on in-1: a reply would quote it to them

    email(client, ticket, "Second answer")
    deliver(db_session)

    second = [c for c in sim.calls if "Second answer" in (c.get("comment") or c.get("body") or "")][0]
    assert second["how"] == "reply" and second["cc"] == ["late@example.com"]  # now everyone was on the first answer


def test_a_follower_update_replies_only_to_an_email_that_follower_was_on(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    message = inbound_mail(db_session, ticket, "in-1", cc="boss@example.com")  # the boss was copied on the requester's mail
    other = inbound_mail(db_session, ticket, "in-2", minutes_ago=5)  # a later one, to the mailbox alone
    row = outbox.queue_follower_update(db_session, db_session.get(Ticket, ticket.id), other, "boss@example.com")
    db_session.commit()
    assert message.id != other.id and row is not None

    deliver(db_session)

    [call] = [c for c in sim.calls if c["to"] == "boss@example.com"]
    assert call["how"] == "reply" and call["to_id"] == "in-1"  # not in-2, which the boss never had


def test_an_msp_message_or_another_persons_email_is_never_the_replied_to_message(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "msp-1", sender="desk@msp.example", cc=REQUESTER, msp="not_needed", minutes_ago=10)
    inbound_mail(db_session, ticket, "other-1", sender="stranger@example.com", minutes_ago=5)
    email(client, ticket, "Hello")

    deliver(db_session)

    # The first email (raised for you) has nothing it may reply to, so it is fresh; none is a reply to the MSP's or the stranger's mail.
    assert sim.calls[0]["how"] == "send"
    assert {c["to_id"] for c in sim.calls if c["how"] == "reply"}.isdisjoint({"msp-1", "other-1"})


def test_an_agents_only_note_is_never_the_replied_to_message(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    note = inbound_mail(db_session, ticket, "in-1")
    note.visibility = MessageVisibility.AGENTS_ONLY
    db_session.add(note)
    db_session.commit()
    row = OutboundEmail(kind=outbox.REPLY, ticket_id=ticket.id, to_address=REQUESTER)
    assert mail_threading.reply_target(db_session, row, [REQUESTER]) is None


def test_the_latest_email_the_person_was_on_wins_over_an_older_one(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-old", minutes_ago=60)
    inbound_mail(db_session, ticket, "in-new", minutes_ago=20)
    row = OutboundEmail(kind=outbox.REPLY, ticket_id=ticket.id, to_address=REQUESTER)
    assert mail_threading.reply_target(db_session, row, [REQUESTER]) == "in-new"


def test_approval_and_msp_kinds_are_never_replies(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")
    for kind in (
        outbox.APPROVAL, outbox.APPROVAL_REMINDER, outbox.APPROVAL_OVERDUE, outbox.APPROVAL_WITHDRAWN, outbox.MSP_HANDOFF, outbox.MSP_RELEASE,
        outbox.MSP_FORWARD, outbox.MAILBOX_CHANGE, outbox.EMERGENCY_RESET,
    ):
        assert kind not in mail_threading.THREADED_KINDS
        row = OutboundEmail(kind=kind, ticket_id=ticket.id, to_address=REQUESTER)
        assert mail_threading.reply_target(db_session, row, [REQUESTER]) is None
    # And the sender: a Mail with no reply target is a fresh sendMail, which is all those kinds ever carry.
    mail.send_through_graph(mail.Mail(REQUESTER, [], "s", "b", "b", automatic=False, reference="INC-1"))
    assert sim.calls[-1]["how"] == "send"


def test_an_email_built_for_an_approval_is_not_given_a_reply_target(client, db_session, dev_mail, sim, monkeypatch):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")
    row = OutboundEmail(kind=outbox.APPROVAL, ticket_id=ticket.id, to_address=REQUESTER)
    db_session.add(row)
    db_session.commit()
    seen = []
    monkeypatch.setattr(
        mail, "BUILDERS", {outbox.APPROVAL: lambda s, r: mail.Mail(REQUESTER, [], "s", "b", "b", automatic=True, reference="INC-1")}
    )
    mail.deliver_mail(db_session, send=lambda m: seen.append(m.reply_to), recent_sent=lambda: [])
    assert seen == [None]


# ---- Fallbacks ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 400, 403])
def test_a_reply_that_graph_refuses_goes_fresh_and_the_row_says_so(client, db_session, dev_mail, sim, status):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")
    sim.gone, sim.refuse_with = {"in-1"}, status
    email(client, ticket, "Hello")

    sent, failed = deliver(db_session)

    assert (sent, failed) == (2, 0)  # "raised for you", then the agent's email
    # The first went fresh when in-1 was refused; the second then replies to that fresh email's copy, not to in-1 again.
    assert [c["how"] for c in sim.calls] == ["send", "reply"] and sim.calls[1]["to_id"] == "sent-1"
    [raised] = rows(db_session, outbox.RAISED_FOR)
    assert raised.sent_at is not None and raised.thread_note.startswith("Sent as a new email") and str(status) in raised.thread_note
    # Only a message that is gone is remembered as unusable: after another refusal (a bad attachment, say) it stays a possible target.
    assert raised.reply_to_graph_id == ("in-1" if status == 404 else None)
    assert only_reply_rows(db_session, ticket)[0].thread_note is None

    email(client, ticket, "Again")  # the refused message is not tried again
    deliver(db_session)
    assert sim.calls[-1]["how"] == "reply" and sim.calls[-1]["to_id"] == "sent-2"


def test_other_failures_of_a_reply_retry_as_any_send_does(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    inbound_mail(db_session, ticket, "in-1")
    sim.gone, sim.refuse_with = {"in-1"}, 429
    email(client, ticket, "Hello")

    sent, failed = deliver(db_session)

    assert sent == 0 and failed >= 1 and sim.calls == []
    row = rows(db_session)[0]
    assert row.sent_at is None and row.attempts == 1 and row.thread_note is None


def test_a_failed_sent_items_lookup_does_not_fail_the_email(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, "Hello")

    def broken():
        raise httpx.ConnectError("down")

    assert mail.deliver_mail(db_session, send=mail.send_through_graph, recent_sent=broken) == (2, 0)
    assert all(r.graph_message_id is None and r.sent_at is not None for r in rows(db_session))


# ---- Learning the Sent Items id ----------------------------------------------------------------------


def test_the_reactions_job_finds_the_id_when_the_first_lookup_missed(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    without_raised_for(db_session)
    email(client, ticket, "Hello")
    mail.deliver_mail(db_session, send=mail.send_through_graph, recent_sent=lambda: [])  # Sent Items had not caught up
    row = rows(db_session)[-1]
    assert row.graph_message_id is None

    assert email_reactions.claim_sent_item(db_session, sim.items[0]) is True
    db_session.commit()

    assert rows(db_session)[-1].graph_message_id == sim.items[0]["id"]
    assert email_reactions.claim_sent_item(db_session, sim.items[0]) is False  # nothing left to claim


def test_a_reply_copy_with_re_in_front_is_matched_to_its_row(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    without_raised_for(db_session)
    inbound_mail(db_session, ticket, "in-1")
    email(client, ticket, "Hello")
    mail.deliver_mail(db_session, send=mail.send_through_graph, recent_sent=lambda: [])
    assert sim.items[0]["subject"].startswith("RE: ")

    assert email_reactions.claim_sent_item(db_session, sim.items[0]) is True


def test_an_email_is_never_claimed_by_a_copy_that_belongs_to_another(client, db_session, dev_mail, sim):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, "One")
    email(client, ticket, "Two")
    mail.deliver_mail(db_session, send=mail.send_through_graph, recent_sent=lambda: [])
    first, second = [r for r in rows(db_session) if r.sent_at is not None][-2:]

    # Two copies seconds apart cannot be told apart: nothing is claimed (the reactions job decides, with the same guard).
    assert mail_threading.claim_sent_copy(db_session, first, sim.items) is False
    for item, minutes in zip(sim.items, (5, 0, 10)):  # the newest copy 5 minutes off, the middle one on time, the oldest 10 off
        item["sentDateTime"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    assert mail_threading.claim_sent_copy(db_session, first, sim.items) is True
    assert mail_threading.claim_sent_copy(db_session, second, sim.items) is True
    assert first.graph_message_id != second.graph_message_id


def test_subject_prefixes_are_ignored_when_matching():
    assert mail_threading.norm_subject("RE: [INC-1] Printer") == mail_threading.norm_subject("[INC-1] Printer") == "[INC-1] Printer"
    assert mail_threading.norm_subject("Re: RE: [INC-1] Printer") == "[INC-1] Printer"
    assert mail_threading.norm_subject(None) == ""


# ---- The Graph calls ---------------------------------------------------------------------------------


def test_the_reply_call_posts_to_reply_with_the_text_in_comment_and_explicit_recipients(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "it-support@example.org")
    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    client.reply_to_message(
        "AAMk=id/1", "walkin@example.com", "[INC-5] Printer", "Hello", cc=["boss@example.com"],
        headers={"X-Ticket-Reference": "INC-5"}, attachments=[("a.txt", "text/plain", b"hi")],
    )

    assert captured["url"] == "https://graph.microsoft.com/v1.0/users/it-support@example.org/messages/AAMk%3Did%2F1/reply"
    payload = captured["json"]
    assert payload["comment"] == "Hello" and "body" not in payload["message"]  # Graph refuses comment together with message.body
    assert "createReply" not in captured["url"]
    message = payload["message"]
    assert message["toRecipients"] == [{"emailAddress": {"address": "walkin@example.com"}}]
    assert message["ccRecipients"] == [{"emailAddress": {"address": "boss@example.com"}}]
    assert message["subject"] == "[INC-5] Printer"
    assert message["internetMessageHeaders"] == [{"name": "X-Ticket-Reference", "value": "INC-5"}]
    assert message["attachments"][0]["name"] == "a.txt"
    assert captured["headers"]["Authorization"] == "Bearer token"


def test_the_sent_items_lookup_reads_the_newest_messages(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "it-support@example.org")
    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params)
        return httpx.Response(200, json={"value": [{"id": "x"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    assert client.recent_sent_items(5) == [{"id": "x"}]
    assert captured["url"].endswith("/users/it-support@example.org/mailFolders/sentitems/messages")
    assert captured["params"]["$orderby"] == "sentDateTime desc" and captured["params"]["$top"] == 5


def test_an_automatic_email_keeps_its_headers_on_a_reply(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        mail.graph_client, "reply_to_message",
        lambda message_id, to, subject, comment, cc=None, headers=None, attachments=None: sent.update(headers=headers, id=message_id),
    )
    note = mail.Mail("a@example.com", [], "s", "b", "b", automatic=True, reference="INC-1", reply_to="in-1")
    mail.send_through_graph(note)
    assert sent == {"id": "in-1", "headers": {"X-Ticket-Reference": "INC-1", "X-Auto-Response-Suppress": "All"}}
    assert note.thread_note is None

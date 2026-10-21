"""4B-6: followers who were not copied on an emailed update are told. Copied people get nothing, the
others get exactly one email each, the sender never does, a replay is a no-op, internal notes never
leak, and the cap, opt-out, never-follow list and triage rules are asked again at send time."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app import email_templates as et
from app import mail, outbox
from app.config import settings
from app.follower_updates import queue_follower_updates
from app.followers import add_follower, bare_address, followers_of, remove_follower, uncopied_recipients
from app.inbound import DUPLICATE, REPLY_ADDED, ingest_mail, parse_graph_message
from app.messages import add_note
from app.models import InboundMail, Message, NeverFollowAddress, NotificationPreference, OutboundEmail, Ticket, User, UserRole
from app.settings_store import AUTOMATIC_EMAIL_CAP, set_setting
from tests.conftest import login_as
from tests.test_inbound_mail import MAILBOX, graph_message, open_ticket, triage_by_hand
from tests.test_inbound_mail import mail as incoming
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)


def updates(db_session):
    return rows(db_session, outbox.FOLLOWER_UPDATE)


def who(db_session):
    return sorted(r.to_address for r in updates(db_session))


def world(db_session, cc=("pat@example.com", "sam@example.com"), triaged=True):
    """A ticket jo opened by mail, copying pat and sam, who therefore follow it."""
    ticket = open_ticket(db_session, cc=list(cc))
    if triaged:
        triage_by_hand(db_session, ticket)
    mail.deliver_mail(db_session)  # the receipt for the new ticket goes first, out of the way
    return ticket


def reply(db_session, ticket, message_id, sender="jo@example.com", to=(MAILBOX,), cc=(), **fields):
    result = ingest_mail(
        db_session,
        incoming(message_id, sender=sender, to=list(to), cc=list(cc), subject=f"Re: [{ticket.reference}] Printer jam", **fields),
    )
    db_session.commit()
    assert result.outcome == REPLY_ADDED
    return result


def an_agent(db_session):
    agent = User(email="agent@example.com", display_name="Agent", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.commit()
    return agent


# ---- Who is told ------------------------------------------------------------------------------------


def test_only_the_followers_who_were_not_copied_are_told_once_each(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1", cc=["pat@example.com"])
    # pat was copied, sam was not, jo wrote it.
    assert who(db_session) == ["sam@example.com"]
    [row] = updates(db_session)
    assert row.ticket_id == ticket.id and db_session.get(Message, row.about_message_id).body == "It is stuck"

    assert mail.deliver_mail(db_session) == (1, 0)
    [row] = updates(db_session)
    assert row.sent_at is not None and row.subject == f"[{ticket.reference}] Printer jam"
    assert "It is stuck" in row.body and ticket.reference in row.body


def test_everyone_copied_means_nobody_is_told(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1", cc=["pat@example.com", "sam@example.com"])
    assert updates(db_session) == []


def test_a_follower_writing_tells_the_requester_unless_the_requester_was_copied(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1", sender="pat@example.com")
    assert who(db_session) == ["jo@example.com", "sam@example.com"]  # not pat, who wrote it
    first = updates(db_session)[0].about_message_id
    reply(db_session, ticket, "r2", sender="sam@example.com", cc=["jo@example.com"])
    second = [r.to_address for r in updates(db_session) if r.about_message_id != first]
    assert second == ["pat@example.com"]  # the requester was copied on this one


def test_the_sender_the_mailbox_and_internal_followers_are_never_told(db_session, dev_mail):
    ticket = world(db_session)
    add_follower(db_session, ticket, "agent@example.com", "jo@example.com", internal_user=an_agent(db_session))
    db_session.commit()
    told = uncopied_recipients(db_session, ticket, "jo@example.com")
    assert told == ["pat@example.com", "sam@example.com"]  # no agent, no mailbox, not the sender
    assert "jo@example.com" not in uncopied_recipients(db_session, ticket, "JO@Example.com")
    assert "pat@example.com" not in uncopied_recipients(db_session, ticket, " Pat@Example.com ")
    assert MAILBOX not in uncopied_recipients(db_session, ticket, "pat@example.com")


@pytest.mark.parametrize(
    "copied", ["PAT@Example.COM", "Pat Smith <pat@example.com>", '"Smith, Pat" <PAT@example.com>', " pat@example.com "]
)
def test_copied_is_compared_ignoring_case_and_display_names(db_session, copied):
    ticket = world(db_session)
    assert uncopied_recipients(db_session, ticket, "jo@example.com", [copied]) == ["sam@example.com"]
    assert bare_address(copied) == "pat@example.com"


def test_a_graph_recipient_with_a_display_name_in_the_address_counts_as_copied():
    raw = graph_message("g1")
    raw["ccRecipients"] = [{"emailAddress": {"name": "Pat", "address": "Pat Smith <PAT@Example.com>"}}]
    raw["toRecipients"].append({"emailAddress": {"address": " Lee@Example.com "}})
    parsed = parse_graph_message(raw)
    assert parsed.cc == ["pat@example.com"] and parsed.to == [MAILBOX, "lee@example.com"]


def test_a_mail_with_no_follower_to_tell_queues_nothing(db_session, dev_mail):
    ticket = open_ticket(db_session)  # no one copied
    triage_by_hand(db_session, ticket)
    reply(db_session, ticket, "r1")
    assert updates(db_session) == []


# ---- Replays, the record and the switch --------------------------------------------------------------


def test_the_same_mail_twice_tells_nobody_twice(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1")
    again = ingest_mail(db_session, incoming("r1", subject=f"Re: [{ticket.reference}] Printer jam"))
    db_session.commit()
    assert again.outcome == DUPLICATE
    assert who(db_session) == ["pat@example.com", "sam@example.com"]
    message = db_session.get(Message, updates(db_session)[0].about_message_id)
    assert queue_follower_updates(db_session, ticket, message, "jo@example.com") == []  # the database refuses a second
    db_session.commit()
    assert len(updates(db_session)) == 2


def test_the_to_and_cc_of_each_mail_are_stored(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1", to=[MAILBOX, "Lee@Example.com"], cc=["pat@example.com"])
    db_session.expire_all()
    row = db_session.exec(select(InboundMail).where(InboundMail.graph_message_id == "r1")).one()
    assert row.to_addresses == f"{MAILBOX}, Lee@Example.com" and row.cc_addresses == "pat@example.com"
    first = db_session.exec(select(InboundMail).where(InboundMail.graph_message_id == "m1")).one()
    assert first.cc_addresses == "pat@example.com, sam@example.com"


def test_nothing_is_queued_with_mail_sending_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "off")
    ticket = world(db_session)
    reply(db_session, ticket, "r1")
    assert updates(db_session) == []


def test_the_mail_that_opens_a_ticket_tells_no_one(db_session, dev_mail):
    world(db_session)
    assert updates(db_session) == []


def test_the_mail_that_makes_a_colleague_a_follower_updates_the_others(db_session, dev_mail):
    ticket = open_ticket(db_session, cc=["pat@example.com"])
    triage_by_hand(db_session, ticket)
    result = ingest_mail(
        db_session,
        incoming("r1", sender="kim@example.com", to=["jo@example.com"], cc=[MAILBOX], conversation_id="conv-m1", subject="Re: Printer jam"),
    )
    db_session.commit()
    assert result.outcome == REPLY_ADDED and "kim@example.com" in [f.email for f in followers_of(db_session, ticket)]
    assert who(db_session) == ["pat@example.com"]  # kim wrote it and jo was on it; pat was not


# ---- Send-time rules ---------------------------------------------------------------------------------


def test_an_internal_note_can_never_be_passed_on(db_session, dev_mail):
    ticket = world(db_session)
    note = add_note(db_session, ticket, an_agent(db_session), "SECRET note")
    db_session.flush()
    db_session.add(OutboundEmail(kind=outbox.FOLLOWER_UPDATE, ticket_id=ticket.id, about_message_id=note.id, to_address="pat@example.com"))
    db_session.commit()
    assert mail.deliver_mail(db_session) == (0, 1)
    [row] = updates(db_session)
    assert row.sent_at is None and "may not see" in row.last_error and "SECRET" not in (row.body or "")


def test_only_what_the_sender_wrote_goes_out_never_a_note_on_the_ticket(db_session, dev_mail):
    ticket = world(db_session)
    add_note(db_session, ticket, an_agent(db_session), "SECRET note")
    db_session.commit()
    reply(db_session, ticket, "r1", body="my words")
    built = []
    mail.deliver_mail(db_session, send=built.append)
    assert len(built) == 2 and all("SECRET" not in m.body and "my words" in m.body and m.cc == [] and m.automatic for m in built)


def test_an_untriaged_ticket_waits_and_goes_when_it_is_triaged(db_session, dev_mail):
    ticket = world(db_session, triaged=False)
    reply(db_session, ticket, "r1")
    assert mail.deliver_mail(db_session) == (0, 0)
    assert all(r.sent_at is None and r.attempts == 0 and "triaged" in r.last_error for r in updates(db_session))
    triage_by_hand(db_session, db_session.get(Ticket, ticket.id))
    for row in updates(db_session):
        row.next_attempt_at = None
        db_session.add(row)
    db_session.commit()
    assert mail.deliver_mail(db_session) == (2, 0)


def test_someone_who_stopped_following_after_queuing_is_not_sent_it(db_session, dev_mail):
    ticket = world(db_session)
    reply(db_session, ticket, "r1")
    sam = [f for f in followers_of(db_session, ticket) if f.email == "sam@example.com"][0]
    remove_follower(db_session, ticket, sam.id, "jo@example.com")
    db_session.commit()
    assert mail.deliver_mail(db_session) == (1, 1)
    sent = {r.to_address: r for r in updates(db_session)}
    assert sent["pat@example.com"].sent_at is not None and "no longer follows" in sent["sam@example.com"].last_error


def test_a_never_follow_address_is_skipped_at_queue_time_and_at_send_time(db_session, dev_mail):
    ticket = world(db_session)
    db_session.add(NeverFollowAddress(email="sam@example.com", added_by="admin@example.com"))  # listed after following
    db_session.commit()
    reply(db_session, ticket, "r1")
    assert who(db_session) == ["pat@example.com"]

    db_session.add(NeverFollowAddress(email="pat@example.com", added_by="admin@example.com"))  # listed after queuing
    db_session.commit()
    assert mail.deliver_mail(db_session) == (0, 1)
    assert "never-follow" in updates(db_session)[0].last_error


def test_a_person_who_turned_follower_updates_off_is_not_sent_them(client, db_session, dev_mail):
    ticket = world(db_session)
    sam_id = login_as(client, "sam@example.com")
    login_as(client, "pat@example.com")
    db_session.add(NotificationPreference(user_id=sam_id, kind="follower_update", channel="email", enabled=False))
    db_session.commit()
    reply(db_session, ticket, "r1")
    assert mail.deliver_mail(db_session) == (1, 1)
    sent = {r.to_address: r for r in updates(db_session)}
    assert sent["pat@example.com"].sent_at is not None  # an account with the default on is told
    assert sent["sam@example.com"].sent_at is None and "turned these updates off" in sent["sam@example.com"].last_error


def test_the_cap_is_counted_per_person_and_holds_not_loses(db_session, dev_mail):
    ticket = world(db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    db_session.add(OutboundEmail(
        kind=outbox.FOLLOWER_UPDATE, ticket_id=ticket.id, to_address="Sam@example.com", sent_at=datetime.now(timezone.utc),
    ))
    db_session.commit()
    reply(db_session, ticket, "r1")
    assert mail.deliver_mail(db_session) == (1, 0)  # pat goes; sam has had his one
    sent = {r.to_address: r for r in updates(db_session) if r.about_message_id}
    assert sent["pat@example.com"].sent_at is not None
    assert sent["sam@example.com"].sent_at is None and sent["sam@example.com"].attempts == 0
    assert sent["sam@example.com"].next_attempt_at is not None


def test_follower_updates_never_use_up_the_requesters_allowance_for_their_own_notices(db_session, dev_mail):
    ticket = world(db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "2")  # the receipt has used one
    db_session.add(OutboundEmail(
        kind=outbox.FOLLOWER_UPDATE, ticket_id=ticket.id, to_address="jo@example.com", sent_at=datetime.now(timezone.utc),
    ))
    db_session.commit()
    notice = OutboundEmail(kind=outbox.NOTICE, ticket_id=ticket.id, to_address="jo@example.com")
    assert mail.cap_release(db_session, notice, datetime.now(timezone.utc)) is None


def test_the_admin_wording_is_used(db_session, dev_mail):
    ticket = world(db_session)
    defn = et.definition("follower_update")
    assert not defn.subject_editable
    with pytest.raises(et.TemplateError):
        et.validate(defn, None, "Nothing to see")  # {message_text} is required
    et.save_template(db_session, "follower_update", None, "{sender} wrote on {reference}: {message_text}", "admin@example.com")
    db_session.commit()
    reply(db_session, ticket, "r1", body="{link} stays as written")
    mail.deliver_mail(db_session)
    assert updates(db_session)[0].body == f"jo@example.com wrote on {ticket.reference}: {{link}} stays as written"


# ---- Portal comments ---------------------------------------------------------------------------------


def test_a_portal_comment_by_the_requester_tells_every_follower(client, db_session, dev_mail):
    ticket = world(db_session)
    login_as(client, "jo@example.com")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Any news?"}).status_code == 200
    assert who(db_session) == ["pat@example.com", "sam@example.com"]
    assert mail.deliver_mail(db_session) == (2, 0)


def test_a_portal_comment_by_a_follower_tells_the_requester_and_the_other_followers(client, db_session, dev_mail):
    ticket = world(db_session)
    login_as(client, "pat@example.com")
    assert client.post(f"/portal/{ticket.number}/comment", data={"body": "Me too"}).status_code == 200
    assert who(db_session) == ["jo@example.com", "sam@example.com"]

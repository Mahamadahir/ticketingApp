"""4B-3: the acknowledgement email. A ticket opened by a person's email gets one content-free receipt,
before triage, counted toward the automatic-email cap and sent as automatic mail; mail that is not from a
person (automatic, our own, a flood, a never-follow address) gets none."""

from datetime import datetime, timezone

import pytest

from app import email_templates as et
from app import mail, outbox
from app.config import settings
from app.inbound import IGNORED_AUTOMATIC, IGNORED_FLOOD, IGNORED_OWN, REPLY_ADDED, TICKET_CREATED, ingest_mail
from app.models import NeverFollowAddress, OutboundEmail
from app.settings_store import AUTOMATIC_EMAIL_CAP, INBOUND_MAX_PER_SENDER_HOUR, set_setting
from tests.test_inbound_mail import MAILBOX, tickets
from tests.test_inbound_mail import mail as incoming
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)


def acks(db_session):
    return rows(db_session, outbox.ACKNOWLEDGEMENT)


def test_an_emailed_ticket_gets_one_receipt_before_triage(db_session, dev_mail):
    assert ingest_mail(db_session, incoming("m1")).outcome == TICKET_CREATED
    db_session.commit()
    [ticket] = tickets(db_session)
    [row] = acks(db_session)
    assert row.ticket_id == ticket.id and row.to_address == "jo@example.com"

    assert mail.deliver_mail(db_session) == (1, 0)  # goes although the ticket is not triaged
    [row] = acks(db_session)
    assert row.sent_at is not None
    assert row.body == f"Your issue has been logged as {ticket.reference} with IT. You will be updated once the ticket is taken."
    assert row.subject == f"[{ticket.reference}] Printer jam"  # the requester's own subject, so a reply finds the ticket


def test_the_receipt_has_no_ticket_content_and_is_automatic_mail(db_session, dev_mail):
    ingest_mail(db_session, incoming("m1", body="My password is hunter2"))
    db_session.commit()
    built = []
    mail.deliver_mail(db_session, send=built.append)
    [sent] = built
    assert sent.automatic and sent.cc == []  # automatic: X-Auto-Response-Suppress goes with it
    assert "hunter2" not in sent.body and "Printer jam" not in sent.body


def test_a_reply_on_a_ticket_and_a_second_attempt_never_add_a_second_receipt(db_session, dev_mail):
    ingest_mail(db_session, incoming("m1"))
    db_session.commit()
    [ticket] = tickets(db_session)
    assert ingest_mail(db_session, incoming("m2", subject=f"Re: [{ticket.reference}] Printer jam")).outcome == REPLY_ADDED
    assert outbox.queue_acknowledgement(db_session, ticket) is None  # once per ticket, whoever asks
    db_session.commit()
    assert len(acks(db_session)) == 1


def test_mail_that_is_not_from_a_person_gets_no_receipt(db_session, dev_mail):
    assert ingest_mail(db_session, incoming("a", headers={"auto-submitted": "auto-replied"})).outcome == IGNORED_AUTOMATIC
    assert ingest_mail(db_session, incoming("b", headers={"x-auto-response-suppress": "All"})).outcome == IGNORED_AUTOMATIC
    assert ingest_mail(db_session, incoming("c", sender=MAILBOX)).outcome == IGNORED_OWN
    set_setting(db_session, INBOUND_MAX_PER_SENDER_HOUR, "1")
    assert ingest_mail(db_session, incoming("d", sender="kim@example.com")).outcome == TICKET_CREATED
    assert ingest_mail(db_session, incoming("e", sender="kim@example.com")).outcome == IGNORED_FLOOD
    db_session.commit()
    assert [r.to_address for r in acks(db_session)] == ["kim@example.com"]  # only the real first mail


def test_a_never_follow_address_is_not_answered(db_session, dev_mail):
    db_session.add(NeverFollowAddress(email="it-all@example.com", added_by="admin@example.com"))
    db_session.commit()
    assert ingest_mail(db_session, incoming("m1", sender="it-all@example.com")).outcome == TICKET_CREATED
    db_session.commit()
    assert len(tickets(db_session)) == 1 and acks(db_session) == []


def test_nothing_is_queued_with_mail_sending_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "off")
    ingest_mail(db_session, incoming("m1"))
    db_session.commit()
    assert acks(db_session) == []


def test_the_receipt_counts_toward_the_cap_and_is_held_not_lost(db_session, dev_mail):
    ingest_mail(db_session, incoming("m1"))
    db_session.commit()
    [ticket] = tickets(db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    db_session.add(OutboundEmail(
        kind=outbox.NOTICE, ticket_id=ticket.id, to_address="jo@example.com", sent_at=datetime.now(timezone.utc),
    ))
    db_session.commit()
    assert mail.deliver_mail(db_session) == (0, 0)
    [row] = acks(db_session)
    assert row.sent_at is None and row.attempts == 0 and row.next_attempt_at is not None


def test_the_admin_wording_is_used_and_cannot_carry_ticket_content(db_session, dev_mail):
    defn = et.definition("acknowledgement")
    assert not defn.subject_editable
    with pytest.raises(et.TemplateError):
        et.validate(defn, None, "Thanks about {subject}")
    et.save_template(db_session, "acknowledgement", None, "Got it: {reference}.", "admin@example.com")
    ingest_mail(db_session, incoming("m1"))
    db_session.commit()
    mail.deliver_mail(db_session)
    [ticket] = tickets(db_session)
    assert acks(db_session)[0].body == f"Got it: {ticket.reference}."


def test_the_receipt_is_never_sent_to_the_shared_mailbox_itself(db_session, dev_mail):
    ingest_mail(db_session, incoming("m1"))
    db_session.commit()
    row = acks(db_session)[0]
    row.to_address = MAILBOX
    db_session.add(row)
    db_session.commit()
    assert mail.deliver_mail(db_session) == (0, 1)
    assert "loop" in acks(db_session)[0].last_error

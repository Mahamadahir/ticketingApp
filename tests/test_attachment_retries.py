"""#348: an email whose attachments cannot be read twice is taken without its files, and the files are retried on their own.

Graph is the fake from test_inbound_mail, with a switch that makes the attachments calls fail (the message fetch keeps working)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import inbound
from app.attachment_retries import attachment_retries_job, retry_attachments
from app.config import settings
from app.inbound import process_notifications
from app.job_runs import SENTENCES
from app.models import Attachment, AttachmentRetry
from app.run_summary import RunSummary
from app.scheduled_jobs import SCHEDULED_JOBS
from app.teams import MAX_ATTEMPTS
from tests.test_inbound_mail import BIG_PNG, MAILBOX, PDF, FakeGraph, events_of, messages_of, row_of, stored, storage, tickets  # noqa: F401


class GraphWithBrokenFiles(FakeGraph):
    def __init__(self):
        super().__init__()
        self.files_broken: Exception | None = None

    def get_attachments(self, message_id):
        if self.files_broken:
            self.listed.append(message_id)
            raise self.files_broken
        return super().get_attachments(message_id)


DOWN = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://graph.example/secret-mailbox@corp.example"), response=httpx.Response(503))
PENDING = "This email's attachments couldn't be read yet. Still trying."


def retries(db_session):
    db_session.expire_all()
    return db_session.exec(select(AttachmentRetry)).all()


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)  # the mail's To line is the mailbox, never a follower


def kinds(db_session, ticket):
    return [e.event_type for e in events_of(db_session, ticket)]


def mail_with_files_down(db_session, storage):
    """A mail with a pdf and a pasted picture whose files are down: two failures, then the third run takes the mail without them.
    Returns (graph, notification id, the time of the run that took the mail)."""
    graph = GraphWithBrokenFiles()
    graph.add("m1", body="Please see the files")
    graph.attach("m1", "report.pdf", PDF)
    graph.attach("m1", "photo.png", BIG_PNG, inline=True)
    graph.files_broken = DOWN
    row_id = stored(db_session, "m1")
    now = datetime.now(timezone.utc)
    assert process_notifications(db_session, graph, storage, now=now) == (0, 1)  # the first failure: the whole mail waits
    assert tickets(db_session) == [] and row_of(db_session, row_id).attachment_failures == 1
    now += timedelta(minutes=2)
    assert process_notifications(db_session, graph, storage, now=now) == (0, 1)  # the second: taken without its files on the next run
    assert tickets(db_session) == [] and row_of(db_session, row_id).attachment_failures == 2
    assert process_notifications(db_session, graph, storage, now=now) == (1, 0)
    return graph, row_id, now


def test_two_failed_reads_take_the_mail_without_files_and_tell_the_agents(db_session, storage):
    graph, row_id, now = mail_with_files_down(db_session, storage)

    [ticket] = tickets(db_session)
    [message] = messages_of(db_session, ticket)
    assert message.body == "Please see the files" and message.graph_message_id == "m1"
    assert db_session.exec(select(Attachment)).all() == []
    [pending] = events_of(db_session, ticket, inbound.EVENT_ATTACHMENTS_PENDING)
    assert (pending.detail, pending.actor_email) == (PENDING, None)
    row = row_of(db_session, row_id)
    assert row.processed_at is not None and row.attempts == 2
    [retry] = retries(db_session)
    assert (retry.graph_message_id, retry.message_id, retry.ticket_id, retry.attempts) == ("m1", message.id, ticket.id, 2)
    assert retry.last_error == "Graph answered 503" and "secret-mailbox" not in retry.last_error
    assert retry.next_attempt_at == now + timedelta(minutes=2)  # the same backoff: 1, 2, 4 ...


def test_a_later_success_stores_the_files_on_the_same_message(db_session, storage):
    graph, _, now = mail_with_files_down(db_session, storage)
    [ticket] = tickets(db_session)
    [message] = messages_of(db_session, ticket)

    assert retry_attachments(db_session, graph, storage, now=now + timedelta(minutes=1)).counts == {"retried": 0, "added": 0, "gave_up": 0}  # not due yet
    graph.files_broken = None
    summary = retry_attachments(db_session, graph, storage, now=now + timedelta(minutes=3))

    assert summary.counts == {"retried": 1, "added": 1, "gave_up": 0} and summary.ticket_ids == [ticket.id]
    db_session.expire_all()
    files = db_session.exec(select(Attachment).order_by(Attachment.id)).all()
    assert [(f.filename, f.is_inline, f.message_id) for f in files] == [("report.pdf", False, message.id), ("photo.png", True, message.id)]
    assert storage.read(files[0].blob_key) == PDF
    [added] = events_of(db_session, ticket, inbound.EVENT_ATTACHMENTS_ADDED)
    assert added.detail == "Attachments added after a retry"
    assert retries(db_session) == [] and len(messages_of(db_session, ticket)) == 1
    again = retry_attachments(db_session, graph, storage, now=now + timedelta(days=1))
    assert again.counts["retried"] == 0 and len(db_session.exec(select(Attachment)).all()) == 2  # nothing left to do


def test_the_files_obey_the_usual_rules_when_added_later(db_session, storage):
    from app.settings_store import INBOUND_MAX_ATTACHMENTS, set_setting

    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "1")
    db_session.commit()
    graph, _, now = mail_with_files_down(db_session, storage)
    graph.attach("m1", "second.pdf", PDF + b"2")
    graph.files_broken = None
    retry_attachments(db_session, graph, storage, now=now + timedelta(minutes=3))
    [ticket] = tickets(db_session)
    assert [f.filename for f in db_session.exec(select(Attachment)).all()] == ["report.pdf"]
    assert sorted(e.to_value for e in events_of(db_session, ticket, "attachment_skipped")) == ["photo.png", "second.pdf"]


def test_eight_failures_in_all_write_the_final_event_and_stop(db_session, storage):
    graph, _, now = mail_with_files_down(db_session, storage)
    [ticket] = tickets(db_session)
    seen = []
    for _ in range(MAX_ATTEMPTS - 2):
        now += timedelta(hours=2)  # past any backoff
        seen.append(retry_attachments(db_session, graph, storage, now=now).counts)

    assert [c["retried"] for c in seen] == [1] * (MAX_ATTEMPTS - 2)  # 2 failures before + 6 retries = 8 tries
    assert [c["gave_up"] for c in seen] == [0] * (MAX_ATTEMPTS - 3) + [1]
    [failed] = events_of(db_session, ticket, inbound.EVENT_ATTACHMENTS_FAILED)
    assert failed.detail == "This email's attachments could not be read"
    assert retries(db_session) == []
    listed = len(graph.listed)
    assert retry_attachments(db_session, graph, storage, now=now + timedelta(days=2)).counts["retried"] == 0
    assert len(graph.listed) == listed and db_session.exec(select(Attachment)).all() == []
    assert inbound.EVENT_ATTACHMENTS_ADDED not in kinds(db_session, ticket)


def test_a_failed_retry_backs_off_with_the_same_steps(db_session, storage):
    graph, _, now = mail_with_files_down(db_session, storage)
    waits = []
    for _ in range(4):
        now += timedelta(hours=2)
        retry_attachments(db_session, graph, storage, now=now)
        [retry] = retries(db_session)
        waits.append(int((retry.next_attempt_at - now).total_seconds() // 60))
    assert waits == [4, 8, 16, 32]  # after tries 3 to 6


def test_one_failed_read_still_retries_the_whole_mail(db_session, storage):
    graph = GraphWithBrokenFiles()
    graph.add("m1")
    graph.attach("m1", "report.pdf", PDF)
    graph.files_broken = DOWN
    row_id = stored(db_session, "m1")
    now = datetime.now(timezone.utc)
    assert process_notifications(db_session, graph, storage, now=now) == (0, 1)
    graph.files_broken = None
    assert process_notifications(db_session, graph, storage, now=now + timedelta(minutes=2)) == (1, 0)
    [ticket] = tickets(db_session)
    assert [f.filename for f in db_session.exec(select(Attachment)).all()] == ["report.pdf"]
    assert retries(db_session) == [] and inbound.EVENT_ATTACHMENTS_PENDING not in kinds(db_session, ticket)
    assert row_of(db_session, row_id).processed_at is not None


def test_a_message_fetch_failure_is_not_an_attachment_failure(db_session, storage):
    graph = GraphWithBrokenFiles()
    graph.add("m1")
    graph.broken = DOWN
    row_id = stored(db_session, "m1")
    now = datetime.now(timezone.utc)
    for round_ in range(3):
        assert process_notifications(db_session, graph, storage, now=now + timedelta(hours=2 * round_)) == (0, 1)
    row = row_of(db_session, row_id)
    assert row.attempts == 3 and row.attachment_failures == 0 and tickets(db_session) == [] and retries(db_session) == []
    assert row.next_attempt_at > now + timedelta(hours=4)  # the ordinary backoff
    graph.broken = None
    assert process_notifications(db_session, graph, storage, now=now + timedelta(hours=7)) == (1, 0)
    assert len(tickets(db_session)) == 1 and retries(db_session) == []


def test_a_download_that_fails_counts_like_a_failed_listing(db_session, storage):
    graph = FakeGraph()
    graph.add("m1")
    graph.attach("m1", "report.pdf", PDF)

    def broken(message_id, attachment_id):
        raise DOWN

    graph.get_attachment_bytes = broken
    stored(db_session, "m1")
    now = datetime.now(timezone.utc)
    process_notifications(db_session, graph, storage, now=now)
    process_notifications(db_session, graph, storage, now=now + timedelta(minutes=2))
    assert process_notifications(db_session, graph, storage, now=now + timedelta(minutes=2)) == (1, 0)
    assert len(retries(db_session)) == 1 and len(tickets(db_session)) == 1


def test_a_ticket_anonymised_meanwhile_drops_the_retry_quietly(db_session, storage):
    graph, _, now = mail_with_files_down(db_session, storage)
    [ticket] = tickets(db_session)
    ticket.requester_email = "anonymised-7@invalid"
    db_session.add(ticket)
    db_session.commit()
    graph.files_broken = None
    before = len(graph.listed)

    summary = retry_attachments(db_session, graph, storage, now=now + timedelta(hours=1))

    assert summary.counts == {"retried": 0, "added": 0, "gave_up": 0}
    assert retries(db_session) == [] and len(graph.listed) == before
    assert db_session.exec(select(Attachment)).all() == [] and kinds(db_session, ticket) == ["created", inbound.EVENT_ATTACHMENTS_PENDING]


def test_a_reply_gets_its_files_on_its_own_message(db_session, storage):
    first = FakeGraph()
    first.add("m1")
    stored(db_session, "m1")
    process_notifications(db_session, first, storage)
    graph = GraphWithBrokenFiles()
    graph.add("m2", subject="Re: Printer jam [INC-%d]" % tickets(db_session)[0].number, body="Here you go")
    graph.attach("m2", "report.pdf", PDF)
    graph.files_broken = DOWN
    stored(db_session, "m2")
    now = datetime.now(timezone.utc)
    process_notifications(db_session, graph, storage, now=now)
    process_notifications(db_session, graph, storage, now=now + timedelta(minutes=2))
    process_notifications(db_session, graph, storage, now=now + timedelta(minutes=2))
    [ticket] = tickets(db_session)
    reply = messages_of(db_session, ticket)[-1]
    assert reply.graph_message_id == "m2" and len(retries(db_session)) == 1
    graph.files_broken = None
    retry_attachments(db_session, graph, storage, now=now + timedelta(hours=1))
    [file] = db_session.exec(select(Attachment)).all()
    assert (file.message_id, file.filename) == (reply.id, "report.pdf")


def test_the_job_is_registered_and_reports_its_counts(db_session, storage):
    [job] = [j for j in SCHEDULED_JOBS if j.name == "inbound-attachments"]
    assert job.run is attachment_retries_job and job.description
    assert set(SENTENCES["inbound-attachments"]) == {"retried", "added", "gave_up"}
    assert attachment_retries_job(db_session) == RunSummary.of(retried=0, added=0, gave_up=0)  # inbound mail is off
    graph, _, now = mail_with_files_down(db_session, storage)
    graph.files_broken = None
    summary = retry_attachments(db_session, graph, storage, now=now + timedelta(minutes=3))
    assert summary.counts == {"retried": 1, "added": 1, "gave_up": 0}


def test_the_new_columns_are_classified():
    from app.person_data import COLUMNS, NOT_PERSON

    for column in ("graph_message_id", "last_error"):
        assert COLUMNS[f"attachment_retry.{column}"].role == NOT_PERSON


def test_taking_the_mail_without_files_still_decides_the_forward_before_the_claim(db_session, storage, monkeypatch):
    real = inbound.decide_forward
    calls = []

    def spy(session, sender, subject, text, *rest):
        calls.append(session.exec(select(inbound.InboundMail)).all() == [])  # True: asked before the mail was claimed
        return real(session, sender, subject, text, *rest)

    monkeypatch.setattr(inbound, "decide_forward", spy)
    graph = GraphWithBrokenFiles()
    graph.add("m1")
    graph.attach("m1", "report.pdf", PDF)
    result = inbound.process_message(db_session, "m1", graph, storage, skip_files=True)
    assert result.files_unread and calls == [True]

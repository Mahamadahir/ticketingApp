import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app.config import settings
from app.models import TeamsPost, TicketSource
from app.teams import MAX_ATTEMPTS, deliver_pending, new_ticket_card, plain, post_card
from app.ticket_service import create_ticket
from tests.test_raising import desk, raise_it
from tests.test_sla import triaged_ticket
from tests.test_splitting import split

URL = "https://example.invalid/workflows/SECRET-TOKEN"


@pytest.fixture
def teams_on(monkeypatch):
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", URL)
    monkeypatch.setattr(settings, "public_base_url", "")


def posts(db_session):
    db_session.expire_all()
    return db_session.exec(select(TeamsPost).order_by(TeamsPost.id)).all()


def new_ticket(db_session, subject="Printer jam", source=TicketSource.EMAIL):
    ticket = create_ticket(db_session, subject=subject, requester_email="jane@example.com", source=source)
    db_session.commit()
    return ticket


class Sender:
    """Stands in for Teams: records what it was sent, and fails on request."""

    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    def __call__(self, payload):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(payload)


# ---- Queueing ---------------------------------------------------------------------------------------


def test_nothing_is_queued_when_announcing_is_off(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", False)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", URL)
    new_ticket(db_session)
    assert posts(db_session) == []


def test_a_process_without_the_address_queues_but_cannot_send(client, db_session, monkeypatch):
    """The web app has the switch and not the URL; the worker has the URL."""
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")
    new_ticket(db_session)
    assert len(posts(db_session)) == 1
    sender = Sender()
    assert deliver_pending(db_session, sender) == (0, 0)
    assert sender.sent == [] and posts(db_session)[0].attempts == 0  # waits for a process that can send


def test_a_new_ticket_queues_one_post_from_every_way_in(client, db_session, teams_on):
    team, category, _ = desk(client, db_session)
    emailed = new_ticket(db_session, "By email")
    raise_it(client, subject="In the portal", for_email="walkin@example.com")  # an agent raising for someone
    queued = posts(db_session)
    assert len(queued) == 2
    assert queued[0].ticket_id == emailed.id and queued[0].kind == "new_ticket"
    assert all(p.sent_at is None and p.attempts == 0 for p in queued)


def test_the_post_is_part_of_the_tickets_transaction(client, db_session, teams_on):
    create_ticket(db_session, subject="Rolled back", requester_email="jane@example.com", source=TicketSource.EMAIL)
    db_session.rollback()
    assert posts(db_session) == []  # a ticket that never existed is never announced


def test_the_parts_of_a_split_are_not_announced(client, db_session, teams_on):
    original, _ = triaged_ticket(client, db_session)
    before = len(posts(db_session))
    assert split(client, original, ("Replace keyboard", ""), ("Install Office", "")).status_code == 200
    assert len(posts(db_session)) == before  # only the original was ever new


# ---- Delivery ---------------------------------------------------------------------------------------


def test_delivery_sends_the_card_and_marks_the_post_sent_once(client, db_session, teams_on):
    ticket = new_ticket(db_session, "Printer jam")
    sender = Sender()

    assert deliver_pending(db_session, sender) == (1, 0)
    db_session.commit()
    [post] = posts(db_session)
    assert post.sent_at is not None and post.attempts == 0 and post.last_error is None
    [payload] = sender.sent
    text = str(payload)
    assert ticket.reference in text and ticket.tag not in text and "Printer jam" in text and "jane@example" in text
    assert payload["type"] == "message"
    assert payload["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    assert deliver_pending(db_session, sender) == (0, 0)  # already sent: never again
    assert len(sender.sent) == 1


def test_the_card_links_to_the_ticket_only_when_the_address_is_known(client, db_session, teams_on, monkeypatch):
    ticket = new_ticket(db_session)
    assert "actions" not in new_ticket_card(ticket)["attachments"][0]["content"]
    monkeypatch.setattr(settings, "public_base_url", "https://support.example.com/")
    [action] = new_ticket_card(ticket)["attachments"][0]["content"]["actions"]
    assert action["url"] == f"https://support.example.com/tickets/{ticket.number}"


def test_a_failed_send_is_kept_with_its_reason_and_does_not_stop_the_rest(client, db_session, teams_on):
    first = new_ticket(db_session, "First")
    new_ticket(db_session, "Second")

    class FailsOnFirst(Sender):
        def __call__(self, payload):
            if "First" in str(payload):
                raise httpx.ConnectError("boom")
            super().__call__(payload)

    sender = FailsOnFirst()
    assert deliver_pending(db_session, sender) == (1, 1)
    db_session.commit()
    failed, sent = posts(db_session)
    assert failed.ticket_id == first.id and failed.sent_at is None
    assert (failed.attempts, failed.last_error) == (1, "ConnectError")
    assert sent.sent_at is not None


def test_the_webhook_address_never_reaches_the_database(client, db_session, teams_on, monkeypatch):
    new_ticket(db_session)

    def refuses(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(401, request=request)

    monkeypatch.setattr(httpx, "post", refuses)
    assert deliver_pending(db_session, post_card) == (0, 1)  # the real sender, with Teams answering 401
    db_session.commit()
    [post] = posts(db_session)
    assert post.last_error == "Teams answered 401"
    assert "SECRET-TOKEN" not in post.last_error


def later(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_a_failed_post_waits_longer_each_time_before_it_is_tried_again(client, db_session, teams_on):
    new_ticket(db_session)
    sender = Sender(fail_with=RuntimeError("down"))
    assert deliver_pending(db_session, sender) == (0, 1)
    db_session.commit()
    assert deliver_pending(db_session, sender) == (0, 0)  # not due yet: a short outage must not burn the attempts
    assert deliver_pending(db_session, sender, now=later(1)) == (0, 0)
    assert deliver_pending(db_session, sender, now=later(3)) == (0, 1)  # due after 2 minutes
    db_session.commit()
    assert deliver_pending(db_session, sender, now=later(3 + 3)) == (0, 0)  # the second wait is 4 minutes
    [post] = posts(db_session)
    assert post.attempts == 2


def test_a_post_that_recovers_is_sent_when_it_is_due(client, db_session, teams_on):
    new_ticket(db_session)
    assert deliver_pending(db_session, Sender(fail_with=httpx.ReadTimeout("slow"))) == (0, 1)
    db_session.commit()
    sender = Sender()
    assert deliver_pending(db_session, sender, now=later(3)) == (1, 0)
    assert len(sender.sent) == 1


def test_a_request_teams_refuses_is_not_retried_but_a_rate_limit_is(client, db_session, teams_on):
    def answering(code):
        def send(payload):
            raise httpx.HTTPStatusError("x", request=httpx.Request("POST", URL), response=httpx.Response(code))
        return send

    new_ticket(db_session, "Refused")
    new_ticket(db_session, "Limited")

    deliver_pending(db_session, answering(404), limit=1)
    db_session.commit()
    deliver_pending(db_session, answering(429), limit=1, now=later(0))
    db_session.commit()
    gone, kept = posts(db_session)
    assert (gone.attempts, gone.last_error) == (MAX_ATTEMPTS, "Teams answered 404")  # given up at once
    assert (kept.attempts, kept.last_error) == (1, "Teams answered 429")  # will be tried again


def test_a_post_is_given_up_on_after_the_attempt_limit(client, db_session, teams_on):
    new_ticket(db_session)
    sender = Sender(fail_with=RuntimeError("down"))
    for n in range(MAX_ATTEMPTS + 2):
        deliver_pending(db_session, sender, now=later(24 * 60 * (n + 1)))  # each run well after the last wait
        db_session.commit()
    [post] = posts(db_session)
    assert post.attempts == MAX_ATTEMPTS and post.sent_at is None and post.last_error == "RuntimeError"

    working = Sender()
    assert deliver_pending(db_session, working, now=later(10 * 24 * 60)) == (0, 0)  # left alone; the reason stays
    assert working.sent == []


def test_delivery_is_oldest_first_and_limited_to_a_batch(client, db_session, teams_on):
    tickets = [new_ticket(db_session, f"Ticket {n}") for n in range(3)]
    sender = Sender()
    assert deliver_pending(db_session, sender, limit=2) == (2, 0)
    db_session.commit()
    assert [p.ticket_id for p in posts(db_session) if p.sent_at] == [tickets[0].id, tickets[1].id]
    assert deliver_pending(db_session, sender, limit=2) == (1, 0)


def test_a_post_whose_ticket_is_missing_is_marked_failed_not_retried(client, db_session, teams_on):
    from app.models import TeamsPost as Post

    db_session.add(Post(kind="new_ticket", ticket_id=None))
    db_session.commit()
    sender = Sender()
    assert deliver_pending(db_session, sender) == (0, 1)
    db_session.commit()
    [post] = posts(db_session)
    assert (post.attempts, post.last_error) == (MAX_ATTEMPTS, "Ticket not found")
    assert sender.sent == []


def test_a_post_of_an_unknown_kind_is_not_sent_as_a_new_ticket(client, db_session, teams_on):
    ticket = new_ticket(db_session)
    db_session.add(TeamsPost(kind="sla_breach", ticket_id=ticket.id))
    db_session.commit()
    sender = Sender()
    assert deliver_pending(db_session, sender) == (1, 1)  # the new-ticket post went; the other did not
    db_session.commit()
    assert len(sender.sent) == 1
    assert posts(db_session)[1].last_error == "Unknown kind"


def test_text_from_a_requester_cannot_make_a_link_or_formatting_in_the_channel(client, db_session, teams_on):
    assert plain("[Reset your password](https://evil.example)") == "\\[Reset your password\\]\\(https://evil\\.example\\)"
    assert plain("**urgent** `x` <b>") == "\\*\\*urgent\\*\\* \\`x\\` \\<b\\>"
    assert plain("line one\nline two   spaced") == "line one line two spaced"
    assert len(plain("a" * 1000)) == 200
    ticket = new_ticket(db_session, "[click](https://evil.example)")
    card = str(new_ticket_card(ticket))
    assert "[click](" not in card and "\\\\[click\\\\]" in card


def test_a_process_that_cannot_send_says_so_when_posts_are_waiting(client, db_session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "teams_new_ticket_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")
    with caplog.at_level(logging.WARNING, logger="app.teams"):
        deliver_pending(db_session, Sender())
    assert "waiting" not in caplog.text  # nothing queued yet: nothing to say
    new_ticket(db_session)
    with caplog.at_level(logging.WARNING, logger="app.teams"):
        deliver_pending(db_session, Sender())
    assert "TEAMS_NEW_TICKET_WEBHOOK_URL is not set" in caplog.text


def test_the_worker_runs_the_teams_job_every_minute_and_it_sends_what_is_waiting(
    client, db_session, teams_on, monkeypatch
):
    from app import teams
    from app.jobs import JOBS

    [job] = [j for j in JOBS if j.name == "teams-posts"]
    assert job.every_seconds == 60
    new_ticket(db_session, "Waiting for the worker")
    sender = Sender()
    monkeypatch.setattr(teams, "post_card", sender)

    job.run(db_session)
    db_session.commit()

    assert len(sender.sent) == 1 and "Waiting for the worker" in str(sender.sent[0])
    assert posts(db_session)[0].sent_at is not None


def test_a_new_kind_of_post_needs_only_a_builder(client, db_session, teams_on, monkeypatch):
    from app import teams

    ticket = new_ticket(db_session)
    db_session.add(TeamsPost(kind="other", ticket_id=ticket.id))
    db_session.commit()
    monkeypatch.setitem(teams.CARD_BUILDERS, "other", lambda session, post: {"hello": post.kind})
    sender = Sender()

    assert deliver_pending(db_session, sender) == (2, 0)
    assert {"hello": "other"} in sender.sent


def test_a_builder_that_cannot_build_gives_up_with_its_reason(client, db_session, teams_on, monkeypatch):
    from app import teams

    ticket = new_ticket(db_session)
    db_session.add(TeamsPost(kind="other", ticket_id=ticket.id))
    db_session.commit()

    def gone(session, post):
        raise teams.Unsendable("Person not found")

    monkeypatch.setitem(teams.CARD_BUILDERS, "other", gone)
    assert deliver_pending(db_session, Sender()) == (1, 1)
    db_session.commit()
    assert (posts(db_session)[1].attempts, posts(db_session)[1].last_error) == (MAX_ATTEMPTS, "Person not found")


def test_config_flag_follows_the_url(monkeypatch):
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")
    assert settings.teams_new_ticket_configured is False
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", URL)
    assert settings.teams_new_ticket_configured is True

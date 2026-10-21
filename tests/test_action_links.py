"""4F: the feedback and reopen links in the resolution email (app/action_links.py, app/routers/action_links.py):
made at send time and never stored, GET never changes anything, POST acts once, expiry, the right kind and
ticket, rating limits, the rate limit and the access log."""

import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import action_links, mail, outbox
from app.config import settings
from app.log_redaction import ApprovalTokenFilter
from app.main import build_app
from app.models import ActionToken, Message, OutboundEmail, Ticket, TicketEvent, TicketFeedback, TicketStatus
from app.settings_store import FEEDBACK_LINK_MINUTES, set_setting
from app.telemetry import EXCLUDED_URLS
from tests.test_email_preview import ticket_for_email
from tests.test_raising import raise_it
from tests.test_resolution import fresh, resolve

BASE = "https://tickets.example.org"
LINK = re.compile(r"https://tickets\.example\.org(/(?:feedback|reopen)/)(\S+)")


@pytest.fixture(autouse=True)
def mail_on(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", BASE)


@pytest.fixture
def public():
    with TestClient(build_app("public")) as public_client:
        yield public_client


def resolved_ticket(client, db_session):
    ticket = ticket_for_email(client, db_session)
    assert resolve(client, ticket).status_code == 200
    return fresh(db_session, ticket)


def sent_email(db_session, ticket):
    db_session.expire_all()
    [row] = db_session.exec(
        select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)
    ).all()
    built = mail.build_message_mail(db_session, row)
    db_session.commit()
    return row, built


def links(db_session, ticket):
    _, built = sent_email(db_session, ticket)
    found = {path.strip("/"): token for path, token in LINK.findall(built.body)}
    assert set(found) == {"feedback", "reopen"}
    return found


def tokens_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(ActionToken).where(ActionToken.ticket_id == ticket.id)).all()


def feedbacks(db_session):
    db_session.expire_all()
    return db_session.exec(select(TicketFeedback)).all()


def expire(db_session, ticket):
    for row in tokens_of(db_session, ticket):
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.add(row)
    db_session.commit()


# ---- The email --------------------------------------------------------------------------------------


def test_the_email_carries_both_links_but_nothing_stores_them(client, db_session):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)

    stored = tokens_of(db_session, ticket)
    assert {t.kind for t in stored} == {"feedback", "reopen"}
    hashes = {t.token_hash for t in stored}
    for token in found.values():
        assert token not in hashes and action_links.hash_token(token) in hashes
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.direction == "outbound")).all()
    assert "[feedback link]" in message.body and "[reopen link]" in message.body
    assert not LINK.search(message.body)


def test_a_sent_row_never_keeps_a_link(client, db_session):
    ticket = resolved_ticket(client, db_session)
    mail.deliver_mail(db_session)
    db_session.expire_all()
    [row] = db_session.exec(
        select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)
    ).all()
    assert row.sent_at is not None and "/feedback/" not in row.body and "/reopen/" not in row.body
    assert "[feedback link]" in row.body
    assert len(tokens_of(db_session, ticket)) == 2  # the links that went out were made at send time


def test_tokens_last_the_setting_and_the_default_is_thirty_days(client, db_session):
    ticket = resolved_ticket(client, db_session)
    set_setting(db_session, FEEDBACK_LINK_MINUTES, "60")
    db_session.commit()
    links(db_session, ticket)
    by_kind = {t.kind: t for t in tokens_of(db_session, ticket)}
    assert by_kind["feedback"].expires_at - by_kind["feedback"].created_at == timedelta(minutes=60)
    assert by_kind["reopen"].expires_at - by_kind["reopen"].created_at == timedelta(days=30)


def test_no_public_address_means_no_link_lines_rather_than_dead_markers(client, db_session, monkeypatch):
    ticket = resolved_ticket(client, db_session)
    monkeypatch.setattr(settings, "public_base_url", "")
    _, built = sent_email(db_session, ticket)
    assert "[feedback link]" not in built.body and "[reopen link]" not in built.body and "/reopen/" not in built.body
    assert not tokens_of(db_session, ticket)


def test_a_ticket_reopened_before_the_email_goes_out_gets_no_links(client, db_session):
    ticket = resolved_ticket(client, db_session)
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()
    _, built = sent_email(db_session, ticket)
    assert "/reopen/" not in built.body and "[reopen link]" not in built.body and not tokens_of(db_session, ticket)


# ---- A GET never changes anything -------------------------------------------------------------------


def test_a_get_shows_a_page_and_changes_nothing(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    before = len(db_session.exec(select(TicketEvent)).all())

    feedback_page = public.get(f"/feedback/{found['feedback']}")
    reopen_page = public.get(f"/reopen/{found['reopen']}")

    assert feedback_page.status_code == 200 and 'name="rating"' in feedback_page.text
    assert reopen_page.status_code == 200 and "Reopen this ticket" in reopen_page.text
    assert "no-store" in reopen_page.headers["cache-control"] and "set-cookie" not in reopen_page.headers
    assert all(t.used_at is None for t in tokens_of(db_session, ticket))
    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.RESOLVED and ticket.reopen_count == 0
    assert not feedbacks(db_session) and len(db_session.exec(select(TicketEvent)).all()) == before


# ---- Feedback ---------------------------------------------------------------------------------------


def test_posting_feedback_records_it_against_the_ticket_and_the_resolving_agent(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)

    response = public.post(f"/feedback/{found['feedback']}", data={"rating": "4", "comment": "  Quick, thanks  "})

    assert response.status_code == 200 and "feedback has been recorded" in response.text
    [feedback] = feedbacks(db_session)
    assert (feedback.ticket_id, feedback.rating, feedback.comment) == (ticket.id, 4, "Quick, thanks")
    assert feedback.resolved_by_id == ticket.resolved_by_id is not None


def test_a_second_post_of_the_same_feedback_link_changes_nothing(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    public.post(f"/feedback/{found['feedback']}", data={"rating": "5"})

    again = public.post(f"/feedback/{found['feedback']}", data={"rating": "1", "comment": "changed my mind"})

    assert "already been used" in again.text
    [feedback] = feedbacks(db_session)
    assert feedback.rating == 5 and feedback.comment == ""


@pytest.mark.parametrize("rating", ["0", "6", "-1", "abc", ""])
def test_a_rating_outside_one_to_five_is_refused_and_the_link_stays_usable(client, db_session, public, rating):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)

    refused = public.post(f"/feedback/{found['feedback']}", data={"rating": rating})

    assert refused.status_code in (200, 422) and not feedbacks(db_session)
    assert all(t.used_at is None for t in tokens_of(db_session, ticket))
    assert public.post(f"/feedback/{found['feedback']}", data={"rating": "3"}).status_code == 200
    assert len(feedbacks(db_session)) == 1


def test_an_over_long_comment_is_refused(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    public.post(f"/feedback/{found['feedback']}", data={"rating": "3", "comment": "x" * 2100})
    assert not feedbacks(db_session)


def test_an_expired_feedback_link_shows_it_and_records_nothing(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    expire(db_session, ticket)

    assert public.get(f"/feedback/{found['feedback']}").status_code == 410
    posted = public.post(f"/feedback/{found['feedback']}", data={"rating": "5"})

    assert "expired" in posted.text and not feedbacks(db_session)


def test_a_link_of_the_other_kind_or_a_made_up_one_is_not_valid(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)

    assert public.get(f"/reopen/{found['feedback']}").status_code == 404
    assert public.post(f"/feedback/{found['reopen']}", data={"rating": "5"}).status_code == 404
    assert public.get("/feedback/not-a-real-token").status_code == 404
    assert public.post("/reopen/not-a-real-token").status_code == 404
    assert not feedbacks(db_session) and fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_a_token_only_ever_acts_on_its_own_ticket(client, db_session, public):
    first = resolved_ticket(client, db_session)
    found = links(db_session, first)
    raise_it(
        client, subject="Second one", for_email="walkin@example.com", category_id=first.category_id,
        impact="low", urgency="low",
    )
    db_session.expire_all()
    second = db_session.exec(select(Ticket).where(Ticket.subject == "Second one")).one()
    assert resolve(client, second).status_code == 200

    public.post(f"/reopen/{found['reopen']}")

    assert fresh(db_session, first).status != TicketStatus.RESOLVED
    assert fresh(db_session, second).status == TicketStatus.RESOLVED


# ---- Reopen -----------------------------------------------------------------------------------------


def test_posting_the_reopen_link_reopens_through_reopen_ticket(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)

    response = public.post(f"/reopen/{found['reopen']}")

    assert response.status_code == 200 and "has been reopened" in response.text
    ticket = fresh(db_session, ticket)
    assert ticket.status != TicketStatus.RESOLVED and ticket.reopen_count == 1
    [event] = db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "reopened")
    ).all()
    assert event.actor_email == ticket.requester_email and "emailed link" in event.detail


def test_a_second_post_of_the_reopen_link_does_nothing(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    public.post(f"/reopen/{found['reopen']}")

    again = public.post(f"/reopen/{found['reopen']}")

    assert "already been used" in again.text and fresh(db_session, ticket).reopen_count == 1


def test_a_closed_ticket_is_not_reopened_the_page_says_to_reply(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    ticket.status = TicketStatus.CLOSED
    db_session.add(ticket)
    db_session.commit()

    page = public.get(f"/reopen/{found['reopen']}")
    posted = public.post(f"/reopen/{found['reopen']}")

    assert "Reply to the email" in page.text and "Reopen this ticket" not in page.text
    assert "Reply to the email" in posted.text
    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.CLOSED and ticket.reopen_count == 0
    assert all(t.used_at is None for t in tokens_of(db_session, ticket))


def test_a_ticket_an_agent_already_reopened_is_not_reopened_again(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()

    posted = public.post(f"/reopen/{found['reopen']}")

    assert "not resolved" in posted.text and fresh(db_session, ticket).reopen_count == 0


def test_an_expired_reopen_link_does_not_reopen(client, db_session, public):
    ticket = resolved_ticket(client, db_session)
    found = links(db_session, ticket)
    expire(db_session, ticket)

    assert public.get(f"/reopen/{found['reopen']}").status_code == 410
    assert "expired" in public.post(f"/reopen/{found['reopen']}").text
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_claiming_is_atomic_a_second_claim_loses(client, db_session):
    ticket = resolved_ticket(client, db_session)
    links(db_session, ticket)
    row = tokens_of(db_session, ticket)[0]
    assert action_links.claim(db_session, row) is True
    assert action_links.claim(db_session, row) is False


# ---- Public surface, rate limit, logs ---------------------------------------------------------------


def test_the_public_app_serves_the_links_and_the_web_app_does_not():
    public_paths = set(build_app("public").openapi()["paths"])
    web_paths = set(build_app("web").openapi()["paths"])
    assert {"/feedback/{token}", "/reopen/{token}"} <= public_paths
    assert not {"/feedback/{token}", "/reopen/{token}"} & web_paths


def test_the_links_are_rate_limited_per_caller(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_action_links_per_minute", 3)

    codes = [client.get("/feedback/guess").status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]
    assert int(client.get("/reopen/guess").headers["retry-after"]) >= 1  # the two paths share a bucket
    assert client.post("/reopen/guess").status_code == 429


@pytest.mark.parametrize("path", ["feedback", "reopen"])
def test_the_token_in_these_paths_is_replaced_in_the_access_log_line(path):
    entry = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5555", "POST", f"/{path}/abc.DEF-ghi_123", "1.1", 200), None,
    )
    ApprovalTokenFilter().filter(entry)
    assert entry.getMessage() == f'10.0.0.1:5555 - "POST /{path}/{{token}} HTTP/1.1" 200'


def test_request_telemetry_skips_the_token_paths():
    assert "/feedback/" in EXCLUDED_URLS and "/reopen/" in EXCLUDED_URLS


def test_other_paths_with_the_same_words_are_logged_as_they_are():
    entry = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '"%s %s"', ("POST", "/portal/5/reopen"), None)
    ApprovalTokenFilter().filter(entry)
    assert "/portal/5/reopen" in entry.getMessage()


def test_a_template_cannot_have_a_pasted_feedback_or_reopen_address(db_session):
    from app.email_templates import TemplateError, definition, validate

    for pasted in ("https://x.example/feedback/abc", "https://x.example/reopen/abc"):
        with pytest.raises(TemplateError):
            validate(definition("resolution"), None, "{resolution_notes} " + pasted)

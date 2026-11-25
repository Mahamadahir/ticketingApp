"""#299: reactions made in Outlook are read from the shared mailbox's Sent Items by a delta job and recorded with the same model
and effects as Teams reactions (app/reactions.py). Graph is a fake (as in test_mail_subscriptions.py): what is tested is our side,
the decoding, the delta link, the matching to a ticket, a kind and a person, the fail-safe, and that the effects are the Teams ones.
Real Outlook, and whether a delta query returns the extended properties, are staging checks. Times are relative to now."""

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import email_reactions, outbox, reactions
from app.auto_close_chase import run_chase
from app.config import settings
from app.email_reactions import FORMAT_WARNING, decode_blob, run_email_reactions
from app.graph_client import REACTIONS_BLOB_ID, REACTIONS_COUNT_ID
from app.messages import mail_subject
from app.models import MailSyncState, OutboundEmail, Ticket, TicketEvent, TicketFeedback, TicketReaction, TicketStatus, User, UserRole
from app.scheduled_jobs import SCHEDULED_JOBS
from app.settings_store import (
    AUTO_CLOSE_AFTER_DAYS, AUTO_CLOSE_ENABLED, CHASE_AFTER_DAYS, CHASE_ENABLED, CHASE_RESOLVE_AFTER_DAYS, EMAIL_REACTIONS_MINUTES, set_setting,
)
from app.subscriptions import health_warnings, sent_folder
from tests.conftest import login_as, set_role
from tests.test_auto_close_chase import resolved as make_resolved, waiting as make_waiting
from tests.test_outbound_mail import dev_mail, rows as outbox_rows  # noqa: F401  (a fixture and a helper)

MAILBOX = "itsupport@example.com"
ANN = "ann@example.com"


def now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def graph_on(monkeypatch):
    monkeypatch.setattr(settings, "graph_tenant_id", "tenant")
    monkeypatch.setattr(settings, "graph_client_id", "client")
    monkeypatch.setattr(settings, "graph_client_secret", "secret")
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)


def backdate(db_session, ticket, days=1):
    """The resolution or the wait began `days` ago, so a reaction a few minutes ago to an email sent hours ago is about it."""
    then = now() - timedelta(days=days)
    for event in db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)).all():
        event.created_at = then
        db_session.add(event)
    ticket.created_at = then - timedelta(days=1)
    if ticket.resolved_at is not None:
        ticket.resolved_at = then
    db_session.add(ticket)
    db_session.commit()
    return ticket


@pytest.fixture
def resolved(db_session):
    return backdate(db_session, make_resolved(db_session, requester=ANN))


@pytest.fixture
def waiting(db_session):
    return backdate(db_session, make_waiting(db_session, requester=ANN))


@pytest.fixture
def ann(db_session):
    person = User(email=ANN, display_name="Ann Able", role=UserRole.END_USER)
    db_session.add(person)
    db_session.commit()
    return person


def stamp(moment):
    """Outlook writes seven fractional digits."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "3293571+00:00"


def blob(*entries, version=1):
    """The MapiReactionsBlob as Outlook stores it: base64 of JSON. An entry is (address, type, name, minutes ago, is_bcc)."""
    defaults = ("", "", "Ann Able", 5, False)
    reactions_list = [
        {"IsBcc": bcc, "ST": 0, "Name": name, "Email": address, "Type": kind, "DateTime": stamp(now() - timedelta(minutes=ago))}
        for address, kind, name, ago, bcc in (tuple(e) + defaults[len(e):] for e in entries)
    ]
    return base64.b64encode(json.dumps({"Version": version, "Reactions": reactions_list}).encode()).decode()


def props(count, blob_value=None):
    found = [{"id": REACTIONS_COUNT_ID.replace("Name", "name"), "value": str(count)}]
    if blob_value is not None:
        found.append({"id": REACTIONS_BLOB_ID.replace("Name", "name"), "value": blob_value})
    return found


def sent_item(ticket, *, reactions_blob=None, count=None, subject=None, to=ANN, sent=None, ref="<mid-1@example.com>", with_props=True, **extra):
    """A Sent Items message as Graph's delta query returns it."""
    sent = sent or now() - timedelta(hours=2)
    item = {
        "id": f"AAMk-{ref}", "subject": subject if subject is not None else f"[{ticket.reference}] Printer", "internetMessageId": ref,
        "sentDateTime": stamp(sent), "lastModifiedDateTime": stamp(sent), "toRecipients": [{"emailAddress": {"address": to}}], "ccRecipients": [],
    }
    if with_props and reactions_blob is not None:
        item["singleValueExtendedProperties"] = props(count if count is not None else 1, reactions_blob)
    item.update(extra)
    return item


class FakeSent:
    """Just enough of Graph: a Sent Items delta query served from queued pages, and a single message read."""

    def __init__(self):
        self.pages = []
        self.requests = []  # (link, since)
        self.single = {}
        self.single_reads = []
        self.fail = None

    def page(self, *items, delta="https://graph.microsoft.com/v1.0/delta?token=one"):
        self.pages.append({"value": list(items), "@odata.deltaLink": delta})

    def sent_items_delta_page(self, link, since=None):
        self.requests.append((link, since))
        if self.fail is not None:
            raise self.fail
        return self.pages.pop(0) if self.pages else {"value": [], "@odata.deltaLink": link or "https://graph.microsoft.com/v1.0/delta?token=none"}

    def get_sent_message_reactions(self, message_id):
        self.single_reads.append(message_id)
        return self.single.get(message_id)


def sent_row(db_session, ticket, kind, *, to=ANN, minutes_ago=120, subject=None):
    """The outbox row of an email that went, as the mail job leaves it."""
    row = OutboundEmail(
        kind="reply", ticket_id=ticket.id, to_address=to, subject=subject or mail_subject(db_session, ticket),
        sent_at=now() - timedelta(minutes=minutes_ago), reaction_kind=kind,
    )
    db_session.add(row)
    db_session.commit()
    return row


def run(db_session, graph):
    counts = run_email_reactions(db_session, graph)
    db_session.commit()
    return counts


def live(db_session):
    db_session.expire_all()
    return db_session.exec(select(TicketReaction).order_by(TicketReaction.id)).all()


def ratings(db_session, ticket):
    db_session.expire_all()
    return [f.rating for f in db_session.exec(select(TicketFeedback).where(TicketFeedback.ticket_id == ticket.id)).all()]


def state(db_session):
    db_session.expire_all()
    return db_session.exec(select(MailSyncState).where(MailSyncState.folder == sent_folder())).first()


# ---- Decoding and recording ----------------------------------------------------------------------------------


def test_a_real_shaped_blob_records_one_like_from_the_right_person_on_the_right_ticket(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like", "Ann Able", 5, False))))

    counts = run(db_session, graph)

    [row] = live(db_session)
    assert (row.ticket_id, row.channel, row.message_kind, row.raw_type, row.user_id) == (resolved.id, "email", "resolution", "like", ann.id)
    assert (row.reactor_ref, row.reactor_address, row.reactor_name, row.sentiment, row.by_party) == (ANN, ANN, "Ann Able", "positive", True)
    assert row.message_ref == "<mid-1@example.com>" and row.removed_at is None
    assert abs((row.reacted_at - (now() - timedelta(minutes=5))).total_seconds()) < 60
    assert counts.recorded == 1 and db_session.get(Ticket, resolved.id).status == TicketStatus.RESOLVED  # a reaction never changes a status


def test_decode_blob_reads_the_documented_example_and_refuses_what_it_does_not_know():
    example = base64.b64encode(
        b'{"Version":1,"Reactions":[{"IsBcc":false,"ST":0,"Name":"Ann Able","Email":"Ann@Example.com","Type":"like","DateTime":"2026-11-24T08:50:46.3293571+00:00"}]}'
    ).decode()

    [reaction] = decode_blob(example)

    assert (reaction.address, reaction.type, reaction.name, reaction.is_bcc) == ("ann@example.com", "like", "Ann Able", False)
    assert reaction.at == datetime(2026, 11, 24, 8, 50, 46, 329357, tzinfo=timezone.utc)
    for bad in ("!!! not base64 !!!", base64.b64encode(b"not json").decode(), blob(version=2), base64.b64encode(b'{"Version":"1","Reactions":[]}').decode(),
                base64.b64encode(b'{"Version":1}').decode(), base64.b64encode(b"[1,2]").decode()):
        with pytest.raises(email_reactions.FormatNotUnderstood):
            decode_blob(bad)


def test_the_same_blob_twice_records_nothing_new_and_the_delta_link_is_stored_and_reused(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    item = sent_item(resolved, reactions_blob=blob((ANN, "like")))
    graph = FakeSent()
    graph.page(item, delta="https://graph.microsoft.com/v1.0/delta?token=first")
    run(db_session, graph)

    assert state(db_session).delta_link == "https://graph.microsoft.com/v1.0/delta?token=first"
    assert graph.requests[0][0] is None and graph.requests[0][1] is not None  # the first run starts from the backfill window

    graph.page(item, delta="https://graph.microsoft.com/v1.0/delta?token=second")  # Graph hands it over again
    counts = run(db_session, graph)

    assert graph.requests[1] == ("https://graph.microsoft.com/v1.0/delta?token=first", None)
    assert counts.recorded == 0 and len(live(db_session)) == 1
    assert state(db_session).delta_link == "https://graph.microsoft.com/v1.0/delta?token=second"


def test_a_reaction_that_has_gone_from_the_blob_is_marked_removed_and_its_rating_goes(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like"))))
    run(db_session, graph)
    assert ratings(db_session, resolved) == [5]

    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "heart"))))  # like taken back, heart added
    counts = run(db_session, graph)

    by_type = {r.raw_type: r for r in live(db_session)}
    assert by_type["like"].removed_at is not None and by_type["heart"].removed_at is None
    assert counts.removed == 1 and counts.recorded == 1

    graph.page(sent_item(resolved, count=0))  # every reaction taken back: no properties come back at all
    run(db_session, graph)

    assert all(r.removed_at is not None for r in live(db_session)) and ratings(db_session, resolved) == []


def test_a_message_with_no_reactions_and_none_recorded_does_nothing(db_session, ann, resolved):
    graph = FakeSent()
    graph.page(sent_item(resolved), sent_item(resolved, reactions_blob=blob(), count=0, ref="<mid-2@example.com>"))

    counts = run(db_session, graph)

    assert live(db_session) == [] and counts.skipped == 0 and not counts.unreadable and state(db_session).last_warning is None


# ---- Matching ----------------------------------------------------------------------------------------------------


def test_a_subject_without_a_reference_an_unknown_recipient_or_an_unknown_reactor_is_skipped_without_an_error(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(
        sent_item(resolved, reactions_blob=blob((ANN, "like")), subject="Printer", ref="<no-ref@example.com>"),  # no reference, no outbox row
        sent_item(resolved, reactions_blob=blob((ANN, "like")), to="stranger@example.com", ref="<elsewhere@example.com>"),  # not the requester
        sent_item(resolved, reactions_blob=blob(("who@example.com", "like")), ref="<unknown-reactor@example.com>"),  # nobody we know reacted
        sent_item(resolved, reactions_blob=blob((ANN, "like")), subject="[INC-999999] Printer", ref="<no-ticket@example.com>"),  # no such ticket
    )

    counts = run(db_session, graph)

    assert live(db_session) == [] and counts.skipped == 4 and counts.recorded == 0
    assert state(db_session).last_warning is None  # none of that is the format being wrong


def test_the_kind_comes_from_the_outbox_row_that_sent_the_email_not_from_the_ticket_now(db_session, ann, waiting):
    sent_row(db_session, waiting, reactions.AWAITING_USER)
    other = sent_row(db_session, waiting, reactions.OTHER, minutes_ago=600)
    graph = FakeSent()
    graph.page(
        sent_item(waiting, reactions_blob=blob((ANN, "like")), ref="<asks-reply@example.com>"),
        sent_item(waiting, reactions_blob=blob((ANN, "like")), ref="<older@example.com>", sent=other.sent_at),
    )

    run(db_session, graph)

    assert {r.message_ref: r.message_kind for r in live(db_session)} == {"<asks-reply@example.com>": "awaiting_user", "<older@example.com>": "other"}


def test_an_email_with_no_outbox_row_is_matched_by_its_reference_and_is_kind_other(db_session, ann, resolved):
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like"))))

    run(db_session, graph)

    [row] = live(db_session)
    assert (row.ticket_id, row.message_kind, row.message_id) == (resolved.id, "other", None)
    assert ratings(db_session, resolved) == []  # not the resolution email, so no rating


def test_a_follower_who_was_copied_counts_as_a_recipient_and_a_party(db_session, ann, resolved):
    from app.models import TicketFollower

    db_session.add(TicketFollower(ticket_id=resolved.id, email="fran@example.com", added_by_email=ANN))
    fran = User(email="fran@example.com", display_name="Fran", role=UserRole.END_USER)
    db_session.add(fran)
    db_session.commit()
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob(("fran@example.com", "like")), ccRecipients=[{"emailAddress": {"address": "fran@example.com"}}]))

    run(db_session, graph)

    [row] = live(db_session)
    assert (row.user_id, row.by_party) == (fran.id, True)


def test_a_linked_address_is_matched_to_the_person_through_identity(db_session, ann, resolved):
    from app.models import LinkedIdentity

    db_session.add(LinkedIdentity(main_user_id=ann.id, email="ann.able@other.org", status="active", method="admin"))
    db_session.commit()
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob(("ann.able@other.org", "like"))))

    run(db_session, graph)

    [row] = live(db_session)
    assert (row.user_id, row.by_party, row.reactor_ref) == (ann.id, True, "ann.able@other.org")


def test_queuing_an_email_fixes_its_reaction_kind(db_session, dev_mail, ann, resolved, waiting):
    from app.messages import record_notice

    for ticket, expected in ((resolved, reactions.RESOLUTION), (waiting, reactions.AWAITING_USER)):
        record_notice(db_session, db_session.get(Ticket, ticket.id), "Hello")
        db_session.commit()
        [row] = [r for r in outbox_rows(db_session, outbox.NOTICE) if r.ticket_id == ticket.id]
        assert row.reaction_kind == expected
    assert outbox.queue_chase(db_session, waiting).reaction_kind == reactions.AWAITING_USER


# ---- The format changes: fail safe -------------------------------------------------------------------------------


BAD = {
    "bad base64": props(1, "!!! not base64 !!!"),
    "not json": props(1, base64.b64encode(b"not json at all").decode()),
    "another version": props(1, blob((ANN, "like"), version=2)),
    "no blob": props(2),
}


@pytest.mark.parametrize("name", list(BAD))
def test_an_unreadable_blob_records_nothing_warns_once_and_mail_intake_says_so(client, db_session, ann, resolved, caplog, name):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    first = {**sent_item(resolved, ref="<a@example.com>"), "singleValueExtendedProperties": BAD[name]}
    second = {**sent_item(resolved, ref="<b@example.com>"), "singleValueExtendedProperties": BAD[name]}
    graph = FakeSent()
    graph.page(first, second)

    with caplog.at_level(logging.WARNING, logger="app.email_reactions"):
        counts = run(db_session, graph)

    assert live(db_session) == [] and counts.recorded == 0
    assert len([r for r in caplog.records if FORMAT_WARNING in r.getMessage()]) == 1  # once per run, not per message
    assert FORMAT_WARNING in state(db_session).last_warning
    assert any("reaction format not understood" in w for w in health_warnings(db_session))
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    assert "Format not understood" in client.get("/admin/mail-intake").text
    assert "reaction format not understood" in client.get("/admin/mail-intake").text


def test_a_message_in_a_bad_format_leaves_standing_reactions_alone_and_a_good_run_clears_the_warning(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like"))))
    run(db_session, graph)

    graph.page({**sent_item(resolved), "singleValueExtendedProperties": props(1, "garbage!!")})
    run(db_session, graph)

    assert [r.removed_at for r in live(db_session)] == [None] and state(db_session).last_warning is not None

    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like"))))
    run(db_session, graph)

    assert state(db_session).last_warning is None


def test_a_run_that_meets_no_reactions_does_not_clear_the_warning(db_session, ann, resolved):
    graph = FakeSent()
    graph.page({**sent_item(resolved), "singleValueExtendedProperties": props(1, "garbage!!")})
    run(db_session, graph)
    graph.page(sent_item(resolved))

    run(db_session, graph)

    assert state(db_session).last_warning is not None


# ---- The effects are the Teams ones ------------------------------------------------------------------------------


def test_a_bcc_reaction_is_recorded_and_has_no_effect(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "like", "Ann Able", 5, True))))

    run(db_session, graph)

    [row] = live(db_session)
    assert (row.raw_type, row.by_party) == ("like", False) and ratings(db_session, resolved) == []


def test_an_unknown_type_is_kept_as_received_and_is_neutral(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "Party-Popper"))))

    run(db_session, graph)

    [row] = live(db_session)
    assert (row.raw_type, row.sentiment) == ("Party-Popper", "neutral") and ratings(db_session, resolved) == []


def test_a_negative_reaction_rates_one_and_shows_the_red_chip(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    graph = FakeSent()
    graph.page(sent_item(resolved, reactions_blob=blob((ANN, "sad"))))

    run(db_session, graph)

    assert ratings(db_session, resolved) == [1] and reactions.open_negative_ids(db_session, [resolved.id]) == {resolved.id}


def test_a_positive_reaction_on_a_request_for_a_reply_stops_the_chase(db_session, ann, waiting, dev_mail):
    for key, value in ((CHASE_ENABLED, "on"), (AUTO_CLOSE_ENABLED, "on"), (CHASE_AFTER_DAYS, "3"), (CHASE_RESOLVE_AFTER_DAYS, "3"), (AUTO_CLOSE_AFTER_DAYS, "7")):
        set_setting(db_session, key, value)
    db_session.commit()
    sent_row(db_session, waiting, reactions.AWAITING_USER)
    graph = FakeSent()
    graph.page(sent_item(waiting, reactions_blob=blob((ANN, "like"))))
    run(db_session, graph)

    run_chase(db_session, now() + timedelta(days=4))
    db_session.commit()

    assert outbox_rows(db_session, outbox.CHASE) == []
    assert db_session.get(Ticket, waiting.id).status == TicketStatus.AWAITING_USER


# ---- Delta mechanics ---------------------------------------------------------------------------------------------


def test_graph_forgetting_the_delta_link_restarts_from_the_backfill_window(db_session, ann, resolved):
    graph = FakeSent()
    graph.page(sent_item(resolved), delta="https://graph.microsoft.com/v1.0/delta?token=old")
    run(db_session, graph)
    gone = httpx.HTTPStatusError("gone", request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x"), response=httpx.Response(410, request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")))

    class Forgets(FakeSent):
        def sent_items_delta_page(self, link, since=None):
            self.requests.append((link, since))
            if link is not None:
                raise gone
            return {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=new"}

    again = Forgets()
    run(db_session, again)

    assert [r[0] is None for r in again.requests] == [False, True] and again.requests[1][1] is not None
    assert state(db_session).delta_link.endswith("token=new")


def test_a_failure_is_recorded_and_raised_so_the_runner_retries(db_session):
    graph = FakeSent()
    graph.fail = httpx.HTTPStatusError(
        "boom https://graph.microsoft.com/v1.0/secret", request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x"),
        response=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}, request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")),
    )

    with pytest.raises(Exception, match="403"):
        run_email_reactions(db_session, graph)

    assert "secret" not in state(db_session).last_error and "403" in state(db_session).last_error
    assert any("last run failed" in w for w in health_warnings(db_session))


def test_a_changed_message_that_came_without_properties_is_read_once_on_its_own(db_session, ann, resolved):
    sent_row(db_session, resolved, reactions.RESOLUTION)
    sent = now() - timedelta(hours=2)
    plain = sent_item(resolved, ref="<plain@example.com>", sent=sent)  # never changed after it was sent: not read again
    changed = sent_item(resolved, ref="<changed@example.com>", sent=sent, lastModifiedDateTime=stamp(sent + timedelta(minutes=30)))
    graph = FakeSent()
    graph.single[changed["id"]] = sent_item(resolved, reactions_blob=blob((ANN, "like")), ref="<changed@example.com>", sent=sent)
    graph.page(plain, changed)

    run(db_session, graph)

    assert graph.single_reads == [changed["id"]]
    assert [r.message_ref for r in live(db_session)] == ["<changed@example.com>"]


def test_without_graph_configured_the_job_does_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_client_secret", "")
    graph = FakeSent()

    run_email_reactions(db_session, graph)

    assert graph.requests == [] and state(db_session) is None


# ---- Registration ------------------------------------------------------------------------------------------------


def test_the_job_is_registered_sends_nothing_and_its_interval_is_a_setting(db_session):
    [job] = [j for j in SCHEDULED_JOBS if j.name == "email-reactions"]

    assert job.description and job.warns == () and job.run is run_email_reactions
    assert job.seconds(db_session) == 600
    set_setting(db_session, EMAIL_REACTIONS_MINUTES, "3")
    db_session.commit()
    assert job.seconds(db_session) == 180
    set_setting(db_session, EMAIL_REACTIONS_MINUTES, "")
    db_session.commit()
    assert job.seconds(db_session) == 600  # blank falls back to the default

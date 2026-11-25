"""#303: an Awaiting user wait acknowledged by a reaction is shown (a blue chip on the ticket, the Service level box and the queue
row, and a Status filter option), and has a backstop: `acknowledged_wait_resolve_days` after the reaction, with no reply, the ordinary
auto-resolve runs. The reaction rows are written directly here (the Teams side is covered by test_reactions.py); app/reactions.py is
the one place that says whether a wait is acknowledged."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app import reactions
from app.auto_close_chase import run_chase
from app.models import OutboundEmail, Ticket, TicketReaction, TicketStatus
from app.settings_store import ACKNOWLEDGED_WAIT_RESOLVE_DAYS, CHASE_AFTER_DAYS, CHASE_ENABLED, CHASE_RESOLVE_AFTER_DAYS, set_setting
from app.status_display import AWAITING_ACKNOWLEDGED, status_options
from app.ticket_service import set_status
from tests.test_outbound_mail import dev_mail  # noqa: F401  (a fixture)
from tests.test_queue_page import triaged
from tests.test_queue_status_sort import desk_agent, subjects

_refs = iter(range(1, 10_000))


def awaiting(db_session, desk, category, subject="Waiting"):
    return triaged(db_session, desk, category, subject, status=TicketStatus.AWAITING_USER)


def react(db_session, ticket, *, at=None, kind="awaiting_user", sentiment="positive", by_party=True, removed=False, sent_at=None):
    at = at or datetime.now(timezone.utc)
    row = TicketReaction(
        ticket_id=ticket.id, channel="teams", message_kind=kind, message_ref=f"m-{next(_refs)}", reactor_ref="oid-1", raw_type="like",
        sentiment=sentiment, by_party=by_party, reacted_at=at, removed_at=at if removed else None, message_sent_at=sent_at,
    )
    db_session.add(row)
    db_session.commit()
    return row


def settings(db_session, days="14"):
    for key, value in ((CHASE_ENABLED, "on"), (CHASE_AFTER_DAYS, "3"), (CHASE_RESOLVE_AFTER_DAYS, "3"), (ACKNOWLEDGED_WAIT_RESOLVE_DAYS, days)):
        set_setting(db_session, key, value)
    db_session.commit()


def status_of(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id).status


def chase_emails(db_session):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == "chase")).all()


# ---- The one decision -----------------------------------------------------------------------------------------


def test_the_helper_says_when_the_current_wait_was_acknowledged(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    ticket = awaiting(db_session, desk, category)
    assert reactions.acknowledged_at(db_session, ticket) is None

    row = react(db_session, ticket)

    assert reactions.acknowledged_at(db_session, ticket) == row.reacted_at


def test_neutral_negative_removed_and_non_party_reactions_do_not_acknowledge(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    ticket = awaiting(db_session, desk, category)
    react(db_session, ticket, sentiment="neutral")
    react(db_session, ticket, sentiment="negative")
    react(db_session, ticket, removed=True)
    react(db_session, ticket, by_party=False)
    react(db_session, ticket, kind="other")

    assert reactions.acknowledged_at(db_session, ticket) is None


def test_only_awaiting_user_is_acknowledged_and_a_new_wait_starts_clean(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    ticket = awaiting(db_session, desk, category)
    react(db_session, ticket)
    assert reactions.acknowledged_at(db_session, ticket) is not None

    set_status(db_session, ticket, TicketStatus.IN_PROGRESS, None)  # an agent (or a reply) ends the wait
    db_session.commit()
    assert reactions.acknowledged_at(db_session, ticket) is None

    set_status(db_session, ticket, TicketStatus.AWAITING_USER, None)  # a new wait: the old reaction is not about it
    db_session.commit()
    assert reactions.acknowledged_at(db_session, ticket) is None


# ---- Shown -------------------------------------------------------------------------------------------------------


def test_an_acknowledged_ticket_shows_the_chip_and_what_happens_next_and_an_unacknowledged_one_does_not(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    quiet = awaiting(db_session, desk, category, "Quiet")
    acked = awaiting(db_session, desk, category, "Acked")
    react(db_session, acked, at=datetime.now(timezone.utc) + timedelta(seconds=1))

    page = client.get(f"/tickets/{acked.number}").text
    assert page.count("Acknowledged by reaction") == 1  # the header chip
    assert re.search(r'<li><span>Acknowledged</span><span class="chip t-blue">By reaction · (today|0 days)</span></li>', page)  # the Service level row
    assert '<p class="note">Chases stopped. Auto-resolves in 14 days' in page
    assert re.search(r'chip t-blue"[^>]*>Acknowledged by reaction · (today|0 days)<', page)
    assert "Chases stopped. Auto-resolves in 14 days if there is no reply." in page
    assert "Acknowledged by reaction" not in client.get(f"/tickets/{quiet.number}").text

    queue = client.get("/tickets").text
    assert queue.count("Acknowledged by reaction") == 1


def test_with_the_backstop_off_the_ticket_says_it_will_not_auto_resolve(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "0")
    ticket = awaiting(db_session, desk, category)
    react(db_session, ticket, at=datetime.now(timezone.utc) + timedelta(seconds=1))

    assert "Chases stopped. Will not auto-resolve." in client.get(f"/tickets/{ticket.number}").text


def test_the_chip_counts_days_since_the_reaction_and_days_left(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    since = datetime.now(timezone.utc) + timedelta(seconds=1)
    react(db_session, ticket, at=since)

    view = reactions.acknowledged_views(db_session, [ticket], since + timedelta(days=5, hours=2))[ticket.id]

    assert view.chip == "Acknowledged by reaction · 5 days"
    assert view.short == "By reaction · 5 days"
    assert view.line == "Chases stopped. Auto-resolves in 9 days if there is no reply."
    assert reactions.acknowledged_views(db_session, [ticket], since)[ticket.id].chip.endswith("today")


def test_the_reaction_taken_back_removes_the_chip(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    ticket = awaiting(db_session, desk, category)
    row = react(db_session, ticket, at=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert "Acknowledged by reaction" in client.get(f"/tickets/{ticket.number}").text

    row.removed_at = datetime.now(timezone.utc)
    db_session.add(row)
    db_session.commit()

    assert "Acknowledged by reaction" not in client.get(f"/tickets/{ticket.number}").text
    assert "Acknowledged by reaction" not in client.get("/tickets").text


def test_a_requester_reply_that_ends_the_wait_clears_the_chip(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    ticket = awaiting(db_session, desk, category)
    react(db_session, ticket, at=datetime.now(timezone.utc) + timedelta(seconds=1))

    set_status(db_session, ticket, TicketStatus.IN_PROGRESS, None, detail="the requester replied")
    db_session.commit()

    assert "Acknowledged by reaction" not in client.get(f"/tickets/{ticket.number}").text


# ---- The filter ----------------------------------------------------------------------------------------------------


def test_the_filter_option_follows_awaiting_user(client, db_session):
    values = [v for v, _l in status_options()]
    assert values[values.index("awaiting_user") + 1] == AWAITING_ACKNOWLEDGED
    assert dict(status_options())[AWAITING_ACKNOWLEDGED] == "Awaiting user · acknowledged"


def test_the_filter_lists_exactly_the_acknowledged_awaiting_user_tickets(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    acked = awaiting(db_session, desk, category, "Acked")
    awaiting(db_session, desk, category, "Quiet")
    other = triaged(db_session, desk, category, "Open one", status=TicketStatus.OPEN)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    react(db_session, acked, at=now)
    react(db_session, other, at=now)  # a reaction on a ticket that is not waiting on the requester

    page = client.get("/tickets", params={"status": AWAITING_ACKNOWLEDGED}).text

    assert subjects(page) == ["Acked"]
    assert "Showing 1 of 1" in page


def test_the_filter_never_widens_what_the_agent_can_see(client, db_session):
    from tests.conftest import create_category, create_team

    desk, category, _ = desk_agent(client, db_session)
    elsewhere = create_team(db_session, "Networks")
    hidden = awaiting(db_session, elsewhere, create_category(db_session, elsewhere, "Wi-Fi"), "Not mine")
    react(db_session, hidden, at=datetime.now(timezone.utc) + timedelta(seconds=1))

    page = client.get("/tickets", params={"status": AWAITING_ACKNOWLEDGED}).text

    assert subjects(page) == []


# ---- The backstop --------------------------------------------------------------------------------------------------


def test_the_backstop_resolves_on_day_14_through_the_normal_path_and_not_on_day_13(client, db_session, dev_mail):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    since = datetime.now(timezone.utc) + timedelta(seconds=1)
    react(db_session, ticket, at=since)

    run_chase(db_session, since + timedelta(days=13, hours=23))
    db_session.commit()
    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER
    assert chase_emails(db_session) == []  # still no chase email: the acknowledgement stands

    run_chase(db_session, since + timedelta(days=14))
    db_session.commit()
    db_session.expire_all()
    resolved = db_session.get(Ticket, ticket.id)
    assert resolved.status == TicketStatus.RESOLVED and resolved.resolved_by_id is None
    assert chase_emails(db_session) == []
    emails = db_session.exec(select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id)).all()
    assert [e.kind for e in emails] == ["notice"]  # the automatic resolution email (as for an unanswered chase), nothing new


def test_the_backstop_is_counted_from_the_reaction_not_from_the_start_of_the_wait(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    reacted = datetime.now(timezone.utc) + timedelta(days=10)
    react(db_session, ticket, at=reacted)

    run_chase(db_session, datetime.now(timezone.utc) + timedelta(days=20))  # 20 days into the wait, 10 since the reaction
    db_session.commit()

    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER


def test_with_the_backstop_at_zero_nothing_is_ever_resolved(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "0")
    ticket = awaiting(db_session, desk, category)
    since = datetime.now(timezone.utc) + timedelta(seconds=1)
    react(db_session, ticket, at=since)

    run_chase(db_session, since + timedelta(days=400))
    db_session.commit()

    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER


def test_the_reaction_taken_back_resumes_the_normal_chasing(client, db_session, dev_mail):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    since = datetime.now(timezone.utc) + timedelta(seconds=1)
    row = react(db_session, ticket, at=since)
    row.removed_at = since
    db_session.add(row)
    db_session.commit()

    run_chase(db_session, since + timedelta(days=4))
    db_session.commit()

    assert len(chase_emails(db_session)) == 1  # chased as usual, and not auto-resolved by the backstop
    assert status_of(db_session, ticket) == TicketStatus.AWAITING_USER


def test_a_reply_before_the_backstop_means_nothing_is_resolved(client, db_session):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    since = datetime.now(timezone.utc) + timedelta(seconds=1)
    react(db_session, ticket, at=since)
    set_status(db_session, ticket, TicketStatus.IN_PROGRESS, None, detail="the requester replied")
    db_session.commit()

    run_chase(db_session, since + timedelta(days=30))
    db_session.commit()

    assert status_of(db_session, ticket) == TicketStatus.IN_PROGRESS


def test_the_backstop_is_a_setting_with_a_placeholder_default(db_session):
    from app.settings_store import REGISTRY

    assert REGISTRY[ACKNOWLEDGED_WAIT_RESOLVE_DAYS].default == "14"
    assert reactions.backstop_days(db_session) == 14


def test_a_chase_still_held_is_given_up_when_the_backstop_resolves_the_ticket(client, db_session, dev_mail):
    desk, category, _ = desk_agent(client, db_session)
    settings(db_session, "14")
    ticket = awaiting(db_session, desk, category)
    start = datetime.now(timezone.utc)
    run_chase(db_session, start + timedelta(days=4))  # chased, and the email is not sent yet
    db_session.commit()
    assert len(chase_emails(db_session)) == 1
    since = start + timedelta(days=5)
    react(db_session, ticket, at=since)

    run_chase(db_session, since + timedelta(days=14))
    db_session.commit()

    assert status_of(db_session, ticket) == TicketStatus.RESOLVED
    [chase] = chase_emails(db_session)
    assert chase.sent_at is None and chase.attempts >= 1 and chase.last_error

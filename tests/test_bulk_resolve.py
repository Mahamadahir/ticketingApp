"""4I: bulk resolve (app/bulk_resolve.py): one note, one preview for the whole batch, the 4D helper per
ticket, only tickets the agent can work, a mixed-result report, one event and one email per ticket."""

import pytest
from sqlmodel import select

from app import bulk_resolve, outbox
from app.config import settings
from app.models import AuditLog, MembershipLevel, OutboundEmail, Ticket, TicketEvent, TicketStatus, TicketType
from app.resolution import ResolveError
from app.settings_store import BULK_RESOLVE_MAX, set_setting
from tests.conftest import add_membership, create_team, login_as
from tests.test_linking import link, triaged
from tests.test_sla import triaged_ticket
from tests.test_splitting import split, subs_of
from tests.test_triage import agent, new_ticket

NOTE = "Restarted the mail relay"


@pytest.fixture(autouse=True)
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def numbers(*tickets):
    return [t.number for t in tickets]


def preview(client, tickets, notes=NOTE):
    return client.post("/bulk-resolve/preview", data={"number": numbers(*tickets), "resolution_notes": notes})


def confirm(client, tickets, notes=NOTE):
    return client.post("/bulk-resolve", data={"number": numbers(*tickets), "resolution_notes": notes})


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def mails(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(
        select(OutboundEmail).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == outbox.REPLY)
    ).all()


def family(client, db_session):
    """A Major Incident with two tickets linked under it, each from a different requester."""
    desk = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[desk])
    major = triaged(db_session, desk, TicketType.MAJOR_INCIDENT, "Email is down")
    first = triaged(db_session, desk, TicketType.INCIDENT, "Cannot send")
    second = triaged(db_session, desk, TicketType.INCIDENT, "Cannot receive")
    for ticket, requester in ((major, "boss@example.com"), (first, "ann@example.com"), (second, "bob@example.com")):
        ticket.requester_email = requester
        db_session.add(ticket)
    db_session.commit()
    assert link(client, first, major.reference).status_code == 200
    assert link(client, second, major.reference).status_code == 200
    return desk, major, first, second


def test_a_parent_and_its_linked_tickets_are_resolved_each_with_its_own_event_audit_and_email(client, db_session):
    _, major, first, second = family(client, db_session)

    page = client.get(f"/bulk-resolve?parent={major.number}")
    assert all(f'value="{t.number}" checked' in page.text for t in (major, first, second))

    response = confirm(client, [major, first, second])

    assert response.status_code == 200 and "3 resolved, 0 skipped" in response.text
    for ticket, requester in ((major, "boss@example.com"), (first, "ann@example.com"), (second, "bob@example.com")):
        ticket = fresh(db_session, ticket)
        assert ticket.status == TicketStatus.RESOLVED and ticket.resolution_notes == NOTE
        assert ticket.resolved_by_id is not None
        [event] = db_session.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "status_changed",
                                      TicketEvent.to_value == "resolved")
        ).all()
        assert event.actor_email == "agent@example.com"
        [mail] = mails(db_session, ticket)
        assert mail.to_address == requester
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "resolved")).all()) == 3


def test_the_preview_lists_every_ticket_and_recipient_and_records_nothing(client, db_session):
    _, major, first, second = family(client, db_session)
    before = (len(db_session.exec(select(TicketEvent)).all()), len(db_session.exec(select(OutboundEmail)).all()))

    response = preview(client, [major, first, second])

    assert response.status_code == 200
    for address in ("boss@example.com", "ann@example.com", "bob@example.com"):
        assert address in response.text
    assert all(t.reference in response.text for t in (major, first, second))
    assert NOTE in response.text and "3 tickets will be resolved" in response.text
    db_session.expire_all()
    assert before == (len(db_session.exec(select(TicketEvent)).all()), len(db_session.exec(select(OutboundEmail)).all()))
    assert fresh(db_session, first).status != TicketStatus.RESOLVED


def test_a_mixed_batch_resolves_what_it_can_and_reports_the_rest_with_reasons(client, db_session):
    desk, _, first, second = family(client, db_session)
    done = triaged(db_session, desk, subject="Already done")
    done.status = TicketStatus.RESOLVED
    db_session.add(done)
    foreign = triaged(db_session, create_team(db_session, "Network"), subject="Not ours")

    page = preview(client, [first, done, foreign]).text
    assert "already resolved or closed" in page and bulk_resolve.NOT_WORKABLE in page and "1 ticket will be resolved" in page

    response = confirm(client, [first, done, foreign, second])

    assert "2 resolved, 2 skipped" in response.text
    assert fresh(db_session, first).status == TicketStatus.RESOLVED
    assert fresh(db_session, second).status == TicketStatus.RESOLVED
    assert fresh(db_session, foreign).status != TicketStatus.RESOLVED and mails(db_session, foreign) == []
    assert mails(db_session, done) == []


def test_a_ticket_the_agent_can_only_read_is_skipped(client, db_session):
    desk = create_team(db_session, "Service Desk")
    other = create_team(db_session, "Network")
    agent_id = agent(client, db_session, member_of=[desk])
    add_membership(db_session, agent_id, other.id, level=MembershipLevel.VIEWER)
    mine = triaged(db_session, desk, subject="Mine")
    readable = triaged(db_session, other, subject="Readable")

    response = confirm(client, [mine, readable])

    assert "1 resolved, 1 skipped" in response.text and bulk_resolve.NOT_WORKABLE in response.text
    assert fresh(db_session, readable).status == TicketStatus.OPEN


def test_untriaged_and_awaiting_approval_tickets_are_skipped_with_the_reason(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[desk])
    untriaged = new_ticket(db_session, desk)
    waiting = triaged(db_session, desk, subject="Needs approval")
    waiting.status = TicketStatus.AWAITING_APPROVAL
    db_session.add(waiting)
    db_session.commit()

    text = confirm(client, [untriaged, waiting]).text

    assert "0 resolved, 2 skipped" in text
    assert "Triage the ticket before resolving it" in text and "waiting for its approvers" in text


def test_a_failure_part_way_through_one_ticket_leaves_nothing_of_it_and_the_rest_go_ahead(client, db_session, monkeypatch):
    _, major, first, second = family(client, db_session)
    real = bulk_resolve.resolve_ticket

    def flaky(session, ticket, *args, **kwargs):
        real(session, ticket, *args, **kwargs)  # everything written, then refused
        if ticket.id == first.id:
            raise ResolveError("Something went wrong half way")

    monkeypatch.setattr(bulk_resolve, "resolve_ticket", flaky)

    text = confirm(client, [major, first, second]).text

    assert "2 resolved, 1 skipped" in text and "Something went wrong half way" in text
    assert fresh(db_session, first).status != TicketStatus.RESOLVED and fresh(db_session, first).resolution_notes is None
    assert mails(db_session, first) == []
    assert db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == first.id,
                                                      TicketEvent.event_type == "status_changed")).all() == []
    assert fresh(db_session, major).status == TicketStatus.RESOLVED and len(mails(db_session, second)) == 1


def test_the_batch_is_refused_without_a_note_without_tickets_or_over_the_limit(client, db_session):
    _, major, first, second = family(client, db_session)

    assert preview(client, [first], notes="   ").status_code == 400
    assert confirm(client, [first], notes="").status_code == 400
    assert preview(client, []).status_code == 400
    set_setting(db_session, BULK_RESOLVE_MAX, "2")
    db_session.commit()
    too_many = confirm(client, [major, first, second])
    assert too_many.status_code == 400
    assert fresh(db_session, first).status != TicketStatus.RESOLVED


def test_only_agents_can_use_it(client, db_session):
    login_as(client, "requester@example.com")

    assert client.get("/bulk-resolve").status_code == 403
    assert client.post("/bulk-resolve", data={"number": [1], "resolution_notes": NOTE}).status_code == 403


def test_a_split_originals_parts_come_before_it_and_a_repeated_number_is_resolved_once(client, db_session):
    original, _ = triaged_ticket(client, db_session)
    assert split(client, original, ("Part one", "a"), ("Part two", "b")).status_code == 200
    one, two = subs_of(db_session, original)
    original = fresh(db_session, original)

    assert [t.id for t in sorted([original, two, one], key=bulk_resolve._lock_order)] == [one.id, two.id, original.id]

    response = client.post(
        "/bulk-resolve", data={"number": [original.number, two.number, one.number, one.number], "resolution_notes": NOTE}
    )

    assert response.status_code == 200
    assert fresh(db_session, one).status == TicketStatus.RESOLVED and fresh(db_session, two).status == TicketStatus.RESOLVED
    assert len(mails(db_session, one)) == 1
    assert fresh(db_session, original).status == TicketStatus.RESOLVED


def test_the_list_and_the_parent_page_offer_it(client, db_session):
    _, major, first, _ = family(client, db_session)

    assert 'name="number"' in client.get("/tickets").text
    assert f"/bulk-resolve?parent={major.number}" in client.get(f"/tickets/{major.number}").text
    assert "/bulk-resolve?parent=" not in client.get(f"/tickets/{first.number}").text


def test_the_preview_judges_a_split_original_after_its_parts_and_changes_nothing(client, db_session):
    original, _ = triaged_ticket(client, db_session)
    assert split(client, original, ("Part one", "a"), ("Part two", "b")).status_code == 200
    one, two = subs_of(db_session, original)
    original = fresh(db_session, original)

    text = preview(client, [original, one, two]).text

    assert "3 tickets will be resolved" in text and "will be skipped" not in text
    assert all(fresh(db_session, t).status != TicketStatus.RESOLVED for t in (original, one, two))
    assert db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == outbox.REPLY)).all() == []
    # The numbers the preview's confirm form posts resolve the lot.
    assert all(f'name="number" value="{t.number}"' in text for t in (original, one, two))
    assert "3 resolved, 0 skipped" in confirm(client, [original, one, two]).text
    assert fresh(db_session, original).status == TicketStatus.RESOLVED


def test_the_choose_page_lists_at_most_the_limit(client, db_session):
    _, major, first, second = family(client, db_session)
    set_setting(db_session, BULK_RESOLVE_MAX, "2")
    db_session.commit()

    page = client.get(f"/bulk-resolve?parent={major.number}").text

    assert "1 more are not listed" in page and page.count('name="number"') == 2

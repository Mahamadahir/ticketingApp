"""4E-1: reopening a resolved ticket, reclosing it with a reason, and replying after the ticket is finished
(app/reopening.py): a reply on a Resolved ticket reopens it, a reply on a Closed one opens a new ticket
linked to it (shown on both pages), the assignee is told, and a split family stays consistent."""

import pytest
from sqlmodel import select

from app import mail
from app.inbound import REPLY_ADDED, TICKET_CREATED, ingest_mail
from app.models import AuditLog, Message, RelatedTicket, Ticket, TicketEvent, TicketSource, TicketStatus, User, UserRole
from app.reopening import RECLOSE_REASONS, latest_follow_on, open_follow_on_ticket
from app.resolution import resolve_ticket
from tests.conftest import create_team, login_as, set_role
from tests.test_inbound_mail import MAILBOX, mail as incoming
from tests.test_notifications import agent_in_desk, back_to, notifications_for
from tests.test_reply_notification import all_notifications, assigned_ticket, comment_as
from tests.test_splitting import split, subs_of, world


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def finish(db_session, ticket, status=TicketStatus.RESOLVED):
    """Resolved (through the real helper, no email) and optionally closed."""
    resolve_ticket(db_session, ticket, None, "Replaced the roller", send_email=False)
    if status == TicketStatus.CLOSED:
        from app.ticket_service import set_status

        set_status(db_session, ticket, TicketStatus.CLOSED, "system")
    db_session.commit()
    return fresh(db_session, ticket)


def events(db_session, ticket, kind):
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)).all()


def reopen(client, ticket, reason="Still broken"):
    return client.post(f"/tickets/{ticket.number}/reopen", data={"reason": reason})


# ---- The agent's Reopen ---------------------------------------------------------------------------


def test_reopening_goes_back_to_in_progress_counts_it_records_the_reason_and_tells_the_assignee(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    assert ticket.status == TicketStatus.RESOLVED and ticket.resolution_notes
    agent_in_desk(client, db_session, "bob@example.com", "Bob")

    assert reopen(client, ticket).status_code == 200

    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.IN_PROGRESS, 1)
    assert ticket.resolution_notes is None and ticket.resolved_at is None
    [event] = events(db_session, ticket, "reopened")
    assert (event.actor_email, event.detail) == ("bob@example.com", "Still broken")
    assert [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.action == "reopened")).all()] == ["reopened"]
    [told] = notifications_for(db_session, "agent@example.com")
    assert (told.kind, told.ticket_id, told.actor_email) == ("reopened", ticket.id, "bob@example.com")
    assert "Still broken" in told.text


def test_the_agent_who_reopens_is_not_told_about_it_themselves(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)

    assert reopen(client, ticket).status_code == 200

    assert notifications_for(db_session, "agent@example.com") == []


def test_an_unassigned_ticket_goes_back_to_open(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket.assigned_agent_id = None
    db_session.add(ticket)
    db_session.commit()
    ticket = finish(db_session, ticket)

    reopen(client, ticket)

    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_reopening_needs_a_reason_and_a_resolved_ticket(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)

    assert reopen(client, ticket).status_code == 400  # still open
    ticket = finish(db_session, ticket)
    assert reopen(client, ticket, reason="  ").status_code == 400
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
    assert fresh(db_session, ticket).reopen_count == 0


def test_a_closed_ticket_cannot_be_reopened_by_hand(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)

    response = reopen(client, ticket)

    assert response.status_code == 400 and "new ticket" in response.text
    assert fresh(db_session, ticket).status == TicketStatus.CLOSED


def test_a_second_reopen_is_refused_and_counts_once(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)

    assert reopen(client, ticket).status_code == 200
    assert reopen(client, ticket).status_code == 400

    assert fresh(db_session, ticket).reopen_count == 1


def test_the_status_buttons_reopen_a_resolved_ticket_the_same_way(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)

    assert client.post(f"/tickets/{ticket.number}/status", data={"status": "with_supplier"}).status_code == 200

    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.WITH_SUPPLIER, 1)
    assert len(events(db_session, ticket, "reopened")) == 1


def test_the_page_offers_reopen_only_on_a_resolved_ticket(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    assert "/reopen" not in client.get(f"/tickets/{ticket.number}").text
    ticket = finish(db_session, ticket)
    assert f"/tickets/{ticket.number}/reopen" in client.get(f"/tickets/{ticket.number}").text


# ---- A reply on a Resolved ticket -----------------------------------------------------------------


def test_a_portal_reply_on_a_resolved_ticket_reopens_it_and_the_assignee_is_told_once(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)

    comment_as(client, "jo@example.com", ticket, "It broke again")

    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.IN_PROGRESS, 1)
    [told] = notifications_for(db_session, "agent@example.com")
    assert told.kind == "replied"  # the reply already told them: no second notification for the reopening
    [event] = events(db_session, ticket, "reopened")
    assert event.detail == "The requester replied"
    audit = [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()]
    assert audit == ["resolved -> in_progress"]


def test_a_mail_reply_on_a_resolved_ticket_reopens_it(db_session):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)

    result = ingest_mail(
        db_session, incoming("r1", subject=f"Re: [{ticket.reference}] Printer jam", body="Still not working")
    )
    db_session.commit()

    assert result.outcome == REPLY_ADDED
    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.OPEN, 1)  # nobody assigned


# ---- A reply on a Closed ticket -------------------------------------------------------------------


def test_a_portal_reply_on_a_closed_ticket_opens_a_new_portal_ticket_linked_to_it(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)
    before = len(db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all())

    comment_as(client, "jo@example.com", ticket, "Same problem again")

    old = fresh(db_session, ticket)
    new = db_session.exec(select(Ticket).where(Ticket.id != ticket.id)).one()
    assert old.status == TicketStatus.CLOSED and old.reopen_count == 0
    assert len(db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()) == before
    assert (new.source, new.requester_email, new.subject, new.status) == (
        TicketSource.PORTAL, "jo@example.com", "Printer jam", TicketStatus.NEW,
    )
    [message] = db_session.exec(select(Message).where(Message.ticket_id == new.id)).all()
    assert message.body == "Same problem again"
    [link] = db_session.exec(select(RelatedTicket)).all()
    assert (link.ticket_id, link.related_ticket_id) == (new.id, old.id)
    assert len(events(db_session, new, "follows")) == 1 and len(events(db_session, old, "followed_by")) == 1
    [told] = notifications_for(db_session, "agent@example.com")  # the old ticket's assignee hears of the reply
    assert told.ticket_id == old.id


def test_both_ticket_pages_show_the_link(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)
    comment_as(client, "jo@example.com", ticket, "Same problem again")
    new = db_session.exec(select(Ticket).where(Ticket.id != ticket.id)).one()

    # The requester's portal pages.
    assert f"/portal/{ticket.number}" in client.get(f"/portal/{new.number}").text
    assert f"/portal/{new.number}" in client.get(f"/portal/{ticket.number}").text
    assert "opens a new request linked to it" in client.get(f"/portal/{ticket.number}").text
    # The agent's pages.
    back_to(client)
    on_new = client.get(f"/tickets/{new.number}").text
    on_old = client.get(f"/tickets/{ticket.number}").text
    assert "Related tickets" in on_new and f"/tickets/{ticket.number}" in on_new and "follows on from it" in on_new
    assert "Related tickets" in on_old and f"/tickets/{new.number}" in on_old and "opened after this one was closed" in on_old


def test_an_agent_who_cannot_open_the_other_ticket_does_not_see_the_link(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)
    comment_as(client, "jo@example.com", ticket, "Same problem again")
    new = db_session.exec(select(Ticket).where(Ticket.id != ticket.id)).one()
    # The new ticket lands in another team, which the second agent is the only member of.
    other = create_team(db_session, "Network")
    new.team_id = other.id
    db_session.add(new)
    db_session.commit()
    client.post("/auth/logout")
    outsider = login_as(client, "net@example.com", "Net Nick")
    set_role(db_session, outsider, UserRole.AGENT, other.id)

    page = client.get(f"/tickets/{new.number}").text

    assert f"/tickets/{ticket.number}" not in page  # the earlier ticket is the desk's, not theirs


def test_a_mail_reply_on_a_closed_ticket_opens_a_new_email_ticket_and_the_thread_then_follows_it(db_session):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    old = open_ticket(db_session)
    triage_by_hand(db_session, old)
    old = finish(db_session, old, TicketStatus.CLOSED)

    first = ingest_mail(db_session, incoming("r1", subject=f"Re: [{old.reference}] Printer jam", body="Back again", conversation_id="conv-m1"))
    db_session.commit()

    assert first.outcome == TICKET_CREATED
    new = first.ticket
    assert (new.source, new.requester_email) == (TicketSource.EMAIL, "jo@example.com")
    assert new.id != old.id and fresh(db_session, old).status == TicketStatus.CLOSED
    [link] = db_session.exec(select(RelatedTicket)).all()
    assert (link.ticket_id, link.related_ticket_id) == (new.id, old.id)
    assert len(db_session.exec(select(Message).where(Message.ticket_id == old.id)).all()) == 1  # only its own first mail

    # Another mail in the same Graph conversation goes to the new ticket, not to a third one.
    second = ingest_mail(db_session, incoming("r2", subject="Re: Printer jam", body="Any news?", conversation_id="conv-m1"))
    db_session.commit()
    assert second.outcome == REPLY_ADDED and second.ticket.id == new.id
    assert len(db_session.exec(select(Ticket)).all()) == 2


def test_the_chain_is_followed_to_the_newest_open_ticket(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    first = finish(db_session, ticket, TicketStatus.CLOSED)
    second = open_follow_on_ticket(
        db_session, first, subject="Again", sender_email="jo@example.com", source=TicketSource.PORTAL, actor_email=None
    )
    db_session.commit()
    assert latest_follow_on(db_session, first).id == second.id
    assert latest_follow_on(db_session, second).id == second.id  # open: stays
    second.status = TicketStatus.CLOSED
    third = open_follow_on_ticket(
        db_session, second, subject="Again", sender_email="jo@example.com", source=TicketSource.PORTAL, actor_email=None
    )
    db_session.commit()
    assert latest_follow_on(db_session, first).id == third.id


def test_an_agent_sourced_follow_on_is_refused(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    closed = finish(db_session, ticket, TicketStatus.CLOSED)
    with pytest.raises(ValueError):
        open_follow_on_ticket(
            db_session, closed, subject="x", sender_email="jo@example.com", source=TicketSource.AGENT, actor_email=None
        )


def test_a_stranger_with_only_the_subject_reference_still_gets_an_unlinked_ticket_of_their_own(db_session):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    old = open_ticket(db_session)
    triage_by_hand(db_session, old)
    old = finish(db_session, old, TicketStatus.CLOSED)

    result = ingest_mail(db_session, incoming("x1", sender="eve@example.com", subject=f"[{old.reference}] hi"))
    db_session.commit()

    assert result.outcome == TICKET_CREATED
    assert db_session.exec(select(RelatedTicket)).all() == []


# ---- The requester's Reopen button ----------------------------------------------------------------


def test_the_requester_reopens_their_resolved_ticket_from_my_requests(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert f"/portal/{ticket.number}/reopen" in client.get("/portal?tab=finished").text  # Resolved sits in the Finished tab
    assert f"/portal/{ticket.number}/reopen" in client.get(f"/portal/{ticket.number}").text

    response = client.post(f"/portal/{ticket.number}/reopen")

    assert response.status_code == 200  # followed the redirect to the ticket
    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.IN_PROGRESS, 1)
    [told] = notifications_for(db_session, "agent@example.com")
    assert told.kind == "reopened" and told.actor_email == "jo@example.com"
    for tab in ("open", "finished"):  # no longer resolved, so no Reopen on either tab
        assert f"/portal/{ticket.number}/reopen" not in client.get(f"/portal?tab={tab}").text


def test_a_follower_or_a_stranger_cannot_reopen_it_from_the_portal(client, db_session):
    from app.followers import add_follower

    ticket, team, ann_id = assigned_ticket(client, db_session)
    add_follower(db_session, ticket, "pat@example.com", "jo@example.com")
    ticket = finish(db_session, ticket)
    for email in ("pat@example.com", "eve@example.com"):
        client.post("/auth/logout")
        login_as(client, email)
        assert client.post(f"/portal/{ticket.number}/reopen").status_code == 404

    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


# ---- Reclose --------------------------------------------------------------------------------------


def reclose(client, ticket, reason="duplicate"):
    return client.post(f"/tickets/{ticket.number}/reclose", data={"reason": reason})


@pytest.mark.parametrize("key", list(RECLOSE_REASONS))
def test_a_reopened_ticket_is_reclosed_with_a_reason(client, db_session, key):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    reopen(client, ticket)
    assert "Reclose" in client.get(f"/tickets/{ticket.number}").text

    assert reclose(client, ticket, key).status_code == 200

    ticket = fresh(db_session, ticket)
    assert ticket.status == TicketStatus.CLOSED and ticket.closed_at is not None
    [event] = events(db_session, ticket, "reclosed")
    assert (event.to_value, event.detail, event.actor_email) == (key, RECLOSE_REASONS[key], "agent@example.com")
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.action == "reclosed")).all()] == [RECLOSE_REASONS[key]]


def test_reclose_needs_a_reopened_ticket_and_a_listed_reason(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    assert reclose(client, ticket).status_code == 400  # never reopened
    assert "Reclose" not in client.get(f"/tickets/{ticket.number}").text
    ticket = finish(db_session, ticket)
    reopen(client, ticket)

    assert reclose(client, ticket, "because").status_code == 400
    assert reclose(client, ticket, "").status_code == 400
    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS
    assert reclose(client, ticket, "duplicate").status_code == 200
    assert reclose(client, ticket, "duplicate").status_code == 400  # already closed


# ---- Split families -------------------------------------------------------------------------------


def resolved_family(client, db_session):
    original, desk = world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Install Office", ""))
    parts = subs_of(db_session, original)
    for part in parts:
        resolve_ticket(db_session, part, None, "Done", send_email=False)
    db_session.commit()
    original = fresh(db_session, original)
    resolve_ticket(db_session, original, None, "All done", send_email=False)
    db_session.commit()
    return fresh(db_session, original), [fresh(db_session, p) for p in parts]


def test_reopening_a_resolved_original_reopens_its_resolved_parts(client, db_session):
    original, parts = resolved_family(client, db_session)
    assert original.status == TicketStatus.RESOLVED

    assert reopen(client, original).status_code == 200

    original = fresh(db_session, original)
    assert original.status != TicketStatus.RESOLVED and original.reopen_count == 1
    for part in parts:
        part = fresh(db_session, part)
        assert part.status != TicketStatus.RESOLVED and part.reopen_count == 1


def test_reopening_a_resolved_part_reopens_a_resolved_original_too(client, db_session):
    original, parts = resolved_family(client, db_session)
    first, second = parts

    assert reopen(client, first).status_code == 200

    assert fresh(db_session, first).status != TicketStatus.RESOLVED
    assert fresh(db_session, second).status == TicketStatus.RESOLVED  # the other part is untouched
    original = fresh(db_session, original)
    assert original.status == fresh(db_session, first).status and original.reopen_count == 1


def test_a_reply_on_a_resolved_original_reopens_the_family(client, db_session):
    original, parts = resolved_family(client, db_session)
    client.post("/auth/logout")
    login_as(client, original.requester_email)

    assert client.post(f"/portal/{original.number}/comment", data={"body": "Not fixed"}).status_code == 200

    assert fresh(db_session, original).status != TicketStatus.RESOLVED
    assert all(fresh(db_session, p).status != TicketStatus.RESOLVED for p in parts)


# ---- Found in code review -------------------------------------------------------------------------


def test_a_second_portal_reply_on_the_closed_ticket_joins_the_request_the_first_one_opened(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)

    comment_as(client, "jo@example.com", ticket, "Same problem again")
    comment_as(client, "jo@example.com", ticket, "And again")

    new = db_session.exec(select(Ticket).where(Ticket.id != ticket.id)).one()  # one new ticket, not two
    bodies = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == new.id).order_by(Message.id)).all()]
    assert bodies == ["Same problem again", "And again"]
    assert len(db_session.exec(select(RelatedTicket)).all()) == 1


def test_a_followers_reply_after_closure_follows_the_new_ticket_and_the_requester_stays_the_requester(client, db_session):
    from app.followers import add_follower, followers_of

    ticket, team, ann_id = assigned_ticket(client, db_session)
    add_follower(db_session, ticket, "pat@example.com", "jo@example.com")
    ticket = finish(db_session, ticket, TicketStatus.CLOSED)

    comment_as(client, "pat@example.com", ticket, "It is still broken for me too")

    new = db_session.exec(select(Ticket).where(Ticket.id != ticket.id)).one()
    assert new.requester_email == "jo@example.com"
    assert [f.email for f in followers_of(db_session, new)] == ["pat@example.com"]


def test_mail_from_someone_not_on_the_newest_ticket_gets_their_own_linked_ticket(db_session):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    old = open_ticket(db_session, cc=["pat@example.com"])
    triage_by_hand(db_session, old)
    old = finish(db_session, old, TicketStatus.CLOSED)
    pats = ingest_mail(db_session, incoming("p1", sender="pat@example.com", subject="Re: Printer jam", conversation_id="conv-m1"))
    db_session.commit()
    assert pats.outcome == TICKET_CREATED and pats.ticket.requester_email == "jo@example.com"  # jo's conversation

    # Eve is not on that ticket (nor a follower): a strong thread match opens her own, linked to the old one.
    eves = ingest_mail(db_session, incoming("e1", sender="eve@example.com", subject="Re: Printer jam", conversation_id="conv-m1"))
    db_session.commit()
    assert eves.outcome == TICKET_CREATED and eves.ticket.id != pats.ticket.id
    link = db_session.exec(select(RelatedTicket).where(RelatedTicket.ticket_id == eves.ticket.id)).one()
    assert link.related_ticket_id == old.id


def test_a_reply_that_reopens_a_family_tells_the_assignees_of_the_other_tickets(client, db_session):
    original, parts = resolved_family(client, db_session)
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    for member in [original, *parts]:
        member = db_session.get(Ticket, member.id)
        member.assigned_agent_id = ann.id
        db_session.add(member)
    db_session.commit()
    original = fresh(db_session, original)
    client.post("/auth/logout")
    login_as(client, original.requester_email)
    assert client.post(f"/portal/{original.number}/comment", data={"body": "Not fixed"}).status_code == 200

    kinds = sorted(n.kind for n in notifications_for(db_session, "agent@example.com"))
    assert kinds.count("reopened") == len(parts)  # every part, but not the original replied on (told of the reply)
    assert "replied" in kinds


def test_the_status_buttons_do_not_reopen_a_resolved_original(client, db_session):
    original, parts = resolved_family(client, db_session)

    response = client.post(f"/tickets/{original.number}/status", data={"status": "with_supplier"})

    assert response.status_code == 400 and "follows them" in response.text
    assert fresh(db_session, original).status == TicketStatus.RESOLVED


def test_reopen_from_a_stale_portal_page_shows_the_ticket_instead_of_an_error(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)  # open, not resolved
    client.post("/auth/logout")
    login_as(client, "jo@example.com")

    response = client.post(f"/portal/{ticket.number}/reopen")

    assert response.status_code == 200 and fresh(db_session, ticket).reopen_count == 0

"""#259: splitting before triage (untriaged parts, the original as their parent), the pre-filled Split card, and the
automatic email to the requester when a ticket is split (its template, its switch, the cap and the before-triage rule)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import mail, outbox
from app.followers import add_follower
from app.messages import EMAIL_BEFORE_TRIAGE, can_email_now, record_inbound
from app.models import EmailTemplate, Message, OutboundEmail, Ticket, TicketStatus, User, UserRole
from app.settings_store import AUTOMATIC_EMAIL_CAP, SPLIT_EMAIL_ENABLED, set_setting
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)
from tests.test_splitting import split, subs_of, world
from tests.test_triage import agent, events, new_ticket

REQUEST_TEXT = "My keyboard sticks and I also need Office installed."


def untriaged_world(client, db_session, text=REQUEST_TEXT):
    """An untriaged emailed ticket with the requester's words on it, and an agent in its team (so one who works it)."""
    desk = create_team(db_session, "Service Desk")
    category = create_category(db_session, desk, "Hardware")
    ticket = new_ticket(db_session, desk)
    record_inbound(db_session, ticket, sender=ticket.requester_email, body=text, channel="email")
    db_session.commit()
    agent(client, db_session, member_of=[desk])
    return ticket, desk, category


def triage(client, part, category):
    response = client.post(
        f"/tickets/{part.number}/triage",
        data={"ticket_type": "incident", "impact": "medium", "urgency": "medium", "category_id": category.id},
    )
    assert response.status_code == 200, response.text


# ---- Splitting an untriaged ticket ---------------------------------------------------------------


def test_an_untriaged_ticket_can_be_split_and_its_parts_are_untriaged_and_in_the_queue(client, db_session):
    original, desk, _ = untriaged_world(client, db_session)

    response = split(client, original, ("Replace keyboard", "The E key sticks"), ("Install Office", ""))

    assert response.status_code == 200
    first, second = subs_of(db_session, original)
    for part in (first, second):
        assert part.split_from_id == original.id
        assert part.triaged_at is None and part.status == TicketStatus.NEW
        assert part.category_id is None and part.impact is None and part.priority is None
        assert part.requester_email == original.requester_email and part.source == original.source
        assert part.team_id == desk.id
    page = client.get("/tickets?untriaged=1").text  # the shared untriaged queue
    assert first.reference in page and second.reference in page


def test_the_original_becomes_a_parent_with_no_category_that_tracks_its_parts(client, db_session):
    original, _, category = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    db_session.refresh(original)

    # Marked triaged as a parent, with no category and no priority of its own; every part still untriaged: still New.
    assert original.triaged_at is not None and original.category_id is None and original.impact is None
    assert original.status == TicketStatus.NEW
    assert [e.detail for e in events(db_session, original, "split")] == ["split before triage: the parts are triaged one by one"] * 2
    assert client.get(f"/tickets/{original.number}").status_code == 200  # the page draws with no category

    triage(client, first, category)  # triaging one part leaves the other alone, and the original follows them
    db_session.expire_all()
    first, second, original = db_session.get(Ticket, first.id), db_session.get(Ticket, second.id), db_session.get(Ticket, original.id)
    assert first.triaged_at is not None and first.status == TicketStatus.OPEN
    assert second.triaged_at is None and second.status == TicketStatus.NEW and second.category_id is None
    assert original.status == TicketStatus.IN_PROGRESS


def test_an_untriaged_original_is_finished_only_when_every_part_is_and_then_an_agent_resolves_it(client, db_session):
    original, _, category = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)
    triage(client, first, category)
    triage(client, second, category)

    refused = client.post(f"/tickets/{original.number}/resolve", data={"resolution_notes": "Done"})
    assert refused.status_code == 400 and "Finish its sub-tickets first" in refused.text

    for part in (first, second):
        assert client.post(f"/tickets/{part.number}/resolve", data={"resolution_notes": "Fixed"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Ticket, original.id).status != TicketStatus.RESOLVED  # left for an agent: it sends the one resolution email

    assert client.post(f"/tickets/{original.number}/resolve", data={"resolution_notes": "All done"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Ticket, original.id).status == TicketStatus.RESOLVED


def test_a_triaged_original_still_finishes_when_every_part_has(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    for part in subs_of(db_session, original):
        assert client.post(f"/tickets/{part.number}/resolve", data={"resolution_notes": "Fixed"}).status_code == 200

    assert client.post(f"/tickets/{original.number}/resolve", data={"resolution_notes": "All done"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Ticket, original.id).status == TicketStatus.RESOLVED


def test_splitting_still_needs_at_least_two_parts_a_finished_ticket_and_not_a_sub_ticket(client, db_session):
    original, _, _ = untriaged_world(client, db_session)
    assert split(client, original, ("Only", "")).status_code == 400
    assert subs_of(db_session, original) == []

    split(client, original, ("One", ""), ("Two", ""))
    first, _ = subs_of(db_session, original)
    assert split(client, first, ("X", ""), ("Y", "")).status_code == 400  # a sub-ticket is never split again


def test_the_split_chip_is_offered_before_triage_to_an_agent_who_works_the_ticket(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    page = client.get(f"/tickets/{original.number}").text

    assert "?action=split" in page


# ---- The pre-filled Split card ---------------------------------------------------------------------


def test_the_card_starts_with_the_originals_subject_and_text_and_makes_nothing(client, db_session):
    original, _, _ = untriaged_world(client, db_session)
    before = len(db_session.exec(select(Ticket)).all())

    page = client.get(f"/tickets/{original.number}?action=split").text
    card = page.split('id="tp-card"')[1].split("</form>")[0]  # the thread shows the requester's words too

    assert card.count(f'value="{original.subject}"') == 2  # the first two parts
    assert card.count(REQUEST_TEXT) == 2
    assert len(db_session.exec(select(Ticket)).all()) == before


def test_what_the_agent_edits_is_what_is_saved(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    split(client, original, ("Replace keyboard", "My keyboard sticks"), ("Install Office", "I need Office installed"))

    first, second = subs_of(db_session, original)
    assert (first.subject, second.subject) == ("Replace keyboard", "Install Office")
    notes = {m.ticket_id: m.body for m in db_session.exec(select(Message).where(Message.ticket_id.in_([first.id, second.id]))).all()}
    assert notes == {first.id: "My keyboard sticks", second.id: "I need Office installed"}


def test_the_card_of_a_triaged_ticket_is_prefilled_too(client, db_session):
    original, _ = world(client, db_session)
    record_inbound(db_session, original, sender=original.requester_email, body="Two things are wrong", channel="email")
    db_session.commit()

    page = client.get(f"/tickets/{original.number}?action=split").text
    card = page.split('id="tp-card"')[1].split("</form>")[0]

    assert card.count("Two things are wrong") == 2 and "triaged like this one" in card


def test_a_refusal_shows_inside_the_card_and_keeps_what_was_typed(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    response = split(client, original, ("Only one part I typed", "Some details I typed"))

    assert response.status_code == 400
    assert 'id="tp-card-error"' in response.text and "at least 2" in response.text
    assert response.text.count("Only one part I typed") == 1 and response.text.count("Some details I typed") == 1
    assert original.subject not in response.text.split('id="tp-card"')[1].split("</form>")[0].replace("Only one part I typed", "")


def test_the_card_notes_that_the_parts_will_be_untriaged_and_that_the_requester_is_emailed(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    page = client.get(f"/tickets/{original.number}?action=split").text

    assert "has not been triaged" in page and "emailed once" in page
    set_setting(db_session, SPLIT_EMAIL_ENABLED, "off")
    db_session.commit()
    assert "emailed once" not in client.get(f"/tickets/{original.number}?action=split").text


# ---- The email to the requester --------------------------------------------------------------------


def split_row(db_session):
    [row] = rows(db_session, outbox.SPLIT_NOTICE)
    return row


def test_splitting_queues_one_split_email_listing_every_new_reference_even_before_triage(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Install Office", ""))
    first, second = subs_of(db_session, original)

    row = split_row(db_session)
    built = mail.build_split_notice_mail(db_session, row)

    assert row.ticket_id == original.id and row.to_address == original.requester_email
    assert row.kind in outbox.AUTOMATIC_TO_REQUESTER
    assert f"{first.reference} Replace keyboard" in built.body and f"{second.reference} Install Office" in built.body
    assert f"{original.reference} ({original.subject})" in built.body
    assert built.subject == f"[{original.reference}] {original.subject}"
    assert built.body.startswith(f"IT Support sent an update on {original.reference}.")  # the #257 automatic-notice header
    assert built.automatic is True
    assert first.triaged_at is None and second.triaged_at is None  # sent while the parts are untriaged
    assert mail.deliver_mail(db_session) == (1, 0) and split_row(db_session).sent_at is not None


def test_the_split_email_is_recorded_on_the_original_as_an_automatic_message(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))

    row = split_row(db_session)
    message = db_session.get(Message, row.message_id)

    assert message.ticket_id == original.id and message.author_user_id is None  # not an agent's email: no first response
    assert message.channel == "system" and message.visibility.value == "public"
    db_session.refresh(original)
    assert original.first_responded_at is None


def test_a_split_email_for_an_original_that_is_not_triaged_still_builds_and_goes(client, db_session, dev_mail):
    """The before-triage exception, decided in messages.can_email_now, not by anything in the builder."""
    original, _, _ = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    row = split_row(db_session)
    db_session.expire_all()
    ticket = db_session.get(Ticket, original.id)
    ticket.triaged_at = None
    db_session.add(ticket)
    db_session.commit()

    assert outbox.SPLIT_NOTICE in EMAIL_BEFORE_TRIAGE and can_email_now(ticket, outbox.SPLIT_NOTICE)
    assert not can_email_now(ticket) and not can_email_now(ticket, outbox.NOTICE)
    assert "One" in mail.build_split_notice_mail(db_session, row).body
    assert mail.deliver_mail(db_session) == (1, 0)


def test_followers_are_copied_as_usual(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    add_follower(db_session, original, "watcher@example.com", "agent@example.com")
    db_session.commit()
    split(client, original, ("One", ""), ("Two", ""))

    assert mail.build_split_notice_mail(db_session, split_row(db_session)).cc == ["watcher@example.com"]


def test_the_split_email_counts_toward_the_automatic_cap(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    set_setting(db_session, AUTOMATIC_EMAIL_CAP, "1")
    earlier = OutboundEmail(
        kind=outbox.NOTICE, ticket_id=original.id, to_address=original.requester_email,
        sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(earlier)
    db_session.commit()
    split(client, original, ("One", ""), ("Two", ""))
    now = datetime.now(timezone.utc)

    assert mail.deliver_mail(db_session, now=now) == (0, 0)  # held back, not lost
    row = split_row(db_session)
    assert row.sent_at is None and "Held back" in row.last_error and row.next_attempt_at > now

    assert mail.deliver_mail(db_session, now=now + timedelta(hours=25)) == (1, 0)


def test_the_split_email_uses_the_admin_template_until_it_is_reset(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    saved = client.post(
        "/admin/email-templates/split_notice",
        data={"subject": "[{reference}] Split: {subject}", "body": "We split {reference} into:\n{new_tickets}"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    login_as(client, "agent@example.com")
    split(client, original, ("One", ""), ("Two", ""))
    first, second = subs_of(db_session, original)

    built = mail.build_split_notice_mail(db_session, split_row(db_session))

    assert built.subject == f"[{original.reference}] Split: {original.subject}"
    assert built.body.endswith(f"We split {original.reference} into:\n{first.reference} One\n{second.reference} Two")

    login_as(client, "admin@example.com")
    assert client.post("/admin/email-templates/split_notice/reset", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(EmailTemplate, "split_notice") is None


def test_the_template_needs_the_list_and_keeps_the_reference_in_the_subject(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)

    no_list = client.post("/admin/email-templates/split_notice", data={"subject": "[{reference}] x", "body": "Split."}, follow_redirects=False)
    no_reference = client.post("/admin/email-templates/split_notice", data={"subject": "Split", "body": "{new_tickets}"}, follow_redirects=False)

    assert no_list.status_code != 303 and "new_tickets" in no_list.text
    assert no_reference.status_code != 303 and "reference" in no_reference.text
    assert db_session.get(EmailTemplate, "split_notice") is None
    page = client.get("/admin/email-templates/split_notice").text
    assert "Split notice" in page and "{new_tickets}" in page


def test_the_switch_turns_the_email_off(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    set_setting(db_session, SPLIT_EMAIL_ENABLED, "off")
    db_session.commit()

    split(client, original, ("One", ""), ("Two", ""))

    assert len(subs_of(db_session, original)) == 2
    assert rows(db_session, outbox.SPLIT_NOTICE) == []
    assert db_session.exec(select(Message).where(Message.ticket_id == original.id, Message.channel == "system")).all() == []


def test_switching_it_off_after_it_was_queued_stops_it_going(client, db_session, dev_mail):
    original, _, _ = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    set_setting(db_session, SPLIT_EMAIL_ENABLED, "off")
    db_session.commit()

    assert mail.deliver_mail(db_session) == (0, 1)  # given up, never sent


def test_the_switch_is_on_by_default_and_sits_with_the_other_mail_settings(client, db_session):
    from app.settings_store import GROUP_MAIL, REGISTRY, get_flag

    assert get_flag(db_session, SPLIT_EMAIL_ENABLED) is True
    assert REGISTRY[SPLIT_EMAIL_ENABLED].group == GROUP_MAIL and REGISTRY[AUTOMATIC_EMAIL_CAP].group == GROUP_MAIL


def test_a_triaged_ticket_split_emails_its_requester_too(client, db_session, dev_mail):
    original, _ = world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Fix the screen", ""))

    built = mail.build_split_notice_mail(db_session, split_row(db_session))

    assert "Replace keyboard" in built.body and "Fix the screen" in built.body


def test_with_mail_off_the_split_queues_no_email(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    split(client, original, ("One", ""), ("Two", ""))

    assert rows(db_session) == []


def test_two_parts_left_exactly_alike_are_refused_as_an_untrimmed_card(client, db_session):
    original, _, _ = untriaged_world(client, db_session)

    response = split(client, original, (original.subject, REQUEST_TEXT), (original.subject, REQUEST_TEXT))

    assert response.status_code == 400 and "exactly the same" in response.text
    assert subs_of(db_session, original) == []
    assert split(client, original, (original.subject, REQUEST_TEXT), (original.subject, "Only the Office part")).status_code == 200


def test_the_queues_and_the_original_with_no_category_still_draw_after_an_untriaged_split(client, db_session):
    original, _, _ = untriaged_world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))

    assert client.get("/tickets").status_code == 200
    assert client.get("/tickets?untriaged=1").status_code == 200
    assert client.get(f"/tickets/{original.number}").status_code == 200
    assert client.get(f"/tickets/{subs_of(db_session, original)[0].number}").status_code == 200


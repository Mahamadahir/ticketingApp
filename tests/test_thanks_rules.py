"""4E-2: thank-you rules (app/thanks_rules.py): a short thanks on a Resolved ticket is recorded without
reopening it, anything else reopens; matching rules (wildcards, length, case, punctuation, no regular
expressions), attachments, quoted text, the portal path, the admin page and the review queue."""

import time

import pytest
from sqlmodel import select

from datetime import datetime, timezone

from app.inbound import IncomingAttachment, REPLY_ADDED, ingest_mail
from app.models import AuditLog, Message, ThanksCandidate, ThanksRule, Ticket, TicketSource, TicketStatus
from app.ticket_service import create_ticket
from app.thanks_rules import (
    ThanksRuleError, add_rule, clean_rule, is_thanks_reply, normalise_text, record_candidate, rule_matches,
)
from tests.conftest import login_as
from tests.test_inbound_mail import mail as incoming
from tests.test_inbound_mail import mailbox, storage  # noqa: F401  (fixtures)
from tests.test_notifications import notifications_for
from tests.test_reopening import events, fresh, finish, reclose, reopen
from tests.test_reply_notification import assigned_ticket, comment_as


def match(match_type, pattern, text, max_length=50):
    match_type, pattern, max_length = clean_rule(match_type, pattern, max_length)
    return rule_matches(match_type, pattern, max_length, normalise_text(text))


# ---- Matching -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, pattern, text, expected",
    [
        ("equals", "thanks", "Thanks", True),
        ("equals", "thanks", "THANKS!!!", True),
        ("equals", "thanks", "Thanks :) \U0001F44D", True),  # punctuation and emoji are ignored
        ("equals", "thanks", "thanks a lot", False),
        ("equals", "thank you", "Thank   you.", True),
        ("equals", "thank you", "Thank-you", True),  # a hyphen is punctuation, so a space
        ("equals", "dont worry", "Don't worry", True),  # apostrophes are dropped
        ("starts_with", "thanks", "Thanks for fixing it, but now it is slow", True),
        ("starts_with", "thanks", "Many thanks", False),
        ("ends_with", "thanks", "Many thanks!", True),
        ("ends_with", "thanks", "Thanks a lot", False),
        ("contains", "thank", "Oh, thank you so much", True),
        ("contains", "thank", "great work", False),
        ("equals", "thanks for resolving this*", "Thanks for resolving this quickly", True),
        ("equals", "thanks for resolving this*", "Thanks for resolving this", True),
        ("equals", "thanks for resolving this*", "Oh thanks for resolving this", False),
        ("equals", "*resolving this", "Thanks for resolving this", True),
        ("equals", "thanks*resolving", "thanks for resolving", True),
        ("equals", "thanks*resolving", "thanks for resolving it", False),
        ("equals", "thanks*resolving", "thanksresolving", True),  # the wildcard can match nothing
        ("starts_with", "thanks*resolving", "Thanks for resolving it though", True),
        ("starts_with", "thanks*resolving", "Thanks for fixing it", False),
        ("ends_with", "thanks*team", "Thanks so much to the team", True),
        ("ends_with", "thanks*team", "Thanks team, but it is still broken", False),
        ("contains", "thanks*fixing*team", "a big thanks for fixing it, team", True),
        ("contains", "thanks*fixing*team", "team, thanks for fixing it", False),  # the pieces keep their order
        ("equals", "ab*ab", "ab", False),  # the same text cannot serve as both pieces
        ("equals", "ab*ab", "abab", True),
    ],
)
def test_matching(kind, pattern, text, expected):
    assert match(kind, pattern, text) is expected


def test_the_maximum_length_counts_the_normalised_text():
    assert match("starts_with", "thanks", "Thanks!!!!!!!!!!!!!!!!!!!!!!!!!!!!!", max_length=6)  # punctuation not counted
    assert not match("starts_with", "thanks", "Thanks for fixing it", max_length=19)
    assert match("starts_with", "thanks", "Thanks for fixing it", max_length=20)


def test_no_regular_expressions_a_pattern_is_plain_text():
    # Regex characters are punctuation: dropped like any other, never interpreted.
    assert not match("equals", "th.nks", "thanks")  # "th nks": the dot is not "any character"
    assert not match("contains", "[a-z]+", "thanks")  # "a z", not a character class
    assert match("equals", "(thanks)+", "Thanks")  # just the word "thanks"
    with pytest.raises(ThanksRuleError):
        clean_rule("contains", ".*", 20)


def test_a_hostile_pattern_and_text_finish_quickly():
    start = time.perf_counter()
    # The classic backtracking shape: with a regular expression this would not finish.
    assert not match("equals", "a*a*a*a*a*b", "a" * 190, max_length=200)
    assert not match("contains", "a*a*a*a*a*b", "a " * 90, max_length=200)
    assert time.perf_counter() - start < 1


def test_a_reply_that_is_too_long_to_be_thanks_is_not_even_scanned(db_session):
    add_rule(db_session, "contains", "thanks", 200, "admin@example.com")
    assert is_thanks_reply(db_session, "thanks")
    assert not is_thanks_reply(db_session, "thanks " + "x" * 2000)


def test_rules_are_validated():
    for bad in [("sounds_like", "thanks", 10), ("equals", "", 10), ("equals", "  ** ", 10), ("equals", "!!!", 10),
                ("equals", "thanks", 0), ("equals", "thanks", 201), ("equals", "thanks", "many"),
                ("equals", "a*b*c*d*e*f*g", 10), ("equals", "x" * 201, 10)]:
        with pytest.raises(ThanksRuleError):
            clean_rule(*bad)
    assert clean_rule("equals", "  Thanks!!  For ** resolving  *", "30") == ("equals", "thanks for*resolving*", 30)


def test_a_disabled_rule_matches_nothing_and_no_rules_means_everything_reopens(db_session):
    assert not is_thanks_reply(db_session, "thanks")
    rule = add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    assert is_thanks_reply(db_session, "Thanks!")
    rule.enabled = False
    db_session.add(rule)
    db_session.flush()
    assert not is_thanks_reply(db_session, "Thanks!")


def test_an_attachment_always_reopens(db_session):
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    assert is_thanks_reply(db_session, "thanks")
    assert not is_thanks_reply(db_session, "thanks", has_attachment=True)


# ---- A reply on a Resolved ticket -----------------------------------------------------------------


def resolved_with_rule(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    add_rule(db_session, "starts_with", "thanks", 20, "admin@example.com")
    db_session.commit()
    return ticket


def test_a_portal_thank_you_is_recorded_and_the_ticket_stays_resolved(client, db_session):
    ticket = resolved_with_rule(client, db_session)

    comment_as(client, "jo@example.com", ticket, "Thanks!! :)")

    ticket = fresh(db_session, ticket)
    assert (ticket.status, ticket.reopen_count) == (TicketStatus.RESOLVED, 0)
    bodies = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()]
    assert "Thanks!! :)" in bodies
    [event] = events(db_session, ticket, "thanks_ignored")
    assert event.detail == "Thanks!! :)"
    assert events(db_session, ticket, "reopened") == []
    [told] = notifications_for(db_session, "agent@example.com")
    assert told.kind == "replied"  # the agent still hears of the reply


def test_a_portal_reply_that_is_not_a_thank_you_reopens_as_before(client, db_session):
    ticket = resolved_with_rule(client, db_session)

    comment_as(client, "jo@example.com", ticket, "Thanks, but it is broken again")

    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS


def test_the_same_rule_decides_for_mail_and_new_text_only_is_looked_at(db_session, mailbox):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    db_session.commit()
    subject = f"Re: [{ticket.reference}] Printer jam"

    # uniqueBody is the new text only; this is what a real thanks looks like after the quoted thread is stripped
    result = ingest_mail(db_session, incoming("t1", subject=subject, body="Thanks!"))
    db_session.commit()
    assert result.outcome == REPLY_ADDED
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED
    assert len(events(db_session, ticket, "thanks_ignored")) == 1

    # The same words with the quoted thread still attached (so not "new text only") are not a thank-you.
    quoted = "Thanks!\n\nOn Tue, IT wrote:\n> Your ticket was resolved: Replaced the roller"
    ingest_mail(db_session, incoming("t2", subject=subject, body=quoted))
    db_session.commit()
    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_a_thank_you_with_an_attachment_reopens(db_session, mailbox):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    db_session.commit()
    subject = f"Re: [{ticket.reference}] Printer jam"

    ingest_mail(db_session, incoming("a1", subject=subject, body="Thanks", has_attachments=True))
    db_session.commit()

    assert fresh(db_session, ticket).status == TicketStatus.OPEN


def test_a_signature_logo_alone_does_not_stop_a_thank_you(db_session, mailbox, storage):
    from tests.test_inbound_mail import open_ticket, triage_by_hand

    ticket = open_ticket(db_session)
    triage_by_hand(db_session, ticket)
    ticket = finish(db_session, ticket)
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    db_session.commit()
    logo = IncomingAttachment(filename="logo.png", data=b"\x89PNG\r\n\x1a\n" + b"0" * 200, inline=True)

    ingest_mail(
        db_session, incoming("l1", subject=f"Re: [{ticket.reference}] Printer jam", body="Thanks", has_attachments=True),
        [logo], storage,
    )
    db_session.commit()

    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_a_disabled_rule_reopens(client, db_session):
    ticket = resolved_with_rule(client, db_session)
    rule = db_session.exec(select(ThanksRule)).one()
    rule.enabled = False
    db_session.add(rule)
    db_session.commit()

    comment_as(client, "jo@example.com", ticket, "Thanks")

    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS


def test_a_thank_you_on_an_open_ticket_is_just_a_reply(client, db_session):
    ticket, team, ann_id = assigned_ticket(client, db_session)
    add_rule(db_session, "starts_with", "thanks", 20, "admin@example.com")
    db_session.commit()

    comment_as(client, "jo@example.com", ticket, "Thanks")

    assert events(db_session, fresh(db_session, ticket), "thanks_ignored") == []


# ---- The review queue -----------------------------------------------------------------------------


def next_ticket(client, db_session):
    """A triaged ticket assigned to Ann; the desk team and Ann are made the first time and reused after."""
    existing = db_session.exec(select(Ticket)).first()
    if existing is None:
        return assigned_ticket(client, db_session)
    ticket = create_ticket(
        db_session, subject="Printer jam", requester_email="jo@example.com", source=TicketSource.PORTAL, team_id=existing.team_id
    )
    ticket.triaged_at = datetime.now(timezone.utc)
    ticket.assigned_agent_id = existing.assigned_agent_id
    ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()
    return ticket, None, existing.assigned_agent_id


def reopened_by_reply_and_reclosed(client, db_session, text="Cheers mate!"):
    ticket, team, ann_id = next_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    comment_as(client, "jo@example.com", ticket, text)
    assert fresh(db_session, ticket).status == TicketStatus.IN_PROGRESS
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    assert reclose(client, ticket, "said_thanks").status_code == 200
    return ticket


def reopened_by_reply_and_reclosed_with_attachment(client, db_session):
    """A reply that a rule covers but that reopened the ticket (as if it had a file): reclosed as said thanks."""
    ticket, team, ann_id = next_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    from app.reopening import reopen_ticket

    db_session.add(Message(ticket_id=ticket.id, sender="Jo", body="Thanks", direction="inbound"))
    db_session.flush()
    reopen_ticket(db_session, ticket, "jo@example.com", "The requester replied")
    db_session.commit()
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    assert reclose(client, ticket, "said_thanks").status_code == 200
    return ticket


def candidates(db_session):
    db_session.expire_all()
    return db_session.exec(select(ThanksCandidate).order_by(ThanksCandidate.id)).all()


def test_reclosing_as_said_thanks_queues_the_reply_and_it_counts_repeats(client, db_session):
    reopened_by_reply_and_reclosed(client, db_session)
    [row] = candidates(db_session)
    assert (row.normalised_text, row.sample_text, row.occurrences, row.status) == ("cheers mate", "Cheers mate!", 1, "pending")

    reopened_by_reply_and_reclosed(client, db_session, "cheers MATE")
    [row] = candidates(db_session)
    assert row.occurrences == 2


def test_other_reclose_reasons_and_buttons_queue_nothing(client, db_session):
    ticket = reopened_by_reply_and_reclosed(client, db_session, "Still broken")
    assert [r.normalised_text for r in candidates(db_session)] == ["still broken"]  # the one said_thanks above

    ticket, team, ann_id = next_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    comment_as(client, "jo@example.com", ticket, "Nope, still not fixed")
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    reclose(client, ticket, "duplicate")

    ticket, team, ann_id = next_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    client.post("/auth/logout")
    login_as(client, "agent@example.com")
    reopen(client, ticket)  # a button, not a reply
    reclose(client, ticket, "said_thanks")

    assert [r.normalised_text for r in candidates(db_session)] == ["still broken"]


def test_a_dismissed_text_is_not_queued_again_and_an_emoji_only_reply_is_not_queued(client, db_session):
    reopened_by_reply_and_reclosed(client, db_session)
    client.post("/auth/logout")
    login_as(client, "admin@example.com")
    [row] = candidates(db_session)
    assert client.post(f"/admin/thanks-rules/candidates/{row.id}/dismiss", follow_redirects=False).status_code == 303

    reopened_by_reply_and_reclosed(client, db_session, "Cheers mate!!")
    reopened_by_reply_and_reclosed(client, db_session, "\U0001F44D")

    [row] = candidates(db_session)
    assert (row.status, row.decided_by, row.occurrences) == ("dismissed", "admin@example.com", 1)


# ---- The admin page -------------------------------------------------------------------------------


def admin(client):
    client.post("/auth/logout")
    login_as(client, "admin@example.com")


def test_only_a_super_admin_sees_the_page(client, db_session):
    login_as(client, "someone@example.com")
    assert client.get("/admin/thanks-rules").status_code == 403
    assert client.post("/admin/thanks-rules", data={"match_type": "equals", "pattern": "x", "max_length": "5"}).status_code == 403


def test_an_admin_adds_edits_disables_and_enables_a_rule_and_each_is_audited(client, db_session):
    admin(client)
    assert client.get("/admin/thanks-rules").status_code == 200
    response = client.post(
        "/admin/thanks-rules", data={"match_type": "starts_with", "pattern": "Thanks for resolving this*", "max_length": "40"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rule = db_session.exec(select(ThanksRule)).one()
    assert (rule.match_type, rule.pattern, rule.max_length, rule.enabled) == ("starts_with", "thanks for resolving this*", 40, True)
    assert "thanks for resolving this*" in client.get("/admin/thanks-rules").text

    client.post(f"/admin/thanks-rules/{rule.id}", data={"match_type": "equals", "pattern": "cheers", "max_length": "12"})
    client.post(f"/admin/thanks-rules/{rule.id}/disable")
    db_session.expire_all()
    rule = db_session.get(ThanksRule, rule.id)
    assert (rule.match_type, rule.pattern, rule.max_length, rule.enabled) == ("equals", "cheers", 12, False)
    client.post(f"/admin/thanks-rules/{rule.id}/enable")
    db_session.expire_all()
    assert db_session.get(ThanksRule, rule.id).enabled is True

    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "thanks_rule").order_by(AuditLog.id)).all()]
    assert actions == ["added", "edited", "disabled", "enabled"]


def test_a_bad_rule_is_refused_and_unknown_ids_are_not_found(client, db_session):
    admin(client)
    for data in [
        {"match_type": "regex", "pattern": "x", "max_length": "5"},
        {"match_type": "equals", "pattern": "***", "max_length": "5"},
        {"match_type": "equals", "pattern": "x", "max_length": "9999"},
        {"match_type": "equals", "pattern": "x", "max_length": "abc"},
    ]:
        assert client.post("/admin/thanks-rules", data=data).status_code == 400
    assert db_session.exec(select(ThanksRule)).all() == []
    assert client.post("/admin/thanks-rules/999", data={"match_type": "equals", "pattern": "x", "max_length": "5"}).status_code == 404
    assert client.post("/admin/thanks-rules/999/disable").status_code == 404
    assert client.post("/admin/thanks-rules/999/delete").status_code == 404


def test_the_admin_turns_a_candidate_into_a_rule_that_then_stops_the_reopening(client, db_session):
    reopened_by_reply_and_reclosed(client, db_session)
    admin(client)
    page = client.get("/admin/thanks-rules").text
    assert "Cheers mate!" in page
    [row] = candidates(db_session)

    response = client.post(
        f"/admin/thanks-rules/candidates/{row.id}/rule",
        data={"match_type": "equals", "pattern": row.normalised_text, "max_length": "20"}, follow_redirects=False,
    )
    assert response.status_code == 303
    [row] = candidates(db_session)
    assert (row.status, row.decided_by) == ("rule", "admin@example.com")
    assert "Cheers mate!" not in client.get("/admin/thanks-rules").text
    assert client.post(f"/admin/thanks-rules/candidates/{row.id}/rule", data={"match_type": "equals", "pattern": "x", "max_length": "5"}).status_code == 404  # already decided
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "thanks_rule")).all()]
    assert actions == ["added_from_review"]

    ticket, team, ann_id = next_ticket(client, db_session)
    ticket = finish(db_session, ticket)
    comment_as(client, "jo@example.com", ticket, "Cheers mate")
    assert fresh(db_session, ticket).status == TicketStatus.RESOLVED


def test_a_bad_rule_from_the_queue_leaves_the_candidate_pending(client, db_session):
    reopened_by_reply_and_reclosed(client, db_session)
    admin(client)
    [row] = candidates(db_session)

    assert client.post(
        f"/admin/thanks-rules/candidates/{row.id}/rule", data={"match_type": "equals", "pattern": "*", "max_length": "5"}
    ).status_code == 400
    db_session.rollback()
    assert [r.status for r in candidates(db_session)] == ["pending"]
    assert db_session.exec(select(ThanksRule)).all() == []


def test_a_skipped_inline_logo_is_still_not_an_attachment(db_session):
    from app.inbound import IncomingMail, _has_real_attachment

    mail = IncomingMail(graph_message_id="x", sender="jo@example.com", has_attachments=True)
    assert not _has_real_attachment(mail, [IncomingAttachment(filename="logo.png", skipped="no longer available", inline=True)])
    assert _has_real_attachment(mail, [IncomingAttachment(filename="log.txt", skipped="larger than the 10 MB limit")])


def test_a_text_a_rule_already_covers_is_not_queued(client, db_session):
    add_rule(db_session, "equals", "thanks", 20, "admin@example.com")
    db_session.commit()
    ticket = reopened_by_reply_and_reclosed_with_attachment(client, db_session)
    assert candidates(db_session) == []

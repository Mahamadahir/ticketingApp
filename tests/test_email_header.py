"""The header at the top of an email to a requester (#257): who sent it and "Please reply to this email...".
One function builds it (app/messages.email_header), for the preview and for the email that is sent."""

import inspect
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app import email_templates, mail, messages, outbox
from app.closing import close_ticket
from app.email_templates import save_template
from app.messages import HEADER_RULE, preview_email, record_notice
from app.models import Message, OutboundEmail, Ticket, TicketStatus, User
from app.resolution import resolve_ticket
from tests.test_email_preview import follow, preview, ticket_for_email
from tests.test_outbound_mail import dev_mail, email, reply_row, rows  # noqa: F401  (dev_mail is a fixture)
from tests.test_splitting import split, subs_of, world as split_world

REPLY_LINE = "Please reply to this email to respond, keeping [{ref}] in the subject."


def header_of(reference, first=None):
    lines = ([first] if first else []) + [REPLY_LINE.format(ref=reference), HEADER_RULE]
    return "\n".join(lines) + "\n\n"


def agent_of(db_session):
    return db_session.exec(select(User).where(User.email == "agent@example.com")).one()


def fresh(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def edit(db_session, key, body):
    save_template(db_session, key, None, body, "admin@example.com")
    db_session.commit()


# ---- What each kind gets ----------------------------------------------------------------------------


def test_an_agents_reply_says_who_sent_it_and_to_reply(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Please restart it")

    built = mail.build_message_mail(db_session, reply_row(db_session, ticket))

    expected = header_of(ticket.reference, f"Agent Ann from IT Support replied to {ticket.reference}.") + "Please restart it"
    assert built.body == expected and built.record == expected


def test_the_resolution_email_has_the_agent_header(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    resolve_ticket(db_session, ticket, agent_of(db_session), "Replaced the roller")
    db_session.commit()
    built = mail.build_message_mail(db_session, reply_row(db_session, ticket))
    assert built.body.startswith(f"Agent Ann from IT Support replied to {ticket.reference}.\n" + REPLY_LINE.format(ref=ticket.reference))
    assert "Replaced the roller" in built.body


def test_the_closing_email_has_the_agent_header(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    close_ticket(db_session, ticket, agent_of(db_session), "All done")
    db_session.commit()
    built = mail.build_message_mail(db_session, reply_row(db_session, ticket))
    assert built.body.startswith(f"Agent Ann from IT Support replied to {ticket.reference}.\n")
    assert "All done" in built.body


def test_an_automatic_notice_has_the_automatic_header(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    record_notice(db_session, ticket, "Your request has been approved.")
    db_session.commit()
    [row] = rows(db_session, outbox.NOTICE)

    built = mail.build_message_mail(db_session, row)

    assert built.body == header_of(ticket.reference, f"IT Support sent an update on {ticket.reference}.") + "Your request has been approved."
    assert built.automatic


def test_the_raised_for_you_email_has_only_the_reply_line(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    [row] = rows(db_session, outbox.RAISED_FOR)

    built = mail.build_raised_for_mail(db_session, row)

    assert built.body.startswith(header_of(ticket.reference))
    assert "from IT Support replied" not in built.body and "sent an update" not in built.body
    assert built.record == built.body


def test_the_acknowledgement_and_the_chase_have_only_the_reply_line(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    ack = OutboundEmail(kind=outbox.ACKNOWLEDGEMENT, ticket_id=ticket.id, to_address="walkin@example.com")
    chase = OutboundEmail(kind=outbox.CHASE, ticket_id=ticket.id, to_address="walkin@example.com")
    db_session.add(ack)
    db_session.add(chase)
    db_session.commit()
    built = mail.build_acknowledgement_mail(db_session, ack)
    assert built.body.startswith(header_of(ticket.reference)) and built.record == built.body

    ticket = fresh(db_session, ticket)
    ticket.status = TicketStatus.AWAITING_USER
    ticket.chased_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    built = mail.build_chase_mail(db_session, chase)
    assert built.body.startswith(header_of(ticket.reference)) and "Is this still an issue?" in built.body


def test_a_split_part_has_the_originals_reference_as_the_subject_does_and_the_about_line_stays_below(client, db_session, dev_mail):
    original, _ = split_world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Fix the screen", ""))
    part = subs_of(db_session, original)[0]
    email(client, part, body="On it")

    built = mail.build_message_mail(db_session, reply_row(db_session, part))

    assert built.subject.startswith(f"[{original.reference}]")
    assert built.body == (
        header_of(original.reference, f"Test User from IT Support replied to {original.reference}.")
        + f"About: {part.reference} {part.subject}\n\nOn it"
    )


# ---- No header on anything else ---------------------------------------------------------------------


def test_only_the_requester_builders_make_a_header():  # the four of #257, the split notice (#259) and the end-user-changed notice (#279), automatic notices
    callers = {
        name for name, fn in inspect.getmembers(mail, inspect.isfunction)
        if fn.__module__ == mail.__name__ and "email_header(" in inspect.getsource(fn)
    }
    assert callers == {"build_message_mail", "build_raised_for_mail", "build_acknowledgement_mail", "build_chase_mail",
                       "build_split_notice_mail", "build_requester_changed_mail"}


def test_a_follower_update_has_no_header(client, db_session, dev_mail):
    from tests.test_follower_updates import reply, updates, world

    ticket = world(db_session)
    reply(db_session, ticket, "r1")
    built = mail.build_follower_update_mail(db_session, updates(db_session)[0])
    assert HEADER_RULE not in built.body and "Please reply to this email to respond" not in built.body


def test_an_approval_email_has_no_header(client, db_session, dev_mail):
    from tests.test_approvals import person, raised_and_triaged, request_type

    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    [row] = rows(db_session, outbox.APPROVAL)
    built = mail.build_approval_mail(db_session, row)
    assert HEADER_RULE not in built.body and "Please reply to this email to respond" not in built.body


# ---- The preview ------------------------------------------------------------------------------------


def test_the_preview_header_is_the_header_of_the_sent_email(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    shown = preview_email(db_session, fresh(db_session, ticket), "Please restart it", author=agent_of(db_session))
    email(client, ticket, body="Please restart it")

    built = mail.build_message_mail(db_session, reply_row(db_session, ticket))

    assert shown.header and built.body == shown.header + shown.body


def test_the_reply_preview_page_shows_the_header_and_still_posts_the_plain_body(client, db_session):
    ticket = ticket_for_email(client, db_session)
    response = preview(client, ticket, body="Please restart it")
    assert "Agent Ann from IT Support replied to" in response.text and HEADER_RULE in response.text
    hidden = response.text.split('<textarea name="body" hidden>')[1].split("</textarea>")[0]
    assert hidden.strip() == "Please restart it"


def test_a_teams_only_reply_has_no_header_but_one_that_also_goes_by_email_has_it(client, db_session, monkeypatch):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    shown = preview_email(db_session, ticket, "Hello", author=agent_of(db_session))
    assert shown.also_email and shown.header

    monkeypatch.setattr(messages, "route_for", lambda *a, **k: type("R", (), {"teams": True, "email": False})())
    shown = preview_email(db_session, ticket, "Hello", author=agent_of(db_session))
    assert shown.by_teams and not shown.also_email and shown.header == ""

    monkeypatch.setattr(messages, "route_for", lambda *a, **k: type("R", (), {"teams": True, "email": True})())
    shown = preview_email(db_session, ticket, "Hello", author=agent_of(db_session))
    assert shown.by_teams and shown.also_email and shown.header


# ---- The wording ------------------------------------------------------------------------------------


def test_an_edited_template_is_used_by_an_email_still_in_the_queue(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Hi")
    row = reply_row(db_session, ticket)
    edit(db_session, "header_agent", "{agent} (IT) wrote about {reference}:")
    edit(db_session, "header_reply", "Reply here.")

    built = mail.build_message_mail(db_session, row)

    assert built.body == f"Agent Ann (IT) wrote about {ticket.reference}:\nReply here.\n{HEADER_RULE}\n\nHi"


def test_a_blanked_line_is_dropped_and_all_blank_means_no_rule(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Hi")
    row = reply_row(db_session, ticket)

    edit(db_session, "header_agent", "")
    assert mail.build_message_mail(db_session, row).body == header_of(ticket.reference) + "Hi"

    edit(db_session, "header_agent", "{agent} wrote.")
    edit(db_session, "header_reply", "")
    assert mail.build_message_mail(db_session, row).body == f"Agent Ann wrote.\n{HEADER_RULE}\n\nHi"

    edit(db_session, "header_agent", "")
    built = mail.build_message_mail(db_session, row)
    assert built.body == "Hi" and HEADER_RULE not in built.body
    shown = preview_email(db_session, fresh(db_session, ticket), "Hi", author=agent_of(db_session))
    assert shown.header == ""


def test_a_blank_header_template_can_be_saved_from_the_admin_page_and_the_others_still_cannot(client, db_session):
    for key in ("header_agent", "header_automatic", "header_reply"):
        assert email_templates.REGISTRY[key].may_be_blank and email_templates.AUDIENCE_OF[key] == "requester"
    assert not email_templates.REGISTRY["chase"].may_be_blank
    with pytest.raises(email_templates.TemplateError):
        email_templates.validate(email_templates.REGISTRY["chase"], None, "  ")
    assert email_templates.validate(email_templates.REGISTRY["header_reply"], None, "  ") == (None, "")


def test_a_renamed_agent_does_not_change_a_queued_email(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Hi")
    row = reply_row(db_session, ticket)
    agent = agent_of(db_session)
    agent.display_name = "Someone Else"
    db_session.add(agent)
    db_session.commit()

    built = mail.build_message_mail(db_session, row)

    assert built.body.startswith(f"Agent Ann from IT Support replied to {ticket.reference}.")
    assert "Someone Else" not in built.body


# ---- Not stored anywhere else -----------------------------------------------------------------------


def test_the_stored_message_the_portal_and_the_teams_copy_have_no_header(client, db_session, dev_mail):
    from app.messages import requester_visible_messages

    ticket = ticket_for_email(client, db_session)
    email(client, ticket, body="Please restart it")
    stored = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.body == "Please restart it")).one()
    assert stored.body == "Please restart it"
    visible = [m.body for m in requester_visible_messages(db_session, fresh(db_session, ticket))]
    assert "Please restart it" in visible and not any(HEADER_RULE in text for text in visible)
    # The Teams copy is built from the message body by app/teams.py, which never calls email_header.
    import app.teams as teams
    assert "email_header" not in inspect.getsource(teams)
    assert "email_header" not in inspect.getsource(__import__("app.bot_cards", fromlist=["x"]))


def test_an_inbound_reply_whose_unique_body_is_only_the_new_text_is_recorded_without_the_header(db_session, dev_mail):
    from tests.test_follower_updates import reply, world

    ticket = world(db_session)
    reply(db_session, ticket, "r1", body="It is still stuck")
    recorded = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert any(m.body == "It is still stuck" for m in recorded)
    assert not any(HEADER_RULE in m.body or "Please reply to this email to respond" in m.body for m in recorded)


# ---- The default templates do not repeat the header's instruction (#276) ------------------------------


@pytest.mark.parametrize("key", ["raised_for", "resolution", "chase", "auto_resolve_note"])
def test_the_default_no_longer_says_reply_to_this_email(key):
    assert "reply to this email" not in email_templates.definition(key).body_default.lower()


def test_the_closed_default_still_says_a_reply_opens_a_new_ticket():
    body = email_templates.definition("closed").body_default
    assert "reply to this email" not in body.lower() and "your reply will open a new ticket linked to this one." in body


def latest_reply(db_session, ticket):
    candidates = [r for r in rows(db_session, outbox.REPLY) if r.ticket_id == ticket.id]
    return max(candidates, key=lambda r: r.id)


def reply_lines(body):
    return body.lower().count("reply to this email")


def test_the_raised_for_chase_and_resolution_emails_have_the_reply_line_once(client, db_session, dev_mail):
    from app.resolution import auto_resolution_note

    ticket = fresh(db_session, ticket_for_email(client, db_session))
    [raised] = rows(db_session, outbox.RAISED_FOR)
    built = mail.build_raised_for_mail(db_session, raised)
    assert built.body.startswith(header_of(ticket.reference)) and reply_lines(built.body) == 1

    chase = OutboundEmail(kind=outbox.CHASE, ticket_id=ticket.id, to_address="walkin@example.com")
    db_session.add(chase)
    ticket.status = TicketStatus.AWAITING_USER
    ticket.chased_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    built = mail.build_chase_mail(db_session, chase)
    assert built.body.startswith(header_of(ticket.reference)) and reply_lines(built.body) == 1
    assert "Let us know. If we do not hear from you" in built.body

    ticket = fresh(db_session, ticket)
    resolve_ticket(db_session, ticket, agent_of(db_session), "Replaced the roller")
    db_session.commit()
    built = mail.build_message_mail(db_session, latest_reply(db_session, ticket))
    assert reply_lines(built.body) == 1 and "tell us or reopen the ticket." in built.body


def test_the_closing_email_has_the_reply_line_once(client, db_session, dev_mail):
    ticket = fresh(db_session, ticket_for_email(client, db_session))
    close_ticket(db_session, ticket, agent_of(db_session), "All done")
    db_session.commit()
    built = mail.build_message_mail(db_session, latest_reply(db_session, ticket))
    assert reply_lines(built.body) == 1 and "your reply will open a new ticket linked to this one." in built.body


def test_an_automatic_resolution_email_has_the_reply_line_once(client, db_session, dev_mail):
    from app.resolution import auto_resolution_note

    ticket = fresh(db_session, ticket_for_email(client, db_session))
    resolve_ticket(db_session, ticket, None, auto_resolution_note(db_session, ticket, 3))
    db_session.commit()
    [row] = [r for r in rows(db_session, outbox.NOTICE) if r.ticket_id == ticket.id]
    built = mail.build_message_mail(db_session, row)
    assert reply_lines(built.body) == 1 and "resolved automatically." in built.body

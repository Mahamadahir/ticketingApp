"""#347: the email that opens a ticket keeps its whole body (the earlier chain folded by the thread), a reply keeps the new text
with a fallback to the whole body when that is blank, and a forward from someone inside the organisation decides the
requester (app/forwards.py). Graph is the simulated one from test_inbound_mail; the directory is the local stand-in built
from the users (Graph off). The real text Graph gives for forwards is not proved: the staging checklist."""

import pytest
from sqlmodel import select

from app import forwards, outbox, people_directory
from app.config import settings
from app.inbound import REPLY_ADDED, TICKET_CREATED, parse_graph_message, process_message
from app.models import NeverFollowAddress, TicketFollower, User, UserRole
from app.quoted_text import split_quoted
from app.settings_store import ACCEPTED_DOMAINS, MSP_ADDRESS, set_setting
from tests.test_inbound_mail import MAILBOX, FakeGraph, events_of, graph_message, messages_of, mail, open_ticket, tickets
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)

OUTLOOK_BLOCK = (
    "________________________________\n"
    "From: Sam Outside <sam@outside.example>\n"
    "Sent: 27 November 2026 09:00\n"
    "To: Owen Owner <owen@corp.example>\n"
    "Subject: Printer jam\n\n"
    "The printer on floor 2 is jammed again."
)


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", MAILBOX)


def person(db_session, email, name):
    user = User(email=email, display_name=name, role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    return user


def followers(db_session, ticket):
    db_session.expire_all()
    return sorted(f.email for f in db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id)).all())


INTERNAL = [{"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"}]


def forward(db_session, body, *, sender="owen@corp.example", subject="FW: Printer jam", message_id="f1", headers=INTERNAL):
    graph = FakeGraph()
    graph.add(message_id, sender=sender, subject=subject, body=body, internetMessageHeaders=headers)
    result = process_message(db_session, message_id, graph)
    db_session.commit()
    assert result.outcome == TICKET_CREATED
    return result.ticket


# ---- The body ---------------------------------------------------------------------------------------------------


def test_an_empty_forward_keeps_the_forwarded_text_and_its_box_shows_it_whole(db_session):
    graph = FakeGraph()
    graph.add("m1", subject="FW: Printer jam", body="")  # uniqueBody comes back blank
    graph.messages["m1"]["body"] = {"contentType": "text", "content": OUTLOOK_BLOCK}
    result = process_message(db_session, "m1", graph)
    db_session.commit()
    [message] = messages_of(db_session, result.ticket)
    assert message.body == OUTLOOK_BLOCK
    assert split_quoted(message.body) == (OUTLOOK_BLOCK, None)  # the marker is first: nothing is folded


def test_a_new_ticket_keeps_the_whole_email_with_the_earlier_chain_to_fold(db_session):
    graph = FakeGraph()
    graph.add("m1", body="Adding support, please help")
    whole = "Adding support, please help\n\n" + OUTLOOK_BLOCK
    graph.messages["m1"]["body"] = {"contentType": "text", "content": whole}
    result = process_message(db_session, "m1", graph)
    db_session.commit()
    [message] = messages_of(db_session, result.ticket)
    assert message.body == whole
    kept, quoted = split_quoted(message.body)
    assert kept == "Adding support, please help" and "The printer on floor 2" in quoted


def test_a_reply_keeps_the_unique_body_and_a_blank_one_falls_back_to_the_whole_body(db_session):
    ticket = open_ticket(db_session)
    graph = FakeGraph()
    graph.add("r1", subject=f"Re: [{ticket.reference}] Printer jam", body="Still stuck")
    graph.messages["r1"]["body"] = {"contentType": "text", "content": "Still stuck\n\nOn Mon, Jo wrote:\n> It is stuck"}
    assert process_message(db_session, "r1", graph).outcome == REPLY_ADDED
    graph.add("r2", subject=f"Re: [{ticket.reference}] Printer jam", body=" \n")
    graph.messages["r2"]["body"] = {"contentType": "text", "content": "Thanks, see below"}
    assert process_message(db_session, "r2", graph).outcome == REPLY_ADDED
    db_session.commit()
    bodies = [m.body for m in messages_of(db_session, ticket)]
    assert bodies[1:] == ["Still stuck", "Thanks, see below"]


def test_parse_keeps_both_bodies_and_falls_back_when_the_unique_one_is_blank():
    raw = graph_message(body="new")
    raw["body"] = {"content": "new\nold"}
    parsed = parse_graph_message(raw)
    assert (parsed.body, parsed.full_body) == ("new", "new\nold")
    raw["uniqueBody"] = {"content": "  "}
    parsed = parse_graph_message(raw)
    assert (parsed.body, parsed.full_body) == ("new\nold", "new\nold")


def test_msp_mail_still_uses_the_unique_body_only(db_session):
    from app.inbound import _body_of

    incoming = mail(body="new text")
    incoming.full_body = "new text\n\nthe whole chain"
    assert _body_of(incoming) == "new text"  # MSP mail and replies read this
    assert _body_of(incoming, opening=True) == "new text\n\nthe whole chain"


def test_gmails_forward_marker_is_recognised_but_an_empty_forward_is_shown_whole():
    gmail = "See below\n\n---------- Forwarded message ---------\nFrom: A <a@x.com>\nDate: Mon\nSubject: s\nTo: b@x.com\n\nHello"
    kept, quoted = split_quoted(gmail)
    assert kept == "See below" and quoted.startswith("---------- Forwarded message")
    bare = "---------- Forwarded message ---------\nFrom: A <a@x.com>\n\nHello"
    assert split_quoted(bare) == (bare, None)


# ---- Forwards ---------------------------------------------------------------------------------------------------


def test_an_internal_empty_forward_makes_the_original_sender_the_requester_and_the_forwarder_a_follower(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, OUTLOOK_BLOCK)
    assert ticket.requester_email == "sam@outside.example"  # an outside original sender is the requester too
    assert followers(db_session, ticket) == ["owen@corp.example"]
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert event.detail == "Forwarded by owen@corp.example; sam@outside.example is the requester."
    [message] = messages_of(db_session, ticket)
    assert message.sender == "owen@corp.example"  # who wrote to the mailbox


def test_the_original_sender_gets_the_acknowledgement(db_session, dev_mail):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, OUTLOOK_BLOCK)
    [row] = rows(db_session, outbox.ACKNOWLEDGEMENT)
    assert row.ticket_id == ticket.id and row.to_address == "sam@outside.example"


def test_an_internal_forward_with_text_makes_the_forwarder_the_requester_and_the_original_sender_a_follower(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, "Please look at this\n\n" + OUTLOOK_BLOCK)
    assert ticket.requester_email == "owen@corp.example"
    assert followers(db_session, ticket) == ["sam@outside.example"]
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert "owen@corp.example is the requester and sam@outside.example follows" in event.detail


def test_a_gmail_style_forward_is_read_too(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    body = "---------- Forwarded message ---------\nFrom: Sam <sam@outside.example>\nDate: Fri\nSubject: Printer jam\nTo: owen@corp.example\n\nHi"
    assert forward(db_session, body, subject="Fwd: Printer jam").requester_email == "sam@outside.example"


def test_an_outside_forwarder_is_unchanged(db_session):
    ticket = forward(db_session, OUTLOOK_BLOCK, sender="stranger@outside.example")
    assert ticket.requester_email == "stranger@outside.example"
    assert followers(db_session, ticket) == [] and events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED) == []


def test_a_disabled_account_is_not_internal(db_session):
    user = person(db_session, "owen@corp.example", "Owen Owner")
    user.is_active = False
    db_session.add(user)
    db_session.commit()
    assert forward(db_session, OUTLOOK_BLOCK).requester_email == "owen@corp.example"


def test_a_linked_address_of_an_internal_person_counts(db_session):
    from app.models import LinkedIdentity

    main = person(db_session, "owen.owner@corp.example", "Owen Owner")
    db_session.add(LinkedIdentity(main_user_id=main.id, email="owen@corp.example", status="active", method="admin"))
    db_session.commit()
    assert people_directory.is_internal(db_session, "owen@corp.example")
    assert forward(db_session, OUTLOOK_BLOCK).requester_email == "sam@outside.example"


def test_a_name_only_from_line_resolves_through_the_directory_with_exactly_one_match(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    person(db_session, "pat.jones@corp.example", "Pat Jones")
    body = OUTLOOK_BLOCK.replace("Sam Outside <sam@outside.example>", "Pat Jones")
    assert forward(db_session, body).requester_email == "pat.jones@corp.example"


@pytest.mark.parametrize("how_many", [0, 2])
def test_a_name_that_matches_none_or_two_people_falls_back_to_the_forwarder_with_an_event(db_session, how_many):
    person(db_session, "owen@corp.example", "Owen Owner")
    for n in range(how_many):
        person(db_session, f"pat{n}@corp.example", "Pat Jones")
    body = OUTLOOK_BLOCK.replace("Sam Outside <sam@outside.example>", "Pat Jones")
    ticket = forward(db_session, body)
    assert ticket.requester_email == "owen@corp.example" and followers(db_session, ticket) == []
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert "could not be read" in event.detail


def test_the_mailbox_the_msp_and_a_never_follow_address_as_original_sender_fall_back(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    set_setting(db_session, MSP_ADDRESS, "msp@vendor.example")
    db_session.add(NeverFollowAddress(email="list@corp.example", reason="a list", added_by="a@corp.example"))
    db_session.commit()
    for n, address in enumerate([MAILBOX, "msp@vendor.example", "list@corp.example"]):
        body = OUTLOOK_BLOCK.replace("sam@outside.example", address)
        ticket = forward(db_session, body, message_id=f"f{n}")
        assert ticket.requester_email == "owen@corp.example", address
        [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
        assert "could not be read" in event.detail


def test_a_full_follower_list_does_not_stop_the_ticket(db_session, monkeypatch):
    from app import followers as followers_module

    person(db_session, "owen@corp.example", "Owen Owner")
    monkeypatch.setattr(followers_module, "MAX_EXTERNAL_FOLLOWERS", 0)
    ticket = forward(db_session, "Look\n\n" + OUTLOOK_BLOCK)
    assert ticket.requester_email == "owen@corp.example" and followers(db_session, ticket) == []


def test_only_a_forward_subject_with_a_header_block_counts(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    assert forward(db_session, OUTLOOK_BLOCK, subject="Printer jam").requester_email == "owen@corp.example"  # no FW:
    quote = "Hello\n\nOn Mon, 17 Nov 2026 at 10:15, Sam <sam@outside.example> wrote:\n> hi"
    assert forward(db_session, quote, message_id="f2").requester_email == "owen@corp.example"  # a reply's quote, no From: block


def test_a_forward_that_matches_an_existing_ticket_is_a_reply_as_now(db_session):
    person(db_session, "jo@example.com", "Jo")
    ticket = open_ticket(db_session)
    graph = FakeGraph()
    graph.add("r1", sender="jo@example.com", subject=f"FW: [{ticket.reference}] Printer jam", body="")
    graph.messages["r1"]["body"] = {"contentType": "text", "content": OUTLOOK_BLOCK}
    assert process_message(db_session, "r1", graph).outcome == REPLY_ADDED
    assert len(tickets(db_session)) == 1 and ticket.requester_email == "jo@example.com"


def test_the_directory_failing_is_not_a_lost_mail(db_session, monkeypatch):
    def broken(*args, **kwargs):
        raise people_directory.DirectoryError("down")

    monkeypatch.setattr(people_directory, "internal_person", broken)
    ticket = forward(db_session, OUTLOOK_BLOCK)
    assert ticket.requester_email == "owen@corp.example"
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert "could not be asked" in event.detail


def test_with_graph_on_the_directory_is_asked_for_the_address_and_the_name(db_session, monkeypatch):
    import app.graph_client as graph_module

    class Directory:
        def search_users(self, text, limit=25):
            return [
                {"id": "1", "displayName": "Owen Owner", "mail": "owen@corp.example", "accountEnabled": True, "userType": "Member"},
                {"id": "2", "displayName": "Gus Guest", "mail": "gus@corp.example", "accountEnabled": True, "userType": "Guest"},
            ]

    monkeypatch.setattr(people_directory, "_graph_on", lambda: True)
    monkeypatch.setattr(graph_module, "graph_client", Directory())
    assert people_directory.is_internal(db_session, "owen@corp.example")
    assert not people_directory.is_internal(db_session, "gus@corp.example")  # a guest
    assert [p.mail for p in people_directory.people_named(db_session, "owen owner")] == ["owen@corp.example"]
    assert people_directory.people_named(db_session, "Gus Guest") == []


def test_reading_the_from_line():
    read = forwards.read_forward
    assert read("FW: x", OUTLOOK_BLOCK).from_text == "Sam Outside <sam@outside.example>"
    assert read("Fwd: x", "Note\n\n" + OUTLOOK_BLOCK).above == "Note"
    assert read("FW: x", OUTLOOK_BLOCK).above == ""
    assert read("Re: x", OUTLOOK_BLOCK) is None
    assert forwards._ADDRESS.search("Sam [mailto:sam@outside.example]").group(0) == "sam@outside.example"


def test_msp_mail_keeps_a_blank_unique_body_blank_while_a_reply_falls_back():
    from app.inbound import _body_of
    from app.intake import NO_TEXT

    raw = graph_message(body="")
    raw["body"] = {"content": "the whole chain"}
    parsed = parse_graph_message(raw)
    assert _body_of(parsed) == "the whole chain"  # a reply
    assert _body_of(parsed, msp=True) == NO_TEXT


def test_a_refused_follower_is_said_in_the_event(db_session, monkeypatch):
    from app import followers as followers_module

    person(db_session, "owen@corp.example", "Owen Owner")
    monkeypatch.setattr(followers_module, "MAX_EXTERNAL_FOLLOWERS", 0)
    ticket = forward(db_session, "Look\n\n" + OUTLOOK_BLOCK)
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert "could not be added as a follower" in event.detail


def test_forwarding_ones_own_mail_from_another_linked_address_changes_nothing(db_session):
    from app.models import LinkedIdentity

    main = person(db_session, "owen@corp.example", "Owen Owner")
    db_session.add(LinkedIdentity(main_user_id=main.id, email="owen@alt.example", status="active", method="admin"))
    db_session.commit()
    body = OUTLOOK_BLOCK.replace("sam@outside.example", "owen@alt.example")
    ticket = forward(db_session, body)
    assert ticket.requester_email == "owen@corp.example" and followers(db_session, ticket) == []


def test_the_directory_is_asked_before_the_claim_and_never_inside_ingest(db_session, monkeypatch):
    from app.models import InboundMail

    person(db_session, "owen@corp.example", "Owen Owner")
    asked = []
    real = people_directory._rows_for

    def spy(session, text):
        asked.append(session.exec(select(InboundMail)).all())  # the claim is a row: none may exist yet
        return real(session, text)

    monkeypatch.setattr(people_directory, "_rows_for", spy)
    forward(db_session, OUTLOOK_BLOCK.replace("Sam Outside <sam@outside.example>", "Owen Owner"))
    assert asked and all(rows_ == [] for rows_ in asked)

    asked.clear()
    from app.inbound import ingest_mail

    ingest_mail(db_session, mail("f9", sender="owen@corp.example", subject="FW: x", body=OUTLOOK_BLOCK))
    assert asked == []  # ingest_mail makes no directory call: the decision is passed in


def test_a_re_fw_subject_is_not_a_forward():
    assert forwards.read_forward("RE: FW: Printer jam", OUTLOOK_BLOCK) is None
    assert forwards.read_forward("Re: Fwd: Printer jam", OUTLOOK_BLOCK) is None


def test_the_address_in_angle_brackets_wins_over_a_display_name_that_looks_like_one(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    body = OUTLOOK_BLOCK.replace("Sam Outside <sam@outside.example>", "it-director@corp.example <x@evil.example>")
    assert forward(db_session, body).requester_email == "x@evil.example"
    assert forwards.original_address(db_session, "Sam [mailto:sam@outside.example]") == ("sam@outside.example", "")


def test_a_never_follow_or_flooding_sender_makes_no_directory_call(db_session, monkeypatch):
    person(db_session, "owen@corp.example", "Owen Owner")
    db_session.add(NeverFollowAddress(email="owen@corp.example", reason="x", added_by="a@corp.example"))
    db_session.commit()
    asked = []
    monkeypatch.setattr(people_directory, "_rows_for", lambda session, text: asked.append(text) or [])
    graph = FakeGraph()
    graph.add("n1", sender="owen@corp.example", subject="FW: x", body=OUTLOOK_BLOCK)
    process_message(db_session, "n1", graph)
    assert asked == []


# ---- Only mail Exchange authenticated as internal (#356) ----------------------------------------------------------

DOMAIN = MAILBOX.split("@")[1]


def _not_applied(db_session, ticket, requester="owen@corp.example"):
    assert ticket.requester_email == requester
    assert followers(db_session, ticket) == []
    (event,) = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert "not authenticated as internal" in event.detail


def _verdict(result):
    return [{"name": "Authentication-Results", "value": f"mx.microsoft.com 1; spf=pass; dkim=pass; {result}"}]


def test_an_internal_forward_with_the_stamp_applies_the_rules(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    assert forward(db_session, OUTLOOK_BLOCK).requester_email == "sam@outside.example"


@pytest.mark.parametrize("value", ["Anonymous", "External", "Partner"])
def test_the_forward_without_the_internal_stamp_is_an_outside_forward_with_an_event(db_session, value):
    person(db_session, "owen@corp.example", "Owen Owner")
    headers = [{"name": "X-MS-Exchange-Organization-AuthAs", "value": value}]
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, headers=headers))


def test_no_stamp_and_no_verdict_is_an_outside_forward_with_an_event(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, headers=[]))


def test_a_stamp_further_down_the_headers_does_not_count(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    headers = [
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"},
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"},
    ]
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, headers=headers))


def test_the_fallback_accepts_a_compauth_pass_from_exchange_for_the_mailbox_domain(db_session):
    person(db_session, f"owen@{DOMAIN}", "Owen Owner")
    ticket = forward(db_session, OUTLOOK_BLOCK, sender=f"owen@{DOMAIN}", headers=_verdict("compauth=pass reason=100"))
    assert ticket.requester_email == "sam@outside.example"


@pytest.mark.parametrize("result", ["compauth=softpass reason=202", "compauth=fail reason=000", "spf=pass"])
def test_the_fallback_refuses_a_softpass_a_fail_or_no_compauth(db_session, result):
    person(db_session, f"owen@{DOMAIN}", "Owen Owner")
    ticket = forward(db_session, OUTLOOK_BLOCK, sender=f"owen@{DOMAIN}", headers=_verdict(result))
    _not_applied(db_session, ticket, f"owen@{DOMAIN}")


def test_the_fallback_ignores_a_verdict_not_written_by_exchange(db_session):
    person(db_session, f"owen@{DOMAIN}", "Owen Owner")
    headers = [{"name": "Authentication-Results", "value": "evil.example 1; compauth=pass reason=100"}]
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, sender=f"owen@{DOMAIN}", headers=headers), f"owen@{DOMAIN}")


def test_the_fallback_refuses_another_domain_until_it_is_accepted(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    headers = _verdict("compauth=pass reason=100")
    set_setting(db_session, ACCEPTED_DOMAINS, "other.example")  # set, so the mailbox's own domain is no longer the default
    db_session.commit()
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, headers=headers))
    set_setting(db_session, ACCEPTED_DOMAINS, "Other.example, corp.example")
    db_session.commit()
    assert forward(db_session, OUTLOOK_BLOCK, headers=headers, message_id="f2").requester_email == "sam@outside.example"


def test_a_present_non_internal_stamp_is_not_rescued_by_the_fallback(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    set_setting(db_session, ACCEPTED_DOMAINS, "corp.example")
    db_session.commit()
    headers = [{"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"}] + _verdict("compauth=pass")
    _not_applied(db_session, forward(db_session, OUTLOOK_BLOCK, headers=headers))


def test_forwarding_ones_own_mail_without_the_stamp_gets_no_event(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    body = OUTLOOK_BLOCK.replace("Sam Outside <sam@outside.example>", "Owen Owner <owen@corp.example>")
    ticket = forward(db_session, body, headers=[])
    assert ticket.requester_email == "owen@corp.example"
    assert events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED) == []


def test_ordinary_mail_is_unchanged_without_the_stamp(db_session):
    graph = FakeGraph()
    graph.add("o1", sender="owen@corp.example", subject="Printer jam", body="It is stuck")
    result = process_message(db_session, "o1", graph)
    db_session.commit()
    assert result.outcome == TICKET_CREATED and result.ticket.requester_email == "owen@corp.example"
    assert events_of(db_session, result.ticket, forwards.EVENT_MAIL_FORWARDED) == []

"""#359: "Treat as empty" rules and the forwarder's own signature, when the forward rules decide whether a forward has text above
the forwarded block (app/treat_as_empty.py, app/forwards.py). The matching is the Thanks rules' own (no regular expressions);
the admin page is on the Thanks rules pattern."""

import pytest
from sqlmodel import select

from app import forwards, people_directory, treat_as_empty
from app.models import AuditLog, TreatAsEmptyRule
from app.thanks_rules import ThanksRuleError
from tests.conftest import login_as
from tests.test_forward_mail import INTERNAL, OUTLOOK_BLOCK, events_of, followers, forward, mailbox, messages_of, person  # noqa: F401
from tests.test_inbound_mail import MAILBOX  # noqa: F401

SEEDED = [
    "Sent from my iPhone", "Sent from my iPad", "Sent from my * phone", "Get Outlook for iOS", "Get Outlook for Android",
    "Sent from Outlook for *", "Sent from Mail for Windows",
]


@pytest.fixture
def seeded(db_session):
    for pattern in SEEDED:
        treat_as_empty.add_rule(db_session, "equals", pattern, "system")
    db_session.commit()


def above(text):
    return text + "\n\n" + OUTLOOK_BLOCK


def requester_of(db_session, body, message_id="f1"):
    return forward(db_session, body, message_id=message_id).requester_email


# ---- The decision -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["Sent from my iPhone", "Get Outlook for iOS", "Sent from Outlook for Android", "Sent from my Galaxy phone"])
def test_a_phone_sign_off_alone_above_the_block_is_empty(db_session, seeded, line):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, above(line))
    assert ticket.requester_email == "sam@outside.example"
    assert followers(db_session, ticket) == ["owen@corp.example"]
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert event.detail == "Forwarded by owen@corp.example; sam@outside.example is the requester (1 line treated as empty)."


def test_the_stored_body_is_unchanged(db_session, seeded):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, above("Sent from my iPhone"))
    [message] = messages_of(db_session, ticket)
    assert message.body.startswith("Sent from my iPhone")


def test_the_forwarders_signature_alone_is_empty(db_session, monkeypatch):
    import app.graph_client as graph_module

    class Directory:
        def search_users(self, text, limit=25):
            return [{
                "id": "1", "displayName": "Owen Owner", "mail": "owen@corp.example", "jobTitle": "IT Manager",
                "department": "Technology", "accountEnabled": True, "userType": "Member",
            }]

    monkeypatch.setattr(people_directory, "_graph_on", lambda: True)
    monkeypatch.setattr(graph_module, "graph_client", Directory())
    signature = "Owen Owner\nIT Manager\nTechnology\n[Vardags]\nTel: 020 7946 0000\nFax: 020 7946 0001\nWeb: www.example.com\n"
    ticket = forward(db_session, above(signature))
    assert ticket.requester_email == "sam@outside.example"
    [event] = events_of(db_session, ticket, forwards.EVENT_MAIL_FORWARDED)
    assert event.detail.endswith("(7 lines treated as empty).")

    other = forward(db_session, above(signature + "\nCan you look at this?"), message_id="f2")
    assert other.requester_email == "owen@corp.example"  # signature plus a typed line: with text
    assert followers(db_session, other) == ["sam@outside.example"]
    [event] = events_of(db_session, other, forwards.EVENT_MAIL_FORWARDED)
    assert event.detail == (
        "Forwarded by owen@corp.example with a message; owen@corp.example is the requester and sam@outside.example follows "
        "(7 lines treated as empty)."
    )


def test_a_signature_line_of_someone_else_still_counts(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    assert requester_of(db_session, above("Pat Other")) == "owen@corp.example"


def test_a_typed_line_beside_a_sign_off_is_with_text(db_session, seeded):
    person(db_session, "owen@corp.example", "Owen Owner")
    ticket = forward(db_session, above("Can you look at this?\n\nSent from my iPhone"))
    assert ticket.requester_email == "owen@corp.example"


def test_a_disabled_rule_does_not_count_and_a_new_admin_rule_does(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    rule = treat_as_empty.add_rule(db_session, "equals", "Sent from my iPhone", "admin@example.com")
    treat_as_empty.set_rule_enabled(db_session, rule.id, False)
    db_session.commit()
    assert requester_of(db_session, above("Sent from my iPhone")) == "owen@corp.example"
    treat_as_empty.add_rule(db_session, "starts_with", "Typed on my*", "admin@example.com")
    db_session.commit()
    assert requester_of(db_session, above("Typed on my watch, sorry"), message_id="f2") == "sam@outside.example"


def test_contact_labels_are_a_constant_and_only_when_they_start_the_line(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    assert requester_of(db_session, above("Mobile: 07700 900000")) == "sam@outside.example"
    assert requester_of(db_session, above("Please phone: me later"), message_id="f2") == "owen@corp.example"


def test_no_rules_and_nothing_above_is_as_before(db_session):
    person(db_session, "owen@corp.example", "Owen Owner")
    [event] = events_of(db_session, forward(db_session, OUTLOOK_BLOCK), forwards.EVENT_MAIL_FORWARDED)
    assert event.detail == "Forwarded by owen@corp.example; sam@outside.example is the requester."


# ---- The matching is the Thanks rules' helper -------------------------------------------------------------------


def test_rules_are_matched_by_the_thanks_rules_helper_with_no_regex(db_session, monkeypatch):
    from app import thanks_rules

    calls = []
    real = thanks_rules.rule_matches
    monkeypatch.setattr(treat_as_empty, "rule_matches", lambda *a: calls.append(a) or real(*a))
    treat_as_empty.add_rule(db_session, "equals", "a.b", "admin@example.com")
    treat_as_empty.add_rule(db_session, "contains", "(z+)+$", "admin@example.com")
    judged = treat_as_empty.judge_above(db_session, "a b\nAXB")
    assert calls and judged.ignored == 1 and judged.text == "AXB"  # "a.b" is the words "a b", not a pattern; "AXB" is not matched
    assert treat_as_empty.judge_above(db_session, "x" * 5000).text == "x" * 5000  # a long line is never scanned


def test_a_pattern_needs_words(db_session):
    with pytest.raises(ThanksRuleError):
        treat_as_empty.add_rule(db_session, "equals", " * !! ", "admin@example.com")
    with pytest.raises(ThanksRuleError):
        treat_as_empty.add_rule(db_session, "regex", "x", "admin@example.com")


# ---- The admin page ---------------------------------------------------------------------------------------------


def test_only_a_super_admin_sees_the_page(client, db_session):
    login_as(client, "someone@example.com")
    assert client.get("/admin/treat-as-empty").status_code == 403
    assert client.post("/admin/treat-as-empty", data={"match_type": "equals", "pattern": "x"}).status_code == 403
    assert client.post("/admin/treat-as-empty/1/delete").status_code == 403


def test_an_admin_adds_edits_disables_and_deletes_a_rule_and_each_is_audited(client, db_session):
    login_as(client, "admin@example.com")
    page = client.get("/admin/treat-as-empty")
    assert page.status_code == 200 and "No rules." in page.text and "Treat as empty" in page.text
    assert client.post(
        "/admin/treat-as-empty", data={"match_type": "starts_with", "pattern": "Typed on my*"}, follow_redirects=False
    ).status_code == 303
    rule = db_session.exec(select(TreatAsEmptyRule)).one()
    assert (rule.match_type, rule.pattern, rule.enabled, rule.created_by) == ("starts_with", "typed on my*", True, "admin@example.com")
    assert 'action="/admin/treat-as-empty/1"' in client.get("/admin/treat-as-empty").text
    assert client.post("/admin/treat-as-empty/1", data={"match_type": "equals", "pattern": "Sent via Teams"}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(TreatAsEmptyRule, 1).pattern == "sent via teams"
    assert client.post("/admin/treat-as-empty/1/disable", follow_redirects=False).status_code == 303
    assert "chip t-gray" in client.get("/admin/treat-as-empty").text
    assert client.post("/admin/treat-as-empty/1/enable", follow_redirects=False).status_code == 303
    assert client.post("/admin/treat-as-empty/1/delete", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.exec(select(TreatAsEmptyRule)).all() == []
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "treat_as_empty_rule").order_by(AuditLog.id)).all()]
    assert actions == ["added", "edited", "disabled", "enabled", "deleted"]


def test_bad_input_and_missing_rules_are_refused(client, db_session):
    login_as(client, "admin@example.com")
    assert client.post("/admin/treat-as-empty", data={"match_type": "equals", "pattern": "***"}).status_code == 400
    assert client.post("/admin/treat-as-empty", data={"match_type": "regex", "pattern": "x"}).status_code == 400
    assert client.post("/admin/treat-as-empty/9", data={"match_type": "equals", "pattern": "x"}).status_code == 404
    assert client.post("/admin/treat-as-empty/9/delete").status_code == 404
    assert client.post("/admin/treat-as-empty/9/enable").status_code == 404
    assert client.post("/admin/treat-as-empty/9/explode").status_code == 404


def test_the_page_is_in_the_mail_and_teams_menu(client, db_session):
    login_as(client, "admin@example.com")
    assert 'href="/admin/treat-as-empty"' in client.get("/admin/thanks-rules").text

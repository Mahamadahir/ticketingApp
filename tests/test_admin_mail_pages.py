"""The Mail pages of the admin overhaul (9L, part 1): Email templates, Never follow, Thanks rules, Mail intake and
Outbound mail on the admin patterns. One smoke test per page; the rules the pages show are tested where they live."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from app.inbound import ingest_mail, parse_graph_message
from app.models import GraphSubscription, InboundMail, MailSyncState, OutboundEmail, UserRole
from app.subscriptions import (
    CATCH_UP_PROBLEM,
    SUBSCRIPTION_PROBLEM,
    HealthWarning,
    mail_folder,
    subscription_resource,
)
from app.time_zones import default_zone, format_moment
from tests.conftest import login_as, set_role

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
MAIL_PAGES = (
    "admin_email_templates.html", "admin_email_template_edit.html", "admin_never_follow.html",
    "admin_thanks_rules.html", "admin_mail_intake.html", "admin_outbound_mail.html",
)


def admin(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)


def test_the_mail_pages_use_tokens_never_raw_colours():
    for name in MAIL_PAGES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|style=", text), name


def test_never_follow_lists_in_a_table_and_adds_by_the_same_form(client, db_session):
    admin(client, db_session)
    empty = client.get("/admin/never-follow").text
    assert "<h1>Never-follow list</h1>" in empty and "empty-state" in empty and "table-box" not in empty
    assert 'action="/admin/never-follow"' in empty and 'name="email"' in empty and 'name="reason"' in empty
    assert client.post("/admin/never-follow", data={"email": "all@corp.example", "reason": "List"}, follow_redirects=False).status_code == 303
    page = client.get("/admin/never-follow").text
    assert "table-box" in page and "all@corp.example" in page and "/admin/never-follow/1/remove" in page
    assert 'aria-current="page"' in page  # the admin menu marks the page


def test_thanks_rules_show_chips_an_edit_panel_and_the_old_routes(client, db_session):
    admin(client, db_session)
    empty = client.get("/admin/thanks-rules").text
    assert "No rules." in empty and "Nothing to review." in empty and 'action="/admin/thanks-rules"' in empty
    assert client.post("/admin/thanks-rules", data={"match_type": "equals", "pattern": "thanks", "max_length": "20"}, follow_redirects=False).status_code == 303
    page = client.get("/admin/thanks-rules").text
    assert "chip t-green" in page and "Enabled" in page and 'action="/admin/thanks-rules/1"' in page
    assert 'name="match_type"' in page and 'name="pattern"' in page and 'name="max_length"' in page
    assert client.post("/admin/thanks-rules/1/disable", follow_redirects=False).status_code == 303
    assert "chip t-gray" in client.get("/admin/thanks-rules").text


def test_mail_intake_shows_status_cards_and_the_last_mail_received(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/mail-intake").text
    assert "Not set up" in page and "Not run" in page and "Last mail arrived" in page and "Nothing yet" in page
    now = datetime.now(timezone.utc)
    db_session.add(InboundMail(graph_message_id="m1", sender="a@example.com", outcome="ticket_created", created_at=now))
    db_session.commit()
    page = client.get("/admin/mail-intake").text
    assert "Nothing yet" not in page and "intake-card" in page and "a@example.com" not in page


def test_outbound_mail_shows_a_state_chip_per_email_and_no_invented_actions(client, db_session):
    admin(client, db_session)
    assert "empty-state" in client.get("/admin/outbound-mail").text
    now = datetime.now(timezone.utc)
    for to, attempts, sent in (("w@example.com", 0, None), ("r@example.com", 1, None), ("g@example.com", 99, None), ("s@example.com", 1, now)):
        db_session.add(OutboundEmail(kind="acknowledgement", to_address=to, subject="s", body="b", attempts=attempts, sent_at=sent, created_at=now))
    db_session.commit()
    page = client.get("/admin/outbound-mail").text
    for chip in ("t-gray\">Waiting", "t-amber\">Retrying", "t-red\">Given up", "t-green\">Sent"):
        assert chip in page
    assert "table-box" in page and "Retry" not in page.replace("Retrying", "") and "Cancel" not in page
    waiting = client.get("/admin/outbound-mail", params={"waiting": "true"}).text
    assert "s@example.com" not in waiting and "w@example.com" in waiting and "Clear filters" in waiting


def test_outbound_mail_waiting_filter_with_nothing_waiting_says_so(client, db_session):
    admin(client, db_session)
    now = datetime.now(timezone.utc)
    db_session.add(OutboundEmail(kind="acknowledgement", to_address="s@example.com", subject="s", body="b", attempts=1, sent_at=now, created_at=now))
    db_session.commit()
    page = client.get("/admin/outbound-mail", params={"waiting": "true"}).text
    assert "Nothing waiting" in page and "No emails yet" not in page


def _intake_card(page, heading):
    """The text of the intake card with this heading."""
    match = re.search(rf"<h2 id=\"[^\"]+\">{heading}</h2>(.*?)</section>", page, re.S)
    assert match, heading
    return match.group(1)


def test_mail_intake_chips_come_from_the_warning_kind_not_its_wording(client, db_session, monkeypatch):
    admin(client, db_session)
    db_session.add(GraphSubscription(
        resource=subscription_resource(), subscription_id="s1", expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    db_session.add(MailSyncState(folder=mail_folder()))
    db_session.commit()
    page = client.get("/admin/mail-intake").text
    assert "Needs attention" not in page and "Last run failed" not in page and page.count('t-green">Healthy') == 2

    # Each kind turns only its own chip, whatever words the warning uses.
    for kind, chip, other in (
        (SUBSCRIPTION_PROBLEM, 't-amber">Needs attention', "Last run failed"),
        (CATCH_UP_PROBLEM, 't-red">Last run failed', "Needs attention"),
    ):
        monkeypatch.setattr("app.routers.admin.health_problems", lambda s, n, kind=kind: [HealthWarning(kind, "Something else entirely")])
        page = client.get("/admin/mail-intake").text
        assert chip in page and other not in page and "Something else entirely" in page


def test_mail_intake_chips_follow_real_trouble(client, db_session):
    admin(client, db_session)
    db_session.add(GraphSubscription(
        resource=subscription_resource(), subscription_id="s1", expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    db_session.add(MailSyncState(folder=mail_folder(), last_error="Graph answered 500"))
    db_session.commit()
    page = client.get("/admin/mail-intake").text
    assert 't-amber">Needs attention' in page and 't-red">Last run failed' in page


def test_last_mail_arrived_is_when_it_reached_the_mailbox_and_last_processed_is_when_it_was_recorded(client, db_session):
    admin(client, db_session)
    # A catch-up after an outage records a mail that arrived long ago.
    arrived = datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)
    raw = {
        "id": "old1", "internetMessageId": "<old1@mail.example.com>", "subject": "Old",
        "from": {"emailAddress": {"address": "jo@example.com"}},
        "uniqueBody": {"content": "Hello"}, "receivedDateTime": "2026-03-05T10:00:00.0000000Z",
    }
    ingest_mail(db_session, parse_graph_message(raw))
    db_session.commit()
    stored = db_session.exec(select(InboundMail)).one()
    assert stored.received_at == arrived and stored.created_at > arrived + timedelta(days=1)

    zone = default_zone(db_session)
    card = _intake_card(client.get("/admin/mail-intake").text, "Last mail arrived")
    before, after = card.split("Last processed")
    assert format_moment(arrived, zone) in before
    assert format_moment(stored.created_at, zone) in after and format_moment(arrived, zone) not in after


def test_last_mail_arrived_says_not_recorded_for_mail_from_before_the_time_was_kept(client, db_session):
    admin(client, db_session)
    db_session.add(InboundMail(graph_message_id="m1", sender="a@example.com", outcome="ticket_created"))
    db_session.commit()
    card = _intake_card(client.get("/admin/mail-intake").text, "Last mail arrived")
    assert "Not recorded" in card and "Nothing yet" not in card


def test_mail_intake_long_values_get_the_fit_classes_and_wrap_points(client, db_session):
    """#294: the Subscription card's resource and id are `mono-wrap` with break points after / - . @ _, and the two detail cards
    sit in their own wider grid, apart from the two number cards. Structure only; the CSS is checked in the browser."""
    admin(client, db_session)
    resource = "users/itsupport-staging@vardags.com/mailFolders('Inbox')/messages"
    db_session.add(GraphSubscription(
        resource=resource, subscription_id="3f2a1c9e-7b44-4d0e-9a51-2c8d6e0b1a77",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    db_session.add(MailSyncState(folder=mail_folder()))
    db_session.commit()
    page = client.get("/admin/mail-intake").text
    card = _intake_card(page, "Subscription")
    assert '<dd class="mono-wrap">users/<wbr>itsupport-<wbr>staging@<wbr>vardags.<wbr>com/<wbr>' in card
    assert '<dd class="mono-wrap">3f2a1c9e-<wbr>7b44-<wbr>' in card
    assert 'class="dl"' in card
    assert len(re.findall(r'<div class="intake-cards">', page)) == 1 and 'class="intake-cards intake-numbers"' in page
    detail, numbers = page.split('class="intake-cards intake-numbers"')
    assert "in-sub" in detail and "in-sync" in detail and "in-last" not in detail
    assert "in-last" in numbers and "in-wait" in numbers


def test_break_points_escapes_the_value_before_adding_break_opportunities():
    from app.templating import _break_points

    assert str(_break_points("<b>a/b</b>")) == "&lt;b&gt;a/<wbr>b&lt;/<wbr>b&gt;"

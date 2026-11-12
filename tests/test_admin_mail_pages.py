"""The Mail pages of the admin overhaul (9L, part 1): Email templates, Never follow, Thanks rules, Mail intake and
Outbound mail on the admin patterns. One smoke test per page; the rules the pages show are tested where they live."""

import re
from datetime import datetime, timezone
from pathlib import Path

from app.models import InboundMail, OutboundEmail, UserRole
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
    assert "Not set up" in page and "Not run" in page and "Last mail received" in page and "Nothing yet" in page
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

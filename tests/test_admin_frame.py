"""The admin frame (9H, #212): the grouped admin menu beside every admin page (app/navigation.py, ADMIN_GROUPS) and the
admin home (app/admin_home.py): its "Needs attention" lines and its area cards, each limited to the pages the viewer
may open."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import outage
from app.admin_home import BLOB_DELETE_FAILURES_TO_SHOW, area_cards, needs_attention
from app.models import (
    AuditLog,
    GraphSubscription,
    OutboundEmail,
    PendingBlobDelete,
    ScheduledJob,
    Setting,
    TeamsPost,
    TicketStatus,
    User,
    UserPermission,
    UserRole,
)
from app.navigation import ADMIN_GROUPS, ADMIN_PAGES, current_admin_page
from app.scheduled_jobs import SCHEDULED_JOBS
from app.subscriptions import subscription_resource
from app.teams import MAX_ATTEMPTS
from tests.conftest import login_as, set_role
from tests.test_triage import agent, new_ticket

ZONE = "Europe/London"
EVERY_PAGE = frozenset(ADMIN_PAGES)
JOB = SCHEDULED_JOBS[0].name


def super_admin(client, db_session, email="boss@example.com") -> int:
    user_id = login_as(client, email)
    set_role(db_session, user_id, UserRole.SUPER_ADMIN)
    return user_id


def grant(db_session, user_id: int, permission: str) -> None:
    db_session.add(UserPermission(user_id=user_id, permission=permission))
    db_session.commit()


def admin_menu(page) -> dict[str, list[str]]:
    """Group title -> the labels under it, from the admin menu as drawn."""
    nav = re.search(r'<nav class="admin-nav" aria-label="Admin">(.*?)</nav>', page.text, re.S)
    assert nav, "no admin menu on the page"
    groups = re.findall(r'<p class="admin-group-title"[^>]*>([^<]+)</p>\s*<ul[^>]*>(.*?)</ul>', nav.group(1), re.S)
    return {title: re.findall(r'class="admin-link"[^>]*>([^<]+)</a>', items) for title, items in groups}


def current_admin_links(page) -> list[str]:
    return re.findall(r'class="admin-link[^"]*" aria-current="page">([^<]+)</a>', page.text)


# ---- The menu --------------------------------------------------------------------------------------------------


def test_a_super_admin_gets_every_group_and_page_in_order(client, db_session):
    super_admin(client, db_session)
    page = client.get("/admin")
    assert page.status_code == 200
    assert admin_menu(page) == {title: [p.label for p in pages] for title, pages in ADMIN_GROUPS}
    assert list(admin_menu(page)) == ["People", "Tickets", "Mail and Teams", "System", "Logs"]
    assert 'href="/admin" class="admin-link admin-home-link" aria-current="page">Admin home</a>' in page.text


@pytest.mark.parametrize("permission,group,label", [
    ("manage_request_templates", "Tickets", "Request types"),
    ("data_protection", "System", "Data protection"),
])
def test_an_extra_permission_shows_only_the_page_it_opens(client, db_session, permission, group, label):
    grant(db_session, agent(client, db_session), permission)
    page = client.get("/admin")
    assert page.status_code == 200
    assert admin_menu(page) == {group: [label]}
    # The same menu on the page itself, where the item is the current one.
    own = client.get(ADMIN_PAGES[{"Request types": "request-types", "Data protection": "data-protection"}[label]].href)
    assert admin_menu(own) == {group: [label]} and current_admin_links(own) == [label]


def test_both_permissions_show_both_pages(client, db_session):
    agent_id = agent(client, db_session)
    grant(db_session, agent_id, "manage_request_templates")
    grant(db_session, agent_id, "data_protection")
    assert admin_menu(client.get("/admin")) == {"Tickets": ["Request types"], "System": ["Data protection"]}


def test_a_plain_agent_gets_no_admin_home_and_no_admin_menu(client, db_session):
    agent(client, db_session)
    assert client.get("/admin").status_code == 403
    assert 'class="admin-nav"' not in client.get("/tickets").text


def test_an_end_user_cannot_open_the_admin_home(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.END_USER)
    assert client.get("/admin").status_code == 403


def test_the_admin_menu_is_only_on_admin_pages(client, db_session):
    super_admin(client, db_session)
    assert 'class="admin-nav"' not in client.get("/tickets").text
    assert 'class="admin-nav"' not in client.get("/reports").text


@pytest.mark.parametrize("key", sorted(ADMIN_PAGES))
def test_every_admin_page_renders_in_the_frame_with_its_item_current(client, db_session, key):
    super_admin(client, db_session)
    page = client.get(ADMIN_PAGES[key].href)
    assert page.status_code == 200, key
    assert admin_menu(page) == {title: [p.label for p in pages] for title, pages in ADMIN_GROUPS}
    assert current_admin_links(page) == [ADMIN_PAGES[key].label]
    assert page.text.count('id="main-content"') == 1


def test_the_current_page_follows_the_address():
    assert current_admin_page("/admin") == "home" and current_admin_page("/admin/") == "home"
    assert current_admin_page("/admin/users") == "users"
    assert current_admin_page("/admin/links/preview") == "users"  # reviewing a linked identity is part of Users
    assert current_admin_page("/admin/data-protection/anonymise") == "data-protection"
    assert current_admin_page("/admin/request-types/questions/3") == "request-types"
    assert current_admin_page("/admin/usersx") is None


def test_the_main_navigation_keeps_admin_current_on_admin_pages(client, db_session):
    super_admin(client, db_session)
    page = client.get("/admin/settings")
    assert re.findall(r'class="navlink" aria-current="page">.*?<span class="navlink-label">([^<]+)</span>', page.text, re.S) == ["Admin"]


# ---- Needs attention -------------------------------------------------------------------------------------------


def lines(db_session, allowed=EVERY_PAGE, now=None):
    return needs_attention(db_session, allowed, now or datetime.now(timezone.utc), ZONE)


def texts(db_session, allowed=EVERY_PAGE, now=None) -> list[str]:
    return [line.text for line in lines(db_session, allowed, now)]


def test_nothing_needs_attention_on_a_quiet_system(client, db_session):
    assert lines(db_session) == []
    super_admin(client, db_session)
    page = client.get("/admin")
    assert "Nothing needs attention." in page.text and 'class="attention-list"' not in page.text


def test_outage_mode_on_is_shown(db_session):
    db_session.add(Setting(key=outage.OUTAGE_KEY, value=json.dumps({"since": "2026-11-11T09:00:00+00:00", "by": "boss@example.com"})))
    db_session.commit()
    [line] = lines(db_session)
    assert line.tone == "red" and line.href == "/admin/outage"
    assert line.text.startswith("Outage mode is on since") and "switched on by boss@example.com" in line.text
    assert lines(db_session, frozenset({"users"})) == []


def test_a_failing_job_is_shown(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(ScheduledJob(name=JOB, next_run_at=now + timedelta(minutes=5), failures_in_a_row=2, last_error="boom"))
    db_session.commit()
    [line] = lines(db_session, now=now)
    assert line.tag == "Failing" and JOB in line.text and line.href == "/admin/scheduled-jobs"


def test_an_overdue_job_is_shown_and_one_on_time_is_not(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(ScheduledJob(name=JOB, next_run_at=now - timedelta(minutes=1)))
    db_session.commit()
    assert lines(db_session, now=now) == []  # due a minute ago is within the late setting
    assert [line.tag for line in lines(db_session, now=now + timedelta(hours=1))] == ["Overdue"]


def test_a_job_whose_lease_ran_out_is_shown_as_stuck(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(ScheduledJob(name=JOB, next_run_at=now, lease_owner="w1", lease_expires_at=now - timedelta(minutes=1)))
    db_session.commit()
    assert [line.tag for line in lines(db_session, now=now)] == ["Stuck"]


def test_a_job_no_longer_registered_is_ignored(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(ScheduledJob(name="retired-job", next_run_at=now - timedelta(days=3), failures_in_a_row=9))
    db_session.commit()
    assert lines(db_session, now=now) == []


def test_outbound_mail_waiting_to_retry_and_given_up_are_shown(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(OutboundEmail(kind="reply", to_address="a@example.com", attempts=1, last_error="Graph 503"))
    db_session.add(OutboundEmail(kind="reply", to_address="b@example.com", attempts=MAX_ATTEMPTS))
    db_session.add(OutboundEmail(kind="reply", to_address="c@example.com", attempts=MAX_ATTEMPTS))
    db_session.add(OutboundEmail(kind="reply", to_address="d@example.com", attempts=2, sent_at=now))  # sent in the end
    db_session.add(OutboundEmail(kind="reply", to_address="e@example.com"))  # queued, not tried yet
    db_session.commit()
    assert texts(db_session) == [
        f"2 outbound emails were given up after {MAX_ATTEMPTS} tries.",
        "1 outbound email is waiting to retry after an error.",
    ]
    assert {line.href for line in lines(db_session)} == {"/admin/outbound-mail?waiting=true"}
    assert lines(db_session, frozenset({"data-protection"})) == []


def test_teams_posts_given_up_are_shown(db_session):
    db_session.add(TeamsPost(kind="new_ticket", attempts=MAX_ATTEMPTS))
    db_session.add(TeamsPost(kind="new_ticket", attempts=1))
    db_session.commit()
    [line] = lines(db_session)
    assert line.text == f"1 Teams post was given up after {MAX_ATTEMPTS} tries." and line.href is None


def test_mail_intake_problems_are_shown(db_session):
    db_session.add(GraphSubscription(resource=subscription_resource()))
    db_session.commit()
    [line] = lines(db_session)
    assert line.text == "Mail subscription does not exist yet." and line.href == "/admin/mail-intake"


def test_stored_files_that_keep_failing_to_delete_are_shown(db_session):
    db_session.add(PendingBlobDelete(blob_key="a", attempts=BLOB_DELETE_FAILURES_TO_SHOW - 1))
    db_session.commit()
    assert lines(db_session) == []
    db_session.add(PendingBlobDelete(blob_key="b", attempts=BLOB_DELETE_FAILURES_TO_SHOW))
    db_session.commit()
    [line] = lines(db_session, frozenset({"data-protection"}))
    assert line.text.startswith("1 stored file could not be deleted") and line.href == "/admin/data-protection"


def test_resolved_tickets_waiting_for_category_review_are_shown(db_session):
    open_one = new_ticket(db_session)
    open_one.needs_category_review = True
    resolved = new_ticket(db_session)
    resolved.needs_category_review = True
    resolved.status = TicketStatus.RESOLVED
    db_session.add(open_one)
    db_session.add(resolved)
    db_session.commit()
    [line] = lines(db_session)
    assert line.text == '1 resolved ticket triaged as "Other" needs a proper category.' and line.href == "/admin/category-review"


def test_lines_render_on_the_page(client, db_session):
    super_admin(client, db_session)
    db_session.add(OutboundEmail(kind="reply", to_address="a@example.com", attempts=MAX_ATTEMPTS))
    db_session.commit()
    page = client.get("/admin").text
    assert 'class="attention-list"' in page and "Nothing needs attention." not in page
    assert '<span class="chip t-red">Given up</span>' in page and 'href="/admin/outbound-mail?waiting=true"' in page


def test_an_agent_with_one_permission_sees_only_its_lines(client, db_session):
    grant(db_session, agent(client, db_session), "data_protection")
    db_session.add(OutboundEmail(kind="reply", to_address="a@example.com", attempts=MAX_ATTEMPTS))
    db_session.add(PendingBlobDelete(blob_key="b", attempts=BLOB_DELETE_FAILURES_TO_SHOW))
    db_session.commit()
    page = client.get("/admin").text
    assert "could not be deleted" in page and "outbound email" not in page


# ---- Area cards ------------------------------------------------------------------------------------------------


def stats(cards) -> dict[str, dict[str, str]]:
    return {card.title: {s.label: s.value for s in card.stats} for card in cards}


def test_a_super_admin_gets_a_card_per_group_with_its_links(db_session):
    cards = area_cards(db_session, EVERY_PAGE, datetime.now(timezone.utc), ZONE)
    assert [card.title for card in cards] == [title for title, _ in ADMIN_GROUPS]
    for card, (_title, pages) in zip(cards, ADMIN_GROUPS):
        assert [link.href for link in card.links] == [page.href for page in pages]


def test_the_counts_are_real(client, db_session):
    super_admin(client, db_session)
    now = datetime.now(timezone.utc)
    retired = User(email="gone@example.com", display_name="Gone", is_active=False)
    db_session.add(retired)
    db_session.add(OutboundEmail(kind="reply", to_address="a@example.com"))
    db_session.add(PendingBlobDelete(blob_key="k"))
    db_session.add(ScheduledJob(name=JOB, next_run_at=now + timedelta(minutes=5), failures_in_a_row=1))
    db_session.add(AuditLog(entity_type="user", entity_id=1, actor_email="x@example.com", action="a", created_at=now))
    db_session.add(AuditLog(entity_type="user", entity_id=1, actor_email="x@example.com", action="b", created_at=now - timedelta(days=2)))
    db_session.commit()
    today = db_session.exec(select(AuditLog).where(AuditLog.created_at >= now - timedelta(days=1))).all()
    active = len(db_session.exec(select(User).where(User.is_active.is_(True))).all())
    found = stats(area_cards(db_session, EVERY_PAGE, now, ZONE))
    assert found["People"] == {"active users": str(active), "inactive": "1"}
    assert found["Mail and Teams"]["emails waiting"] == "1"
    assert found["System"] == {"jobs healthy": "0", "not healthy": "1", "files waiting to be deleted": "1"}
    assert found["Logs"]["audit entries today"] == str(len(today))


def test_cards_hold_only_what_the_viewer_may_open(db_session):
    cards = area_cards(db_session, frozenset({"data-protection"}), datetime.now(timezone.utc), ZONE)
    assert [card.title for card in cards] == ["System"]
    assert [link.label for link in cards[0].links] == ["Data protection"]
    assert [s.label for s in cards[0].stats] == ["files waiting to be deleted"]
    cards = area_cards(db_session, frozenset({"request-types"}), datetime.now(timezone.utc), ZONE)
    assert [(card.title, [s.label for s in card.stats]) for card in cards] == [("Tickets", ["request types"])]

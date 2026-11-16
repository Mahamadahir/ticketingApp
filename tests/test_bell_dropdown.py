"""The bell's dropdown (#237): the latest five notifications this person can see, loaded when it opens."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Notification, Ticket, UserRole
from tests.conftest import create_team, login_as, set_role
from tests.test_bell_access import bob_with_two
from tests.test_notifications import notifications_for


HX = {"HX-Request": "true"}  # what the dropdown sends: the rows only, no frame


def agent_with(client, db_session, count, **extra):
    """A signed-in agent with `count` ticketless notifications, "Note 1" the oldest."""
    agent_id = login_as(client, "ann@example.com", "Ann Agent")
    set_role(db_session, agent_id, UserRole.AGENT)
    start = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    for n in range(1, count + 1):
        db_session.add(Notification(user_id=agent_id, kind="assigned", text=f"Note {n}", created_at=start + timedelta(hours=n), **extra))
    db_session.commit()
    return agent_id


def test_the_dropdown_shows_the_latest_five_newest_first(client, db_session):
    agent_with(client, db_session, 7)
    texts = re.findall(r"Note \d+", client.get("/notifications/latest", headers=HX).text)
    assert texts == ["Note 7", "Note 6", "Note 5", "Note 4", "Note 3"]  # the 6th and 7th oldest are not shown


def test_the_dropdown_leaves_out_what_the_page_hides(client, db_session):
    agent_id = agent_with(client, db_session, 2)
    db_session.add(Notification(user_id=agent_id, kind="assigned", text="Teams only", bell=False))
    db_session.commit()
    assert "Teams only" not in client.get("/notifications/latest", headers=HX).text
    assert "Teams only" not in client.get("/notifications").text


def test_a_notification_about_a_ticket_the_person_lost_is_not_in_the_dropdown(client, db_session):
    bob, secret, kept = bob_with_two(client, db_session)
    assert "Secret subject" in client.get("/notifications/latest", headers=HX).text
    moved = db_session.get(Ticket, secret.id)
    moved.team_id = create_team(db_session, "Network").id
    db_session.add(moved)
    db_session.commit()
    latest = client.get("/notifications/latest", headers=HX).text
    assert "Secret subject" not in latest and "Kept subject" in latest
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_unread_rows_are_marked_and_opening_the_dropdown_marks_nothing_read(client, db_session):
    agent_id = agent_with(client, db_session, 2)
    read = db_session.exec(select(Notification).where(Notification.text == "Note 1")).one()
    read.read_at = datetime.now(timezone.utc)
    db_session.add(read)
    db_session.commit()

    html = client.get("/notifications/latest", headers=HX).text
    unread_row = re.search(r'<button[^>]*class="bell-row bell-unread".*?</button>', html, re.S).group(0)
    assert "Note 2" in unread_row and "Unread: " in unread_row  # marked in words as well as by weight and dot
    read_row = re.search(r'<button[^>]*class="bell-row"[^>]*>.*?</button>', html, re.S).group(0)
    assert "Note 1" in read_row and "Unread" not in read_row

    client.get("/notifications/latest", headers=HX)
    client.get("/tickets")
    assert [n.read_at is None for n in notifications_for(db_session, "ann@example.com")] == [False, True]
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_each_row_is_a_post_to_the_existing_open_route_and_view_more_goes_to_the_page(client, db_session):
    agent_with(client, db_session, 2)
    html = client.get("/notifications/latest", headers=HX).text
    ids = [n.id for n in notifications_for(db_session, "ann@example.com")]
    forms = re.findall(r'<form method="post" action="(/notifications/\d+/open)">', html)
    assert forms == [f"/notifications/{i}/open" for i in reversed(ids)]
    assert "<a " not in html and 'href="/notifications/' not in html  # a row is never a GET link that could mark anything
    page = client.get("/tickets").text
    assert re.search(r'<a class="bell-more" href="/notifications">View more</a>', page)
    opened = client.post(forms[0], follow_redirects=False)  # the page's own route: marks read, then goes on
    assert opened.status_code == 303 and opened.headers["location"] == "/notifications"
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_the_empty_state_says_so(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.AGENT)
    html = client.get("/notifications/latest", headers=HX).text
    assert "No notifications yet" in html and "bell-row" not in html
    assert 'href="/notifications">View more' in client.get("/tickets").text  # the link is in the panel whatever it holds


def test_the_partial_has_no_frame_and_is_never_cached(client, db_session):
    agent_with(client, db_session, 1)
    response = client.get("/notifications/latest", headers={"HX-Request": "true"})
    assert response.headers["cache-control"] == "no-store"
    assert "<html" not in response.text and "<nav" not in response.text and "<header" not in response.text
    assert "Note 1" in response.text


def test_the_dropdown_is_for_signed_in_people_only(client, db_session):
    assert client.get("/notifications/latest", follow_redirects=False).status_code in (303, 307, 401, 403)


def test_the_bell_is_a_details_in_both_frames_with_the_poll_contract_on_its_summary(client, db_session):
    agent_with(client, db_session, 3)
    pages = {"console": client.get("/tickets").text}
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "jo@example.com"), UserRole.END_USER)
    pages["portal"] = client.get("/portal").text
    for frame, page in pages.items():
        bell = re.search(r'<details class="bell-menu">.*?</details>', page, re.S).group(0)
        summary = re.search(r"<summary[^>]*>.*?</summary>", bell, re.S).group(0)
        assert 'hx-get="/notifications/count"' in summary and "every 30s" in summary and "data-poll" in summary, frame
        assert 'hx-target="find .bell-label"' in summary and 'class="sr-only bell-label">Notifications' in summary, frame
        assert 'id="bell-count"' in summary and "aria-label" not in summary, frame  # the label text names it
        assert 'hx-get="/notifications/latest"' in bell and "toggle[target.open]" in bell, frame
        assert "data-poll" not in bell.replace(summary, ""), frame  # the poll never touches the panel
        assert page.count('class="bell-menu"') == 1, frame


def test_the_menus_script_covers_the_bell_and_the_person_menu(client, db_session):
    agent_with(client, db_session, 1)
    page = client.get("/tickets").text
    assert 'querySelectorAll("details.person-menu, details.bell-menu")' in page
    assert "Escape" in page and 'closest(".person-menu-panel a, .bell-panel a")' in page
    assert 'addEventListener("toggle"' in page  # opening one closes the other
    assert 'hasAttribute("data-quiet")' in page  # filling the dropdown must not clear the flash area either

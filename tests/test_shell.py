"""The app shell (9A, issue #195): the colour theme setting, the navigation built from roles and permissions (app/navigation.py),
the bell in the frame, and an accessibility smoke test. The pages' bodies are not tested here; the rest of the suite does that."""

import re
from pathlib import Path

import pytest
from sqlmodel import select

from app.models import FINISHED_STATUSES, Notification, Ticket, TicketStatus, User, UserPermission, UserRole
from app.navigation import build_shell
from app.notifications import notify_outage_mode
from app.theme import DARK, LIGHT, SYSTEM, next_theme, safe_return, theme_for
from tests.conftest import create_team, login_as, set_role
from tests.test_triage import agent, new_ticket

STATIC = Path("app/static")


def html_class(page) -> str:
    return re.search(r'<html lang="en" class="(\w+)"', page.text).group(1)


def nav_links(page) -> dict[str, str]:
    """label -> href of every item in the shell's navigation."""
    found = re.findall(r'<a href="([^"]+)" class="navlink"[^>]*>.*?<span class="navlink-label">([^<]+)</span>', page.text, re.S)
    return {label: href.replace("&amp;", "&") for href, label in found}


def current_items(page) -> list[str]:
    return re.findall(r'class="navlink" aria-current="page">.*?<span class="navlink-label">([^<]+)</span>', page.text, re.S)


def stored_theme(db_session, email) -> str | None:
    db_session.expire_all()
    return db_session.exec(select(User).where(User.email == email)).one().theme


# ---- The theme --------------------------------------------------------------------------------------------


def test_system_is_the_default_and_the_server_writes_the_class(client, db_session):
    agent(client, db_session)
    page = client.get("/tickets")
    assert html_class(page) == SYSTEM
    assert '<meta name="color-scheme" content="light dark"' in page.text
    assert stored_theme(db_session, "agent@example.com") is None


@pytest.mark.parametrize("choice,expected_class", [(DARK, "dark"), (LIGHT, "light")])
def test_a_chosen_theme_is_saved_and_rendered_on_every_page(client, db_session, choice, expected_class):
    agent(client, db_session)
    response = client.post("/settings/theme", data={"theme": choice, "return_to": "/tickets"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tickets"
    assert stored_theme(db_session, "agent@example.com") == choice
    for path in ("/tickets", "/settings/notifications", "/portal"):
        page = client.get(path)
        assert html_class(page) == expected_class, path
        assert f'<meta name="color-scheme" content="{choice}"' in page.text


def test_choosing_system_again_clears_the_stored_choice(client, db_session):
    agent(client, db_session)
    client.post("/settings/theme", data={"theme": DARK})
    client.post("/settings/theme", data={"theme": SYSTEM})
    assert stored_theme(db_session, "agent@example.com") is None
    assert html_class(client.get("/tickets")) == SYSTEM


def test_an_unknown_theme_is_refused_and_changes_nothing(client, db_session):
    agent(client, db_session)
    client.post("/settings/theme", data={"theme": DARK})
    assert client.post("/settings/theme", data={"theme": "purple"}).status_code == 400
    assert client.post("/settings/theme", data={}).status_code == 400
    assert stored_theme(db_session, "agent@example.com") == DARK


def test_the_theme_belongs_to_the_person(client, db_session):
    agent(client, db_session, email="one@example.com")
    client.post("/settings/theme", data={"theme": DARK})
    client.post("/auth/logout")
    login_as(client, "two@example.com")
    assert html_class(client.get("/portal")) == SYSTEM
    assert stored_theme(db_session, "one@example.com") == DARK


def test_the_change_needs_a_signed_in_person(client, db_session):
    response = client.post("/settings/theme", data={"theme": DARK}, follow_redirects=False)
    assert response.status_code in (303, 307, 401, 403)
    assert client.get("/auth/login").status_code == 200 and html_class(client.get("/auth/login")) == SYSTEM


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", "tickets", "/a\nb"])
def test_the_return_address_can_only_be_a_path_on_this_site(client, db_session, target):
    agent(client, db_session)
    response = client.post("/settings/theme", data={"theme": DARK, "return_to": target}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/settings/notifications"
    assert safe_return(target) == "/settings/notifications"


def test_my_settings_offers_the_three_choices_with_the_current_one_marked(client, db_session):
    agent(client, db_session)
    client.post("/settings/theme", data={"theme": LIGHT})
    page = client.get("/settings/notifications").text
    assert re.search(r'name="theme" value="light" checked', page)
    assert re.search(r'name="theme" value="system"(?! checked)', page) and re.search(r'name="theme" value="dark"(?! checked)', page)
    saved = client.post("/settings/theme", data={"theme": DARK, "return_to": "/settings/notifications?saved=1"}, follow_redirects=True)
    assert "Saved." in saved.text and re.search(r'name="theme" value="dark" checked', saved.text)


def test_the_top_bar_button_steps_through_the_three_and_returns_to_the_same_path(client, db_session):
    agent(client, db_session)
    seen = []
    for _ in range(3):
        page = client.get("/tickets?mine=true")
        button = re.search(r'<form method="post" action="/settings/theme"[^>]*>(.*?)</form>', page.text, re.S).group(1)
        assert 'name="return_to" value="/tickets"' in button  # the path only: a query can hold a search
        assert 'aria-label="Colour theme:' in button
        seen.append(html_class(page))
        wanted = re.search(r'name="theme" value="(\w+)"', button).group(1)
        assert wanted == next_theme(seen[-1])
        redirected = client.post("/settings/theme", data={"theme": wanted, "return_to": "/tickets"}, follow_redirects=False)
        assert redirected.headers["location"] == "/tickets"
    assert seen == [SYSTEM, LIGHT, DARK]
    assert html_class(client.get("/tickets")) == SYSTEM


def test_a_page_drawn_by_a_post_does_not_offer_a_return_address(client, db_session):
    """After a POST the address cannot be fetched again, so the button then goes to My settings."""
    ticket = new_ticket(db_session)
    team = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[team])
    response = client.post(f"/tickets/{ticket.number}/note", data={"body": "a note"})
    if response.status_code == 200 and "<html" in response.text:
        assert 'name="return_to"' not in response.text


def test_theme_helpers():
    assert theme_for(None) == SYSTEM
    assert [next_theme(t) for t in (SYSTEM, LIGHT, DARK, "junk")] == [LIGHT, DARK, SYSTEM, SYSTEM]


def test_the_tokens_are_the_mockups_in_both_themes():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    light, dark = css.split(":root.dark")[0], css.split(":root.dark")[1].split("@media")[0]
    for token, value in {"--bg": "#F4F5F7", "--surface": "#FFFFFF", "--raised": "#F8F9FB", "--border": "#E1E4E8", "--text": "#1B1F24",
                         "--muted": "#57606A", "--accent": "#2F5BD3", "--accent-text": "#FFFFFF", "--accent-soft": "#E6EEFB",
                         "--note-bg": "#FFF8EC", "--red": "#B42318", "--green": "#1E6B3A"}.items():
        assert f"{token}: {value};" in light, token
    for token, value in {"--bg": "#0E1116", "--surface": "#161B22", "--raised": "#1C2230", "--border": "#2A313C", "--text": "#E6EAF0",
                         "--muted": "#9AA4B2", "--accent": "#7AA2FF", "--accent-text": "#0E1116", "--accent-soft": "#1B2740",
                         "--note-bg": "#221C12", "--red": "#FF8A80", "--green": "#6FD38F"}.items():
        assert f"{token}: {value};" in dark, token
    # System is the same dark set behind the media query, so it can never drift from Dark.
    system = css.split("@media (prefers-color-scheme: dark)")[1]
    assert re.findall(r"(--[\w-]+): (#[0-9A-F]{6})", system) == re.findall(r"(--[\w-]+): (#[0-9A-F]{6})", dark)


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_text_contrast_is_at_least_4_5_to_1_in_both_themes():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    sets = {"light": css.split(":root.dark")[0], "dark": css.split(":root.dark")[1].split("@media")[0]}
    pairs = [("text", "bg"), ("text", "surface"), ("text", "raised"), ("text", "accent-soft"), ("text", "note-bg"), ("muted", "bg"),
             ("muted", "surface"), ("muted", "raised"), ("muted", "gray-bg"), ("accent", "surface"), ("accent", "bg"), ("accent", "accent-soft"),
             ("accent-text", "accent"), ("surface", "red"), ("gray", "gray-bg"), ("blue", "blue-bg"), ("amber", "amber-bg"),
             ("red", "red-bg"), ("green", "green-bg")]
    for name, block in sets.items():
        tokens = dict(re.findall(r"--([\w-]+): (#[0-9A-Fa-f]{6});", block))
        for foreground, background in pairs:
            assert contrast(tokens[foreground], tokens[background]) >= 4.5, (name, foreground, background)


def test_inter_is_self_hosted_with_its_licence(client):
    fonts = STATIC / "fonts"
    assert (fonts / "Inter-OFL-LICENSE.txt").read_text(encoding="utf-8").count("SIL Open Font License") >= 1
    served = client.get("/static/fonts/Inter-4.0-variable.woff2")
    assert served.status_code == 200 and served.content[:4] == b"wOF2"
    assert "immutable" in served.headers["cache-control"]
    tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
    assert "url(\"/static/fonts/Inter-4.0-variable.woff2\")" in tokens and "http" not in tokens


# ---- The navigation ---------------------------------------------------------------------------------------


AGENT_ITEMS = ["Queue", "My tickets", "Untriaged", "Approvals", "MSP mail", "My requests", "My feedback", "Settings", "Appearance"]


def test_an_agent_sees_the_work_items_and_nothing_they_may_not_open(client, db_session):
    agent(client, db_session)
    links = nav_links(client.get("/tickets"))
    assert list(links) == AGENT_ITEMS
    assert links["Queue"] == "/tickets" and links["My tickets"] == "/tickets?mine=true" and links["Untriaged"] == "/tickets?untriaged=true"
    assert links["Approvals"] == "/portal/approvals" and links["MSP mail"] == "/msp-mail"
    assert "/reports" not in links.values() and "/admin" not in " ".join(links.values())


def test_a_super_admin_also_sees_reports_canned_responses_and_admin(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    links = nav_links(client.get("/tickets"))
    assert list(links) == ["Queue", "My tickets", "Untriaged", "Approvals", "MSP mail", "Reports", "Canned responses", "Admin", "My requests", "My feedback", "Settings", "Appearance"]
    assert links["Reports"] == "/reports" and links["Canned responses"] == "/admin/canned" and links["Admin"] == "/admin"


def test_view_reports_alone_adds_reports_and_nothing_else(client, db_session):
    agent_id = agent(client, db_session)
    db_session.add(UserPermission(user_id=agent_id, permission="view_reports"))
    db_session.commit()
    links = nav_links(client.get("/tickets"))
    assert list(links) == ["Queue", "My tickets", "Untriaged", "Approvals", "MSP mail", "Reports", "My requests", "My feedback", "Settings", "Appearance"]


@pytest.mark.parametrize("permission,destination", [("manage_request_templates", "/admin/request-types"), ("data_protection", "/admin/data-protection")])
def test_an_extra_permission_adds_an_admin_item_that_leads_to_its_page(client, db_session, permission, destination):
    agent_id = agent(client, db_session)
    db_session.add(UserPermission(user_id=agent_id, permission=permission))
    db_session.commit()
    links = nav_links(client.get("/tickets"))
    assert links["Admin"] == "/admin" and "Reports" not in links and "Canned responses" not in links
    assert client.get(destination).status_code == 200
    # The admin home leads on to the page the permission opens.
    assert f'href="{destination}"' in client.get("/admin").text


def test_an_end_user_gets_the_portal_frame_and_no_agent_navigation(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.END_USER)
    page = client.get("/portal")
    assert list(nav_links(page)) == ["My requests", "New request", "Approvals", "Settings", "Appearance"]
    assert "Agent console" not in page.text and 'class="sidebar"' not in page.text
    assert "/tickets" not in nav_links(page).values()
    assert 'action="/tickets"' not in page.text  # no ticket search for someone who cannot use it
    assert "Raise ticket" not in page.text
    # An end user is in the portal frame on every page they can open, not only /portal.
    assert list(nav_links(client.get("/settings/notifications"))) == ["My requests", "New request", "Approvals", "Settings", "Appearance"]


def test_an_agent_on_a_portal_page_gets_the_portal_frame_with_a_way_back(client, db_session):
    agent(client, db_session)
    links = nav_links(client.get("/portal"))
    assert list(links) == ["My requests", "New request", "Approvals", "Agent console", "Settings", "Appearance"]
    assert links["Agent console"] == "/tickets"


def test_the_sign_in_page_has_the_bare_frame(client):
    page = client.get("/auth/login")
    assert page.status_code == 200 and nav_links(page) == {}
    assert 'class="skip-link"' in page.text and "IT Support Ticketing" in page.text and "Sign out" not in page.text


@pytest.mark.parametrize(
    "path,current",
    [
        ("/tickets", ["Queue"]),
        ("/tickets?mine=true", ["My tickets"]),
        ("/tickets?untriaged=true", ["Untriaged"]),
        ("/tickets?q=printer", ["Queue"]),
        ("/portal/approvals", ["Approvals"]),
        ("/msp-mail", ["MSP mail"]),
        ("/my-feedback", ["My feedback"]),
        ("/reports", ["Reports"]),
        ("/admin/canned", ["Canned responses"]),
        ("/admin/teams", ["Admin"]),
        ("/settings/notifications", ["Settings"]),
    ],
)
def test_exactly_one_item_is_marked_as_the_current_page(client, db_session, path, current):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    assert current_items(client.get(path)) == current


def test_a_ticket_page_belongs_to_the_queue(client, db_session):
    ticket = new_ticket(db_session)
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    assert current_items(client.get(f"/tickets/{ticket.number}")) == ["Queue"]


def test_the_portal_pages_mark_their_own_item(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.END_USER)
    assert current_items(client.get("/portal")) == ["My requests"]
    assert current_items(client.get("/portal/new")) == ["New request"]
    assert current_items(client.get("/portal/approvals")) == ["Approvals"]


def test_the_counts_beside_the_items(client, db_session):
    new_ticket(db_session)  # untriaged, in the shared queue
    triaged = new_ticket(db_session)
    triaged.triaged_at = triaged.created_at
    finished = new_ticket(db_session)
    finished.status = next(iter(FINISHED_STATUSES))
    db_session.add(triaged)
    db_session.add(finished)
    db_session.commit()
    agent_id = agent(client, db_session)
    mine = db_session.get(Ticket, triaged.id)
    mine.assigned_agent_id = agent_id
    db_session.add(mine)
    db_session.commit()
    page = client.get("/tickets")
    counts = dict(re.findall(r'<span class="navlink-label">([^<]+)</span>\s*<span class="chip t-\w+"><span class="sr-only">[^<]+</span>(\d+)</span>', page.text))
    # The agent is in no team: only untriaged tickets are theirs to see, and none of them is finished.
    assert counts.get("Untriaged") == "1" and counts.get("Queue") == "1"
    assert "My tickets" not in counts and "Approvals" not in counts and "MSP mail" not in counts  # a zero shows no chip


def test_the_untriaged_filter_lists_only_untriaged_open_tickets(client, db_session):
    untriaged = new_ticket(db_session)
    triaged = new_ticket(db_session)
    triaged.triaged_at = triaged.created_at
    db_session.add(triaged)
    done = new_ticket(db_session)
    done.status = TicketStatus.CLOSED
    db_session.add(done)
    db_session.commit()
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    page = client.get("/tickets?untriaged=true").text
    assert f"/tickets/{untriaged.number}\"" in page
    assert f"/tickets/{triaged.number}\"" not in page and f"/tickets/{done.number}\"" not in page
    assert f"/tickets/{triaged.number}\"" in client.get("/tickets").text


# ---- The bell in the frame --------------------------------------------------------------------------------


def test_the_bell_shows_the_unread_count_and_keeps_the_poll(client, db_session):
    admin_id = login_as(client, "boss@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    other = db_session.get(User, login_as(client, "other@example.com"))
    login_as(client, "boss@example.com")
    boss = db_session.get(User, admin_id)
    page = client.get("/tickets")
    assert 'id="bell-count" aria-hidden="true" hidden>0</span>' in page.text.replace("\n", "") or " hidden>" in page.text
    notify_outage_mode(db_session, boss, other, True)
    notify_outage_mode(db_session, boss, other, False)
    db_session.commit()
    assert db_session.exec(select(Notification).where(Notification.user_id == admin_id)).all()
    page = client.get("/tickets")
    assert '<span class="sr-only bell-label">Notifications (2)</span>' in page.text
    assert re.search(r'id="bell-count" aria-hidden="true">2</span>', page.text)
    # Same polling contract as before the redesign: the count endpoint, every 30 seconds, never wiping a message.
    assert 'hx-get="/notifications/count"' in page.text and "every 30s" in page.text and "data-poll" in page.text
    assert 'aria-label' not in re.search(r'<a href="/notifications".*?</a>', page.text, re.S).group(0)  # its text names it


def test_the_bell_is_in_the_portal_frame_too(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.END_USER)
    page = client.get("/portal")
    assert 'hx-get="/notifications/count"' in page.text and 'class="sr-only bell-label">Notifications</span>' in page.text


# ---- Accessibility smoke ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role,path", [(UserRole.AGENT, "/tickets"), (UserRole.SUPER_ADMIN, "/admin/users"), (UserRole.END_USER, "/portal"), (None, "/auth/login")])
def test_landmarks_skip_link_and_labelled_icon_buttons(client, db_session, role, path):
    if role is not None:
        set_role(db_session, login_as(client, f"{(role or UserRole.AGENT).value}@example.com"), role)
    text = client.get(path).text
    assert '<a class="skip-link" href="#main-content">' in text
    assert text.count('id="main-content"') == 1 and text.count("<main ") == 1
    assert "<header" in text and (role is None or "<nav>" in text)
    assert text.index('class="skip-link"') < text.index("<main ")  # the first thing a keyboard reaches
    # Every button or link that holds only an icon is named: by aria-label or by text for screen readers.
    for control in re.findall(r'<(?:button|a)\b[^>]*class="[^"]*icon-btn[^"]*"[^>]*>.*?</(?:button|a)>', text, re.S):
        assert "aria-label=" in control or 'class="sr-only' in control, control
    # Icons are decoration, hidden from screen readers.
    assert all('aria-hidden="true"' in svg for svg in re.findall(r"<svg\b[^>]*>", text))
    assert 'lang="en"' in text and 'name="viewport"' in text


def test_the_search_is_a_labelled_landmark_that_opens_the_ticket_list(client, db_session):
    agent(client, db_session)
    text = client.get("/tickets").text
    form = re.search(r'<form method="get" action="/tickets" role="search"[^>]*>(.*?)</form>', text, re.S).group(1)
    assert '<span class="sr-only">Search tickets</span>' in form and 'name="q"' in form


def test_active_item_uses_aria_current_not_colour_alone(client, db_session):
    agent(client, db_session)
    text = client.get("/tickets").text
    assert text.count('aria-current="page"') == 1
    assert '[aria-current="page"]' in (STATIC / "styles.css").read_text(encoding="utf-8")


def test_focus_is_visible_and_targets_are_big_enough():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css and "outline: 2px solid var(--focus)" in css
    for selector in (".icon-btn", ".navlink", ".btn"):
        block = re.search(re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}", css).group(1)
        size = re.search(r"(?:min-)?(?:height|width): (\d+)px", block)
        assert size and int(size.group(1)) >= 24, selector


def test_an_htmx_partial_carries_no_frame(client, db_session):
    ticket = new_ticket(db_session)
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    response = client.post(f"/tickets/{ticket.number}/note", data={"body": "a note"}, headers={"HX-Request": "true"})
    assert "skip-link" not in response.text and "<html" not in response.text


def test_build_shell_survives_a_person_with_no_role_row(db_session):
    """The frame is a convenience: for nobody it is the bare one."""
    assert build_shell(db_session, None, "/tickets", {}).kind == "bare"


# ---- The person menu and the fixed sidebar (9G, issue #210) ------------------------------------------------

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css").read_text()


def menu_of(page) -> str:
    """The person menu's <details>."""
    return re.search(r'<details class="person-menu.*?</details>', page.text, re.S).group(0)


def test_the_console_sidebar_has_one_nav_and_the_person_menu_is_its_own_landmark(client, db_session):
    agent(client, db_session)
    page = client.get("/tickets")
    assert page.text.count("<nav>") == 1  # the sidebar's navigation; tests/test_htmx_partials.py counts it
    assert page.text.count('<nav aria-label="Account">') == 1
    assert menu_of(page).count('<nav aria-label="Account">') == 1
    # The Me group is gone from the sidebar list: those pages are in the menu.
    sidebar_nav = re.search(r"<nav>.*?</nav>", page.text, re.S).group(0)
    assert "nav-heading" not in sidebar_nav and "My feedback" not in sidebar_nav and "Settings" not in sidebar_nav


def test_an_agents_person_menu_holds_their_pages_and_a_sign_out_post(client, db_session):
    agent(client, db_session)
    menu = menu_of(client.get("/tickets"))
    labels = re.findall(r'<a href="([^"]+)" class="navlink"[^>]*>.*?<span class="navlink-label">([^<]+)</span>', menu, re.S)
    assert labels == [("/portal", "My requests"), ("/my-feedback", "My feedback"), ("/settings/notifications", "Settings"), ("/settings/notifications#appearance", "Appearance")]
    assert re.search(r'<form method="post" action="/auth/logout"><button type="submit" class="btn btn-block">Sign out</button></form>', menu)
    assert 'aria-label="Account menu for ' in menu and "person-menu-up" in menu


def test_an_end_users_person_menu_is_the_same_component_without_agent_pages(client, db_session):
    set_role(db_session, login_as(client, "ann@example.com"), UserRole.END_USER)
    menu = menu_of(client.get("/portal"))
    assert "Settings" in menu and "Appearance" in menu and "Sign out" in menu
    assert "My feedback" not in menu and "person-menu-up" not in menu


def test_sign_out_from_the_menu_ends_the_session(client, db_session):
    agent(client, db_session)
    assert client.get("/tickets").status_code == 200
    client.post("/auth/logout", follow_redirects=False)
    assert client.get("/tickets", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_the_appearance_link_lands_on_the_appearance_fieldset(client, db_session):
    agent(client, db_session)
    assert '<fieldset id="appearance">' in client.get("/settings/notifications").text


def test_the_sidebar_is_fixed_to_the_viewport_and_only_its_navigation_scrolls():
    sidebar = re.search(r"\n\.sidebar \{.*?\n\}", CSS, re.S).group(0)
    assert "position: sticky" in sidebar and "top: 0" in sidebar and "height: 100vh" in sidebar and "height: 100dvh" in sidebar
    assert re.search(r"\.sidebar > nav \{[^}]*overflow-y: auto", CSS)
    # Stacked (the sidebar wraps above the page): no fixed height, so nothing is trapped on a small screen.
    stacked = re.search(r"@media \(max-width: 800px\) \{.*?\n\}", CSS, re.S).group(0)
    assert "position: static" in stacked and "height: auto" in stacked


def test_admin_users_forms_use_the_form_layout_with_their_field_names(client, db_session):
    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    page = client.get("/admin/users?new=1").text  # the new-account panel
    create = page[page.index('<form method="post" action="/admin/users">'):]
    create = create[: create.index("</form>")]
    assert 'class="form-grid"' in create and 'class="form-actions"' in create and create.count('class="field"') == 6
    for name in ("email", "display_name", "password", "role", "team_id", "level"):
        assert f'name="{name}"' in create
    link = page[page.index('action="/admin/links/preview"'):]
    link = link[: link.index("</form>")]
    assert 'class="form-grid"' in link and 'name="main_email"' in link and 'name="other_email"' in link
    assert 'class="form-grid"' in page and 'role="search"' in page  # the filters are a plain GET form


def test_pressing_enter_in_the_time_zone_box_submits_use_this_time_zone_not_the_clear_button(client, db_session):
    uid = agent(client, db_session)
    user = db_session.get(User, uid)
    user.time_zone, user.time_zone_source = "Europe/Paris", "manual"
    db_session.add(user)
    db_session.commit()
    page = client.get("/settings/notifications").text
    zone_form = page[page.index('action="/settings/time-zone"'):]
    zone_form = zone_form[: zone_form.index("</form>")]
    # The first submit button in the markup is the one Enter uses; .form-actions (row-reverse) puts it on the right.
    assert zone_form.index("Use this time zone") < zone_form.index('name="clear"')
    assert "flex-direction: row-reverse" in re.search(r"\n\.form-actions \{[^}]*\}", CSS).group(0)


def test_choosing_a_link_in_the_person_menu_closes_it(client, db_session):
    agent(client, db_session)
    page = client.get("/tickets").text
    assert 'event.target.closest(".person-menu-panel a")' in page and "Escape" in page

"""Team activity as session cards (#243): the one grouping function (unit tests, no database), the page, the "Show
earlier" rule, the side drawer and the sidebar item."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Message, MessageDirection, Ticket, TicketEvent, User, UserRole
from app.settings_store import ACTIVITY_GROUP_MINUTES, GROUP_OTHER, REGISTRY, set_setting
from app.team_activity import (
    ROLE_AGENT,
    ROLE_REQUESTER,
    ROLE_SYSTEM,
    Line,
    Step,
    day_heading,
    group_steps,
)
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_raising import desk, raise_it
from tests.test_team_activity import classified, feed, ticket_named

GAP = timedelta(minutes=30)
T0 = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def step(minutes, ticket=1, person="a@x.com", role=ROLE_AGENT, phrase="Added a note", row=None, **extra):
    return Step(
        ticket_id=ticket, at=T0 + timedelta(minutes=minutes), role=role, person=person if role != ROLE_SYSTEM else None,
        phrase=phrase, row_id=row if row is not None else minutes * 100 + ticket, **extra,
    )


# ---- The grouping function --------------------------------------------------------------------------------------


def test_one_person_on_one_ticket_within_the_gap_is_one_card_and_beyond_it_two():
    [card] = group_steps([step(0), step(10), step(40)], GAP)  # 10 then 30 minutes after: both within the gap
    assert [s.at for s in card.steps] == [T0, T0 + timedelta(minutes=10), T0 + timedelta(minutes=40)]

    cards = group_steps([step(0), step(10), step(41)], GAP)  # 31 minutes after the last one: a second session
    assert [len(c.steps) for c in cards] == [1, 2]  # newest first
    assert cards[0].start == T0 + timedelta(minutes=41)


def test_the_gap_is_the_one_given():
    assert len(group_steps([step(0), step(20)], timedelta(minutes=30))) == 1
    assert len(group_steps([step(0), step(20)], timedelta(minutes=10))) == 2


def test_steps_on_different_tickets_are_never_one_card():
    cards = group_steps([step(0, ticket=1), step(1, ticket=2), step(2, ticket=1)], GAP)
    assert sorted(len(c.steps) for c in cards) == [1, 2]
    assert {c.ticket_id for c in cards} == {1, 2}


def test_several_people_on_one_ticket_within_the_gap_are_one_card_with_a_line_each():
    [card] = group_steps(
        [step(0, person="a@x.com", phrase="Triaged"), step(5, person="b@x.com", phrase="Resolved"), step(8, person="a@x.com", phrase="Added a note")],
        GAP,
    )
    assert [line.person for line in card.lines] == ["a@x.com", "b@x.com"]  # in the order each first acted
    assert [line.summary for line in card.lines] == ["Triaged · Added a note", "Resolved"]
    assert card.role == ROLE_AGENT and len(card.steps) == 3


def test_requester_and_system_steps_stand_alone_and_do_not_split_a_session():
    cards = group_steps(
        [
            step(0, phrase="Triaged"),
            step(5, person="req@x.com", role=ROLE_REQUESTER, phrase="Requester replied"),
            step(6, role=ROLE_SYSTEM, phrase="Chased"),
            step(10, phrase="Resolved"),
        ],
        GAP,
    )
    assert sorted(c.role for c in cards) == [ROLE_AGENT, ROLE_REQUESTER, ROLE_SYSTEM]
    [agent] = [c for c in cards if c.role == ROLE_AGENT]
    assert [s.phrase for s in agent.steps] == ["Triaged", "Resolved"]
    assert all(len(c.steps) == 1 for c in cards if c.role != ROLE_AGENT)
    # Two requester replies are two cards, never one.
    assert len(group_steps([step(0, person="r", role=ROLE_REQUESTER), step(1, person="r", role=ROLE_REQUESTER)], GAP)) == 2


def test_cards_are_ordered_by_their_latest_step_newest_first():
    cards = group_steps([step(0, ticket=1), step(50, ticket=1), step(20, ticket=2), step(25, ticket=2), step(60, ticket=3)], GAP)
    assert [(c.ticket_id, len(c.steps)) for c in cards] == [(3, 1), (1, 1), (2, 2), (1, 1)]


def test_a_line_writes_a_run_of_the_same_step_once_with_a_count():
    line = Line("a@x.com", [step(0, phrase="Added a note"), step(1, phrase="Added a note"), step(2, phrase="Resolved")])
    assert line.summary == "Added a note ×2 · Resolved"


def test_day_headings_are_the_days_in_the_viewers_zone():
    now = datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc)  # 18 Nov in Auckland (UTC+13), 18 Nov 00:30 in London
    late = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)
    assert day_heading(late, "Pacific/Auckland", now) == "Today"  # already the 18th there
    assert day_heading(late, "Europe/London", now) == "Yesterday"
    assert day_heading(now - timedelta(days=3), "Europe/London", now) == "Sunday 15 November"


# ---- The page ---------------------------------------------------------------------------------------------------


def cards_in(page):
    return page.count('<article class="act-card')


def spread(db_session, ticket, hours_apart, bodies):
    """Date the ticket's internal notes with these bodies: the first at `hours_apart[0]` hours ago and so on."""
    for body, hours in zip(bodies, hours_apart):
        message = db_session.exec(select(Message).where(Message.ticket_id == ticket.id, Message.body == body)).one()
        message.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        db_session.add(message)
    db_session.commit()


def second_agent(client, db_session, team, name="Bea Brown", email="bea@example.com"):
    client.post("/auth/logout")
    agent_id = login_as(client, email, name)
    set_role(db_session, agent_id, UserRole.AGENT, team.id)


def test_one_persons_steps_on_one_ticket_are_one_card_with_a_line_per_step(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Printer on fire")
    ticket = ticket_named(db_session, "Printer on fire")
    client.post(f"/tickets/{ticket.number}/email", data={"body": "On our way"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Fuser again"})

    page = feed(client).text
    agent_card = re.search(r'<article class="act-card act-agent">.*?</article>', page, re.S).group(0)

    assert "Printer on fire" in agent_card and "<details" not in agent_card
    assert '<strong class="act-who">You</strong>' in agent_card  # the viewer's own steps
    assert agent_card.count('<li class="act-step">') >= 3  # triaged, emailed, noted: a line each
    assert "Emailed the requester" in agent_card and "Added an internal note" in agent_card
    assert "On our way" in agent_card and "Fuser again" in agent_card  # the steps, with their excerpts, inside the card
    assert re.search(r'class="act-quote act-note">Fuser again<', agent_card)  # the note in its amber quote block


def test_two_people_on_one_ticket_within_the_gap_share_one_card_with_a_line_each(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Shared work")
    ticket = ticket_named(db_session, "Shared work")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann looked"})
    second_agent(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Bea fixed"})

    client.post("/auth/logout")
    login_as(client, "agent@example.com", "Agent Ann")
    page = feed(client, kind="notes").text

    assert cards_in(page) == 1
    assert "You and Bea Brown" in page  # the viewer is Ann: "You"
    assert page.count("Added an internal note") == 2 and "Ann looked" in page and "Bea fixed" in page
    assert '<strong class="act-by">You</strong>' in page and '<strong class="act-by">Bea Brown</strong>' in page


def test_the_gap_is_a_setting(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Slow work")
    ticket = ticket_named(db_session, "Slow work")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "First"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Second"})
    spread(db_session, ticket, [1.0, 0.25], ["First", "Second"])  # 45 minutes apart

    assert cards_in(feed(client, kind="notes").text) == 2  # the default is 30 minutes
    set_setting(db_session, ACTIVITY_GROUP_MINUTES, "60")
    db_session.commit()
    assert cards_in(feed(client, kind="notes").text) == 1


def test_the_gap_setting_is_declared_with_its_group():
    definition = REGISTRY[ACTIVITY_GROUP_MINUTES]
    assert (definition.default, definition.kind, definition.group, definition.unit) == ("30", "minutes", GROUP_OTHER, "minutes")
    assert definition.display_label == "Group activity steps within" and definition.description


def test_a_requester_reply_is_its_own_card_between_an_agents_sessions(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Chatty")
    ticket = ticket_named(db_session, "Chatty")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Agent note"})

    page = feed(client).text

    requester_card = re.search(r'<article class="act-card act-requester">.*?</article>', page, re.S).group(0)
    assert "Replied by email" in requester_card and "It is smoking" in requester_card and "<details" not in requester_card
    assert cards_in(page) == 2


def test_the_person_filter_keeps_only_that_persons_steps_and_cards_drop_out(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Two hands")
    ticket = ticket_named(db_session, "Two hands")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann note"})
    second_agent(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Bea note"})
    other = create_team(db_session, "Network")
    elsewhere = create_category(db_session, other, "Wireless")  # noqa: F841  (Bea cannot see it)

    page = feed(client, kind="notes", person="bea@example.com").text
    assert "Bea note" in page and "Ann note" not in page and "Agent Ann:" not in page
    assert '<option value="agent@example.com"' in page and '<option value="bea@example.com" selected>' in page

    nothing = feed(client, kind="notes", person="nobody@example.com").text
    assert cards_in(nothing) == 0 and "No steps match" in nothing


def test_the_kind_filter_drops_a_card_with_no_matching_step(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Only changes")

    notes = feed(client, kind="notes").text
    assert cards_in(notes) == 0 and "No steps match" in notes
    assert cards_in(feed(client, kind="events").text) >= 1


def test_days_and_times_are_in_the_viewers_zone(client, db_session):
    team, category, agent_id = desk(client, db_session)
    classified(client, category, "Night shift")
    ticket = ticket_named(db_session, "Night shift")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Late note"})
    note = db_session.exec(select(Message).where(Message.body == "Late note")).one()
    note.created_at = datetime(2026, 3, 10, 22, 30, tzinfo=timezone.utc)  # 11 March 11:30 in Auckland (UTC+13 then)
    db_session.add(note)
    db_session.commit()

    assert "Tuesday 10 March" in feed(client, kind="notes").text and "22:30" in feed(client, kind="notes").text
    user = db_session.get(User, agent_id)
    user.time_zone = "Pacific/Auckland"
    db_session.add(user)
    db_session.commit()
    page = feed(client, kind="notes").text
    assert "Wednesday 11 March" in page and "11:30" in page and "22:30" not in page


def test_cards_are_under_today_and_yesterday_headings_newest_first(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Days")
    ticket = ticket_named(db_session, "Days")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Fresh"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Stale"})
    spread(db_session, ticket, [0, 30], ["Fresh", "Stale"])

    page = feed(client, kind="notes").text

    assert page.index(">Today<") < page.index("Fresh") < page.index(">Yesterday<") < page.index("Stale")


def test_another_teams_ticket_is_not_in_the_page_or_the_drawer(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    elsewhere = create_category(db_session, other, "Wireless")
    classified(client, category, "Mine")
    classified(client, elsewhere, "Theirs")

    assert "Theirs" not in feed(client).text
    drawer = client.get("/tickets/activity/latest", headers={"HX-Request": "true"}).text
    assert "Mine" in drawer and "Theirs" not in drawer


# ---- Show earlier never splits a card ---------------------------------------------------------------------------


def test_show_earlier_never_cuts_a_card_in_two(client, db_session, monkeypatch):
    import app.team_activity as ta

    monkeypatch.setattr(ta, "LOAD_STEPS", 3)
    team, category, _ = desk(client, db_session)
    classified(client, category, "Long session")
    ticket = ticket_named(db_session, "Long session")
    bodies = [f"Step {i}" for i in range(8)]
    for body in bodies:
        client.post(f"/tickets/{ticket.number}/note", data={"body": body})
    # Steps 0 and 1 were three hours ago (a session of their own); 2 to 7 are now, one session of six.
    spread(db_session, ticket, [3.0, 2.9], bodies[:2])

    first = feed(client, kind="notes").text
    assert cards_in(first) == 1 and all(f"Step {i}" in first for i in range(2, 8))  # the load asked for 3 steps, got the whole card
    assert "Step 1" not in first and "Step 0" not in first
    link = re.search(r'href="(\?[^"]*before=[^"]*)"[^>]*>Show earlier', first).group(1).replace("&amp;", "&")

    second = client.get("/tickets/activity" + link).text
    assert cards_in(second) == 1 and "Step 0" in second and "Step 1" in second
    assert "Step 2" not in second and "Show earlier" not in second


# ---- The pop-over and the button ---------------------------------------------------------------------------------


def hx(client, path):
    return client.get(path, headers={"HX-Request": "true"})


def test_the_drawer_body_is_a_frameless_uncached_partial_of_about_15_cards(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Busy")
    ticket = ticket_named(db_session, "Busy")
    for i in range(20):
        client.post(f"/tickets/{ticket.number}/note", data={"body": f"Note {i:02d}"})
    notes = db_session.exec(select(Message).where(Message.direction == MessageDirection.INTERNAL).order_by(Message.id)).all()
    for n, message in enumerate(notes):  # an hour apart: a card each
        message.created_at = datetime.now(timezone.utc) - timedelta(hours=len(notes) - n)
        db_session.add(message)
    db_session.commit()

    response = hx(client, "/tickets/activity/latest")

    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    page = response.text
    assert "<html" not in page and "<nav" not in page  # no frame
    assert page.count('<article class="act-card') == 15
    assert "<details" not in page
    assert f'href="/tickets/{ticket.number}"' in page
    for label in ("All", "Changes", "Emails", "Notes"):  # the filter chips are ours, not the template's
        assert f">{label}</button>" in page
    assert "Auto-refresh" not in page and "every 30s" not in page  # it never refreshes itself

    only_notes = hx(client, "/tickets/activity/latest?kind=notes").text
    assert 'aria-pressed="true" data-quiet hx-get="/tickets/activity/latest?tab=team&amp;kind=notes"' in only_notes
    assert hx(client, "/tickets/activity/latest?kind=gossip").status_code == 422
    assert hx(client, "/tickets/activity/latest?tab=everyone").status_code == 422


def test_the_drawer_route_is_not_a_page_and_is_for_agents_only(client, db_session):
    desk(client, db_session)
    response = client.get("/tickets/activity/latest", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tickets/activity"
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert hx(client, "/tickets/activity/latest").status_code == 403


def test_team_activity_is_a_sidebar_item_that_opens_the_drawer_and_the_top_bar_button_is_gone(client, db_session):
    desk(client, db_session)

    console = client.get("/tickets").text
    sidebar = console.split('class="topbar"', 1)[0]
    assert re.search(r'<a href="/tickets/activity" class="navlink" data-act-open[^>]*>', sidebar)  # a real link without script
    assert 'id="activity-drawer"' in console and 'hx-get="/tickets/activity/latest"' in console
    assert 'hx-trigger="act-open"' in console and "Open full activity page" in console
    assert 'aria-label="Close team activity"' in console and "Escape" in console and "data-act-close" in console
    assert 'class="activity-menu"' not in console and "activity-panel" not in console and 'title="Team activity"' not in console
    assert "event.ctrlKey || event.metaKey" in console  # a modifier-click still follows the link

    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    portal = client.get("/portal").text
    assert "data-act-open" not in portal and 'id="activity-drawer"' not in portal


def test_the_queue_page_itself_has_no_team_activity_button(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Anything")

    queue = client.get("/tickets").text.split("<main", 1)[1].split("<script", 1)[0]

    assert "Team activity" not in queue and 'href="/tickets/activity"' not in queue


def test_the_drawer_tabs_whole_team_by_default_and_my_activity_is_only_the_viewers_steps_with_a_count(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Shared work")
    ticket = ticket_named(db_session, "Shared work")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann looked"})
    second_agent(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Bea fixed"})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Bea writes"})
    client.post("/auth/logout")
    login_as(client, "agent@example.com", "Agent Ann")

    team_tab = hx(client, "/tickets/activity/latest").text
    assert 'aria-pressed="true" data-quiet hx-get="/tickets/activity/latest?tab=team"' in team_tab  # Whole team is the default
    assert "Ann looked" in team_tab and "Bea fixed" in team_tab and "Bea writes" in team_tab

    mine = hx(client, "/tickets/activity/latest?tab=mine").text
    assert 'aria-pressed="true" data-quiet hx-get="/tickets/activity/latest?tab=mine"' in mine
    assert "Ann looked" in mine and "Bea fixed" not in mine and "Bea writes" not in mine
    assert "Bea Brown</strong>" not in mine and '<strong class="act-who">You</strong>' in mine
    count = re.search(r'title="Your steps today">(\d+)<', mine).group(1)
    assert int(count) >= 2  # triaged and the note, at least
    assert hx(client, "/tickets/activity/latest?tab=mine&kind=notes").text.count("<article") == 1

    page = client.get("/tickets/activity?tab=mine").text
    assert "Ann looked" in page and "Bea fixed" not in page and 'href="/tickets/activity?tab=mine"' in page


def test_each_step_kind_is_drawn_as_its_sentence(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Sentences")
    ticket = ticket_named(db_session, "Sentences")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Device enrolled"})
    client.post(f"/tickets/{ticket.number}/email", data={"body": "On our way\nSecond line"})
    me = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="status_changed", actor_email=me.email, from_value="new", to_value="with_msp"))
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="assigned", actor_email=me.email, to_value=me.email))
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="status_changed", actor_email=me.email, from_value="in_progress", to_value="resolved"))
    db_session.commit()

    page = hx(client, "/tickets/activity/latest").text
    card = re.search(r'<article class="act-card act-agent">.*?</article>', page, re.S).group(0)

    assert re.search(r'Updated status: <span class="chip t-gray">New</span> <span class="act-arrow"[^>]*>&rarr;</span> <span class="chip t-blue">With MSP</span>', card)
    assert re.search(r'Added an internal note<blockquote class="act-quote act-note">Device enrolled</blockquote>', card)
    assert re.search(r'Emailed the requester<span class="act-first-line">On our way</span>', card) and "Second line" not in card
    assert "Assigned to <strong>Agent Ann</strong> (You)" in card
    assert re.search(r'Marked as <span class="chip t-green">Resolved</span>', card)


def test_an_assignment_to_someone_else_has_no_you(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Handover")
    ticket = ticket_named(db_session, "Handover")
    login_as(client, "bea@example.com", "Bea Brown")
    client.post("/auth/logout")
    login_as(client, "agent@example.com", "Agent Ann")
    db_session.add(TicketEvent(ticket_id=ticket.id, event_type="assigned", actor_email="agent@example.com", to_value="bea@example.com"))
    db_session.commit()

    card = hx(client, "/tickets/activity/latest").text

    assert "Assigned to <strong>Bea Brown</strong>" in card and "Bea Brown</strong> (You)" not in card


# ---- Review fixes ------------------------------------------------------------------------------------------------


class Row:
    def __init__(self, **fields):
        self.__dict__.update(
            dict(kind="event", what="created", from_value=None, to_value=None, detail=None, actor=None, requester="req@x.com",
                 author_id=None, agent=False, ticket_id=1, at=T0, row_id=1) | fields
        )


def test_who_took_a_step_is_by_whether_they_are_an_agent_not_by_being_the_requester():
    from app.team_activity import role_of, step_of

    assert role_of(Row(actor="req@x.com")) == ROLE_REQUESTER
    assert role_of(Row(actor="approver@x.com")) == ROLE_REQUESTER  # anyone who is not an agent stands alone
    assert role_of(Row(actor="req@x.com", agent=True)) == ROLE_AGENT  # an agent working a ticket they raised for themselves
    assert role_of(Row(actor=None)) == ROLE_SYSTEM
    automatic = Row(kind="message", what="outbound", actor="itsupport@x.com", author_id=None)  # an automatic email
    assert role_of(automatic) == ROLE_SYSTEM and step_of(automatic, {}).person is None


def test_an_agent_working_a_ticket_they_raised_for_themselves_still_gets_one_session(client, db_session):
    team, category, _ = desk(client, db_session)
    response = raise_it(
        client, subject="My own laptop", for_email="agent@example.com", category_id=category.id, impact="low", urgency="low"
    )
    assert response.status_code == 204, response.text
    ticket = ticket_named(db_session, "My own laptop")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Mine to fix"})

    page = feed(client, kind="events").text

    assert 'class="act-card act-requester"' not in page.replace("Requester replied", "")  # no event became a requester card
    assert cards_in(page) == 1 and '<strong class="act-who">You</strong>' in page


def test_the_drawer_keeps_the_day_headings_and_back_to_latest_keeps_the_filters(client, db_session, monkeypatch):
    import app.team_activity as ta

    monkeypatch.setattr(ta, "LOAD_STEPS", 2)
    team, category, _ = desk(client, db_session)
    classified(client, category, "Old and new")
    ticket = ticket_named(db_session, "Old and new")
    bodies = ["Fresh", "Older", "Oldest", "Ancient"]
    for body in bodies:
        client.post(f"/tickets/{ticket.number}/note", data={"body": body})
    spread(db_session, ticket, [0, 30, 55, 80], bodies)

    drawer = client.get("/tickets/activity/latest?kind=notes", headers={"HX-Request": "true"}).text
    assert ">Today<" in drawer and ">Yesterday<" in drawer

    first = feed(client, kind="notes").text
    link = re.search(r'href="(\?[^"]*before=[^"]*)"[^>]*>Show earlier', first).group(1).replace("&amp;", "&")
    second = client.get("/tickets/activity" + link).text
    assert 'href="/tickets/activity?kind=notes">&larr; Back to the latest' in second

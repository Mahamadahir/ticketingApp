"""Team activity as session cards (#243): the one grouping function (unit tests, no database), the page, the "Show
earlier" rule, the top-bar pop-over and the Activity button."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Message, MessageDirection, Ticket, User, UserRole
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


def test_one_persons_steps_on_one_ticket_are_one_expandable_card(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Printer on fire")
    ticket = ticket_named(db_session, "Printer on fire")
    client.post(f"/tickets/{ticket.number}/email", data={"body": "On our way"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Fuser again"})

    page = feed(client).text
    agent_card = page[page.index("Agent Ann"):]

    assert "worked on" in page and "Printer on fire" in page
    assert "Agent Ann" in page and '<details class="act-steps">' in page
    assert re.search(r"\d+ steps", page)
    assert "Emailed the requester" in page and "Added a note" in page  # the summary line
    assert "Triaged" in page
    assert "On our way" in agent_card and "Fuser again" in agent_card  # the steps, with their excerpts, inside the card
    assert page.count('<details class="act-steps">') == 1  # the requester's description is a card without one


def test_two_people_on_one_ticket_within_the_gap_share_one_card_with_a_line_each(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Shared work")
    ticket = ticket_named(db_session, "Shared work")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann looked"})
    second_agent(client, db_session, team)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Bea fixed"})

    page = feed(client, kind="notes").text

    assert cards_in(page) == 1
    assert "Agent Ann and Bea Brown worked on" in page or "Agent Ann" in page and "Bea Brown" in page
    assert "<strong>Agent Ann:</strong> Added a note" in page and "<strong>Bea Brown:</strong> Added a note" in page


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
    assert "Requester replied" in requester_card and "It is smoking" in requester_card and "<details" not in requester_card
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


def test_another_teams_ticket_is_not_in_the_page_or_the_popover(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    elsewhere = create_category(db_session, other, "Wireless")
    classified(client, category, "Mine")
    classified(client, elsewhere, "Theirs")

    assert "Theirs" not in feed(client).text
    popover = client.get("/tickets/activity/latest", headers={"HX-Request": "true"}).text
    assert "Mine" in popover and "Theirs" not in popover


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


def test_the_popover_is_a_compact_frameless_uncached_partial_of_about_15_cards(client, db_session):
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

    response = client.get("/tickets/activity/latest", headers={"HX-Request": "true"})

    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    page = response.text
    assert "<html" not in page and "<nav" not in page  # no frame
    assert page.count('class="act-card act-compact') == 15
    assert "<details" not in page and "act-steps" not in page  # compact: the summary line only, no expanding
    assert f'href="/tickets/{ticket.number}"' in page
    for label in ("All", "Changes", "Emails", "Notes"):
        assert f">{label}</button>" in page
    assert "Note 19" not in page  # a summary line, not the excerpt

    only_notes = client.get("/tickets/activity/latest?kind=notes", headers={"HX-Request": "true"}).text
    assert 'aria-pressed="true" data-quiet hx-get="/tickets/activity/latest?kind=notes"' in only_notes
    assert client.get("/tickets/activity/latest?kind=gossip", headers={"HX-Request": "true"}).status_code == 422


def test_the_popover_route_is_not_a_page_and_is_for_agents_only(client, db_session):
    desk(client, db_session)
    response = client.get("/tickets/activity/latest", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tickets/activity"
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert client.get("/tickets/activity/latest", headers={"HX-Request": "true"}).status_code == 403


def test_the_activity_button_is_in_the_console_frame_beside_the_bell_and_not_in_the_portal(client, db_session):
    desk(client, db_session)

    console = client.get("/tickets").text
    assert 'aria-label="Team activity"' in console and 'class="activity-menu"' in console
    assert console.index('class="activity-menu"') < console.index('class="bell-menu"')
    assert 'hx-get="/tickets/activity/latest"' in console and "Open full activity" in console
    assert "details.activity-menu" in console  # the menu script closes it on Escape and a click elsewhere, one at a time

    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    portal = client.get("/portal").text
    assert 'class="activity-menu"' not in portal and 'aria-label="Team activity"' not in portal


def test_the_queue_no_longer_has_its_own_team_activity_button(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Anything")

    queue = client.get("/tickets").text.split("<main", 1)[1]

    assert "Team activity" not in queue and 'href="/tickets/activity"' not in queue

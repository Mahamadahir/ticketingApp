"""Part 2 of splitting: the original's derived status and the single conversation."""

import threading
import time

import pytest
from sqlmodel import Session, select

from app.messages import about_labels, agent_timeline, requester_visible_messages, thread_subject
from app.models import Message, MessageDirection, Ticket, TicketEvent, TicketStatus, User, UserRole
from app.splitting import derived_status
from tests.conftest import create_team, login_as, set_role
from tests.test_splitting import split, subs_of, world
from tests.test_triage import events

OPEN, PROGRESS, AWAITING = TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.AWAITING_USER
RESOLVED, CLOSED = TicketStatus.RESOLVED, TicketStatus.CLOSED


def set_to(client, ticket, status):
    response = client.post(f"/tickets/{ticket.number}/status", data={"status": status.value})
    assert response.status_code == 200, response.text


def status_of(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id).status


def family(client, db_session, parts=2):
    original, desk = world(client, db_session)
    split(client, original, *[(f"Part {i + 1}", "") for i in range(parts)])
    return original, subs_of(db_session, original), desk


def as_requester(client):
    client.post("/auth/logout")
    login_as(client, "me@example.com")


def as_agent(client):
    client.post("/auth/logout")
    login_as(client, "agent@example.com")


# ---- The rule -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], None),
        ([OPEN, OPEN], OPEN),
        ([AWAITING, AWAITING, AWAITING], AWAITING),
        ([OPEN, PROGRESS], PROGRESS),  # more than one unfinished and they differ
        ([OPEN, OPEN, RESOLVED], PROGRESS),  # not all the same, two unfinished
        ([RESOLVED, PROGRESS], PROGRESS),  # exactly one unfinished: its status
        ([RESOLVED, CLOSED, AWAITING], AWAITING),
        ([RESOLVED, RESOLVED], None),  # never derived: an agent resolves the original
        ([RESOLVED, CLOSED], None),
        ([CLOSED, CLOSED], None),
    ],
)
def test_the_derived_status_rule(statuses, expected):
    assert derived_status(statuses) == expected


# ---- The original follows its parts --------------------------------------------------------------


def test_the_original_follows_its_parts_with_a_system_event_for_each_change(client, db_session):
    original, (a, b), _ = family(client, db_session)
    assert status_of(db_session, original) == OPEN

    set_to(client, a, PROGRESS)
    assert status_of(db_session, original) == PROGRESS  # one is in progress, one open
    set_to(client, b, PROGRESS)
    assert status_of(db_session, original) == PROGRESS  # all the same: no change
    set_to(client, a, RESOLVED)
    set_to(client, b, AWAITING)
    assert status_of(db_session, original) == AWAITING  # exactly one unfinished: its status
    set_to(client, b, RESOLVED)
    assert status_of(db_session, original) == PROGRESS  # all finished: nobody waits on the requester

    changes = [e for e in events(db_session, original, "status_changed") if e.detail]  # leaves out triage's
    assert [(e.from_value, e.to_value, e.actor_email, e.detail) for e in changes] == [
        ("open", "in_progress", None, "from its sub-tickets"),
        ("in_progress", "awaiting_user", None, "from its sub-tickets"),
        ("awaiting_user", "in_progress", None, "from its sub-tickets"),
    ]


def test_a_reply_on_an_original_whose_parts_are_all_done_does_not_leave_it_awaiting(client, db_session):
    original, (a, b), _ = family(client, db_session)
    set_to(client, a, RESOLVED)
    set_to(client, b, AWAITING)
    assert status_of(db_session, original) == AWAITING
    set_to(client, b, RESOLVED)

    as_requester(client)
    client.post(f"/portal/tickets/{original.number}/comment", data={"body": "thanks"})

    assert status_of(db_session, original) == PROGRESS
    assert status_of(db_session, a) == RESOLVED


def test_a_split_derives_the_status_once_not_once_per_part(client, db_session):
    original, desk = world(client, db_session)
    set_to(client, original, AWAITING)

    split(client, original, ("One", ""), ("Two", ""), ("Three", ""))

    assert status_of(db_session, original) == OPEN  # every new part is Open
    derived = [e for e in events(db_session, original, "status_changed") if e.detail == "from its sub-tickets"]
    assert len(derived) == 1


def test_when_every_part_is_done_the_original_says_to_resolve_it(client, db_session):
    original, (a, b), _ = family(client, db_session)
    assert "All sub-tickets are done" not in client.get(f"/tickets/{original.number}").text
    set_to(client, a, RESOLVED)
    assert "All sub-tickets are done" not in client.get(f"/tickets/{original.number}").text

    set_to(client, b, CLOSED)

    page = client.get(f"/tickets/{original.number}").text
    assert "All sub-tickets are done: resolve this ticket" in page
    assert status_of(db_session, original) == OPEN  # it does not resolve itself


def test_a_finished_original_is_left_alone_when_a_part_changes(client, db_session):
    original, (a, b), _ = family(client, db_session)
    set_to(client, a, RESOLVED)
    set_to(client, b, RESOLVED)
    set_to(client, original, RESOLVED)

    set_to(client, b, PROGRESS)  # a part is reopened

    assert status_of(db_session, original) == RESOLVED


# ---- Status cannot be set by hand ----------------------------------------------------------------


def test_an_original_with_parts_cannot_have_its_status_set_by_hand(client, db_session):
    original, (a, b), _ = family(client, db_session)

    refused = client.post(f"/tickets/{original.number}/status", data={"status": "in_progress"})
    early = client.post(f"/tickets/{original.number}/status", data={"status": "resolved"})

    assert refused.status_code == 400 and "follows them" in refused.text
    assert early.status_code == 400 and "2 still open" in early.text
    assert status_of(db_session, original) == OPEN

    set_to(client, a, RESOLVED)
    set_to(client, b, RESOLVED)
    set_to(client, original, RESOLVED)  # now an agent can resolve it
    assert status_of(db_session, original) == RESOLVED


def test_a_ticket_without_parts_is_unaffected(client, db_session):
    original, desk = world(client, db_session)

    set_to(client, original, PROGRESS)
    set_to(client, original, RESOLVED)

    assert status_of(db_session, original) == RESOLVED


def test_the_status_page_explains_that_it_follows_the_parts(client, db_session):
    original, (a, _), _ = family(client, db_session)

    assert "its status follows them" in client.get(f"/tickets/{original.number}").text
    assert "its status follows them" not in client.get(f"/tickets/{a.number}").text


# ---- Emailing and replying -------------------------------------------------------------------------


def test_emailing_from_the_original_does_not_move_its_status_but_a_part_does(client, db_session):
    original, (a, b), _ = family(client, db_session)

    assert client.post(f"/tickets/{original.number}/email", data={"body": "Hello"}).status_code == 200
    assert status_of(db_session, original) == OPEN  # derived, so not moved to Awaiting user

    assert client.post(f"/tickets/{a.number}/email", data={"body": "About part one"}).status_code == 200
    assert status_of(db_session, a) == AWAITING
    assert status_of(db_session, original) == PROGRESS  # one waiting, one open


def test_a_reply_on_the_original_refreshes_every_open_part(client, db_session):
    original, (a, b, c), _ = family(client, db_session, parts=3)
    set_to(client, a, AWAITING)  # b stays Open: refreshed (moves up the queue) but not moved
    set_to(client, c, RESOLVED)
    db_session.expire_all()
    resolved_at = db_session.get(Ticket, c.id).updated_at
    before = {t.id: db_session.get(Ticket, t.id).updated_at for t in (a, b)}

    as_requester(client)
    assert client.post(f"/portal/{original.number}/comment", data={"body": "Which one is fixed?"}).status_code == 200

    db_session.expire_all()
    assert status_of(db_session, a) == PROGRESS and status_of(db_session, b) == OPEN
    assert status_of(db_session, c) == RESOLVED and db_session.get(Ticket, c.id).updated_at == resolved_at
    assert all(db_session.get(Ticket, t.id).updated_at > before[t.id] for t in (a, b))
    [handed_back] = [e for e in events(db_session, a, "status_changed") if e.to_value == "in_progress"]
    assert (handed_back.actor_email, handed_back.detail) == ("me@example.com", "requester replied")


def test_a_reply_on_a_part_refreshes_only_that_part(client, db_session):
    original, (a, b), _ = family(client, db_session)
    set_to(client, a, AWAITING)
    set_to(client, b, AWAITING)

    as_requester(client)
    client.post(f"/portal/{a.number}/comment", data={"body": "About part one"})

    assert status_of(db_session, a) == PROGRESS and status_of(db_session, b) == AWAITING


# ---- One conversation ----------------------------------------------------------------------------------


def bodies(messages):
    return [m.body for m in messages]


def test_the_originals_emails_and_replies_show_on_every_open_part_but_siblings_stay_apart(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "From the original"})
    client.post(f"/tickets/{a.number}/email", data={"body": "From part one"})
    client.post(f"/tickets/{b.number}/email", data={"body": "From part two"})
    as_requester(client)
    client.post(f"/portal/{original.number}/comment", data={"body": "Requester reply"})
    db_session.expire_all()

    on_original = bodies(requester_visible_messages(db_session, original))
    on_a = bodies(requester_visible_messages(db_session, a))
    on_b = bodies(requester_visible_messages(db_session, b))

    assert set(on_original) == {"From the original", "From part one", "From part two", "Requester reply"}
    assert set(on_a) == {"From the original", "From part one", "Requester reply"}  # not part two's
    assert set(on_b) == {"From the original", "From part two", "Requester reply"}
    assert on_original == sorted(on_original, key=lambda body: next(
        m.created_at for m in db_session.exec(select(Message).where(Message.body == body)).all()
    ))


def test_messages_are_stored_once_not_copied(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "Only once"})

    assert len(db_session.exec(select(Message).where(Message.body == "Only once")).all()) == 1


def test_internal_notes_never_cross_tickets(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{original.number}/note", data={"body": "Note on the original"})
    client.post(f"/tickets/{a.number}/note", data={"body": "Note on part one"})

    for page_of in (original, a, b):
        page = client.get(f"/tickets/{page_of.number}").text
        assert ("Note on the original" in page) == (page_of is original)
        assert ("Note on part one" in page) == (page_of is a)
    db_session.expire_all()
    for ticket in (original, a, b):
        assert not [m for m in requester_visible_messages(db_session, ticket) if "Note on" in m.body]


def test_a_finished_part_no_longer_shows_the_originals_conversation(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "Before"})
    set_to(client, a, RESOLVED)
    client.post(f"/tickets/{original.number}/email", data={"body": "After"})
    db_session.expire_all()

    assert "After" not in bodies(requester_visible_messages(db_session, a))
    assert "After" in bodies(requester_visible_messages(db_session, b))


def test_the_agent_timeline_labels_what_belongs_to_another_ticket(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{a.number}/email", data={"body": "From part one"})
    set_to(client, a, PROGRESS)

    page = client.get(f"/tickets/{original.number}").text

    assert f"[{a.reference} {a.subject}]" in page and "From part one" in page
    # A part's progress shows on its original, not the other way round.
    assert f"[{original.reference}" not in client.get(f"/tickets/{a.number}").text
    assert "From part one" not in client.get(f"/tickets/{b.number}").text


def test_the_originals_timeline_hides_parts_the_viewer_cannot_open(client, db_session):
    original, (a, b), desk = family(client, db_session)
    client.post(f"/tickets/{a.number}/email", data={"body": "Visible part email"})
    client.post(f"/tickets/{b.number}/email", data={"body": "Hidden part email"})
    secret = create_team(db_session, "Security")
    hidden = db_session.get(Ticket, b.id)
    hidden.team_id = secret.id
    db_session.add(hidden)
    db_session.commit()

    page = client.get(f"/tickets/{original.number}").text
    assert "Visible part email" in page
    assert "Hidden part email" not in page and hidden.reference not in page.split("Timeline")[1]

    # A super admin can open every part, so sees it all.
    client.post("/auth/logout")
    login_as(client, "admin@example.com")
    assert "Hidden part email" in client.get(f"/tickets/{original.number}").text


def test_a_part_shows_the_originals_conversation_only_to_agents_whose_team_can_open_the_original(
    client, db_session
):
    original, (a, b), desk = family(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "On the original"})
    assert "On the original" in client.get(f"/tickets/{a.number}").text  # same team: shown

    # The part moves to another team; its agent has no access to the original.
    other = create_team(db_session, "Security")
    part = db_session.get(Ticket, a.id)
    part.team_id = other.id
    db_session.add(part)
    db_session.commit()
    client.post("/auth/logout")
    outsider_id = login_as(client, "outsider@example.com")
    set_role(db_session, outsider_id, UserRole.AGENT, other.id)

    page = client.get(f"/tickets/{a.number}").text
    assert "Part 1" in page and "On the original" not in page

    # The requester's own conversation is unaffected.
    db_session.expire_all()
    assert "On the original" in bodies(requester_visible_messages(db_session, db_session.get(Ticket, a.id)))


def test_a_timeline_asked_for_with_no_viewer_shows_no_part_items(client, db_session):
    original, (a, _), _ = family(client, db_session)
    client.post(f"/tickets/{a.number}/email", data={"body": "From part one"})
    db_session.expire_all()

    items = agent_timeline(db_session, db_session.get(Ticket, original.id))

    assert not [i for i in items if i.source is not None]


# ---- One mail thread --------------------------------------------------------------------------------


def test_every_email_names_the_part_it_is_about(client, db_session):
    original, (a, b), _ = family(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "General"})
    client.post(f"/tickets/{a.number}/email", data={"body": "Specific"})
    db_session.expire_all()
    messages = requester_visible_messages(db_session, db_session.get(Ticket, original.id))

    labels = about_labels(db_session, messages)

    by_body = {m.body: labels.get(m.id) for m in messages}
    assert by_body["General"] == f"About: {original.reference} {original.subject}"
    assert by_body["Specific"] == f"About: {a.reference} {a.subject}"
    as_requester(client)
    assert f"About: {a.reference} {a.subject}" in client.get(f"/portal/{original.number}").text


def test_an_email_on_a_ticket_that_was_never_split_has_no_about_line(client, db_session):
    original, desk = world(client, db_session)
    client.post(f"/tickets/{original.number}/email", data={"body": "Plain"})
    db_session.expire_all()
    messages = requester_visible_messages(db_session, db_session.get(Ticket, original.id))

    assert about_labels(db_session, messages) == {}


def test_every_part_sends_under_the_originals_subject(client, db_session):
    original, (a, b), _ = family(client, db_session)
    db_session.expire_all()

    assert thread_subject(db_session, db_session.get(Ticket, a.id)) == original.subject
    assert thread_subject(db_session, db_session.get(Ticket, original.id)) == original.subject


# ---- Two parts changing at once ---------------------------------------------------------------------


def test_two_parts_changing_at_once_leave_the_original_with_the_right_status(client, db_session):
    from app.database import engine
    from app.ticket_service import set_status

    original, (a, b), _ = family(client, db_session)
    holding, release = threading.Event(), threading.Event()

    errors = []

    def first():  # part one goes to Awaiting user, then waits before committing
        try:
            with Session(engine) as s:
                set_status(s, s.get(Ticket, a.id), AWAITING, "x@example.com")
                holding.set()
                release.wait(10)
                s.commit()
        except Exception as exc:  # a crash must fail the test, not vanish in the thread
            errors.append(exc)
            holding.set()

    def second():  # part two does the same while the first is still open
        try:
            with Session(engine) as s:
                set_status(s, s.get(Ticket, b.id), AWAITING, "y@example.com")
                s.commit()
        except Exception as exc:
            errors.append(exc)

    one = threading.Thread(target=first)
    one.start()
    assert holding.wait(10)
    two = threading.Thread(target=second)
    two.start()
    time.sleep(0.7)  # with the lock, the second is waiting here
    release.set()
    one.join(10)
    two.join(10)
    assert not one.is_alive() and not two.is_alive()
    assert errors == []

    # Both parts wait on the requester, so the original does too. Without the lock each
    # saw the other as still Open and left it In progress.
    assert status_of(db_session, original) == AWAITING

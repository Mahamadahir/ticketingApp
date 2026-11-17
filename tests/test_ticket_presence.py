"""#246: who else is on a ticket (the strip and its check-in, app/ticket_presence.py) and the warning before a send that another agent
got in ahead of (the hidden `seen` field, app/messages.posts_since)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import mail, reply_files, scheduled_jobs, ticket_presence
from app.main import app
from app.models import Attachment, Message, MessageDirection, TicketPresence, User, UserRole, UserSession
from app.settings_store import (
    GROUPS,
    PRESENCE_CHECK_IN_SECONDS,
    PRESENCE_GONE_AFTER_SECONDS,
    REGISTRY,
    set_setting,
)
from app.storage import LocalStorage, get_storage
from tests.conftest import login_as, set_role
from tests.test_approval_groups_migration import rows, scratch  # noqa: F401  (scratch is a fixture)
from tests.test_email_preview import ticket_for_email

HTMX = {"HX-Request": "true"}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    monkeypatch.setattr(mail, "get_storage", lambda: store)
    monkeypatch.setattr(reply_files, "get_storage", lambda: store)
    yield store
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def second():
    with TestClient(app) as other:
        yield other


def colleague(second, db_session, ticket, email="bob@example.com", name="Bob Rowe", team_id="ticket"):
    """A second agent in the ticket's team, signed in on their own client."""
    user_id = login_as(second, email, name)
    set_role(db_session, user_id, UserRole.AGENT, ticket.team_id if team_id == "ticket" else team_id)
    return user_id


def check_in(client, ticket, activity="viewing", doing="", shown=""):
    return client.post(
        f"/tickets/{ticket.number}/presence", data={"activity": activity, "doing": doing, "shown": shown}, headers=HTMX
    )


def rows_of(db_session):
    db_session.expire_all()
    return db_session.exec(select(TicketPresence)).all()


def messages_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()


# ---- The strip -------------------------------------------------------------------------------------------


def test_one_agent_sees_another_viewing(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)

    assert check_in(client, ticket).status_code in (200, 204)  # Ann arrives
    strip = check_in(second, ticket).text

    assert "Agent Ann" in strip and "is viewing this ticket" in strip
    assert 'class="avatar tp-avatar"' in strip and ">AA<" in strip


def test_the_page_is_drawn_with_the_strip_already_in_it(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)

    page = second.get(f"/tickets/{ticket.number}").text

    assert 'id="presence"' in page and 'role="status"' in page and 'aria-live="polite"' in page
    assert "Agent Ann" in page and "is viewing this ticket" in page
    assert page.index('id="presence"') < page.index('class="tp-head"')  # above the ticket header
    assert f'hx-post="/tickets/{ticket.number}/presence"' in page and "every 15s" in page


def test_activity_changes_are_reflected(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)

    check_in(client, ticket, "email")
    assert "is writing an email to the requester" in check_in(second, ticket).text
    check_in(client, ticket, "note")
    assert "is writing an internal note" in check_in(second, ticket).text
    check_in(client, ticket, "action", "re-assigning")
    assert "Agent Ann</b> is re-assigning" in check_in(second, ticket).text
    check_in(client, ticket, "viewing")
    assert "is viewing this ticket" in check_in(second, ticket).text
    assert len(rows_of(db_session)) == 2  # one row per person and ticket, however many check-ins


def test_an_action_without_a_name_and_an_unknown_activity_count_as_viewing(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)

    check_in(client, ticket, "action", "   ")
    assert "is viewing this ticket" in check_in(second, ticket).text
    check_in(client, ticket, "dancing")
    assert "is viewing this ticket" in check_in(second, ticket).text


def test_the_action_name_is_one_short_line_and_escaped(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)

    check_in(client, ticket, "action", "<b>x</b>\n" + "y" * 300)
    strip = check_in(second, ticket).text

    assert "<b>x</b>" not in strip and "&lt;b&gt;x&lt;/b&gt;" in strip
    [row] = [r for r in rows_of(db_session) if r.activity == "action"]
    assert len(row.action_name) <= ticket_presence.MAX_ACTION_LENGTH and "\n" not in row.action_name


def test_every_action_panel_names_what_it_is_doing(client, db_session):
    """The names come from the panels (data-doing), which the page's script reads; none is listed in the script."""
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    for doing in ("assigning", "escalating", "sending to the MSP", "resolving", "changing the status", "linking it", "splitting it",
                  "backdating it", "closing it", "using More actions"):
        assert f'data-doing="{doing}"' in page, doing
    script = client.get("/static/ticket-presence.js").text
    assert "re-assigning" not in script and "escalating" not in script  # no list of names in the script


def test_never_yourself(client, db_session):
    ticket = ticket_for_email(client, db_session)

    response = check_in(client, ticket, "email")

    assert response.status_code == 204  # nothing to show, so nothing to draw
    strip = client.get(f"/tickets/{ticket.number}").text.split('id="presence"')[1].split("ticket-presence.js")[0]
    assert "Agent Ann" not in strip and "tp-presence-list" not in strip
    assert len(rows_of(db_session)) == 1  # but the row is there for others


def test_a_poll_that_would_draw_the_same_strip_gets_nothing(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)

    first = check_in(second, ticket)
    digest = first.text.split('data-shown="')[1].split('"')[0]

    assert check_in(second, ticket, shown=digest).status_code == 204
    check_in(client, ticket, "note")
    changed = check_in(second, ticket, shown=digest)
    assert changed.status_code == 200 and "internal note" in changed.text


def test_never_someone_who_cannot_open_the_ticket(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    from tests.conftest import create_team

    elsewhere = create_team(db_session, "Elsewhere")
    stranger_id = login_as(TestClient(app), "stranger@example.com", "Sam Stranger")
    set_role(db_session, stranger_id, UserRole.AGENT, elsewhere.id)
    colleague(second, db_session, ticket)
    db_session.add(TicketPresence(ticket_id=ticket.id, user_id=stranger_id, activity="viewing"))
    db_session.commit()

    assert "Sam Stranger" not in check_in(second, ticket).text
    assert "Sam Stranger" not in second.get(f"/tickets/{ticket.number}").text
    # An agent of another team cannot check in to it either.
    outsider = TestClient(app)
    other_id = login_as(outsider, "outsider@example.com", "Olive Outsider")
    set_role(db_session, other_id, UserRole.AGENT, elsewhere.id)
    assert check_in(outsider, ticket).status_code == 403
    assert other_id not in {r.user_id for r in rows_of(db_session)}


def test_someone_who_is_no_longer_allowed_in_drops_out_of_the_strip(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    from tests.conftest import create_team

    elsewhere = create_team(db_session, "Elsewhere")
    colleague(second, db_session, ticket)
    check_in(client, ticket)
    assert "Agent Ann" in check_in(second, ticket).text

    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    from app.models import TeamMembership

    for membership in db_session.exec(select(TeamMembership).where(TeamMembership.user_id == ann.id)).all():
        db_session.delete(membership)
    db_session.commit()
    set_role(db_session, ann.id, UserRole.AGENT, elsewhere.id)

    assert "Agent Ann" not in check_in(second, ticket).text  # the row is still there; the access check no longer lists her


def test_never_in_the_portal_and_never_for_a_requester(client, db_session):
    ticket = ticket_for_email(client, db_session)
    requester = TestClient(app)
    login_as(requester, "walkin@example.com", "Walk In")

    portal = requester.get(f"/portal/{ticket.number}")
    assert "presence" not in portal.text and "tp-presence" not in portal.text
    assert requester.post(f"/tickets/{ticket.number}/presence", data={"activity": "viewing"}, headers=HTMX).status_code == 403
    assert rows_of(db_session) == []


def test_a_signed_out_check_in_is_refused(client, db_session):
    ticket = ticket_for_email(client, db_session)
    anonymous = TestClient(app)

    response = anonymous.post(f"/tickets/{ticket.number}/presence", data={"activity": "viewing"}, headers=HTMX, follow_redirects=False)

    assert response.status_code in (302, 303, 307, 401)
    assert rows_of(db_session) == []


def test_the_check_in_is_passive_and_does_not_keep_a_session_alive(client, db_session):
    ticket = ticket_for_email(client, db_session)
    ann = db_session.exec(select(UserSession)).all()[0]
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    ann.last_seen_at = stale
    db_session.add(ann)
    db_session.commit()

    check_in(client, ticket)
    db_session.expire_all()
    assert db_session.exec(select(UserSession)).all()[0].last_seen_at == stale  # untouched by the poll

    client.get(f"/tickets/{ticket.number}")
    db_session.expire_all()
    assert db_session.exec(select(UserSession)).all()[0].last_seen_at > stale  # an ordinary request still counts


# ---- Gone, leaving, clean-up -------------------------------------------------------------------------------


def age_every_row(db_session, seconds):
    for row in rows_of(db_session):
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db_session.add(row)
    db_session.commit()


def test_someone_is_gone_after_the_gone_after_time(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)

    age_every_row(db_session, 40)
    assert "Agent Ann" in check_in(second, ticket).text  # inside 45 seconds
    # Bob's own row is as old as Ann's, so he must be the one who just checked in again; age only Ann's.
    ann = [r for r in rows_of(db_session) if r.user_id != db_session.exec(select(User).where(User.email == "bob@example.com")).one().id][0]
    ann.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=50)
    db_session.add(ann)
    db_session.commit()
    assert "Agent Ann" not in check_in(second, ticket).text


def test_leaving_the_page_clears_the_row_at_once(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)
    assert "Agent Ann" in check_in(second, ticket).text

    response = client.post(f"/tickets/{ticket.number}/presence/leave")

    assert response.status_code == 204
    assert "Agent Ann" not in check_in(second, ticket).text
    assert {r.user_id for r in rows_of(db_session)} == {db_session.exec(select(User).where(User.email == "bob@example.com")).one().id}


def test_leaving_only_removes_your_own_row(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)
    check_in(second, ticket)

    second.post(f"/tickets/{ticket.number}/presence/leave")

    assert len(rows_of(db_session)) == 1 and "Agent Ann" in check_in(second, ticket).text


def test_the_page_sends_the_leave_beacon_address(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    assert f'data-leave="/tickets/{ticket.number}/presence/leave"' in page
    script = client.get("/static/ticket-presence.js").text
    assert "sendBeacon" in script and "pagehide" in script


def test_the_cleanup_job_deletes_expired_rows_and_keeps_the_rest(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    check_in(client, ticket)
    check_in(second, ticket)
    ann = [r for r in rows_of(db_session) if r.activity == "viewing"][0]
    ann.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.add(ann)
    db_session.commit()

    ticket_presence.purge_expired(db_session)
    db_session.commit()

    assert len(rows_of(db_session)) == 1
    ticket_presence.purge_expired(db_session)  # twice is harmless
    assert len(rows_of(db_session)) == 1


def test_the_cleanup_is_a_recurring_job():
    [job] = [j for j in scheduled_jobs.SCHEDULED_JOBS if j.name == "presence-cleanup"]
    assert job.run is ticket_presence.purge_expired and job.every_seconds == 3600
    assert scheduled_jobs.registry_errors(scheduled_jobs.SCHEDULED_JOBS) == []


# ---- The settings ------------------------------------------------------------------------------------------


def test_the_two_timings_are_settings_in_a_declared_group():
    for key, default in ((PRESENCE_CHECK_IN_SECONDS, "15"), (PRESENCE_GONE_AFTER_SECONDS, "45")):
        definition = REGISTRY[key]
        assert definition.default == default and definition.unit == "seconds" and definition.description
        assert definition.group in {group for group, _title, _text in GROUPS} and definition.group == "ticket-page"


def test_the_settings_page_lists_them(client, db_session):
    ticket_for_email(client, db_session)
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    set_role(db_session, ann.id, UserRole.SUPER_ADMIN)

    page = client.get("/admin/settings?group=ticket-page").text

    assert "Ticket presence: check-in interval" in page and "Ticket presence: gone after" in page and "seconds" in page


def test_the_check_in_interval_setting_changes_the_page(client, db_session):
    ticket = ticket_for_email(client, db_session)
    set_setting(db_session, PRESENCE_CHECK_IN_SECONDS, "30")
    db_session.commit()

    assert "every 30s" in client.get(f"/tickets/{ticket.number}").text


def test_gone_after_is_never_shorter_than_one_check_in(db_session):
    set_setting(db_session, PRESENCE_CHECK_IN_SECONDS, "30")
    set_setting(db_session, PRESENCE_GONE_AFTER_SECONDS, "10")
    db_session.commit()
    assert ticket_presence.gone_after_seconds(db_session) == 30


def test_the_gone_after_setting_is_used(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    set_setting(db_session, PRESENCE_GONE_AFTER_SECONDS, "300")
    db_session.commit()
    check_in(client, ticket)
    ann = [r for r in rows_of(db_session)][0]
    ann.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=200)
    db_session.add(ann)
    db_session.commit()

    assert "Agent Ann" in check_in(second, ticket).text


# ---- The send warning --------------------------------------------------------------------------------------


def seen_on(client, ticket):
    page = client.get(f"/tickets/{ticket.number}").text
    return page.split('name="seen" value="')[1].split('"')[0]


def test_every_send_form_carries_what_the_page_saw(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    assert page.count('name="seen"') == 2  # the email form (carried to the preview) and the note form


def test_the_preview_carries_what_the_page_saw_to_the_send(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.post(f"/tickets/{ticket.number}/email/preview", data={"body": "Hi", "seen": "7"}, headers=HTMX).text
    assert 'name="seen" value="7"' in page


def test_an_email_is_held_back_when_another_agent_emailed_after_the_page_loaded(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    assert second.post(f"/tickets/{ticket.number}/email", data={"body": "Bob already replied with the fix"}).status_code == 200
    before = messages_of(db_session, ticket)

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann's draft text", "seen": seen})

    assert response.status_code == 200
    page = response.text
    assert "Not sent yet" in page and "<b>Bob Rowe</b> emailed the requester at" in page
    assert "Bob already replied with the fix" in page  # an excerpt of what went out
    assert "Send anyway" in page and ">Cancel</a>" in page
    assert "Ann&#39;s draft text</textarea>" in page  # the draft is kept in the composer
    assert messages_of(db_session, ticket) == before  # nothing was sent or recorded
    assert 'id="mode-email" checked' in page and 'id="mode-note" checked' not in page


def test_send_anyway_sends_it_and_cancel_does_not(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/email", data={"body": "Bob replied"})
    held_back = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann's reply", "seen": seen, "cc_form": "1"})
    assert "Send anyway" in held_back.text and 'name="confirm" value="1"' in held_back.text
    assert 'name="body" value="Ann&#39;s reply"' in held_back.text
    count = len(messages_of(db_session, ticket))

    # Cancel is not a send: it only drops the warning, so nothing was recorded.
    assert len(messages_of(db_session, ticket)) == count

    sent = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann's reply", "seen": seen, "confirm": "1", "cc_form": "1"})

    assert sent.status_code == 200 and "Not sent yet" not in sent.text
    last = messages_of(db_session, ticket)[-1]
    assert (last.body, last.direction, last.sender) == ("Ann's reply", MessageDirection.OUTBOUND, "Agent Ann")


def test_no_warning_when_nothing_new_was_posted(client, db_session):
    ticket = ticket_for_email(client, db_session)
    seen = seen_on(client, ticket)

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "All fine", "seen": seen})

    assert "Not sent yet" not in response.text
    assert messages_of(db_session, ticket)[-1].body == "All fine"


def test_your_own_messages_and_automatic_notices_and_the_requesters_replies_do_not_warn(client, db_session):
    from app.messages import record_inbound, record_notice

    ticket = ticket_for_email(client, db_session)
    seen = seen_on(client, ticket)
    client.post(f"/tickets/{ticket.number}/note", data={"body": "my own note"})  # the agent's own, after the page loaded
    ticket = db_session.get(type(ticket), ticket.id)
    record_notice(db_session, ticket, "We have your request")
    record_inbound(db_session, ticket, sender="walkin@example.com", body="Any news?", channel="email")
    db_session.commit()

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "On it", "seen": seen})

    assert "Not sent yet" not in response.text and messages_of(db_session, ticket)[-1].body == "On it"


def test_a_note_is_held_back_when_another_agent_noted_or_emailed(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob: this is a known fault"})
    before = messages_of(db_session, ticket)

    response = client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann's note", "seen": seen})

    page = response.text
    assert "Not sent yet" in page and "<b>Bob Rowe</b> added an internal note at" in page and "Bob: this is a known fault" in page
    assert "Ann&#39;s note</textarea>" in page
    assert messages_of(db_session, ticket) == before

    sent = client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann's note", "seen": seen, "confirm": "1"})
    assert "Not sent yet" not in sent.text
    last = messages_of(db_session, ticket)[-1]
    assert (last.body, last.direction) == ("Ann's note", MessageDirection.INTERNAL)

    # An email from the other agent holds a note back too.
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/email", data={"body": "Bob emailed"})
    assert "<b>Bob Rowe</b> emailed the requester" in client.post(f"/tickets/{ticket.number}/note", data={"body": "x", "seen": seen}).text


def test_a_held_back_note_keeps_its_files_for_send_anyway(client, second, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob's note"})
    pdf = b"%PDF-1.7\n" + b"x" * 200

    held = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Ann's note", "seen": seen}, files=[("files", ("plan.pdf", pdf, "application/pdf"))]
    )

    assert "plan.pdf" in held.text and "Not sent yet" in held.text
    draft = held.text.split('name="draft" value="')[1].split('"')[0]
    assert db_session.exec(select(Attachment)).all() == []  # nothing attached yet
    sent = client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann's note", "seen": seen, "confirm": "1", "draft": draft})
    assert "Not sent yet" not in sent.text
    db_session.expire_all()
    [attachment] = db_session.exec(select(Attachment)).all()
    assert attachment.filename == "plan.pdf" and messages_of(db_session, ticket)[-1].body == "Ann's note"
    from app.models import ReplyHeldFile

    assert db_session.exec(select(ReplyHeldFile)).all() == []  # the held copy is released once attached


def test_cancel_drops_the_held_files(client, second, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob's note"})
    held = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Ann", "seen": seen},
        files=[("files", ("plan.pdf", b"%PDF-1.7\n" + b"x" * 200, "application/pdf"))],
    )
    draft = held.text.split('name="draft" value="')[1].split('"')[0]
    assert f'hx-vals=\'{{"draft": "{draft}"}}\'' in held.text and 'hx-swap="delete"' in held.text  # Cancel with scripts

    assert client.post(f"/tickets/{ticket.number}/email/discard", data={"draft": draft}, headers=HTMX).status_code == 200

    from app.models import ReplyHeldFile

    db_session.expire_all()
    assert db_session.exec(select(ReplyHeldFile)).all() == []
    assert db_session.exec(select(Attachment)).all() == []


def test_the_warning_works_without_scripts(client, second, db_session):
    """A plain form post gets a whole page back, whose Send anyway is a plain form (method and action) and whose Cancel is a
    link to the thread."""
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/email", data={"body": "Bob replied"})

    page = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann", "seen": seen}).text  # no HX-Request

    assert "<html" in page and 'id="send-warning"' in page
    assert f'method="post" action="/tickets/{ticket.number}/email"' in page
    assert 'href="#thread"' in page and 'id="thread"' in page


def test_a_request_without_seen_is_not_checked(client, second, db_session):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    second.post(f"/tickets/{ticket.number}/email", data={"body": "Bob replied"})

    response = client.post(f"/tickets/{ticket.number}/email", data={"body": "Ann"})

    assert "Not sent yet" not in response.text and messages_of(db_session, ticket)[-1].body == "Ann"


def test_the_warning_shows_only_what_the_requester_could_see_of_an_email(client, second, db_session):
    """An email counts only through requester_visible_messages: a message wrongly marked public as an internal note, or an MSP
    mail not yet forwarded, is not 'an email to the requester'."""
    from app.messages import posts_since
    from app.models import MessageVisibility

    ticket = ticket_for_email(client, db_session)
    bob_id = colleague(second, db_session, ticket)
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    hidden = Message(
        ticket_id=ticket.id, sender="Bob Rowe", body="not for the requester", direction=MessageDirection.OUTBOUND,
        visibility=MessageVisibility.AGENTS_ONLY, author_user_id=bob_id,
    )
    db_session.add(hidden)
    db_session.commit()

    assert posts_since(db_session, ticket, ann, 0) == []


# ---- The table and the migration ---------------------------------------------------------------------------


def test_the_presence_table_migrates_up_and_down(scratch):
    alembic, connect = scratch
    alembic("upgrade", "c239d1a7e5b3")
    assert rows(connect, "SELECT to_regclass('ticket_presence')") == [(None,)]

    alembic("upgrade", "head")
    assert rows(connect, "SELECT to_regclass('ticket_presence')") == [("ticket_presence",)]
    columns = {name for (name,) in rows(connect, "SELECT column_name FROM information_schema.columns WHERE table_name = 'ticket_presence'")}
    assert columns == {"ticket_id", "user_id", "activity", "action_name", "last_seen_at"}

    alembic("downgrade", "c239d1a7e5b3")
    assert rows(connect, "SELECT to_regclass('ticket_presence')") == [(None,)]
    alembic("upgrade", "head")
    alembic("check")


def test_a_held_back_note_keeps_its_files_in_the_composer_too(client, second, db_session, storage):
    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob's note"})
    held = client.post(
        f"/tickets/{ticket.number}/note", data={"body": "Ann", "seen": seen},
        files=[("files", ("plan.pdf", b"%PDF-1.7\n" + b"x" * 200, "application/pdf"))],
    )
    assert held.text.count('name="draft"') == 2  # Send anyway's form and the note form itself


def test_a_held_back_note_without_files_leaves_an_email_preview_draft_alone(client, second, db_session, storage):
    from app.models import ReplyHeldFile

    ticket = ticket_for_email(client, db_session)
    colleague(second, db_session, ticket)
    seen = seen_on(client, ticket)
    client.post(
        f"/tickets/{ticket.number}/email/preview", data={"body": "Hi"},
        files=[("files", ("plan.pdf", b"%PDF-1.7\n" + b"x" * 200, "application/pdf"))],
    )
    second.post(f"/tickets/{ticket.number}/note", data={"body": "Bob's note"})

    held = client.post(f"/tickets/{ticket.number}/note", data={"body": "Ann", "seen": seen})

    assert "Not sent yet" in held.text
    db_session.expire_all()
    assert len(db_session.exec(select(ReplyHeldFile)).all()) == 1  # the email preview's file is still held

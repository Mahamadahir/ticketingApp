"""Giving someone Full access and/or Send As on your own mailbox (#310): the one form, the warning that always shows, one request with
one change for each permission, and what happens when one of them fails. Exchange is the FakeExecutor; Graph is replaced where a
directory answer is needed (the fixtures are the forwarding form's, #309)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app import portal_mailbox
from app.database import engine
from app.mailbox_changes import run_mailbox_changes
from app.mailbox_executor import OUTCOME_PERMANENT, FakeExecutor, set_executor
from app.models import (
    MailboxChange, Message, MessageDirection, OutboundEmail, Team, TeamMembership, Ticket, TicketStatus, User, UserRole,
)
from app.outbox import MAILBOX_CHANGE
from app.request_types import TypeDefaults, create_request_type
from app.settings_store import DEFAULT_TIME_ZONE, MAILBOX_CHANGE_MAX_DAYS, set_setting
from tests.conftest import create_category, create_team, login_as
from tests.test_portal_mailbox import (  # noqa: F401  (fixtures, and the helpers every form test uses)
    FRIEND_OID, OWNER, directory, friend, later, no_change, now, signed_in,
)
from tests.test_resolution import dev_mail  # noqa: F401  (a fixture)


@pytest.fixture(autouse=True)
def fake_exchange(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()
    executor = FakeExecutor()
    set_executor(executor)
    yield executor
    set_executor(None)


@pytest.fixture
def world(db_session):
    """A team and category, and a request type with the mailbox action full_access that triages itself."""
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    request_type = create_request_type(
        db_session, "Give someone access to my mailbox", "Full access or Send As", None, 52,
        defaults=TypeDefaults(team.id, category.id, "low", "low"), mailbox_action="full_access",
    )
    db_session.commit()
    db_session.refresh(request_type)
    return SimpleNamespace(team=team, category=category, type=request_type)


def form(world, **extra):
    end_date, end_time = later(30)
    data = {
        "request_type_id": world.type.id, "target_object_id": FRIEND_OID, "target_name": "Jane Friend", "full_access": "on",
        "send_as": "on", "start_mode": "now", "end_date": end_date, "end_time": end_time, "confirm_access": "1",
    }
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def post(client, world, **extra):
    return client.post("/portal/mailbox-change", data=form(world, **extra), follow_redirects=False)


def changes(db_session):
    db_session.expire_all()
    return db_session.exec(select(MailboxChange).order_by(MailboxChange.id)).all()


def run(db_session, at):
    db_session.expire_all()
    with Session(engine) as session:
        return run_mailbox_changes(session, at)


def reload(db_session, *rows):
    db_session.expire_all()
    out = [db_session.get(type(row), row.id) for row in rows]
    return out[0] if len(out) == 1 else out


def mails(db_session):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).order_by(OutboundEmail.id)).all()


def internal_notes(db_session, ticket):
    db_session.expire_all()
    rows = db_session.exec(select(Message).where(Message.ticket_id == ticket.id).order_by(Message.id)).all()
    return [m for m in rows if m.direction == MessageDirection.INTERNAL]


def key(change):
    from app.mailbox_executor import _key

    return _key(change)


def both(client, db_session, world, friend):
    """A signed-in owner who has asked for both: returns the ticket and the two changes (full access first)."""
    signed_in(client, db_session)
    assert post(client, world).status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    full, send = changes(db_session)
    return ticket, full, send


# ---- The form -------------------------------------------------------------------------------------------------------


def test_the_kind_opens_the_access_form_with_its_own_box_ticked_and_the_modal(client, db_session, world):
    signed_in(client, db_session)
    page = client.get(f"/portal/new?request_type_id={world.type.id}").text
    assert 'data-warn="access"' in page and 'name="full_access"' in page and 'name="send_as"' in page
    assert 'id="mb-full-access" checked' in page and 'id="mb-send-as" checked' not in page
    assert 'name="confirm_access"' in page and 'id="mb-dialog"' in page and 'id="mb-dialog-cancel" autofocus' in page
    assert 'class="btn btn-danger" id="mb-dialog-confirm" disabled' in page
    assert 'name="mailbox"' not in page and "keep_copy" not in page  # the mailbox is never a field


def test_send_as_kind_opens_the_same_form_with_send_as_ticked(client, db_session, world):
    signed_in(client, db_session)
    world.type.mailbox_action = "send_as"
    db_session.add(world.type)
    db_session.commit()
    page = client.get(f"/portal/new?request_type_id={world.type.id}").text
    assert 'id="mb-send-as" checked' in page and 'id="mb-full-access" checked' not in page


def test_the_script_builds_the_same_words_and_always_warns_for_access():
    script = open("app/static/mailbox-form.js", encoding="utf-8").read()
    assert 'data-warn' in script and "will be able to read all mail already in your mailbox" in script
    assert "will be able to send email that appears to come from you" in script and "WAIT_SECONDS = 5" in script
    assert "cancel.focus()" in script


# ---- One request, a change for each permission ----------------------------------------------------------------------


def test_choosing_both_makes_two_changes_on_one_ticket_and_neither_is_refused(client, db_session, world, friend):
    ticket, full, send = both(client, db_session, world, friend)
    assert (full.kind, send.kind) == ("full_access", "send_as")
    assert full.ticket_id == send.ticket_id == ticket.id
    assert full.owner_address == send.owner_address == OWNER and full.target_address == send.target_address == "jane@example.com"
    assert full.starts_at == send.starts_at and full.ends_at == send.ends_at
    assert ticket.status == TicketStatus.SCHEDULED and ticket.triaged_at is not None
    assert db_session.exec(select(Ticket)).all() == [ticket]  # one raise
    from app.models import TicketEvent

    clock = db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type.like("sla%"))).all()
    assert len(clock) <= 1  # the clock was not set twice


def test_one_permission_makes_one_change(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world, send_as=None).status_code == 303
    [only] = changes(db_session)
    assert only.kind == "full_access"
    assert post(client, world, full_access=None, end_date=later(80)[0]).status_code == 303
    assert [c.kind for c in changes(db_session)] == ["full_access", "send_as"]


def test_choosing_neither_is_refused_and_writes_nothing(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, full_access=None, send_as=None)
    assert response.status_code == 400 and "Choose Full access, Send As, or both" in response.text
    assert no_change(db_session)


def test_the_form_comes_back_with_the_boxes_as_they_were_typed(client, db_session, world, friend):
    signed_in(client, db_session)
    page = post(client, world, full_access=None, end_date="").text  # a refusal for another reason
    assert 'id="mb-full-access" checked' not in page and 'id="mb-send-as" checked' in page


# ---- The warning always shows ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("boxes", [{"send_as": None}, {"full_access": None}, {}])
def test_without_confirm_access_the_request_is_refused_and_the_warning_is_shown(client, db_session, world, friend, boxes):
    signed_in(client, db_session)
    response = post(client, world, confirm_access=None, **boxes)
    assert response.status_code == 200 and 'id="mb-confirm-block"' in response.text and 'name="confirm_access" value="1"' in response.text
    assert no_change(db_session)


def test_the_warning_says_what_each_permission_lets_them_do_and_until_when(client, db_session, world, friend):
    signed_in(client, db_session)
    def block(**boxes):
        return post(client, world, confirm_access=None, **boxes).text.split('id="mb-confirm-block"')[1].split("</section>")[0]

    both_text = block()
    assert "Jane Friend will be able to read all mail already in your mailbox." in both_text
    assert "Jane Friend will be able to send email that appears to come from you." in both_text and "This lasts until" in both_text
    full_only = block(send_as=None)
    assert "read all mail already in your mailbox" in full_only and "send email that appears" not in full_only
    send_only = block(full_access=None)
    assert "send email that appears" in send_only and "read all mail" not in send_only


def test_any_other_confirmation_does_not_stand_in_for_confirm_access(client, db_session, world, friend):
    signed_in(client, db_session)
    for extra in ({"confirm_no_copy": "1"}, {"confirm_access": "yes"}, {"confirm_access": ""}):
        assert post(client, world, **{"confirm_access": None, **extra}).status_code == 200
    assert no_change(db_session)


def test_another_refusal_comes_before_the_warning(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, confirm_access=None, end_date="")
    assert response.status_code == 400 and "always has an end" in response.text and 'id="mb-confirm-block"' not in response.text


# ---- Times ---------------------------------------------------------------------------------------------------------


def test_the_end_is_required_and_the_wording_is_about_access(client, db_session, world, friend):
    signed_in(client, db_session)
    response = post(client, world, end_date="")
    assert response.status_code == 400 and "access always has an end" in response.text
    assert no_change(db_session)


def test_the_end_is_capped_by_the_setting(client, db_session, world, friend):
    signed_in(client, db_session)
    set_setting(db_session, MAILBOX_CHANGE_MAX_DAYS, "2")
    db_session.commit()
    end_date, end_time = later(24 * 3)
    response = post(client, world, end_date=end_date, end_time=end_time)
    assert response.status_code == 400 and "Access can last at most 2 days" in response.text
    assert no_change(db_session)
    end_date, end_time = later(24)
    assert post(client, world, end_date=end_date, end_time=end_time).status_code == 303


# ---- Own mailbox only, overlap -------------------------------------------------------------------------------------


def test_the_mailbox_is_the_signed_in_person_and_a_posted_one_is_ignored(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world, mailbox="boss@example.com", owner_address="boss@example.com", owner_object_id="oid-boss").status_code == 303
    assert {c.owner_address for c in changes(db_session)} == {OWNER}


def test_the_same_permission_for_the_same_person_overlapping_is_refused(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world, send_as=None).status_code == 303  # full access, now to +30h
    again = post(client, world, send_as=None)
    assert again.status_code == 400 and "already has Full access on your mailbox" in again.text
    both_again = post(client, world)  # full access is live, so the pair is refused whole
    assert both_again.status_code == 400 and len(changes(db_session)) == 1


def test_another_permission_or_a_later_span_is_not_an_overlap(client, db_session, world, friend):
    signed_in(client, db_session)
    assert post(client, world, send_as=None).status_code == 303
    assert post(client, world, full_access=None).status_code == 303  # Send As is another permission
    start_date, _ = later(24 * 5)
    end_date, _ = later(24 * 6)
    later_span = post(client, world, send_as=None, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="09:00")
    assert later_span.status_code == 303 and len(changes(db_session)) == 3


def test_a_forwarding_in_the_span_is_not_an_overlap_for_access(client, db_session, world, friend):
    signed_in(client, db_session)
    forwarding_type = create_request_type(
        db_session, "Forward my mail", "", None, 53, defaults=TypeDefaults(world.team.id, world.category.id, "low", "low"), mailbox_action="forwarding",
    )
    db_session.commit()
    end_date, end_time = later(30)
    assert client.post("/portal/mailbox-change", data={
        "request_type_id": forwarding_type.id, "target_object_id": FRIEND_OID, "keep_copy": "on", "start_mode": "now",
        "end_date": end_date, "end_time": end_time,
    }, follow_redirects=False).status_code == 303
    assert post(client, world).status_code == 303


def test_a_person_who_cannot_be_chosen_is_refused_like_forwarding(client, db_session, world, friend, directory):
    signed_in(client, db_session)
    response = post(client, world, target_object_id="oid-not-there")
    assert response.status_code == 400 and no_change(db_session)


def test_the_feature_off_shows_not_available_and_writes_nothing(client, db_session, world, friend):
    signed_in(client, db_session)
    set_executor(None)
    response = post(client, world)
    assert response.status_code == 400 and "not available right now" in response.text and no_change(db_session)


def test_the_ordinary_raise_route_refuses_an_access_type(client, db_session, world):
    signed_in(client, db_session)
    response = client.post("/portal/new", data={"subject": "x", "description": "y", "request_type_id": world.type.id})
    assert response.status_code == 400 and "own form" in response.text


# ---- The ticket resolves when every change has been made ------------------------------------------------------------


def test_both_made_resolves_the_request_once_and_tells_everyone(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    assert run(db_session, full.starts_at + timedelta(minutes=1)) == 2
    ticket, full, send = reload(db_session, ticket, full, send)
    assert full.state == send.state == "active" and fake_exchange.holds(full) and fake_exchange.holds(send)
    assert ticket.status == TicketStatus.RESOLVED
    assert "Full access to your mailbox" in ticket.resolution_notes and "can send email as you" in ticket.resolution_notes
    owner_mail = [m for m in mails(db_session) if m.to_address == OWNER]
    target_mail = [m for m in mails(db_session) if m.to_address == "jane@example.com"]
    assert len(target_mail) == 2 and all(m.kind == MAILBOX_CHANGE for m in target_mail)
    assert any("Full access to Alex Owner's mailbox" in m.body and "short while to appear in Outlook" in m.body for m in target_mail)
    send_mail = [m for m in target_mail if "send email as Alex Owner" in m.body]
    assert send_mail and "Outlook" not in send_mail[0].body and "until" in send_mail[0].body
    assert owner_mail  # the requester's started notices


def test_the_ticket_is_not_resolved_while_one_change_is_still_waiting(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    send.starts_at = full.starts_at + timedelta(hours=1)  # as if its start were later
    db_session.add(send)
    db_session.commit()
    assert run(db_session, full.starts_at + timedelta(minutes=1)) == 1
    ticket, full, send = reload(db_session, ticket, full, send)
    assert full.state == "active" and send.state == "scheduled" and ticket.status == TicketStatus.SCHEDULED
    assert run(db_session, send.starts_at + timedelta(minutes=1)) == 1
    assert reload(db_session, ticket).status == TicketStatus.RESOLVED


def test_one_applied_and_one_failed_leaves_the_ticket_open_and_the_note_names_the_failed_one(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    fake_exchange.script("apply", OUTCOME_PERMANENT, error="Couldn't find object 'jane@example.com'")  # the first call: full access
    assert run(db_session, full.starts_at + timedelta(minutes=1)) == 2
    ticket, full, send = reload(db_session, ticket, full, send)
    assert full.state == "failed" and send.state == "active"
    assert ticket.status == TicketStatus.OPEN and ticket.assigned_agent_id is None  # back in the team's queue, not resolved
    [note] = internal_notes(db_session, ticket)
    assert "Full access" in note.body and f"change {full.id}" in note.body and "Couldn't find object" in note.body
    assert "Send As" not in note.body.split("What was attempted")[1].split("\n")[0]
    visible = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction != MessageDirection.INTERNAL]
    assert any("Full access" in body and "could not be made" in body.replace("not been made", "could not be made") for body in visible)
    assert not any("Couldn't find" in body for body in visible)
    # the target heard only about what was made
    target_bodies = [m.body for m in mails(db_session) if m.to_address == "jane@example.com"]
    assert len(target_bodies) == 1 and "send email as" in target_bodies[0]


def test_the_applied_one_still_ends_at_its_end_time_and_the_failed_one_is_never_removed(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    fake_exchange.script("apply", OUTCOME_PERMANENT)
    run(db_session, full.starts_at + timedelta(minutes=1))
    assert run(db_session, full.ends_at - timedelta(minutes=1)) == 0 and fake_exchange.holds(send)
    assert run(db_session, full.ends_at + timedelta(minutes=1)) == 1
    full, send = reload(db_session, full, send)
    assert send.state == "ended" and not fake_exchange.holds(send) and full.state == "failed"
    assert [c for c in fake_exchange.calls if c[0] == "remove"] == [("remove", "send_as", OWNER, "jane@example.com")]


def test_an_agent_settles_the_failed_one_and_the_request_then_resolves(client, db_session, world, friend, fake_exchange, dev_mail):
    agent = User(email="agent@example.com", display_name="Agent Smith", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.flush()
    db_session.add(TeamMembership(user_id=agent.id, team_id=world.team.id))
    db_session.commit()
    ticket, full, send = both(client, db_session, world, friend)
    fake_exchange.script("apply", OUTCOME_PERMANENT)
    run(db_session, full.starts_at + timedelta(minutes=1))
    login_as(client, "agent@example.com")
    page = client.get(f"/tickets/{ticket.number}?action=mbx_done").text
    assert "full access on alex@example.com for jane@example.com" in page  # the card names the failed one
    assert client.post(f"/tickets/{ticket.number}/mailbox/done-by-hand").status_code == 200
    ticket, full, send = reload(db_session, ticket, full, send)
    assert full.state == "active" and send.state == "active" and ticket.status == TicketStatus.RESOLVED
    assert "Full access to your mailbox" in ticket.resolution_notes and "send email as you" in ticket.resolution_notes


def test_cancelling_the_failed_one_resolves_the_request_and_names_what_is_in_place(client, db_session, world, friend, fake_exchange, dev_mail):
    agent = User(email="agent@example.com", display_name="Agent Smith", role=UserRole.AGENT)
    db_session.add(agent)
    db_session.flush()
    db_session.add(TeamMembership(user_id=agent.id, team_id=world.team.id))
    db_session.commit()
    ticket, full, send = both(client, db_session, world, friend)
    fake_exchange.script("apply", OUTCOME_PERMANENT)
    run(db_session, full.starts_at + timedelta(minutes=1))
    login_as(client, "agent@example.com")
    assert client.post(f"/tickets/{ticket.number}/mailbox/cancel").status_code == 200
    ticket, full, send = reload(db_session, ticket, full, send)
    assert full.state == "cancelled" and send.state == "active" and ticket.status == TicketStatus.RESOLVED
    assert "Full access part of this request was cancelled" in ticket.resolution_notes and "send email as you" in ticket.resolution_notes
    owner_bodies = [m.body for m in db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() if m.direction != MessageDirection.INTERNAL]
    assert any("cancelled the Full access part" in body for body in owner_bodies)


# ---- Both removed at the end ----------------------------------------------------------------------------------------


def test_both_are_removed_at_the_end_with_emails_to_both_people(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    run(db_session, full.starts_at + timedelta(minutes=1))
    before = len(mails(db_session))
    assert run(db_session, full.ends_at + timedelta(minutes=1)) == 2
    full, send = reload(db_session, full, send)
    assert full.state == send.state == "ended" and not fake_exchange.holds(full) and not fake_exchange.holds(send)
    new = mails(db_session)[before:]
    assert sorted(m.to_address for m in new if m.kind == MAILBOX_CHANGE) == ["jane@example.com", "jane@example.com"]
    bodies = " ".join(m.body or "" for m in new)
    assert "no longer has Full access" in bodies or "no longer have Full access" in bodies
    assert "can no longer send email as" in bodies


# ---- The request page -----------------------------------------------------------------------------------------------


def test_the_request_page_shows_each_change_with_its_own_buttons(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    page = client.get(f"/portal/{ticket.number}").text
    assert "Full access to my mailbox" in page and "Send As on my mailbox" in page and page.count("Given to") == 2
    assert f'name="change_id" value="{full.id}"' not in page  # still starting: nothing to cancel or stop yet
    run(db_session, full.starts_at + timedelta(minutes=1))
    page = client.get(f"/portal/{ticket.number}").text
    assert f'name="change_id" value="{full.id}"' in page and f'name="change_id" value="{send.id}"' in page
    assert "Remove Full access now" in page and "Remove Send As now" in page


def test_stopping_one_removes_only_that_one(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    run(db_session, full.starts_at + timedelta(minutes=1))
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", data={"change_id": str(send.id)}, follow_redirects=False).status_code == 303
    assert run(db_session, now() + timedelta(minutes=2)) == 1
    full, send = reload(db_session, full, send)
    assert send.state == "ended" and full.state == "active" and fake_exchange.holds(full)


def test_stopping_needs_to_say_which_change_when_there_are_two(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    run(db_session, full.starts_at + timedelta(minutes=1))
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", follow_redirects=False).status_code == 404
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", data={"change_id": "999999"}, follow_redirects=False).status_code == 404
    assert reload(db_session, full).state == "active"


def test_nobody_else_can_stop_a_change(client, db_session, world, friend, fake_exchange, dev_mail):
    ticket, full, send = both(client, db_session, world, friend)
    run(db_session, full.starts_at + timedelta(minutes=1))
    signed_in(client, db_session, email="mallory@example.com", name="Mallory")
    assert client.post(f"/portal/{ticket.number}/mailbox/stop", data={"change_id": str(full.id)}, follow_redirects=False).status_code == 404
    assert reload(db_session, full).state == "active"


def test_the_requester_cancels_one_before_the_start_and_the_request_waits_for_the_other(client, db_session, world, friend, fake_exchange, dev_mail):
    signed_in(client, db_session)
    start_date, _ = later(24 * 2)
    end_date, _ = later(24 * 4)
    assert post(client, world, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="09:00").status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    full, send = changes(db_session)
    assert client.post(f"/portal/{ticket.number}/mailbox/cancel", data={"change_id": str(send.id)}, follow_redirects=False).status_code == 303
    ticket, full, send = reload(db_session, ticket, full, send)
    assert send.state == "cancelled" and full.state == "scheduled" and ticket.status == TicketStatus.SCHEDULED
    assert client.post(f"/portal/{ticket.number}/mailbox/cancel", data={"change_id": str(full.id)}, follow_redirects=False).status_code == 303
    ticket = reload(db_session, ticket)
    assert reload(db_session, full).state == "cancelled" and ticket.status == TicketStatus.RESOLVED


# ---- The registry ---------------------------------------------------------------------------------------------------


def test_the_new_emails_are_in_the_registry_for_the_admin_to_edit():
    from app.email_templates import AUDIENCE_OF, REGISTRY

    for name in ("mailbox_full_access_started_target", "mailbox_part_could_not_start_requester", "mailbox_part_cancelled_requester"):
        assert name in REGISTRY and name in AUDIENCE_OF
    assert portal_mailbox.access_warning("Jane", ["full_access", "send_as"], "1 Jan").count("Jane will be able to") == 2


def test_a_later_start_sets_the_sla_clock_once_for_both_changes(client, db_session, world, friend):
    from app.models import TicketEvent

    signed_in(client, db_session)
    start_date, _ = later(24 * 2)
    end_date, _ = later(24 * 4)
    assert post(client, world, start_mode="later", start_date=start_date, start_time="09:00", end_date=end_date, end_time="09:00").status_code == 303
    db_session.expire_all()
    ticket = db_session.exec(select(Ticket)).one()
    clock = db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "sla_start_changed")).all()
    assert len(clock) == 1 and ticket.sla_starts_at is not None and len(changes(db_session)) == 2

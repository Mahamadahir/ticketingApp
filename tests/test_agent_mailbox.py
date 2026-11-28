"""The agent ticket card "Set up mailbox change" (#364): a request that arrived by email gets its scheduled change from an agent, who
is IT: the required note is the approval (an event and an audit entry, no approval round), the checks are the portal's, and the owner,
the other person and the requester are told as on every other path. Exchange is the FakeExecutor; the people are the local directory
(Graph off), where a person's object id is `local-<id>`."""

import html
import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import agent_mailbox
from app.mailbox_executor import FakeExecutor, set_executor
from app.models import (
    AuditLog, MailboxChange, Message, MessageDirection, RequestType, Ticket, TicketApproval, TicketEvent, TicketStatus, TicketType, User,
    UserRole,
)
from app.outbox import MAILBOX_CHANGE
from app.request_types import TypeDefaults, create_request_type
from app.settings_store import DEFAULT_TIME_ZONE, set_setting
from app.storage import get_storage
from app.subject_access import build_export
from tests.conftest import create_category, create_team, login_as
from tests.test_mailbox_changes import audit, mails, notes, request_ticket, reload, run, team_with_agent
from tests.test_portal_mailbox import later
from tests.test_request_types import group_of
from tests.test_resolution import dev_mail  # noqa: F401  (a fixture)

REQUESTER = "alex@example.com"
JANE = "jane@example.com"
BEE = "bee@example.com"
NOTE = "Confirmed by phone with the owner, 10:05"


@pytest.fixture(autouse=True)
def fake_exchange(db_session):
    set_setting(db_session, DEFAULT_TIME_ZONE, "UTC")
    db_session.commit()
    executor = FakeExecutor()
    set_executor(executor)
    yield executor
    set_executor(None)


def person(db_session, email, name):
    user = User(email=email, display_name=name)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def world(client, db_session):
    """An agent in a team, a triaged open Service Request raised by email for Alex, and Alex, Jane and Bee as people; the agent is
    signed in."""
    team, agent = team_with_agent(db_session)
    ticket = request_ticket(db_session, team, requester=REQUESTER)
    alex, jane, bee = person(db_session, REQUESTER, "Alex Owner"), person(db_session, JANE, "Jane Friend"), person(db_session, BEE, "Bee Owner")
    login_as(client, "agent@example.com")
    return SimpleNamespace(team=team, agent=agent, ticket=ticket, alex=alex, jane=jane, bee=bee)


def oid(user):
    return f"local-{user.id}"


def form(world, **extra):
    end_date, end_time = later(30)
    data = {
        "kind": "forwarding", "owner_object_id": oid(world.alex), "target_object_id": oid(world.jane), "keep_copy": "on",
        "start_mode": "now", "end_date": end_date, "end_time": end_time, "note": NOTE,
    }
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def token_for(data):
    """The token the confirm button carries: the fingerprint of the values the warning was shown for."""
    shown = agent_mailbox.AgentMailboxForm(
        kind=data["kind"], owner_object_id=data.get("owner_object_id", ""), target_object_id=data.get("target_object_id", ""),
        keep_copy=data.get("keep_copy") == "on", start_mode=data["start_mode"], start_date=data.get("start_date", ""),
        start_time=data.get("start_time", ""), end_date=data["end_date"], end_time=data["end_time"],
    )
    return agent_mailbox.fingerprint(shown)


def setup(client, world, **extra):
    data = form(world, **extra)
    if data.get("confirmed") and "confirm_token" not in data:
        data["confirm_token"] = token_for(data)  # as the confirm button posts it
    return client.post(f"/tickets/{world.ticket.number}/mailbox/set-up", data=data)


def changes_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(MailboxChange).where(MailboxChange.ticket_id == ticket.id).order_by(MailboxChange.id)).all()


def events_of(db_session, ticket, event_type):
    db_session.expire_all()
    return db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == event_type)).all()


def nothing_made(db_session, ticket):
    return not changes_of(db_session, ticket) and not events_of(db_session, ticket, agent_mailbox.EVENT_CONFIRMED)


# ---- The chip ---------------------------------------------------------------------------------------------------------------------


def test_the_chip_shows_on_a_triaged_unfinished_service_request_with_no_change(client, db_session, world):
    page = client.get(f"/tickets/{world.ticket.number}").text
    assert "?action=mailbox" in page and "Set up mailbox change" in page
    card = client.get(f"/tickets/{world.ticket.number}?action=mailbox").text
    assert "How you confirmed this request" in card and "Alex Owner (alex@example.com)" in card  # the owner starts as the requester


def test_the_chip_is_not_offered_where_the_card_does_not_apply(client, db_session, world):
    number = world.ticket.number
    # an incident
    world.ticket.type = TicketType.INCIDENT
    db_session.add(world.ticket)
    db_session.commit()
    assert "?action=mailbox" not in client.get(f"/tickets/{number}").text
    assert setup(client, world).status_code == 400 and nothing_made(db_session, world.ticket)
    # untriaged
    world.ticket.type, world.ticket.triaged_at = TicketType.SERVICE_REQUEST, None
    db_session.add(world.ticket)
    db_session.commit()
    assert "?action=mailbox" not in client.get(f"/tickets/{number}").text
    # finished
    world.ticket.triaged_at, world.ticket.status = world.ticket.created_at, TicketStatus.CLOSED
    db_session.add(world.ticket)
    db_session.commit()
    assert "?action=mailbox" not in client.get(f"/tickets/{number}").text
    assert setup(client, world).status_code == 400 and nothing_made(db_session, world.ticket)


def test_the_chip_is_gone_once_a_change_exists_and_a_second_post_makes_nothing_more(client, db_session, world):
    assert setup(client, world).status_code == 200
    assert "?action=mailbox" not in client.get(f"/tickets/{world.ticket.number}").text
    assert setup(client, world, target_object_id=oid(world.bee)).status_code == 400
    assert len(changes_of(db_session, world.ticket)) == 1


def test_a_request_raised_in_the_portal_for_a_mailbox_has_its_change_and_offers_no_card(client, db_session, world):
    from app.mailbox_changes import create_mailbox_change

    start = world.ticket.created_at + timedelta(days=1)
    create_mailbox_change(
        db_session, world.ticket, kind="forwarding", owner_address=REQUESTER, target_address=JANE, starts_at=start, ends_at=start + timedelta(days=1),
        created_by=REQUESTER,
    )
    db_session.commit()
    assert "?action=mailbox" not in client.get(f"/tickets/{world.ticket.number}").text


def test_only_an_agent_who_works_the_ticket_may_set_one_up(client, db_session, world):
    outsider = User(email="other-agent@example.com", display_name="Other", role=UserRole.AGENT)  # an agent of no team
    db_session.add(outsider)
    db_session.commit()
    login_as(client, outsider.email)
    assert "?action=mailbox" not in client.get(f"/tickets/{world.ticket.number}").text
    assert setup(client, world).status_code in (302, 303, 400, 403, 404) and nothing_made(db_session, world.ticket)
    login_as(client, REQUESTER)  # the requester is not an agent
    assert setup(client, world).status_code in (302, 303, 400, 401, 403, 404) and nothing_made(db_session, world.ticket)


# ---- The change -------------------------------------------------------------------------------------------------------------------


def test_forwarding_is_created_scheduled_with_the_note_as_event_and_audit_and_no_approval(client, db_session, world):
    assert setup(client, world).status_code == 200
    (change,) = changes_of(db_session, world.ticket)
    assert (change.kind, change.owner_address, change.target_address, change.keep_copy) == ("forwarding", REQUESTER, JANE, True)
    assert change.created_by == "agent@example.com" and change.state == "scheduled" and "created" in audit(db_session, change)
    ticket = reload(db_session, world.ticket)
    assert ticket.status == TicketStatus.SCHEDULED
    (event,) = events_of(db_session, ticket, agent_mailbox.EVENT_CONFIRMED)
    assert event.actor_email == "agent@example.com" and event.detail == NOTE and event.to_value == "forwarding"
    entries = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "ticket", AuditLog.entity_id == ticket.id, AuditLog.action == "mailbox_change_confirmed")).all()
    assert [e.detail for e in entries] == [NOTE]
    assert db_session.exec(select(TicketApproval).where(TicketApproval.ticket_id == ticket.id)).first() is None  # no approval round


def test_the_sla_clock_starts_at_a_later_start_time(client, db_session, world):
    date, time = later(5)
    assert setup(client, world, start_mode="later", start_date=date, start_time=time).status_code == 200
    (change,) = changes_of(db_session, world.ticket)
    ticket = reload(db_session, world.ticket)
    assert ticket.sla_starts_at is not None and abs(ticket.sla_starts_at.replace(tzinfo=change.starts_at.tzinfo) - change.starts_at) < timedelta(seconds=1)
    assert ticket.due_on == change.starts_at.date()


def test_the_requester_is_the_owner_and_the_target_defaults_to_the_requester_when_the_owner_is_someone_else(client, db_session, world):
    assert setup(client, world, owner_object_id=None).status_code == 200  # no owner picked: the requester
    (mine,) = changes_of(db_session, world.ticket)
    assert mine.owner_address == REQUESTER
    # a second request: the owner is Bee and no other person is picked: it is for the requester
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    other_world = SimpleNamespace(**{**vars(world), "ticket": other})
    assert setup(client, other_world, owner_object_id=oid(world.bee), target_object_id=None).status_code == 200
    (theirs,) = changes_of(db_session, other)
    assert (theirs.owner_address, theirs.target_address) == (BEE, REQUESTER)


def test_the_owner_and_the_other_person_must_differ_and_must_be_chosen(client, db_session, world):
    assert setup(client, world, target_object_id=oid(world.alex)).status_code == 400  # to the owner themselves
    assert setup(client, world, target_object_id=None).status_code == 400  # the requester is the owner: choose who it is for
    assert setup(client, world, target_object_id="local-999999").status_code == 400  # not found
    assert setup(client, world, owner_object_id="local-999999").status_code == 400
    assert nothing_made(db_session, world.ticket)


def test_a_person_on_the_block_list_cannot_be_chosen(client, db_session, world):
    from app.settings_store import MAILBOX_TARGET_BLOCKLIST

    set_setting(db_session, MAILBOX_TARGET_BLOCKLIST, JANE)
    db_session.commit()
    assert setup(client, world).status_code == 400 and nothing_made(db_session, world.ticket)


def test_the_note_is_required(client, db_session, world):
    for note in ("", "   "):
        response = setup(client, world, note=note)
        assert response.status_code == 400 and "how you confirmed" in response.text.lower()
    assert nothing_made(db_session, world.ticket)


def test_the_note_has_a_length_limit(client, db_session, world):
    assert setup(client, world, note="x" * 1200).status_code in (400, 422) and nothing_made(db_session, world.ticket)


def test_the_end_is_required_and_capped_and_the_times_are_the_agents(client, db_session, world):
    assert setup(client, world, end_date="").status_code == 400
    far, _ = later(24 * 400)
    assert setup(client, world, end_date=far).status_code == 400  # over mailbox_change_max_days
    assert setup(client, world, end_time="09:07").status_code == 400  # not a quarter hour
    assert nothing_made(db_session, world.ticket)


def test_an_overlapping_change_on_the_same_mailbox_is_refused(client, db_session, world):
    assert setup(client, world).status_code == 200
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    again = SimpleNamespace(**{**vars(world), "ticket": other})
    response = setup(client, again, target_object_id=oid(world.bee))
    assert response.status_code == 400 and "already has forwarding set up" in response.text
    assert not changes_of(db_session, other)


def test_access_for_the_same_person_overlapping_is_refused_but_another_person_is_fine(client, db_session, world):
    assert setup(client, world, kind="full_access", confirmed="1").status_code == 200
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    again = SimpleNamespace(**{**vars(world), "ticket": other})
    assert setup(client, again, kind="both", confirmed="1").status_code == 400
    assert not changes_of(db_session, other)
    assert setup(client, again, kind="full_access", confirmed="1", target_object_id=oid(world.bee)).status_code == 200


def test_a_forward_back_to_the_owner_is_a_loop(client, db_session, world):
    assert setup(client, world, owner_object_id=oid(world.jane), target_object_id=oid(world.alex)).status_code == 200  # Jane's mail to Alex
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    again = SimpleNamespace(**{**vars(world), "ticket": other})
    response = setup(client, again)  # Alex's mail to Jane
    assert response.status_code == 400 and "loop" in response.text
    assert not changes_of(db_session, other)


def test_turning_the_copy_off_needs_the_red_confirmation_checked_by_the_server(client, db_session, world):
    first = setup(client, world, keep_copy=None)
    assert first.status_code == 400 and "mb-confirm-block" in first.text and "will go only to Jane Friend" in first.text
    assert "Turn off the copy" in first.text and nothing_made(db_session, world.ticket)
    assert setup(client, world, keep_copy=None, confirmed="1").status_code == 200
    (change,) = changes_of(db_session, world.ticket)
    assert change.keep_copy is False


def test_the_access_warning_is_always_required_and_one_change_is_made_for_each_permission(client, db_session, world):
    first = setup(client, world, kind="both")
    assert first.status_code == 400 and "mb-confirm-block" in first.text
    text = html.unescape(first.text)
    assert "read all mail already in Alex Owner's mailbox" in text and "appears to come from Alex Owner" in text
    assert nothing_made(db_session, world.ticket)
    assert setup(client, world, kind="both", confirmed="1").status_code == 200
    made = changes_of(db_session, world.ticket)
    assert [c.kind for c in made] == ["full_access", "send_as"] and all(c.target_address == JANE and c.keep_copy for c in made)
    assert len(events_of(db_session, world.ticket, agent_mailbox.EVENT_CONFIRMED)) == 1


def test_the_refusal_keeps_what_was_typed_in_the_card(client, db_session, world):
    response = setup(client, world, note="", target_name="Jane Friend")
    assert "Jane Friend" in response.text and oid(world.jane) in response.text and 'name="end_date"' in response.text


def test_the_request_type_is_set_from_the_matching_mailbox_type_only_when_there_is_none(client, db_session, world):
    category = create_category(db_session, world.team, "Mailbox")
    group = group_of(db_session, "Mailbox approvers", ("it1@example.com",))
    matching = create_request_type(
        db_session, "Forward my mail", "", group.id, 51, defaults=TypeDefaults(world.team.id, category.id, "low", "low"), mailbox_action="forwarding",
    )
    chosen = create_request_type(db_session, "Something else", "", group.id, 52, defaults=TypeDefaults(world.team.id, category.id, "low", "low"))
    db_session.commit()
    assert setup(client, world).status_code == 200
    assert reload(db_session, world.ticket).request_type_id == matching.id
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    other.request_type_id = chosen.id  # the agent's own choice is left alone
    db_session.add(other)
    db_session.commit()
    again = SimpleNamespace(**{**vars(world), "ticket": other})
    assert setup(client, again, owner_object_id=oid(world.bee)).status_code == 200
    assert reload(db_session, other).request_type_id == chosen.id


# ---- Notifications and the lifecycle ----------------------------------------------------------------------------------------------


def test_the_owner_the_other_person_and_the_requester_are_told_when_it_starts_and_ends_and_the_ticket_resolves(client, db_session, world, dev_mail, fake_exchange):
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    here = SimpleNamespace(**{**vars(world), "ticket": other})
    assert setup(client, here, owner_object_id=oid(world.bee), target_object_id=oid(world.jane)).status_code == 200
    (change,) = changes_of(db_session, other)
    assert run(db_session, change.starts_at + timedelta(minutes=1)) == 1
    assert reload(db_session, other).status == TicketStatus.RESOLVED
    assert run(db_session, change.ends_at + timedelta(minutes=1)) == 1
    assert reload(db_session, change).state == "ended"
    by_address = {}
    for mail in mails(db_session, MAILBOX_CHANGE):
        by_address.setdefault(mail.to_address, []).append(mail)
    assert sorted(by_address) == sorted([BEE, JANE])  # the requester is told by the ticket's own notes, below
    assert all(len(rows) == 2 for rows in by_address.values())  # one on the start, one on the end
    started = by_address[BEE][0].body
    assert "Alex Owner asked for this and it was set up and checked by Agent Smith (IT Support)" in started
    assert NOTE not in started and NOTE not in by_address[BEE][1].body  # who the agent checked with is for the team
    assert "set up and checked by Agent Smith" in by_address[BEE][1].body
    assert any("now going to Jane Friend" in m.body for m in notes(db_session, other, visible=True))  # the requester's own note


def test_the_requester_owner_is_told_as_on_the_portal_path_and_not_twice(client, db_session, world, dev_mail, fake_exchange):
    assert setup(client, world).status_code == 200
    (change,) = changes_of(db_session, world.ticket)
    run(db_session, change.starts_at + timedelta(minutes=1))
    told = sorted(m.to_address for m in mails(db_session, MAILBOX_CHANGE))
    assert told == [JANE]  # the requester is told by the ticket's own note; the other person gets the change email
    assert reload(db_session, world.ticket).status == TicketStatus.RESOLVED


def test_a_failed_start_goes_to_the_usual_failure_handling(client, db_session, world, dev_mail, fake_exchange):
    from app.mailbox_executor import OUTCOME_PERMANENT

    fake_exchange.script("apply", OUTCOME_PERMANENT)
    assert setup(client, world).status_code == 200
    (change,) = changes_of(db_session, world.ticket)
    run(db_session, change.starts_at + timedelta(minutes=1))
    assert reload(db_session, change).state == "failed"
    page = client.get(f"/tickets/{world.ticket.number}").text
    assert "?action=mbx_retry" in page and "?action=mbx_done" in page and "?action=mbx_cancel" in page
    assert "?action=mailbox" not in page  # a failed change is still a live one


def test_the_requester_sees_the_change_on_the_request_page(client, db_session, world):
    assert setup(client, world).status_code == 200
    login_as(client, REQUESTER)
    page = client.get(f"/portal/{world.ticket.number}")
    assert page.status_code == 200 and "Forwarding my mail" in page.text and "Jane Friend" in page.text


# ---- Subject access -------------------------------------------------------------------------------------------------------------------


def export_rows(db_session, address, named=False):
    admin = db_session.exec(select(User).where(User.email == "dpo@example.com")).first()
    if admin is None:
        admin = User(email="dpo@example.com", display_name="DPO", role=UserRole.SUPER_ADMIN)
        db_session.add(admin)
        db_session.commit()
    archive = zipfile.ZipFile(io.BytesIO(build_export(db_session, get_storage(), address, admin, name_other_person=named).file.read()))
    return archive.read("mailbox_changes.json").decode("utf-8")


def test_the_export_names_nobody_else_for_the_agent_the_owner_the_target_or_the_requester(client, db_session, world):
    other = request_ticket(db_session, world.team, requester=REQUESTER)
    here = SimpleNamespace(**{**vars(world), "ticket": other})
    assert setup(client, here, owner_object_id=oid(world.bee), target_object_id=oid(world.jane)).status_code == 200
    agent_side = export_rows(db_session, "agent@example.com")
    row = json.loads(agent_side)[0]
    assert row["asked_by"] == "agent@example.com" and row["mailbox"] == "another person's" and row["shared_with"] == "another person"
    assert BEE not in agent_side and JANE not in agent_side
    owner_side = json.loads(export_rows(db_session, BEE))[0]
    assert owner_side["mailbox"] == BEE and owner_side["shared_with"] == "another person" and owner_side["asked_by"] == "another person"
    target_side = json.loads(export_rows(db_session, JANE))[0]
    assert target_side["shared_with"] == JANE and target_side["mailbox"] == "another person's" and target_side["asked_by"] == "another person"
    # the requester is not the owner, the target or the creator here: nothing of the change is theirs
    assert json.loads(export_rows(db_session, REQUESTER)) == []
    assert "agent@example.com" in export_rows(db_session, BEE, True)


def test_a_confirmation_counts_only_for_the_values_it_was_shown_for(client, db_session, world):
    warned = setup(client, world, keep_copy=None)  # the copy-off warning
    assert warned.status_code == 400 and "mb-confirm-block" in warned.text
    copy_off = form(world, keep_copy=None)
    token = token_for(copy_off)
    assert token in warned.text  # the confirm button carries it
    # the agent switches to access and presses the confirm button: the access warning has not been shown, so it is shown now
    again = setup(client, world, kind="both", keep_copy=None, confirmed="1", confirm_token=token)
    assert again.status_code == 400 and "read all mail already in" in html.unescape(again.text)
    assert setup(client, world, keep_copy=None, confirmed="1", confirm_token="forged").status_code == 400
    assert nothing_made(db_session, world.ticket)

"""Changing a ticket's end user (issue #279): who may, who it can become, what is recorded, what follows (followers,
portal access, the email, the reply channel, the Due date's time zone, approvals, person data)."""

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import mail, outbox
from app.anonymisation import run as anonymise_person
from app.config import settings
from app.email_templates import REGISTRY, current_text
from app.followers import is_follower
from app.models import (
    AuditLog,
    EmailTemplate,
    LinkedIdentity,
    MembershipLevel,
    OutboundEmail,
    Ticket,
    TicketEvent,
    TicketFollower,
    TicketSource,
    TicketStatus,
    User,
    UserRole,
)
from app.person_data import COLUMNS
from app.requester_change import NotAllowed, RequesterChangeError, change_requester, resolve_address, suggestions
from app.subject_access import build_export
from app.ticket_service import create_ticket
from app.time_zones import end_of_day_utc
from app.visibility import access_context, may_change_requester
from tests.conftest import add_membership, create_team, login_as, set_role
from tests.test_approvals import person as approval_person, raised_and_triaged, request_type, status as approval_status
from tests.test_triage import agent, events, new_ticket

OLD, NEW = "old@example.com", "new@example.com"


@pytest.fixture
def dev_mail(monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "dev")
    monkeypatch.setattr(settings, "public_base_url", "https://tickets.example.org")


def worked(db_session, team, requester=OLD, **fields):
    """A triaged, open ticket in the team."""
    ticket = new_ticket(db_session, team, requester=requester)
    ticket.triaged_at, ticket.status, ticket.priority = datetime.now(timezone.utc), TicketStatus.OPEN, "p3"
    for key, value in fields.items():
        setattr(ticket, key, value)
    db_session.add(ticket)
    db_session.commit()
    return ticket


def post(client, ticket, who=NEW, **extra):
    data = {"new_requester": who, "keep_old": "on"}
    data.update(extra)
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(f"/tickets/{ticket.number}/requester", data=data)


def reload(db_session, ticket):
    db_session.expire_all()
    return db_session.get(Ticket, ticket.id)


def person(db_session, email, name=None, **fields):
    user = User(email=email, display_name=name or email.split("@")[0].title(), **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def audit(db_session):
    db_session.expire_all()
    return db_session.exec(select(AuditLog).where(AuditLog.action == "requester_changed")).all()


def mail_rows(db_session, kind=outbox.REQUESTER_CHANGED):
    db_session.expire_all()
    return db_session.exec(select(OutboundEmail).where(OutboundEmail.kind == kind).order_by(OutboundEmail.id)).all()


@pytest.fixture
def desk(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[team])
    return team


# ---- Who may -------------------------------------------------------------------------------------------


def test_an_agent_who_works_the_ticket_can_change_the_end_user(client, db_session, desk):
    ticket = worked(db_session, desk)
    response = post(client, ticket)
    assert response.status_code == 200, response.text
    assert reload(db_session, ticket).requester_email == NEW


def test_an_agent_who_cannot_see_the_ticket_is_refused(client, db_session, desk):
    other = create_team(db_session, "Network")
    ticket = worked(db_session, other)
    assert post(client, ticket).status_code in (403, 404)
    assert reload(db_session, ticket).requester_email == OLD


def test_an_agent_who_only_reads_the_ticket_is_refused(client, db_session):
    team = create_team(db_session, "Service Desk")
    agent_id = agent(client, db_session)
    add_membership(db_session, agent_id, team.id, MembershipLevel.VIEWER)
    ticket = worked(db_session, team)
    assert client.get(f"/tickets/{ticket.number}").status_code == 200
    assert post(client, ticket).status_code == 403
    assert reload(db_session, ticket).requester_email == OLD
    assert events(db_session, ticket, "requester_changed") == []


def test_the_requester_and_a_follower_cannot_change_it(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team)
    db_session.add(TicketFollower(ticket_id=ticket.id, email="friend@example.com", added_by_email="x"))
    db_session.commit()
    for email in (OLD, "friend@example.com"):
        client.post("/auth/logout")
        login_as(client, email)
        assert post(client, ticket).status_code == 403
        assert client.get(f"/tickets/{ticket.number}/requester-search?new_requester=ne").status_code == 403
    assert reload(db_session, ticket).requester_email == OLD


@pytest.mark.parametrize("finished", [TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_a_finished_ticket_is_refused(client, db_session, desk, finished):
    ticket = worked(db_session, desk, status=finished)
    assert post(client, ticket).status_code == 403
    assert reload(db_session, ticket).requester_email == OLD


def test_the_rule_in_visibility_by_role_and_access(client, db_session):
    team, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket = worked(db_session, team)
    works, reads, outsider = (person(db_session, f"{n}@example.com", role=UserRole.AGENT) for n in ("works", "reads", "outsider"))
    add_membership(db_session, works.id, team.id)
    add_membership(db_session, reads.id, team.id, MembershipLevel.VIEWER)
    add_membership(db_session, outsider.id, other.id)
    admin = person(db_session, "admin@example.com", role=UserRole.SUPER_ADMIN)
    end_user = person(db_session, OLD)

    def may(user, t=ticket):
        return may_change_requester(user, t, access_context(db_session, user))

    assert may(works) and may(admin)
    assert not may(reads) and not may(outsider) and not may(end_user)
    ticket.status = TicketStatus.CLOSED
    assert not may(works) and not may(admin)


def test_the_service_checks_the_rule_itself(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = worked(db_session, team)
    outsider = person(db_session, "outsider@example.com", role=UserRole.AGENT)
    with pytest.raises(NotAllowed):
        change_requester(db_session, ticket, outsider, NEW)
    assert ticket.requester_email == OLD


def test_the_page_offers_change_only_to_those_who_may(client, db_session, desk):
    ticket = worked(db_session, desk)
    assert "?edit=requester" in client.get(f"/tickets/{ticket.number}").text
    form = client.get(f"/tickets/{ticket.number}?edit=requester").text
    assert 'name="new_requester"' in form and 'name="email_new"' in form and 'name="keep_old"' in form
    assert 'name="email_new" value="on" checked' not in form  # unticked by default
    assert 'name="keep_old" value="on" checked' in form  # ticked by default
    ticket.status = TicketStatus.RESOLVED
    db_session.add(ticket)
    db_session.commit()
    page = client.get(f"/tickets/{ticket.number}?edit=requester").text
    assert "?edit=requester" not in page and 'name="new_requester"' not in page


# ---- Who it can become -------------------------------------------------------------------------------


def test_change_to_an_existing_user_by_address_or_by_name(client, db_session, desk):
    person(db_session, "pat@example.com", "Pat Smith")
    first, second = worked(db_session, desk), worked(db_session, desk)
    assert post(client, first, "Pat Smith <PAT@example.com>").status_code == 200
    assert post(client, second, "pat smith").status_code == 200
    assert reload(db_session, first).requester_email == "pat@example.com"
    assert reload(db_session, second).requester_email == "pat@example.com"


def test_change_to_an_address_with_no_account_is_normalised(client, db_session, desk):
    ticket = worked(db_session, desk)
    assert post(client, ticket, "  Visitor@Outside.ORG ").status_code == 200
    assert reload(db_session, ticket).requester_email == "visitor@outside.org"


@pytest.mark.parametrize("typed", ["not an address", "a@b", "nobody knows", ""])
def test_a_bad_entry_is_refused_with_a_plain_message(client, db_session, desk, typed):
    ticket = worked(db_session, desk)
    response = post(client, ticket, typed)
    assert response.status_code in (400, 422)
    assert reload(db_session, ticket).requester_email == OLD


def test_the_same_person_is_refused_even_under_another_case(client, db_session, desk):
    ticket = worked(db_session, desk)
    response = post(client, ticket, "OLD@example.com")
    assert response.status_code == 400 and "same person" in response.text
    assert events(db_session, ticket, "requester_changed") == []


def test_a_linked_address_of_the_same_person_is_refused(client, db_session, desk):
    main = person(db_session, OLD, "Old Person")
    db_session.add(LinkedIdentity(main_user_id=main.id, email="old.second@example.com", method="email_code", status="active"))
    db_session.commit()
    ticket = worked(db_session, desk)
    response = post(client, ticket, "old.second@example.com")
    assert response.status_code == 400 and "same person" in response.text
    # and the other way round: the ticket is under the linked address, the main address is typed
    other = worked(db_session, desk, requester="old.second@example.com")
    assert post(client, other, OLD).status_code == 400
    assert reload(db_session, ticket).requester_email == OLD


def test_the_support_mailbox_an_anonymised_address_and_a_switched_off_account_are_refused(client, db_session, desk, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "itsupport@example.com")
    person(db_session, "gone@example.com", "Gone", is_active=False)
    ticket = worked(db_session, desk)
    for typed in ("itsupport@example.com", "anonymised-3@invalid", "gone@example.com"):
        assert post(client, ticket, typed).status_code == 400, typed
    assert reload(db_session, ticket).requester_email == OLD


def test_the_picker_lists_active_accounts_by_name_or_address(client, db_session, desk):
    person(db_session, "pat@example.com", "Pat Smith")
    person(db_session, "patrick@example.com", "Patrick Off", is_active=False)
    ticket = worked(db_session, desk)
    page = client.get(f"/tickets/{ticket.number}/requester-search?new_requester=pat").text
    assert "pat@example.com" in page and "Pat Smith" in page and "patrick@example.com" not in page
    assert client.get(f"/tickets/{ticket.number}/requester-search?new_requester=p").text == ""  # too short to search
    assert [u.email for u in suggestions(db_session, "SMITH")] == ["pat@example.com"]


def test_resolve_address_needs_a_single_exact_name(db_session):
    person(db_session, "a1@example.com", "Sam Jones")
    person(db_session, "a2@example.com", "Sam Jones")
    with pytest.raises(RequesterChangeError):
        resolve_address(db_session, "Sam Jones")


# ---- History -----------------------------------------------------------------------------------------


def test_the_event_and_the_audit_entry_say_from_and_to_and_who(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket)
    [event] = events(db_session, ticket, "requester_changed")
    assert (event.from_value, event.to_value, event.actor_email) == (OLD, NEW, "agent@example.com")
    assert event.created_at is not None
    [entry] = audit(db_session)
    assert entry.entity_id == ticket.id and entry.actor_email == "agent@example.com" and OLD in entry.detail and NEW in entry.detail
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Changed the end user from Old to New." in page


def test_a_refused_change_writes_nothing(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket, OLD)
    assert events(db_session, ticket, "requester_changed") == [] and audit(db_session) == []


def test_the_event_is_not_visible_to_the_requester_and_the_new_requester_sees_only_what_a_requester_sees(client, db_session, desk):
    from app.messages import add_note

    ticket = worked(db_session, desk)
    agent_user = db_session.exec(select(User).where(User.email == "agent@example.com")).one()
    add_note(db_session, ticket, agent_user, "Internal: the old requester is awkward")
    db_session.commit()
    assert post(client, ticket, keep_old="on").status_code == 200

    for email in (NEW, OLD):
        client.post("/auth/logout")
        login_as(client, email)
        page = client.get(f"/portal/{ticket.number}")
        assert page.status_code == 200
        assert "end user" not in page.text.lower() and "requester_changed" not in page.text
        assert "awkward" not in page.text


# ---- The old requester -------------------------------------------------------------------------------


def test_the_old_requester_is_kept_as_a_follower_when_ticked_and_still_sees_the_ticket(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket, keep_old="on")
    db_session.expire_all()
    assert is_follower(db_session, ticket, OLD, include_internal=False)
    [event] = events(db_session, ticket, "requester_changed")
    assert "kept as a follower" in event.detail
    client.post("/auth/logout")
    login_as(client, OLD)
    assert client.get(f"/portal/{ticket.number}").status_code == 200


def test_the_old_requester_loses_the_portal_when_not_kept_and_the_new_one_gets_it(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket, keep_old=None)
    db_session.expire_all()
    assert not is_follower(db_session, ticket, OLD)
    client.post("/auth/logout")
    login_as(client, OLD)
    assert client.get(f"/portal/{ticket.number}").status_code == 404
    client.post("/auth/logout")
    login_as(client, NEW)
    assert client.get(f"/portal/{ticket.number}").status_code == 200
    assert ticket.reference in client.get("/portal").text


def test_a_never_follow_old_address_is_not_made_a_follower_and_the_agent_is_told(client, db_session, desk):
    from app.models import NeverFollowAddress

    db_session.add(NeverFollowAddress(email=OLD, added_by="admin@example.com"))
    db_session.commit()
    ticket = worked(db_session, desk)
    response = post(client, ticket)
    assert response.status_code == 200 and "never-follow" in response.text
    assert reload(db_session, ticket).requester_email == NEW
    assert db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id)).all() == []


def test_the_new_requester_who_followed_stops_being_a_follower(client, db_session, desk):
    ticket = worked(db_session, desk)
    db_session.add(TicketFollower(ticket_id=ticket.id, email=NEW, added_by_email="x"))
    db_session.commit()
    post(client, ticket)
    db_session.expire_all()
    rows = db_session.exec(select(TicketFollower).where(TicketFollower.ticket_id == ticket.id)).all()
    assert [r.email for r in rows] == [OLD]


# ---- Telling the new requester -----------------------------------------------------------------------


def test_nobody_is_emailed_unless_the_box_is_ticked(client, db_session, desk, dev_mail):
    ticket = worked(db_session, desk)
    post(client, ticket)
    assert mail_rows(db_session) == []


def test_ticked_the_new_requester_is_emailed_with_the_template_and_the_old_one_is_not(client, db_session, desk, dev_mail):
    ticket = worked(db_session, desk)
    post(client, ticket, email_new="on")
    [row] = mail_rows(db_session)
    assert row.to_address == NEW and row.ticket_id == ticket.id
    assert [r.to_address for r in db_session.exec(select(OutboundEmail)).all()] == [NEW]
    built = mail.BUILDERS[outbox.REQUESTER_CHANGED](db_session, row)
    assert ticket.reference in built.body and ticket.subject in built.body and "made you the person this ticket is for" in built.body
    assert built.to == NEW and built.automatic


def test_the_email_uses_the_edited_template(client, db_session, desk, dev_mail):
    db_session.add(EmailTemplate(key="requester_changed", subject="", body="Hello {reference} / {subject}", updated_by="admin@example.com"))
    db_session.commit()
    ticket = worked(db_session, desk)
    post(client, ticket, email_new="on")
    [row] = mail_rows(db_session)
    assert f"Hello {ticket.reference} / {ticket.subject}" in mail.BUILDERS[outbox.REQUESTER_CHANGED](db_session, row).body


def test_the_template_is_in_the_admin_list_and_resets(client, db_session):
    assert "requester_changed" in REGISTRY
    login_as(client, "boss@example.com")
    set_role(db_session, db_session.exec(select(User).where(User.email == "boss@example.com")).one().id, UserRole.SUPER_ADMIN)
    page = client.get("/admin/email-templates")
    assert "requester_changed" in page.text
    edit = client.get("/admin/email-templates/requester_changed")
    assert edit.status_code == 200 and "{reference}" in edit.text
    assert "made you the person this ticket is for" in current_text(db_session, "requester_changed")[1]


def test_the_email_is_dropped_when_the_ticket_moved_on_again(client, db_session, desk, dev_mail):
    ticket = worked(db_session, desk)
    post(client, ticket, email_new="on")
    [row] = mail_rows(db_session)
    post(client, ticket, "third@example.com")
    db_session.expire_all()
    with pytest.raises(mail.Unsendable):
        mail.BUILDERS[outbox.REQUESTER_CHANGED](db_session, row)


def test_with_sending_off_the_agent_is_told_the_email_was_not_queued(client, db_session, desk):
    ticket = worked(db_session, desk)
    response = post(client, ticket, email_new="on")
    assert "not emailed" in response.text and mail_rows(db_session) == []
    assert reload(db_session, ticket).requester_email == NEW


# ---- What else follows ---------------------------------------------------------------------------------


def test_replies_go_by_email_until_the_new_requester_writes(client, db_session, desk):
    ticket = worked(db_session, desk, source=TicketSource.TEAMS, last_requester_channel="teams")
    post(client, ticket)
    assert reload(db_session, ticket).last_requester_channel == "email"


def test_the_due_date_deadline_follows_the_new_requesters_time_zone(client, db_session, desk):
    from app.sla import recalculate_deadlines

    person(db_session, NEW, "New", time_zone="Pacific/Auckland")
    person(db_session, OLD, "Old", time_zone="America/New_York")
    due = date.today() + timedelta(days=5)
    ticket = worked(db_session, desk, due_on=due, due_set_by_hand=True)
    recalculate_deadlines(db_session, ticket)
    db_session.add(ticket)
    db_session.commit()
    assert ticket.resolution_due_at == end_of_day_utc(due, "America/New_York")
    post(client, ticket)
    ticket = reload(db_session, ticket)
    assert ticket.due_on == due and ticket.resolution_due_at == end_of_day_utc(due, "Pacific/Auckland")


def test_an_approver_of_a_pending_request_cannot_become_its_requester_but_anyone_else_can(client, db_session):
    approval_person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert approval_status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL
    admin = person(db_session, "admin@example.com", role=UserRole.SUPER_ADMIN)

    with pytest.raises(RequesterChangeError, match="approvers"):
        change_requester(db_session, ticket, admin, "boss@example.com")
    assert ticket.requester_email == "jo@example.com"

    change_requester(db_session, ticket, admin, "someone.else@example.com")
    assert ticket.requester_email == "someone.else@example.com"


def test_sub_tickets_keep_their_own_requester(client, db_session, desk):
    parent = worked(db_session, desk)
    child = create_ticket(db_session, subject="Part", requester_email=OLD, source=TicketSource.EMAIL, team_id=desk.id)
    child.split_from_id = parent.id
    db_session.add(child)
    db_session.commit()
    post(client, parent)
    assert reload(db_session, parent).requester_email == NEW
    assert db_session.get(Ticket, child.id).requester_email == OLD


# ---- Person data -------------------------------------------------------------------------------------


def test_the_events_addresses_are_classified_as_the_persons_own_data():
    for column in ("ticketevent.from_value", "ticketevent.to_value", "ticketevent.actor_email"):
        assert COLUMNS[column].role == "subject"


def _archive(db_session, address):
    admin = db_session.exec(select(User).where(User.email == "dp@example.com")).first() or person(
        db_session, "dp@example.com", role=UserRole.SUPER_ADMIN
    )
    from app.storage import LocalStorage
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        export = build_export(db_session, LocalStorage(folder), address, admin)
        data = export.file.read()
        export.file.close()
    return zipfile.ZipFile(io.BytesIO(data))


def test_the_subject_access_export_includes_the_event_for_both_people(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket)
    for address in (OLD, NEW):
        archive = _archive(db_session, address)
        rows = json.loads(archive.read("ticket_events.json"))
        assert [r["event"] for r in rows if r["event"] == "requester_changed"] == ["requester_changed"], address


def test_anonymising_the_old_requester_scrubs_the_event(client, db_session, desk):
    ticket = worked(db_session, desk)
    post(client, ticket, keep_old=None)
    admin = person(db_session, "dp2@example.com", role=UserRole.SUPER_ADMIN)
    anonymise_person(db_session, OLD, admin, close_open=True)
    db_session.expire_all()
    [event] = events(db_session, ticket, "requester_changed")
    assert event.from_value.startswith("anonymised-") and event.to_value == NEW
    assert OLD not in (event.detail or "")

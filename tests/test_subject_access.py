import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.attachments import store_attachment
from app.main import app
from app.models import (
    ActivityLog,
    ApprovalGroup,
    ApprovalVote,
    AuditLog,
    LinkedIdentity,
    Message,
    MessageDirection,
    MessageVisibility,
    NotificationPreference,
    TeamsConversation,
    Ticket,
    TicketApproval,
    TicketFeedback,
    TicketFollower,
    TicketSource,
    ActionToken,
    User,
    UserPermission,
    UserRole,
)
from app.permissions import DATA_PROTECTION, PERMISSIONS
from app.person_data import COLUMNS, SUBJECT, candidate_keys, find_person, mentions
from app.subject_access import EXPORTED_TABLES
from app.settings_store import SUBJECT_ACCESS_MAX_MB, set_setting
from app.storage import LocalStorage, get_storage
from app.ticket_service import create_ticket
from tests.conftest import login_as, set_role

PDF = b"%PDF-1.7\n" + b"x" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
URL = "/admin/data-protection/subject-access"


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


def mk(db, email, role=UserRole.END_USER, name=None):
    user = User(email=email, display_name=name or email.split("@")[0], role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def msg(db, ticket, body, *, sender="x", direction=MessageDirection.INBOUND, public=True, cc=None, author=None):
    message = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, cc=cc,
        visibility=MessageVisibility.PUBLIC if public else MessageVisibility.AGENTS_ONLY,
        author_user_id=author.id if author else None,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def ticket_for(db, requester, subject):
    ticket = create_ticket(db, subject=subject, requester_email=requester, source=TicketSource.EMAIL)
    ticket.triaged_at = datetime.now(timezone.utc)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def attach(db, storage, ticket, filename, data, message=None, uploaded_by=None):
    attachment = store_attachment(db, storage, ticket=ticket, filename=filename, data=data, message=message, uploaded_by=uploaded_by)
    db.commit()
    return attachment


def admin_client(client, db):
    set_role(db, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def export(client, address="ann@a.example"):
    response = client.post(URL, data={"address": address})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def part(archive, name):
    return json.loads(archive.read(name))


@pytest.fixture
def world(db_session, storage):
    """Ann (two linked addresses and an old address), Bob, Carol, and an agent, with a ticket for each situation."""
    db = db_session
    ann = mk(db, "ann@a.example", name="Ann Able")
    db.add(LinkedIdentity(main_user_id=ann.id, email="ann@b.example", method="admin", status="active"))
    db.add(LinkedIdentity(main_user_id=ann.id, email="ann@old.example", method="email_changed", status="active"))
    bob = mk(db, "bob@x.example", name="Bob Brown")
    mk(db, "carol@x.example", name="Carol Clark")
    agent = mk(db, "dave@a.example", UserRole.AGENT, name="Dave Agent")
    db.commit()

    own = ticket_for(db, "ann@a.example", "Ann's printer")
    ann_msg = msg(db, own, "Ann says printer broke", sender="Ann Able <ann@a.example>")
    reply = msg(db, own, "Agent reply", sender="Dave Agent", direction=MessageDirection.OUTBOUND, author=agent)
    note = msg(db, own, "Secret note about Ann", direction=MessageDirection.INTERNAL, public=False, author=agent)
    own_file = attach(db, storage, own, "photo.png", PNG, message=ann_msg)
    attach(db, storage, own, "agent-guide.pdf", PDF, message=reply)
    attach(db, storage, own, "note-file.pdf", PDF + b"n", message=note)
    token = ActionToken(ticket_id=own.id, kind="feedback", token_hash="h1", resolved_by_id=agent.id, expires_at=datetime.now(timezone.utc))
    db.add(token)
    db.commit()
    db.add(TicketFeedback(ticket_id=own.id, token_id=token.id, rating=2, comment="Ann's feedback words", resolved_by_id=agent.id))

    alias = ticket_for(db, "ann@old.example", "Ann under her old address")
    msg(db, alias, "old address message", sender="ann@old.example")

    shared = ticket_for(db, "bob@x.example", "Bob's VPN")
    db.add(TicketFollower(ticket_id=shared.id, email="ann@b.example", added_by_email="bob@x.example"))
    msg(db, shared, "Bob describes the VPN", sender="Bob Brown")
    msg(db, shared, "Agent note about Bob", direction=MessageDirection.INTERNAL, public=False, author=agent)
    attach(db, storage, shared, "bob-secret.pdf", PDF + b"b", message=None, uploaded_by=bob)
    btoken = ActionToken(ticket_id=shared.id, kind="feedback", token_hash="h2", expires_at=datetime.now(timezone.utc))
    db.add(btoken)
    db.commit()
    db.add(TicketFeedback(ticket_id=shared.id, token_id=btoken.id, rating=5, comment="Bob's private comment"))

    copied = ticket_for(db, "bob@x.example", "Bob's laptop")
    msg(db, copied, "Reply copying Ann", sender="Dave Agent", direction=MessageDirection.OUTBOUND, cc="carol2@x.example, ANN@A.example")

    carol = ticket_for(db, "carol@x.example", "Carol's mouse")
    msg(db, carol, "Carol's private mouse", sender="Carol Clark")
    group = ApprovalGroup(name="Approvers")
    db.add(group)
    db.commit()
    approval = TicketApproval(ticket_id=carol.id, group_id=group.id, pass_percent=50, voters=1, status="approved")
    db.add(approval)
    db.commit()
    db.add(ApprovalVote(approval_id=approval.id, voter_id=ann.id, decision="approved", comment="Ann approves"))
    db.add(NotificationPreference(user_id=ann.id, kind="assigned", channel="email", enabled=False))
    db.add(ActivityLog(event_type="login_success", actor_user_id=ann.id, actor_email="ann@a.example", ip="10.1.2.3"))
    db.add(ActivityLog(event_type="login_success", actor_email="carol@x.example", ip="10.9.9.9"))
    db.add(AuditLog(entity_type="user", entity_id=ann.id, actor_email="admin@x.example", action="role_changed"))
    ann.entra_object_id = "ann-oid"
    db.add(TeamsConversation(entra_object_id="ann-oid", service_url="https://smba.example.net/", conversation_id="a:ann"))
    db.add(TeamsConversation(entra_object_id="bob-oid", service_url="https://smba.example.net/", conversation_id="a:bob"))
    db.commit()
    return dict(ann=ann, bob=bob, agent=agent, own=own, alias=alias, shared=shared, copied=copied, carol=carol, own_file=own_file)


def test_export_covers_every_address_and_every_kind_of_ticket(client, db_session, world):
    admin_client(client, db_session)
    archive = export(client)
    tickets = {t["number"]: t for t in part(archive, "tickets.json")}
    assert set(tickets) == {world["own"].number, world["alias"].number, world["shared"].number, world["copied"].number}
    assert tickets[world["own"].number]["roles"] == ["requester"]
    assert tickets[world["alias"].number]["roles"] == ["requester"]  # raised under her old address
    assert tickets[world["shared"].number]["roles"] == ["follower"]  # followed under a linked address
    assert tickets[world["copied"].number]["roles"] == ["copied"]
    assert "Carol's mouse" not in archive.read("tickets.json").decode()
    summary = part(archive, "summary.json")
    assert {"ann@a.example", "ann@b.example", "ann@old.example"} <= set(summary["addresses"])
    assert "summary.html" in archive.namelist()
    assert "Ann&#39;s printer" in archive.read("summary.html").decode()


def test_asking_with_a_linked_address_gives_the_same_person(client, db_session, world):
    admin_client(client, db_session)
    one = part(export(client, "ann@a.example"), "summary.json")["counts"]
    two = part(export(client, "ANN@old.example"), "summary.json")["counts"]
    assert one == two


def test_only_requester_visible_messages_in_the_thread_and_notes_are_apart(client, db_session, world):
    admin_client(client, db_session)
    archive = export(client)
    tickets = {t["number"]: t for t in part(archive, "tickets.json")}
    bodies = [m["body"] for m in tickets[world["own"].number]["messages"]]
    assert bodies == ["Ann says printer broke", "Agent reply"]
    thread_text = archive.read("tickets.json").decode()
    assert "Secret note about Ann" not in thread_text and "Agent note about Bob" not in thread_text
    notes = part(archive, "internal_notes.json")
    texts = [n["body"] for t in notes["tickets"] for n in t["notes"]]
    assert sorted(texts) == ["Agent note about Bob", "Secret note about Ann"]
    assert "NOT part of what the requester may see" in notes["warning"]
    assert "Secret note about Ann" not in archive.read("summary.html").decode()


def test_another_persons_data_on_a_shared_ticket_is_not_included(client, db_session, world):
    admin_client(client, db_session)
    archive = export(client)
    shared = {t["number"]: t for t in part(archive, "tickets.json")}[world["shared"].number]
    assert [m["body"] for m in shared["messages"]] == ["Bob describes the VPN"]  # the thread, nothing else of Bob's
    assert "feedback" not in shared and "form_answers" not in shared  # Bob's own words
    everything = b"".join(archive.read(n) for n in archive.namelist())
    assert b"Bob's private comment" not in everything
    assert b"bob-secret" not in everything
    assert b"Carol's private mouse" not in everything and b"10.9.9.9" not in everything
    assert b"hash" not in archive.read("account.json").lower() and b"totp" not in archive.read("account.json").lower()


def test_form_answers_feedback_votes_preferences_activity_and_audit(client, db_session, world):
    admin_client(client, db_session)
    archive = export(client)
    own = {t["number"]: t for t in part(archive, "tickets.json")}[world["own"].number]
    assert own["feedback"][0]["rating"] == 2 and own["feedback"][0]["comment"] == "Ann's feedback words"
    assert own["feedback"][0]["about"] == f"staff member user-{world['agent'].id}"
    votes = part(archive, "approvals.json")
    assert votes[0]["comment"] == "Ann approves" and votes[0]["ticket"] == f"#{world['carol'].number}"
    assert "Carol's mouse" not in json.dumps(votes)  # the ticket is named by number only
    assert part(archive, "preferences.json")["notification_preferences"] == [{"event": "assigned", "channel": "email", "enabled": False}]
    activity = archive.read("activity.csv").decode()
    assert "10.1.2.3" in activity and "10.9.9.9" not in activity
    audit = archive.read("audit.csv").decode()
    assert "role_changed" in audit and "admin@x.example" not in audit  # the admin is "a staff member"
    account = part(archive, "account.json")
    assert account["accounts"][0]["email"] == "ann@a.example"
    assert [(c["entra_object_id"], c["conversation_id"]) for c in account["teams_bot"]] == [("ann-oid", "a:ann")]
    assert {l["address"] for l in account["linked_identities"]} == {"ann@b.example", "ann@old.example"}


def test_only_the_persons_own_attachments_are_files(client, db_session, world):
    admin_client(client, db_session)
    archive = export(client)
    names = archive.namelist()
    files = [n for n in names if n.startswith("attachments/")]
    assert len(files) == 1 and files[0].endswith("-photo.png")
    assert archive.read(files[0]) == PNG
    listed = {a["filename"]: a["reason"] for a in part(archive, "attachments_not_added.json")}
    assert "agent-guide.pdf" in listed
    assert "bob-secret.pdf" not in listed
    # A file on an internal note is named only in the internal notes file, never in the parts handed over.
    assert "note-file.pdf" not in listed and "note-file.pdf" not in archive.read("summary.html").decode()
    on_notes = [f["filename"] for t in part(archive, "internal_notes.json")["tickets"] for f in t["files_on_notes"]]
    assert on_notes == ["note-file.pdf"]


def test_a_big_collection_is_listed_with_sizes_not_added(client, db_session, storage, world):
    set_setting(db_session, SUBJECT_ACCESS_MAX_MB, "1")
    big = ticket_for(db_session, "ann@a.example", "Big files")
    message = msg(db_session, big, "heavy", sender="ann@a.example")
    attach(db_session, storage, big, "huge.png", PNG + b"\x01" * (1024 * 1024 + 10), message=message)
    admin_client(client, db_session)
    archive = export(client)
    assert not any(n.endswith("huge.png") for n in archive.namelist())
    listed = {a["filename"]: a for a in part(archive, "attachments_not_added.json")}
    assert listed["huge.png"]["size"] > 1024 * 1024 and "size limit" in listed["huge.png"]["reason"]


def test_an_agent_is_listed_by_reference_only(client, db_session, world):
    admin_client(client, db_session)
    world["own"].assigned_agent_id = world["agent"].id
    db_session.add(world["own"])
    db_session.commit()
    archive = export(client, "dave@a.example")
    staff = part(archive, "staff_references.json")
    assert {"ticket": world["own"].reference, "as": ["assigned"]} in staff["tickets_worked"]
    assert len(staff["messages_written"]) == 3  # two on Ann's ticket, one note on Bob's: by reference
    assert all(set(m) == {"message_id", "ticket", "direction", "written_at"} for m in staff["messages_written"])
    assert b"Agent reply" not in archive.read("staff_references.json")
    assert part(archive, "tickets.json") == []  # not the requester on anything


def test_a_person_with_nothing_held_gets_an_empty_export(client, db_session, world):
    admin_client(client, db_session)
    summary = part(export(client, "nobody@nowhere.example"), "summary.json")
    assert summary["counts"]["tickets"] == 0 and summary["has_account"] is False


def test_each_export_is_audited_with_who_and_for_whom(client, db_session, world):
    admin_client(client, db_session)
    export(client)
    db_session.expire_all()
    row = db_session.exec(select(AuditLog).where(AuditLog.action == "subject_access_export")).one()
    assert row.actor_email == "admin@example.com" and row.entity_type == "subject_access"
    assert "ann@a.example" in row.detail and row.entity_id == world["ann"].id


def test_nothing_is_left_on_disk_after_the_download(client, db_session, world, tmp_path, monkeypatch):
    import tempfile

    created = []
    real = tempfile.TemporaryFile

    def tracking(*args, **kwargs):
        handle = real(*args, **kwargs)
        created.append(handle)
        return handle

    monkeypatch.setattr(tempfile, "TemporaryFile", tracking)
    admin_client(client, db_session)
    export(client)
    assert created and all(handle.closed for handle in created)


def test_permission_super_admin_named_agent_and_nobody_else(client, db_session, world):
    assert DATA_PROTECTION in PERMISSIONS
    agent = world["agent"]
    client.post("/auth/logout")
    login_as(client, "dave@a.example")
    set_role(db_session, agent.id, UserRole.AGENT)
    assert client.get("/admin/data-protection").status_code == 403
    assert client.post(URL, data={"address": "ann@a.example"}).status_code == 403
    db_session.add(UserPermission(user_id=agent.id, permission=DATA_PROTECTION))
    db_session.commit()
    page = client.get("/admin/data-protection")
    assert page.status_code == 200 and "Data protection" in page.text
    assert client.post(URL, data={"address": "ann@a.example"}).status_code == 200
    assert client.get("/admin/users").status_code == 403  # the permission opens this page only
    client.post("/auth/logout")
    login_as(client, "ann@a.example")
    assert client.get("/admin/data-protection").status_code == 403
    assert client.post(URL, data={"address": "ann@a.example"}).status_code == 403
    client.post("/auth/logout")
    admin_client(client, db_session)
    assert client.get("/admin/data-protection").status_code == 200


def test_a_bad_address_is_refused_and_not_audited(client, db_session, world):
    admin_client(client, db_session)
    assert client.post(URL, data={"address": "not an address"}).status_code == 400
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "subject_access_export")).all() == []


# ---- the person lookup ----------------------------------------------------------------------------------------


def test_find_person_gathers_addresses_and_accounts(db_session, world):
    person = find_person(db_session, "ann@old.example")
    assert person.addresses == {"ann@a.example", "ann@b.example", "ann@old.example"}
    assert world["ann"].id in person.user_ids and world["bob"].id not in person.user_ids


def test_mentions_matches_whole_addresses_only():
    person = type("P", (), {"addresses": {"ann@a.example"}})()
    assert mentions("Ann <ANN@a.example>, x@y.example", person)
    assert not mentions("joann@a.example", person)
    assert not mentions("ann@a.example.evil.com", person)


def test_every_column_that_holds_a_persons_data_is_classified():
    """A new email, name, user id, actor or free-text column must be decided in app/person_data.py before this passes."""
    assert candidate_keys() - set(COLUMNS) == set()


def test_the_classification_names_real_columns():
    from sqlmodel import SQLModel

    real = {f"{t.name}.{c.name}" for t in SQLModel.metadata.tables.values() for c in t.columns}
    assert set(COLUMNS) - real == set()


def test_every_table_classified_as_the_persons_own_is_in_the_export():
    """A table whose columns are the person's own data must be exported (or this list consciously extended)."""
    own = {key.split(".")[0] for key, use in COLUMNS.items() if use.role == SUBJECT}
    assert own - set(EXPORTED_TABLES) == set()


def test_mentions_sees_addresses_in_brackets():
    person = type("P", (), {"addresses": {"ann@a.example"}})()
    assert mentions("Ann (ann@a.example)", person) and mentions("[ann@a.example]", person)


# ---- review findings ------------------------------------------------------------------------------------------


def test_a_colleague_with_the_same_display_name_is_not_the_person(client, db_session, storage, world):
    namesake = ticket_for(db_session, "ann@a.example", "Namesake thread")
    other = msg(db_session, namesake, "From the other Ann", sender="Ann Able")  # same name, no address
    attach(db_session, storage, namesake, "other-ann.png", PNG + b"\x07", message=other)
    admin_client(client, db_session)
    archive = export(client)
    thread = {t["number"]: t for t in part(archive, "tickets.json")}[namesake.number]["messages"]
    assert thread[0]["written_by_the_person"] is False
    assert not any(n.endswith("other-ann.png") for n in archive.namelist())


def test_a_cc_on_a_message_the_requester_cannot_see_does_not_put_the_ticket_in_scope(client, db_session, world):
    hidden = ticket_for(db_session, "bob@x.example", "Bob's hidden cc")
    msg(db_session, hidden, "MSP mail waiting", sender="msp@x.example", cc="ann@a.example", public=False)
    admin_client(client, db_session)
    numbers = {t["number"] for t in part(export(client), "tickets.json")}
    assert hidden.number not in numbers


def test_an_agents_events_and_audit_on_other_tickets_are_by_reference(client, db_session, world):
    from app.models import TicketEvent

    db_session.add(TicketEvent(ticket_id=world["carol"].id, event_type="follower_added", actor_email="dave@a.example", to_value="carol.friend@x.example"))
    db_session.add(TicketEvent(ticket_id=world["carol"].id, event_type="status_changed", actor_email="carol@x.example", to_value="open"))
    db_session.add(AuditLog(entity_type="user", entity_id=world["bob"].id, actor_email="dave@a.example", action="identity_linked", detail="bob@x.example linked"))
    db_session.commit()
    admin_client(client, db_session)
    archive = export(client, "dave@a.example")
    events = part(archive, "ticket_events.json")
    assert [e["event"] for e in events] == ["follower_added"] and "to" not in events[0]
    everything = b"".join(archive.read(n) for n in archive.namelist())
    assert b"carol.friend" not in everything and b"bob@x.example linked" not in everything
    assert b"identity_linked" in archive.read("audit.csv")


def test_a_file_that_vanishes_is_reported_not_counted(client, db_session, storage, world):
    storage_path = storage._path(world["own_file"].blob_key)
    storage_path.unlink()
    admin_client(client, db_session)
    archive = export(client)
    summary = part(archive, "summary.json")
    assert summary["counts"]["attachment_files"] == 0
    assert "summary.json" in summary["files"]
    reasons = {a["filename"]: a["reason"] for a in part(archive, "attachments_not_added.json")}
    assert "missing" in reasons["photo.png"]


def test_link_codes_and_follow_dates_are_exported(client, db_session, world):
    from app.models import LinkCode

    db_session.add(LinkCode(user_id=world["ann"].id, address="ann@z.example", code_hash="secret-hash", expires_at=datetime.now(timezone.utc)))
    db_session.commit()
    admin_client(client, db_session)
    archive = export(client)
    codes = part(archive, "account.json")["link_codes_asked"]
    assert codes[0]["address"] == "ann@z.example"
    assert b"secret-hash" not in archive.read("account.json")
    shared = {t["number"]: t for t in part(archive, "tickets.json")}[world["shared"].number]
    assert len(shared["following_since"]) == 1

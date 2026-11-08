"""7C: requester anonymisation (app/anonymisation.py, Admin > Data protection). Keeps the ticket, removes the person.
The world is the one the subject access export tests use (Ann, with two linked addresses and an old one)."""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import String, Text, func
from sqlmodel import SQLModel, select

from app import anonymisation
from app.anonymisation import REMOVED, STEPS, address_hash, expected_step_keys, purge_pending_blobs, run_blob_cleanup
from app.attachments import blob_key
from app.config import settings
from app.inbound import REPLY_ADDED, TICKET_CREATED, IncomingMail, ingest_mail
from app.mail import _guard_recipient
from app.models import (
    ActionToken,
    ActivityLog,
    Attachment,
    AuditLog,
    InboundMail,
    LinkedIdentity,
    Message,
    Notification,
    NotificationPreference,
    OutboundEmail,
    PendingBlobDelete,
    AnonymisedRequester,
    RequesterTimeZone,
    Ticket,
    TicketAnswer,
    TicketEvent,
    TicketFeedback,
    TicketFollower,
    TicketStatus,
    User,
    RequestType,
    RequestTypeQuestion,
    UserPermission,
    UserRole,
    ApprovalGroup,
    ApprovalVote,
    TicketApproval,
)
from app.permissions import DATA_PROTECTION
from app.person_data import COLUMNS, DELETE, KEEP, NULL, REMOVE, REPLACE, SCRUB, BLANK, SUBJECT, TEXT, WITH_ROW
from app.storage import LocalStorage, Storage
from app.teams import Unsendable
from tests.conftest import login_as, set_role
from tests.test_reports import admin_user, notes, run as run_report
from tests.test_subject_access import (  # noqa: F401  (storage and world are fixtures)
    PDF, PNG, admin_client, attach, export, mk, msg, part, storage, ticket_for, world,
)

ADDRESSES = ["ann@a.example", "ann@b.example", "ann@old.example"]
URL = "/admin/data-protection/anonymise"


def anonymise(client, address="ann@a.example", *, close=True, confirm=None):
    data = {"address": address, "confirm": address if confirm is None else confirm}
    if close:
        data["close_open"] = "true"
    return client.post(URL, data=data)


def preview(client, address="ann@a.example", *, close=True):
    return client.post(URL + "/preview", data={"address": address, **({"close_open": "true"} if close else {})})


def everything_text(db):
    """Every text value in every table, lower case, as (table.column, value)."""
    found = []
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (String, Text)):
                for (value,) in db.execute(select(column).where(column.is_not(None))).all():
                    found.append((f"{table.name}.{column.name}", value.lower()))
    return found


# ---- the classification is the single list ---------------------------------------------------------------------


def test_every_decision_has_a_step_and_every_step_has_a_decision():
    assert set(STEPS) == expected_step_keys()


def test_no_column_of_the_persons_own_data_is_left_without_a_reason():
    """A subject or free-text column that is kept says why (the note); everything else has a step."""
    for key, use in COLUMNS.items():
        if use.role in (SUBJECT, TEXT) and use.anon == KEEP:
            assert use.note, f"{key} is kept without a reason"


def test_decisions_are_known_words():
    assert {u.anon for u in COLUMNS.values()} <= {KEEP, REPLACE, SCRUB, BLANK, NULL, REMOVE, DELETE, WITH_ROW}


def test_free_text_columns_the_name_pattern_misses_were_checked_by_hand():
    """Every String column in the schema that the classification does not list is a label, a status or a hash, listed
    here by hand (the pattern in person_data finds columns by name; this finds the rest by type)."""
    labels = {
        "teammembership.level", "user.role", "ticket.type", "ticket.status", "ticket.source", "message.direction",
        "message.visibility",
    }
    unlisted = set()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            if key in COLUMNS or key in labels or column.primary_key or column.foreign_keys:
                continue
            if isinstance(column.type, (String, Text)):
                unlisted.add(key)
    # What is left must be on this reviewed list of non-personal columns (hashes, kinds, ids, configuration).
    reviewed = {
        k for k in unlisted
        if k.split(".")[1] in {
            "kind", "channel", "status", "name", "description", "decision", "outcome", "event_type", "method", "path", "sort", "code_hash",
            "token_hash", "graph_message_id", "dates", "last_error", "region", "normalised_text", "label", "key", "value", "priority",
            "question_label", "question_kind", "lease_owner", "attachment_ids", "filename", "content_type", "content_hash", "blob_key",
            "time_zone", "source", "time_zone_source", "totp_secret", "password_hash", "emergency_password_hash", "msp_number", "msp_forward",
            "msp_hold_reason", "message_text", "client_state", "resource", "subscription_id", "payload", "options", "condition",
            "help_text", "scope", "tier", "hash", "reference", "title",
        }
    }
    assert unlisted - reviewed == set(), f"new text columns to look at by hand: {sorted(unlisted - reviewed)}"


# ---- the whole thing -------------------------------------------------------------------------------------------


def ann_ticket_numbers(world):
    return {world["own"].number, world["alias"].number}


def test_it_removes_the_person_and_keeps_the_tickets(client, db_session, storage, world):
    db = db_session
    own, alias, shared, copied, carol = (world[k] for k in ("own", "alias", "shared", "copied", "carol"))
    ann_id = world["ann"].id
    db.add(Notification(user_id=ann_id, kind="assigned", text="Ann has a bell", actor_email="ann@a.example"))
    db.add(RequesterTimeZone(email="ann@b.example", time_zone="Europe/London"))
    db.add(UserPermission(user_id=ann_id, permission="view_reports"))
    request_type = RequestType(name="Printing")
    db.add(request_type)
    db.commit()
    question = RequestTypeQuestion(request_type_id=request_type.id, label="Which printer?", kind="text")
    db.add(question)
    db.commit()
    db.add(TicketAnswer(ticket_id=own.id, question_id=question.id, question_label="Which printer?", question_kind="text", value="the red one"))
    db.add(AuditLog(entity_type="user", entity_id=ann_id, actor_email="admin@x.example", action="alias_added", detail="ann@old.example (old address)"))
    db.add(TicketEvent(ticket_id=own.id, event_type="follower_added", actor_email="ann@a.example", to_value="ann@b.example", detail="from ann@a.example"))
    db.commit()
    before_statuses = {t.id: (t.number, t.type, t.team_id, t.category_id, t.created_at, t.status) for t in db.exec(select(Ticket)).all()}
    shared_blob = attach(db, storage, shared, "same-bytes.png", PNG, message=None)  # identical to Ann's photo: one blob, two rows
    photo_key = world["own_file"].blob_key
    assert shared_blob.blob_key == photo_key
    agent_guide_key = blob_key(hashlib.sha256(PDF).hexdigest())
    own.graph_conversation_id = "conv-own"
    db.add(own)
    db.commit()
    admin_client(client, db)

    response = anonymise(client)
    assert response.status_code == 200, response.text
    assert "identical to files still used on other tickets" in response.text  # the shared blob was not queued
    db.expire_all()

    # tickets kept with number, type, team, category, dates and (now closed) status; the requester is the placeholder
    for t in db.exec(select(Ticket)).all():
        number, kind, team, category, created, status = before_statuses[t.id]
        assert (t.number, t.type, t.team_id, t.category_id, t.created_at) == (number, kind, team, category, created)
    n = db.exec(select(AnonymisedRequester.id)).one()
    for number in ann_ticket_numbers(world):
        ticket = db.exec(select(Ticket).where(Ticket.number == number)).one()
        assert ticket.requester_email == f"anonymised-{n}@invalid"
        assert ticket.status == TicketStatus.CLOSED
    assert db.get(Ticket, shared.id).requester_email == "bob@x.example"  # someone else's ticket is untouched
    assert db.get(Ticket, own.id).graph_conversation_id is None  # the mail thread is cut

    # the account
    ann = db.get(User, ann_id)
    assert ann.email == f"anonymised-{n}@invalid" and ann.display_name == f"Anonymised requester #{n}"
    assert ann.is_active is False and ann.entra_object_id is None
    # settings and identity rows
    assert db.exec(select(LinkedIdentity)).all() == []
    assert db.exec(select(NotificationPreference)).all() == []
    assert db.exec(select(Notification).where(Notification.user_id == ann_id)).all() == []
    assert not [r for r in db.exec(select(RequesterTimeZone)).all() if r.email.startswith("ann@")]
    assert db.exec(select(UserPermission).where(UserPermission.user_id == ann_id)).all() == []
    # follower rows naming her are gone; Bob's stay
    assert db.exec(select(TicketFollower).where(TicketFollower.email.like("ann@%"))).all() == []
    # what she wrote and the agent emails to her are removed; other people's words stay
    assert sorted(m.body for m in db.exec(select(Message).where(Message.ticket_id == own.id)).all()) == sorted([REMOVED, REMOVED, "Secret note about Ann"])
    assert {m.body for m in db.exec(select(Message).where(Message.ticket_id == carol.id)).all()} == {"Carol's private mouse"}
    assert "Bob describes the VPN" in {m.body for m in db.exec(select(Message).where(Message.ticket_id == shared.id)).all()}
    # copy lists lose her address
    assert db.exec(select(Message.cc).where(Message.ticket_id == copied.id, Message.cc.is_not(None))).one() == "carol2@x.example"
    # feedback: rating kept, words gone; approval vote kept, comment gone; Bob's comment untouched
    ratings = {f.ticket_id: (f.rating, f.comment) for f in db.exec(select(TicketFeedback)).all()}
    assert ratings[own.id] == (2, "") and ratings[shared.id] == (5, "Bob's private comment")
    vote = db.exec(select(ApprovalVote)).one()
    assert (vote.decision, vote.comment) == ("approved", "")
    # her activity rows lose the network address; Carol's keep theirs
    assert {a.actor_email: a.ip for a in db.exec(select(ActivityLog).where(ActivityLog.event_type == "login_success", ActivityLog.actor_email != "admin@example.com")).all()} == {
        f"anonymised-{n}@invalid": None, "carol@x.example": "10.9.9.9",
    }
    # files: hers are deleted from storage; Bob's file stays; the blob shared with Bob's ticket stays too
    assert not storage.exists(agent_guide_key) and not storage.exists(blob_key(hashlib.sha256(PDF + b"n").hexdigest()))
    assert storage.exists(photo_key)  # still used by Bob's ticket
    assert storage.exists(blob_key(hashlib.sha256(PDF + b"b").hexdigest()))
    assert {a.filename for a in db.exec(select(Attachment)).all()} == {"same-bytes.png", "bob-secret.pdf"}
    assert db.exec(select(PendingBlobDelete)).all() == []  # all deletes went through
    assert db.exec(select(TicketAnswer.value)).one() == ""  # form answers blanked
    assert db.exec(select(func.count()).select_from(ActionToken).where(ActionToken.used_at.is_(None), ActionToken.ticket_id == own.id)).one() == 0
    # nothing anywhere holds her addresses or name
    for key, value in everything_text(db):
        for needle in ADDRESSES + ["ann able"]:
            assert needle not in value, f"{needle} still in {key}"


def test_the_audit_holds_a_hash_and_never_the_address(client, db_session, world):
    admin_client(client, db_session)
    anonymise(client)
    db_session.expire_all()
    entry = db_session.exec(select(AuditLog).where(AuditLog.action == "requester_anonymised")).one()
    assert entry.entity_type == "anonymisation" and entry.actor_email == "admin@example.com"
    assert address_hash("ann@a.example") in entry.detail and "2 tickets kept" in entry.detail
    assert "ann@" not in entry.detail
    stored = db_session.exec(select(AnonymisedRequester)).one()
    assert stored.address_hash == address_hash("ANN@a.example ") and "ann" not in stored.address_hash
    assert address_hash("ann@a.example") != address_hash("ann@b.example")


def test_a_repeat_request_is_recognised(client, db_session, world):
    admin_client(client, db_session)
    anonymise(client)
    again = preview(client)
    assert again.status_code == 200
    assert "Nothing is held under that address" in again.text
    assert "was anonymised before" in again.text
    # the person comes back and raises a ticket: a new run gets its own number
    ticket = ticket_for(db_session, "ann@a.example", "Back again")
    ticket.status = TicketStatus.CLOSED
    db_session.add(ticket)
    db_session.commit()
    assert anonymise(client).status_code == 200
    db_session.expire_all()
    details = [e.detail for e in db_session.exec(select(AuditLog).where(AuditLog.action == "requester_anonymised").order_by(AuditLog.id)).all()]
    assert "repeat request: no" in details[0] and "repeat request: yes" in details[1]
    numbers = sorted(r.id for r in db_session.exec(select(AnonymisedRequester)).all())
    assert len(numbers) == 2
    hashes = {r.address_hash for r in db_session.exec(select(AnonymisedRequester)).all()}
    assert len(hashes) == 1
    assert db_session.get(Ticket, ticket.id).requester_email == f"anonymised-{numbers[1]}@invalid"


def test_linked_and_old_addresses_are_all_covered(client, db_session, world):
    admin_client(client, db_session)
    # asked by the old address: the same person
    assert anonymise(client, "ann@old.example").status_code == 200
    db_session.expire_all()
    assert {t.requester_email for t in db_session.exec(select(Ticket).where(Ticket.id.in_([world["own"].id, world["alias"].id]))).all()} != set(ADDRESSES)
    assert not [v for k, v in everything_text(db_session) if any(a in v for a in ADDRESSES)]


def test_reports_still_count_the_tickets(client, db_session, world):
    admin = admin_user(client, db_session)
    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    today = now.date()
    params = {"from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()}

    def figures():
        db_session.expire_all()
        report = run_report(db_session, admin, "volume", params, now=now)
        return notes(report)["Tickets created in the range"], report.rows

    before = figures()
    assert int(before[0]) == 5  # Ann's two, Bob's two, Carol's one
    assert anonymise(client).status_code == 200
    assert figures() == before  # the same tickets, counted under the same categories, teams, types and sources


def test_mail_from_the_old_address_starts_a_fresh_ticket(db_session, monkeypatch, client, storage):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "support@corp.example")
    first = ingest_mail(db_session, IncomingMail("m1", "ann@a.example", "Printer jam", "stuck", internet_message_id="m1@x", conversation_id="conv-1"))
    old = first.ticket
    old.triaged_at = datetime.now(timezone.utc)
    db_session.add(old)
    db_session.commit()
    admin_client(client, db_session)
    assert anonymise(client).status_code == 200
    db_session.expire_all()
    # the same thread, the same Message-ID, the ticket's own reference: none of it reaches the anonymised ticket
    follow = ingest_mail(
        db_session,
        IncomingMail("m2", "ann@a.example", f"Re: [{old.reference}] Printer jam", "still stuck", internet_message_id="m2@x",
                     conversation_id="conv-1", in_reply_to=["m1@x"]),
    )
    db_session.commit()
    assert follow.outcome == TICKET_CREATED
    assert follow.ticket.id != old.id and follow.ticket.requester_email == "ann@a.example"
    assert db_session.get(Ticket, old.id).requester_email.endswith("@invalid")


def test_without_clearing_the_thread_the_mail_would_have_joined(db_session, monkeypatch):
    """The control for the test above: an ordinary thread with the same ids does join."""
    monkeypatch.setattr(settings, "graph_shared_mailbox", "support@corp.example")
    first = ingest_mail(db_session, IncomingMail("m1", "ann@a.example", "Printer jam", "stuck", internet_message_id="m1@x", conversation_id="conv-1"))
    follow = ingest_mail(
        db_session, IncomingMail("m2", "ann@a.example", "Re: Printer jam", "still", internet_message_id="m2@x", conversation_id="conv-1", in_reply_to=["m1@x"])
    )
    assert follow.outcome == REPLY_ADDED and follow.ticket.id == first.ticket.id


def test_a_subject_access_export_for_the_old_address_finds_nothing(client, db_session, world):
    admin_client(client, db_session)
    assert anonymise(client).status_code == 200
    for address in ADDRESSES:
        archive = export(client, address)
        summary = part(archive, "summary.json")
        assert part(archive, "tickets.json") == []
        assert summary["counts"].get("tickets", 0) == 0
        for name in archive.namelist():
            text = archive.read(name).replace(address.encode(), b"")
            for needle in ADDRESSES:
                assert needle.encode() not in text.lower(), (address, name)


# ---- refusals and confirmation ---------------------------------------------------------------------------------


def snapshot(db):
    db.expire_all()
    return (
        sorted((t.id, t.requester_email, t.status.value) for t in db.exec(select(Ticket)).all()),
        sorted((m.id, m.body, m.sender) for m in db.exec(select(Message)).all()),
        sorted(a.id for a in db.exec(select(Attachment)).all()),
        db.exec(select(func.count()).select_from(AnonymisedRequester)).one(),
        db.exec(select(func.count()).select_from(AuditLog)).one(),
    )


@pytest.mark.parametrize("role", [UserRole.AGENT, UserRole.SUPER_ADMIN])
def test_agents_and_super_admins_are_refused(client, db_session, world, role):
    boss = mk(db_session, "boss@x.example", role)
    ticket_for(db_session, "boss@x.example", "Boss ticket").status = TicketStatus.CLOSED
    admin_client(client, db_session)
    before = snapshot(db_session)
    response = anonymise(client, "boss@x.example")
    assert response.status_code == 409 and "normal route" in response.text
    assert snapshot(db_session) == before
    assert db_session.get(User, boss.id).is_active


def test_a_break_glass_account_is_refused(client, db_session, world):
    glass = mk(db_session, "glass@x.example", UserRole.END_USER)
    glass.is_break_glass = True
    db_session.add(glass)
    db_session.commit()
    admin_client(client, db_session)
    assert anonymise(client, "glass@x.example").status_code == 409


def test_a_linked_agent_account_makes_the_person_refused(client, db_session, world):
    agent = world["agent"]
    db_session.add(LinkedIdentity(main_user_id=agent.id, email="dave@home.example", method="admin", status="active"))
    db_session.commit()
    admin_client(client, db_session)
    assert anonymise(client, "dave@home.example").status_code == 409


def test_open_tickets_are_refused_unless_ticked_and_nothing_changes(client, db_session, world):
    admin_client(client, db_session)
    before = snapshot(db_session)
    refused = anonymise(client, close=False)
    assert refused.status_code == 409 and "close open tickets first" in refused.text
    assert snapshot(db_session) == before
    page = preview(client, close=False)
    assert "are not closed" in page.text and "Anonymise permanently" not in page.text  # no confirm button
    assert snapshot(db_session) == before


def test_a_ticket_that_cannot_be_closed_stops_everything(client, db_session, world):
    world["own"].status = TicketStatus.WITH_MSP
    db_session.add(world["own"])
    db_session.commit()
    admin_client(client, db_session)
    before = snapshot(db_session)
    response = anonymise(client)
    assert response.status_code == 409 and "with the MSP" in response.text
    assert snapshot(db_session) == before


def test_an_untriaged_open_ticket_stops_everything(client, db_session, world):
    world["own"].triaged_at = None
    db_session.add(world["own"])
    db_session.commit()
    admin_client(client, db_session)
    before = snapshot(db_session)
    assert anonymise(client).status_code == 409
    assert snapshot(db_session) == before


def test_a_pending_approval_vote_is_refused(client, db_session, world):
    approval = db_session.exec(select(TicketApproval)).one()
    approval.status = "pending"
    vote = db_session.exec(select(ApprovalVote)).one()
    vote.decision = "pending"
    db_session.add(approval)
    db_session.add(vote)
    db_session.commit()
    admin_client(client, db_session)
    response = anonymise(client)
    assert response.status_code == 409 and "approval vote" in response.text


def test_the_shared_mailbox_is_refused(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_shared_mailbox", "support@corp.example")
    admin_client(client, db_session)
    assert "shared mailbox" in anonymise(client, "support@corp.example").text


def test_the_address_must_be_typed_again(client, db_session, world):
    admin_client(client, db_session)
    before = snapshot(db_session)
    response = anonymise(client, confirm="ann@a.exampl")
    assert response.status_code == 400 and "does not match" in response.text
    assert anonymise(client, confirm="").status_code == 400
    assert snapshot(db_session) == before
    assert anonymise(client, confirm="  ANN@A.EXAMPLE ").status_code == 200  # case and spaces do not matter


def test_nothing_held_is_refused_and_not_recorded(client, db_session, world):
    admin_client(client, db_session)
    response = anonymise(client, "nobody@x.example")
    assert response.status_code == 409 and "Nothing is held" in response.text
    assert db_session.exec(select(func.count()).select_from(AnonymisedRequester)).one() == 0


def test_only_data_protection_may_use_it(client, db_session, world):
    login_as(client, "plain@example.com")
    assert client.post(URL, data={"address": "ann@a.example", "confirm": "ann@a.example"}).status_code == 403
    assert client.post(URL + "/preview", data={"address": "ann@a.example"}).status_code == 403
    uid = login_as(client, "dpo@example.com")
    set_role(db_session, uid, UserRole.AGENT)
    db_session.add(UserPermission(user_id=uid, permission=DATA_PROTECTION))
    db_session.commit()
    assert preview(client).status_code == 200


def test_the_preview_shows_what_will_change_and_changes_nothing(client, db_session, world):
    admin_client(client, db_session)
    before = snapshot(db_session)
    page = preview(client)
    assert page.status_code == 200
    for text in ("cannot be undone", "Follower and copy rows removed", "Files deleted", "Messages they wrote, removed",
                 "Earlier audit and activity entries", "Anonymise permanently", "open ticket(s) will be closed first", "ann@b.example"):
        assert text in page.text, text
    assert snapshot(db_session) == before
    assert db_session.get(User, world["ann"].id).is_active


# ---- storage ----------------------------------------------------------------------------------------------------


class FailingStorage(LocalStorage):
    def __init__(self, root):
        super().__init__(root)
        self.fail = True
        self.attempts = []

    def delete(self, key):
        self.attempts.append(key)
        if self.fail:
            raise ConnectionError("storage is down")
        super().delete(key)


def test_a_failed_storage_delete_is_queued_reported_and_retried(client, db_session, world, tmp_path):
    from app.main import app
    from app.storage import get_storage

    flaky = FailingStorage(tmp_path / "flaky")
    for attachment in db_session.exec(select(Attachment)).all():
        flaky.put(attachment.blob_key, b"data", "application/pdf")
    app.dependency_overrides[get_storage] = lambda: flaky
    try:
        admin_client(client, db_session)
        response = anonymise(client)
        assert response.status_code == 200
        assert "could not be deleted yet" in response.text and "blob-cleanup" in response.text
        db_session.expire_all()
        # the database no longer lists the files; the keys wait, with the reason
        waiting = db_session.exec(select(PendingBlobDelete)).all()
        assert waiting and all(w.attempts == 1 and "storage is down" in w.last_error for w in waiting)
        assert all(flaky.exists(w.blob_key) for w in waiting)
        assert client.get("/admin/data-protection").text.count("could not be deleted yet") == 1
        # storage comes back: the job finishes it
        flaky.fail = False
        run_blob_cleanup_with(db_session, flaky)
        db_session.expire_all()
        assert db_session.exec(select(PendingBlobDelete)).all() == []
        assert not any(flaky.exists(w.blob_key) for w in waiting)
    finally:
        app.dependency_overrides.pop(get_storage, None)


def run_blob_cleanup_with(db, storage):
    purge_pending_blobs(db, storage)
    db.commit()


def test_nothing_is_deleted_from_storage_when_the_database_part_fails(client, db_session, world, storage, monkeypatch):
    def boom(ctx):
        raise RuntimeError("database trouble")

    real = dict(STEPS)
    monkeypatch.setitem(STEPS, "user.email", boom)  # the last step: everything before it has run
    keys = {a.blob_key for a in db_session.exec(select(Attachment)).all()}
    admin_client(client, db_session)
    before = snapshot(db_session)
    with pytest.raises(RuntimeError):
        anonymise(client)
    assert snapshot(db_session) == before
    assert all(storage.exists(k) for k in keys)
    assert db_session.exec(select(PendingBlobDelete)).all() == []
    assert real  # keep the original dict alive for the monkeypatch teardown


def test_a_blob_used_again_before_the_delete_is_left_alone(db_session, world, storage):
    key = world["own_file"].blob_key
    db_session.add(PendingBlobDelete(blob_key=key))
    db_session.commit()
    assert purge_pending_blobs(db_session, storage) == (0, 0)  # an attachment row still uses it
    assert storage.exists(key)
    assert db_session.exec(select(PendingBlobDelete)).all() == []


def test_the_cleanup_job_is_registered():
    from app.scheduled_jobs import SCHEDULED_JOBS, registry_errors

    assert "blob-cleanup" in {j.name for j in SCHEDULED_JOBS} and registry_errors(SCHEDULED_JOBS) == []


def test_local_storage_delete_is_idempotent(tmp_path):
    store = LocalStorage(tmp_path)
    key = blob_key("ab" * 32)
    store.put(key, b"x", "text/plain")
    store.delete(key)
    store.delete(key)
    assert not store.exists(key)


def test_blob_delete_removes_every_version_too():
    from azure.core.exceptions import ResourceNotFoundError

    from app.storage import BlobStorage

    key = blob_key("cd" * 32)
    calls = []

    class Version:
        def __init__(self, name, version_id):
            self.name, self.version_id = name, version_id

    class FakeBlob:
        def delete_blob(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("version_id") == "gone":
                raise ResourceNotFoundError("already")

    class FakeContainer:
        def list_blobs(self, name_starts_with=None, include=None):
            assert name_starts_with == key and include == ["versions"]
            return [Version(key, "v1"), Version(key, "gone"), Version(key + "x", "other")]

    class FakeService:
        def get_blob_client(self, container, blob):
            return FakeBlob()

        def get_container_client(self, container):
            return FakeContainer()

    BlobStorage("acct", "attachments", service_client=FakeService()).delete(key)
    assert calls == [{"delete_snapshots": "include"}, {"version_id": "v1"}, {"version_id": "gone"}]


def test_mail_to_the_placeholder_address_is_never_sent():
    with pytest.raises(Unsendable):
        _guard_recipient(OutboundEmail(kind="reply", to_address="anonymised-3@invalid"))

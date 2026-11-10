"""The portal New request page and the approvals page (9F, #206): two steps, the kind cards with description and icon, the
approval notice, Needed by only where offered, files on a new request (the rules of every stored file plus the total-size
limit), the agent options only for agents, and one card per request waiting for approval."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.approval_groups import set_notice
from app.config import settings
from app.main import app
from app.models import ApprovalGroup, Attachment, AuditLog, Message, RequestType, Ticket, TicketStatus, User
from app.request_icons import ICONS, icon_path
from app.request_questions import add_question
from app.request_types import (
    DEFAULT_APPROVAL_NOTICE,
    RequestTypeError,
    approval_notice,
    create_request_type,
    portal_cards,
    portal_tiles,
    update_request_type,
)
from app.settings_store import INBOUND_MAX_ATTACHMENTS, REQUESTER_UPLOAD_TOTAL_MB, set_setting
from app.storage import LocalStorage, get_storage
from app.visibility import access_context, may_view_attachment
from tests.conftest import login_as, set_role
from tests.test_approvals import person, raised_and_triaged, request_type
from tests.test_raising import HX, desk, last_ticket
from tests.test_request_types import admin, make

PDF = b"%PDF-1.7\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 3000


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


def files(*items):
    return [("files", (name, data, "application/octet-stream")) for name, data in items]


def send(client, *items, **fields):
    data = {"subject": "Printer on fire", "description": "It is smoking", **fields}
    return client.post("/portal/new", data=data, files=files(*items) if items else None, headers=HX)


def question(db, request_type, label, kind="text", options="", required=False):
    added = add_question(db, request_type, label, kind, options, required)
    db.commit()
    return added


# ---- step 1: the cards ---------------------------------------------------------------------------------------


def test_the_problem_card_is_first_and_every_active_kind_follows_with_its_icon_and_text(client, db_session):
    first = make(db_session, "Mailbox access", "Read or send from a team mailbox")
    update_request_type(db_session, first, "Mailbox access", "Read or send from a team mailbox", first.approval_group_id, 51, True, icon="mail", set_icon=True)
    make(db_session, "Plain thing")
    gone = make(db_session, "Gone")
    gone.is_active = False
    db_session.add(gone)
    db_session.commit()
    login_as(client, "jo@example.com")
    page = client.get("/portal/new").text
    titles = [c.title for c in portal_cards(db_session)]
    assert titles == ["Something is not working", "Mailbox access", "Plain thing"]
    assert page.index("Something is not working") < page.index('<span class="kind-title">Mailbox access</span>') < page.index('<span class="kind-title">Plain thing</span>')
    assert '<span class="kind-text">Read or send from a team mailbox</span>' in page and "Gone" not in page
    assert f'<path d="{ICONS["mail"][1]}">' in page and f'<path d="{ICONS["alert"][1]}">' in page and f'<path d="{ICONS["chat"][1]}">' in page
    # the cards are plain links (a GET), the current one is marked, and the problem report is chosen by default
    assert f'href="/portal/new?request_type_id={first.id}"' in page and 'href="/portal/new"' in page
    assert page.count('aria-current="true"') == 1 and "2. Something is not working" in page


def test_a_card_marks_the_chosen_kind_and_the_form_posts_it(client, db_session):
    first = make(db_session, "Mailbox access")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/new?request_type_id={first.id}").text
    assert "2. Mailbox access" in page and f'name="request_type_id" value="{first.id}"' in page
    assert 'enctype="multipart/form-data"' in page and 'hx-encoding="multipart/form-data"' in page
    assert client.get("/portal/new?request_type_id=99999").status_code == 200 and "2. Something is not working" in client.get("/portal/new?request_type_id=99999").text


# ---- the icon and description on a request type ----------------------------------------------------------------


def test_an_icon_is_one_of_the_fixed_set(db_session):
    request_type = make(db_session)
    with pytest.raises(RequestTypeError, match="icon"):
        update_request_type(db_session, request_type, "New starter", "", request_type.approval_group_id, 51, True, icon="<script>", set_icon=True)
    with pytest.raises(RequestTypeError, match="icon"):
        create_request_type(db_session, "Other", "", request_type.approval_group_id, 51, icon="nope")
    update_request_type(db_session, request_type, "New starter", "", request_type.approval_group_id, 51, True, icon="key", set_icon=True)
    assert request_type.icon == "key"
    update_request_type(db_session, request_type, "New starter", "", request_type.approval_group_id, 51, True)  # not told about icons: kept
    assert request_type.icon == "key"
    update_request_type(db_session, request_type, "New starter", "", request_type.approval_group_id, 51, True, icon="", set_icon=True)
    assert request_type.icon is None and icon_path(None) == icon_path("nonsense") == ICONS["chat"][1]


def test_an_admin_chooses_the_icon_in_the_request_type_form(client, db_session):
    request_type = make(db_session, "Laptop", "A new laptop")
    admin(client, db_session)
    form = {"name": "Laptop", "description": "A new laptop", "approval_group_id": request_type.approval_group_id, "pass_percent": 51, "is_active": "on"}
    assert 'name="icon"' in client.get("/admin/request-types").text and 'value="printer"' in client.get("/admin/request-types").text
    assert client.post(f"/admin/request-types/{request_type.id}", data={**form, "icon": "laptop"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).icon == "laptop"
    assert client.post(f"/admin/request-types/{request_type.id}", data={**form, "icon": "bogus"}).status_code == 400
    assert client.post(f"/admin/request-types/{request_type.id}", data={**form, "icon": ""}).status_code == 200
    db_session.expire_all()
    assert db_session.get(RequestType, request_type.id).icon is None
    assert client.post("/admin/request-types", data={**form, "name": "Phone", "icon": "phone"}).status_code == 200
    db_session.expire_all()
    assert db_session.exec(select(RequestType).where(RequestType.name == "Phone")).one().icon == "phone"
    changes = [a.detail or "" for a in db_session.exec(select(AuditLog).where(AuditLog.action == "edited")).all()]
    assert any("icon (default) -> laptop" in c for c in changes)


def test_the_home_tiles_are_the_problem_report_then_three_kinds_with_their_icons(client, db_session):
    for name in ("Alpha", "Bravo", "Charlie", "Delta"):
        made = make(db_session, name, f"About {name}")
    made.icon = "printer"
    db_session.add(made)
    db_session.commit()
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert [t.title for t in portal_tiles(db_session)] == ["Something is not working", "Alpha", "Bravo", "Charlie"]
    assert page.count('class="tile"') == 4 and '<span class="tile-title">Delta</span>' not in page
    assert f'<path d="{ICONS["alert"][1]}">' in page and 'href="/portal/new"' in page
    make_featured = db_session.exec(select(RequestType).where(RequestType.name == "Delta")).one()
    make_featured.is_featured = True
    db_session.add(make_featured)
    db_session.commit()
    assert f'<path d="{ICONS["printer"][1]}">' in client.get("/portal").text  # the featured kind's own icon, not the shared one


# ---- step 2: the form --------------------------------------------------------------------------------------------


def test_every_question_kind_renders_and_a_required_one_is_marked(client, db_session):
    kind = make(db_session, "New starter")
    question(db_session, kind, "Their name", "text", required=True)
    question(db_session, kind, "Start date", "date")
    question(db_session, kind, "Laptop", "yes_no")
    question(db_session, kind, "Role", "choice", "Dev\nOps", True)
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/new?request_type_id={kind.id}").text
    assert "Their name (required)" in page and "Role (required)" in page and "Start date (required)" not in page
    assert page.count('type="date"') == 1 and "Dev" in page and "Ops" in page and 'value="yes"' in page
    assert page.count('name="q_') == 4
    # and they validate as they did: a missing required answer is refused, nothing is raised
    refused = send(client, request_type_id=kind.id)
    assert refused.status_code == 400 and last_ticket(db_session) is None


def test_the_approval_notice_only_for_kinds_that_need_approval(client, db_session):
    approved = make(db_session, "Mailbox access")
    free = create_request_type(db_session, "Plain thing", "", None, 51, is_active=False)
    free.is_active = True  # the form refuses an active kind with no group, but one whose group lost its approval setup can exist
    free.approval_group_id = None
    db_session.add(free)
    db_session.commit()
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/new?request_type_id={approved.id}").text
    assert f'<p class="nr-notice" role="note">' in page and DEFAULT_APPROVAL_NOTICE in page
    assert "nr-notice" not in client.get(f"/portal/new?request_type_id={free.id}").text
    assert "nr-notice" not in client.get("/portal/new").text
    assert approval_notice(db_session, free) is None


def test_the_approval_notice_uses_the_groups_own_wording(client, db_session):
    approved = make(db_session, "Mailbox access")
    group = db_session.get(ApprovalGroup, approved.approval_group_id)
    admin(client, db_session)
    assert client.post(f"/admin/approval-groups/{group.id}/notice", data={"request_notice": "The mailbox owners must agree first."}).status_code == 200
    page = client.get(f"/portal/new?request_type_id={approved.id}").text
    assert "The mailbox owners must agree first." in page and DEFAULT_APPROVAL_NOTICE not in page
    assert client.post(f"/admin/approval-groups/{group.id}/notice", data={"request_notice": "x" * 300}).status_code == 400
    assert client.post(f"/admin/approval-groups/{group.id}/notice", data={"request_notice": "  "}).status_code == 200
    db_session.expire_all()
    assert db_session.get(ApprovalGroup, group.id).request_notice is None and DEFAULT_APPROVAL_NOTICE in client.get(f"/portal/new?request_type_id={approved.id}").text
    assert client.post("/admin/approval-groups/9999/notice", data={"request_notice": "x"}).status_code == 404
    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert client.post(f"/admin/approval-groups/{group.id}/notice", data={"request_notice": "x"}).status_code in (302, 303, 403)
    with pytest.raises(Exception):
        set_notice(db_session, group, "y" * 201)


def test_needed_by_only_for_kinds_that_offer_it(client, db_session):
    offers = make(db_session, "Equipment")
    offers.offers_required_by = True
    db_session.add(offers)
    plain = make(db_session, "Mailbox")
    db_session.commit()
    login_as(client, "jo@example.com")
    assert 'name="required_by"' in client.get(f"/portal/new?request_type_id={offers.id}").text
    assert 'name="required_by"' not in client.get(f"/portal/new?request_type_id={plain.id}").text
    assert 'name="required_by"' not in client.get("/portal/new").text


def test_agent_options_only_for_agents(client, db_session):
    login_as(client, "jo@example.com")
    page = client.get("/portal/new").text
    assert "Agent options" not in page and 'name="for_email"' not in page and 'name="reported_at"' not in page
    client.post("/auth/logout")
    desk(client, db_session)
    page = client.get("/portal/new").text
    assert '<details class="nr-agent">' in page and "Agent options" in page
    for name in ("for_email", "ticket_type", "category_id", "impact", "urgency", "agent_id", "reported_at", "resolved_at", "resolution_notes"):
        assert f'name="{name}"' in page, name


def test_an_ordinary_person_cannot_use_the_agent_fields_by_posting_them(client, db_session):
    login_as(client, "jo@example.com")
    assert send(client, for_email="x@example.com").status_code == 400
    assert last_ticket(db_session) is None


def test_an_agent_raising_for_someone_with_backdating_still_works(client, db_session):
    team, category, _ = desk(client, db_session)
    response = send(
        client, for_email="caller@example.com", category_id=category.id, impact="low", urgency="low",
        resolved_at="2026-11-09T09:00", resolution_notes="Fixed at the desk",
    )
    assert response.status_code == 204, response.text
    ticket = last_ticket(db_session)
    assert ticket.status == TicketStatus.RESOLVED and ticket.is_backdated and ticket.requester_email == "caller@example.com"


# ---- files on a new request ----------------------------------------------------------------------------------------


def test_a_request_with_files_stores_them_on_the_first_portal_message_and_both_sides_can_open_them(client, db_session, storage):
    login_as(client, "jo@example.com", "Jo Bloggs")
    response = send(client, ("shot.png", PNG), ("log.pdf", PDF))
    assert response.status_code == 204
    ticket = last_ticket(db_session)
    message = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).one()
    assert message.channel == "portal" and message.body == "It is smoking"
    stored = db_session.exec(select(Attachment).order_by(Attachment.id)).all()
    assert [a.filename for a in stored] == ["shot.png", "log.pdf"] and {a.message_id for a in stored} == {message.id}
    page = client.get(f"/portal/{ticket.number}").text
    assert all(f'href="/attachments/{a.id}"' in page for a in stored)
    requester = db_session.exec(select(User).where(User.email == "jo@example.com")).one()
    assert may_view_attachment(db_session, requester, stored[0], ticket, access_context(db_session, requester))
    assert client.get(f"/attachments/{stored[0].id}").status_code in (200, 302, 307)


def test_a_request_without_files_is_unchanged(client, db_session, storage):
    login_as(client, "jo@example.com")
    assert send(client).status_code == 204
    assert db_session.exec(select(Attachment)).all() == [] and last_ticket(db_session) is not None


def refused(client, db_session, *items, **fields):
    response = send(client, *items, **fields)
    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.exec(select(Ticket)).all() == [] and db_session.exec(select(Attachment)).all() == []
    return response.text


def test_a_blocked_empty_or_oversized_file_refuses_the_whole_request_and_keeps_nothing(client, db_session, storage, monkeypatch):
    login_as(client, "jo@example.com")
    assert "cannot be attached" in refused(client, db_session, ("setup.exe", PDF))
    assert "is empty" in refused(client, db_session, ("empty.pdf", b""))
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024)
    assert "larger than the" in refused(client, db_session, ("big.pdf", PDF + b"y" * 2000))
    assert list(storage.root.rglob("*/*")) == []


def test_more_files_than_the_count_setting_are_refused(client, db_session, storage):
    set_setting(db_session, INBOUND_MAX_ATTACHMENTS, "1")
    db_session.commit()
    login_as(client, "jo@example.com")
    assert "at most 1 files" in refused(client, db_session, ("a.pdf", PDF), ("b.pdf", PDF + b"1"))


def test_the_total_size_limit_names_itself_for_a_new_request_and_for_a_reply(client, db_session, storage):
    set_setting(db_session, REQUESTER_UPLOAD_TOTAL_MB, "1")
    db_session.commit()
    login_as(client, "jo@example.com")
    big = lambda n: b"%PDF-1.7\n" + bytes([n]) * (700 * 1024)  # noqa: E731 - two of these are 1.4 MB, each is fine alone
    message = refused(client, db_session, ("a.pdf", big(1)), ("b.pdf", big(2)))
    assert "1 MB" in message and "1.4 MB" in message
    assert send(client, ("a.pdf", big(1))).status_code == 204  # one alone is fine
    ticket = last_ticket(db_session)
    ticket.status = TicketStatus.AWAITING_USER
    ticket.triaged_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    reply = client.post(f"/portal/{ticket.number}/comment", data={"body": "more"}, files=files(("c.pdf", big(3)), ("d.pdf", big(4))))
    assert reply.status_code == 400 and "1 MB" in reply.text
    db_session.expire_all()
    assert db_session.exec(select(Message).where(Message.body == "more")).all() == [] and len(db_session.exec(select(Attachment)).all()) == 1


def test_the_total_is_a_setting_with_a_default_and_agents_are_not_held_to_it(client, db_session, storage):
    from app.settings_store import REGISTRY

    assert REGISTRY[REQUESTER_UPLOAD_TOTAL_MB].default == "25"
    set_setting(db_session, REQUESTER_UPLOAD_TOTAL_MB, "1")
    db_session.commit()
    desk(client, db_session)
    big = lambda n: b"%PDF-1.7\n" + bytes([n]) * (700 * 1024)  # noqa: E731
    assert send(client, ("a.pdf", big(1)), ("b.pdf", big(2))).status_code in (204, 303)


def test_the_page_says_the_limits(client, db_session):
    login_as(client, "jo@example.com")
    assert "Up to 20 files, 10 MB each and 25 MB in all." in client.get("/portal/new").text


def test_an_agent_raising_for_someone_can_attach_files_to_the_first_message(client, db_session, storage):
    team, category, _ = desk(client, db_session)
    assert send(client, ("shot.png", PNG), for_email="caller@example.com").status_code in (204, 303)
    ticket = last_ticket(db_session)
    assert ticket.requester_email == "caller@example.com" and db_session.exec(select(Attachment)).one().filename == "shot.png"


# ---- My requests: a follower is not the one being waited for ----------------------------------------------------


def test_a_follower_reads_waiting_for_a_reply_on_my_requests(client, db_session):
    from app.followers import add_follower

    ticket = Ticket(subject="Needs info", requester_email="jo@example.com", status=TicketStatus.AWAITING_USER, triaged_at=datetime.now(timezone.utc))
    db_session.add(ticket)
    db_session.commit()
    add_follower(db_session, ticket, "pal@example.com", "jo@example.com")
    db_session.commit()
    login_as(client, "jo@example.com")
    assert '<span class="chip t-amber">Waiting for your reply</span>' in client.get("/portal").text
    client.post("/auth/logout")
    login_as(client, "pal@example.com", "Pal Jones")
    mine = client.get("/portal").text
    assert '<span class="chip t-amber">Waiting for a reply</span>' in mine and "Waiting for your reply" not in mine
    assert '<span class="chip t-amber">Waiting for a reply</span>' in client.get(f"/portal/{ticket.number}").text


# ---- the approvals page ----------------------------------------------------------------------------------------------


def test_a_waiting_approval_is_a_card_with_the_request_who_asked_the_answers_and_the_buttons(client, db_session):
    kind = request_type(db_session, ["boss@example.com"], name="Mailbox access")
    which = question(db_session, kind, "Which mailbox?", "text")
    person(client, db_session, "boss@example.com", "Boss Person")
    client.post("/auth/logout")
    login_as(client, "jo@example.com", "Jo Bloggs")
    assert send(client, request_type_id=kind.id, subject="Finance mailbox", **{f"q_{which.id}": "Finance"}).status_code == 204
    ticket = last_ticket(db_session)
    client.post("/auth/logout")
    team, category, _ = desk(client, db_session)
    client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id})
    client.post("/auth/logout")
    login_as(client, "boss@example.com")
    page = client.get("/portal/approvals").text
    assert '<article class="card ap-card"' in page and ticket.reference in page and "Finance mailbox" in page
    assert "From Jo Bloggs (jo@example.com)" in page and "Mailbox access" in page and "must approve" in page
    assert "Which mailbox?" in page and "Finance" in page
    assert f'hx-post="/portal/{ticket.number}/approval"' in page
    assert 'name="decision" value="approved"' in page and 'name="decision" value="declined"' in page and 'name="comment"' in page
    # the same route and rules as before: declining with a comment settles it
    decided = client.post(f"/portal/{ticket.number}/approval", data={"decision": "declined", "comment": "Not this time"})
    assert decided.status_code == 200
    assert "Nothing is waiting for your approval" in client.get("/portal/approvals").text


def test_with_nothing_waiting_the_page_says_so(client, db_session):
    login_as(client, "jo@example.com")
    page = client.get("/portal/approvals").text
    assert "Nothing is waiting for your approval" in page and "ap-card" not in page

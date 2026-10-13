from sqlmodel import select

from app.canned import MAX_TITLE_LENGTH, CannedError, create_canned, fill, update_canned
from app.forms import MAX_MESSAGE_LENGTH
from app.models import AuditLog, CannedResponse, Message, Ticket, UserRole
from tests.conftest import create_team, login_as, set_role
from tests.test_search import triaged
from tests.test_visibility import agent_in


def admin(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def make(db_session, title="Password reset", body="Hi, your ticket {reference} ({subject}). {agent}", active=True):
    canned = create_canned(db_session, title, body)
    canned.is_active = active
    db_session.add(canned)
    db_session.commit()
    db_session.refresh(canned)
    return canned


def actions(db_session, kind="canned_response"):
    return [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == kind)).all()]


# ---- Admin ---------------------------------------------------------------------------------------


def test_an_admin_creates_edits_and_switches_one_off_with_audit(client, db_session):
    admin(client, db_session)
    assert client.post("/admin/canned", data={"title": " Reset ", "body": " Try this "}, follow_redirects=False).status_code == 303
    [canned] = db_session.exec(select(CannedResponse)).all()
    assert (canned.title, canned.body, canned.is_active) == ("Reset", "Try this", True)

    client.post(f"/admin/canned/{canned.id}", data={"title": "Reset 2", "body": "New text", "is_active": "on"})
    client.post(f"/admin/canned/{canned.id}", data={"title": "Reset 2", "body": "New text"})  # box unticked

    db_session.expire_all()
    canned = db_session.get(CannedResponse, canned.id)
    assert (canned.title, canned.body, canned.is_active) == ("Reset 2", "New text", False)
    assert actions(db_session) == ["created", "edited", "edited", "deactivated"]
    assert "Reset 2" in client.get("/admin/canned").text  # a switched-off one stays listed


def test_bad_input_is_refused(client, db_session):
    admin(client, db_session)
    make(db_session, "Existing")
    for data in (
        {"title": "  ", "body": "x"},
        {"title": "x", "body": "   "},
        {"title": "EXISTING", "body": "x"},  # titles are unique whatever the case
        {"title": "t" * (MAX_TITLE_LENGTH + 1), "body": "x"},
        {"title": "ok", "body": "b" * (MAX_MESSAGE_LENGTH + 1)},
    ):
        assert client.post("/admin/canned", data=data).status_code == 400, data
    assert len(db_session.exec(select(CannedResponse)).all()) == 1
    assert client.post("/admin/canned/9999", data={"title": "a", "body": "b"}).status_code == 404


def test_editing_cannot_take_another_ones_title_but_can_keep_its_own(client, db_session):
    admin(client, db_session)
    one, two = make(db_session, "One"), make(db_session, "Two")
    assert client.post(f"/admin/canned/{two.id}", data={"title": "one", "body": "x", "is_active": "on"}).status_code == 400
    assert client.post(f"/admin/canned/{one.id}", data={"title": "ONE", "body": "y", "is_active": "on"}).status_code in (200, 303)


def test_only_super_admins_manage_them(client, db_session):
    agent_in(client, db_session)
    canned = make(db_session)
    assert client.get("/admin/canned").status_code == 403
    assert client.post("/admin/canned", data={"title": "a", "body": "b"}).status_code == 403
    assert client.post(f"/admin/canned/{canned.id}", data={"title": "a", "body": "b"}).status_code == 403


# ---- Agents --------------------------------------------------------------------------------------


def test_the_picker_lists_only_active_responses_on_a_triaged_ticket(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    make(db_session, "Visible one")
    make(db_session, "Switched off", active=False)
    ticket = triaged(db_session, team, "Printer")
    untriaged = Ticket(subject="New one", requester_email="r@example.com", team_id=team.id)
    db_session.add(untriaged)
    db_session.commit()
    db_session.refresh(untriaged)

    page = client.get(f"/tickets/{ticket.number}").text
    assert "Visible one" in page and "Switched off" not in page
    assert "Visible one" not in client.get(f"/tickets/{untriaged.number}").text  # cannot email yet


def test_picking_fills_the_box_with_placeholders_and_appends_to_what_was_typed(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    canned = make(db_session)
    ticket = triaged(db_session, team, "Printer jam")

    fragment = client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": canned.id}).text
    assert f"Hi, your ticket {ticket.reference} (Printer jam). Test User" in fragment
    assert 'name="body"' in fragment and 'id="email-body"' in fragment

    again = client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": canned.id, "body": "Typed first"}).text
    assert again.index("Typed first") < again.index("Hi, your ticket")

    blank = client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": "", "body": "Keep me"}).text
    assert "Keep me" in blank and "Hi, your ticket" not in blank


def test_unknown_braces_stay_and_html_is_escaped(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    canned = make(db_session, "Odd", "Dear {name}, <script>alert(1)</script> {reference}")
    ticket = triaged(db_session, team, "x")

    fragment = client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": canned.id}).text

    assert "{name}" in fragment and "<script>" not in fragment and "&lt;script&gt;" in fragment


def test_a_switched_off_or_missing_response_cannot_be_inserted(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    off = make(db_session, "Off", active=False)
    ticket = triaged(db_session, team, "x")

    assert client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": off.id}).status_code == 404
    assert client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": 9999}).status_code == 404


def test_the_fill_endpoint_follows_ticket_access(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[mine])
    canned = make(db_session)
    hidden = triaged(db_session, other, "Hidden")

    assert client.get(f"/tickets/{hidden.number}/canned", params={"canned_id": canned.id}).status_code == 403


def test_a_filled_response_is_sent_through_the_normal_email_action(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    canned = make(db_session)
    ticket = triaged(db_session, team, "Printer jam")
    text = client.get(f"/tickets/{ticket.number}/canned", params={"canned_id": canned.id}).text
    body = text.split(">", 1)[1].rsplit("</textarea", 1)[0].replace("&#34;", '"')

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": body}).status_code == 200

    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert message.body.startswith("Hi, your ticket")


def test_fill_only_replaces_known_placeholders(db_session):
    ticket = Ticket(subject="S", requester_email="r@example.com")
    db_session.add(ticket)
    db_session.commit()
    canned = CannedResponse(title="t", body="{reference}/{subject}/{agent}/{other}")
    agent = type("A", (), {"display_name": "Ann"})()

    assert fill(canned, ticket, agent) == f"{ticket.reference}/S/Ann/{{other}}"


def test_update_refuses_an_empty_title(db_session):
    canned = make(db_session)
    try:
        update_canned(db_session, canned, " ", "x", True)
    except CannedError:
        return
    raise AssertionError("expected CannedError")

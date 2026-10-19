"""Escalation tidy-up: the escalation target is set when a category is created, the escalation
reason is also an internal note, and escalate and re-assign share one move."""

import pytest
from sqlmodel import select

from app.messages import requester_visible_messages
from app.models import (
    AuditLog,
    Category,
    Message,
    MessageDirection,
    Notification,
    TicketEvent,
    User,
)
from app.triage import MOVE_ESCALATE, MOVE_REASSIGN, move_ticket
from tests.conftest import create_team, login_as
from tests.test_escalation import categorised, escalate
from tests.test_visibility import agent_in, make_ticket


def admin(client):
    login_as(client, "admin@example.com")


def category_named(db_session, name):
    db_session.expire_all()
    return db_session.exec(select(Category).where(Category.name == name)).one()


def events_of(db_session, ticket, kind):
    db_session.expire_all()
    return db_session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == kind)
    ).all()


# ---- 1. The escalation target is chosen when a category is created ----------------------------------


def test_a_main_category_is_created_with_its_escalation_team(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    admin(client)

    response = client.post("/admin/categories", data={"name": "Software", "team_id": desk.id, "escalation": dev.id})

    assert response.status_code == 200
    software = category_named(db_session, "Software")
    assert (software.team_id, software.escalation_team_id) == (desk.id, dev.id)
    actions = [(a.action, a.detail) for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "category")).all()]
    assert ("created", "Software") in actions and ("escalation_changed", "Software: Dev") in actions


def test_leaving_the_escalation_out_or_choosing_the_default_logs_nothing_extra(client, db_session):
    desk = create_team(db_session, "Support")
    admin(client)

    client.post("/admin/categories", data={"name": "Hardware", "team_id": desk.id})  # no field at all
    client.post("/admin/categories", data={"name": "Network", "team_id": desk.id, "escalation": "msp"})  # the form's default
    hardware = category_named(db_session, "Hardware")
    client.post("/admin/categories", data={"name": "Laptops", "parent_id": hardware.id, "escalation": "inherit"})  # a sub's default

    assert category_named(db_session, "Hardware").escalation_team_id is None  # the MSP, as before
    actions = [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "category")).all()]
    assert actions == ["created", "created", "created"]  # a default is not a change


def test_a_sub_category_can_inherit_override_or_go_to_the_msp(client, db_session):
    desk, dev, net = (create_team(db_session, n) for n in ("Support", "Dev", "Network"))
    admin(client)
    client.post("/admin/categories", data={"name": "Software", "team_id": desk.id, "escalation": dev.id})
    parent = category_named(db_session, "Software")

    for name, choice in (("Office", "inherit"), ("Custom", net.id), ("Plain", "msp")):
        client.post("/admin/categories", data={"name": name, "parent_id": parent.id, "escalation": choice})

    office, custom, plain = (category_named(db_session, n) for n in ("Office", "Custom", "Plain"))
    assert (office.escalation_inherits, office.escalation_team_id) == (True, None)
    assert (custom.escalation_inherits, custom.escalation_team_id) == (False, net.id)
    assert (plain.escalation_inherits, plain.escalation_team_id) == (False, None)
    from app.categories import escalation_team_id_for

    assert [escalation_team_id_for(db_session, c) for c in (office, custom, plain)] == [dev.id, net.id, None]


def test_a_bad_escalation_choice_refuses_the_whole_category(client, db_session):
    desk = create_team(db_session, "Support")
    admin(client)
    # a main category cannot inherit; the team must exist; a number too big for the database is just "no such team"
    for choice in ("inherit", "999999", "abc", "-1", "99999999999999999999", "٣"):
        response = client.post("/admin/categories", data={"name": "Nope", "team_id": desk.id, "escalation": choice})
        assert response.status_code == 400, choice
    assert db_session.exec(select(Category).where(Category.name == "Nope")).all() == []


def test_the_creation_forms_ask_for_it(client, db_session):
    create_team(db_session, "Support")
    admin(client)
    page = client.get("/admin/categories").text
    new_forms = page.split("New main category")[1]
    assert new_forms.count('name="escalation"') == 2  # one in each creation form
    assert 'value="inherit"' in new_forms.split("New sub-category")[1]


def test_a_ticket_in_a_new_category_escalates_where_it_was_created_to_go(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    admin(client)
    client.post("/admin/categories", data={"name": "Software", "team_id": desk.id, "escalation": dev.id})
    client.post("/auth/logout")
    me = agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    ticket.category_id = category_named(db_session, "Software").id
    db_session.add(ticket)
    db_session.commit()

    assert escalate(client, ticket).status_code == 200

    db_session.refresh(ticket)
    assert ticket.team_id == dev.id


# ---- 2. The escalation reason is also an internal note ----------------------------------------------


def test_escalating_leaves_the_reason_as_an_internal_note(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)

    escalate(client, ticket, "In-house app bug")

    [note] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert note.direction == MessageDirection.INTERNAL
    assert note.body == "Escalated to Dev. Reason: In-house app bug"
    assert note.sender == "Test User" and note.author_user_id is not None
    assert [m for m in requester_visible_messages(db_session, ticket)] == []  # never reaches the requester


def test_the_receiving_team_reads_the_note_and_the_event_still_carries_the_reason(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)
    escalate(client, ticket, "In-house app bug")
    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[dev], email="dev@example.com")

    page = client.get(f"/tickets/{ticket.number}").text

    assert "Escalated to Dev. Reason: In-house app bug" in page
    [event] = events_of(db_session, ticket, "escalated")
    assert (event.from_value, event.to_value, event.detail) == ("Support", "Dev", "In-house app bug")


def test_an_at_mention_in_the_reason_notifies_someone_in_the_receiving_team_only(client, db_session):
    desk, dev, other = (create_team(db_session, n) for n in ("Support", "Dev", "Network"))
    agent_in(client, db_session, member_of=[dev], email="dana@example.com")
    agent_in(client, db_session, member_of=[other], email="nora@example.com")
    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)

    escalate(client, ticket, "Over to you @dana, and @nora for info")

    db_session.expire_all()
    told = {u.email: len(db_session.exec(select(Notification).where(Notification.user_id == u.id)).all())
            for u in db_session.exec(select(User)).all()}
    assert told["dana@example.com"] == 1  # in the team it moved to
    assert told["nora@example.com"] == 0  # cannot open the ticket


def test_a_refused_escalation_leaves_no_note(client, db_session):
    desk = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, None)  # the MSP: cannot escalate yet

    assert escalate(client, ticket).status_code == 400

    assert db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all() == []


# ---- 3. Escalate and re-assign share one move -------------------------------------------------------


def test_an_escalation_is_recorded_once_and_a_reassign_is_recorded_once(client, db_session):
    desk, dev, net = (create_team(db_session, n) for n in ("Support", "Dev", "Network"))
    me = agent_in(client, db_session, member_of=[desk, net])
    ticket = make_ticket(db_session, desk.id, assigned_agent_id=me)
    categorised(db_session, ticket, dev)

    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": net.id})
    assert [(e.from_value, e.to_value) for e in events_of(db_session, ticket, "team_changed")] == [("Support", "Network")]
    assert events_of(db_session, ticket, "escalated") == []

    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": desk.id})
    escalate(client, ticket, "Too hard for us")

    assert len(events_of(db_session, ticket, "escalated")) == 1  # the one event carries the move
    assert len(events_of(db_session, ticket, "team_changed")) == 2  # only the two re-assigns


def test_escalating_clears_the_assignee_and_says_so_in_the_history(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    me = agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id, assigned_agent_id=me)
    categorised(db_session, ticket, dev)

    escalate(client, ticket)

    db_session.refresh(ticket)
    assert ticket.assigned_agent_id is None
    [unassigned] = events_of(db_session, ticket, "assigned")
    assert (unassigned.from_value, unassigned.to_value) == ("agent@example.com", None)


def test_the_move_itself_reports_whether_anything_changed(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    me_id = agent_in(client, db_session, member_of=[desk])
    me = db_session.get(User, me_id)
    ticket = make_ticket(db_session, desk.id)

    assert move_ticket(db_session, ticket, me, desk, None, kind=MOVE_REASSIGN) is False  # nowhere to go
    assert events_of(db_session, ticket, "team_changed") == []
    assert move_ticket(db_session, ticket, me, dev, None, kind=MOVE_REASSIGN) is True
    assert move_ticket(db_session, ticket, me, dev, None, kind=MOVE_REASSIGN) is False
    with pytest.raises(ValueError):
        move_ticket(db_session, ticket, me, desk, None, kind="teleport")


def test_an_escalation_must_change_the_team(client, db_session):
    desk = create_team(db_session, "Support")
    me = db_session.get(User, agent_in(client, db_session, member_of=[desk]))
    ticket = make_ticket(db_session, desk.id)

    with pytest.raises(ValueError):
        move_ticket(db_session, ticket, me, desk, None, kind=MOVE_ESCALATE, reason="nowhere to go")
    assert events_of(db_session, ticket, "escalated") == []


def test_only_what_the_agent_wrote_can_mention_someone(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev @dana")
    agent_in(client, db_session, member_of=[dev], email="dana@example.com")
    client.post("/auth/logout")
    agent_in(client, db_session, member_of=[desk])
    ticket = make_ticket(db_session, desk.id)
    categorised(db_session, ticket, dev)

    escalate(client, ticket, "Needs the dev team")  # the team is named "Dev @dana": the system's text, not a mention

    db_session.expire_all()
    dana = db_session.exec(select(User).where(User.email == "dana@example.com")).one()
    assert db_session.exec(select(Notification).where(Notification.user_id == dana.id)).all() == []


def test_the_reason_only_goes_on_an_escalation_event(client, db_session):
    desk, dev = create_team(db_session, "Support"), create_team(db_session, "Dev")
    me = db_session.get(User, agent_in(client, db_session, member_of=[desk]))
    ticket = make_ticket(db_session, desk.id)

    move_ticket(db_session, ticket, me, dev, None, kind=MOVE_REASSIGN, reason="ignored for a re-assign")

    [event] = events_of(db_session, ticket, "team_changed")
    assert event.detail is None
    move_ticket(db_session, ticket, me, desk, None, kind=MOVE_ESCALATE, reason="why")
    assert [e.detail for e in events_of(db_session, ticket, "escalated")] == ["why"]

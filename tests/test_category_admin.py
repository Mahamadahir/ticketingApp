from sqlmodel import select

from app.models import (
    AuditLog,
    Category,
    Setting,
    Ticket,
    TicketEvent,
    TicketSource,
    TicketStatus,
    UserRole,
)
from app.settings_store import DEFAULT_TRIAGE_TEAM
from app.ticket_service import create_ticket
from tests.conftest import add_membership, create_category, create_team, login_as, set_role


def admin(client):
    login_as(client, "admin@example.com")


def other_ticket(client, db_session, desk, status=TicketStatus.RESOLVED):
    """A ticket triaged into Other, then taken to the given status."""
    other = Category(name="Other", is_other=True, team_id=desk.id)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    ticket = create_ticket(
        db_session, subject="Odd request", requester_email="me@example.com", source=TicketSource.EMAIL, team_id=desk.id
    )
    db_session.commit()
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, desk.id)
    client.post(f"/tickets/{ticket.number}/triage", data={"ticket_type": "incident", "impact": "low", "urgency": "low", "category_id": other.id})
    if status == TicketStatus.RESOLVED:
        client.post(f"/tickets/{ticket.number}/resolve", data={"resolution_notes": "Fixed it"})
    else:
        client.post(f"/tickets/{ticket.number}/status", data={"status": status.value})
    client.post("/auth/logout")
    db_session.refresh(ticket)
    return ticket, other


# ---- Categories page -----------------------------------------------------------


def test_the_categories_page_seeds_other_and_lists_the_tree(client, db_session):
    desk = create_team(db_session, "Service Desk")
    hardware = create_category(db_session, desk, "Hardware")
    create_category(db_session, None, "Laptop", parent=hardware)
    admin(client)

    page = client.get("/admin/categories").text

    assert "Hardware" in page and "Laptop" in page and "Other" in page
    assert db_session.exec(select(Category).where(Category.is_other.is_(True))).one().name == "Other"


def test_a_super_admin_creates_main_and_sub_categories(client, db_session):
    desk = create_team(db_session, "Service Desk")
    admin(client)

    assert client.post("/admin/categories", data={"name": "Hardware", "team_id": desk.id}, follow_redirects=False).status_code == 303
    hardware = db_session.exec(select(Category).where(Category.name == "Hardware")).one()
    assert hardware.team_id == desk.id and hardware.parent_id is None
    assert client.post("/admin/categories", data={"name": "Laptop", "parent_id": hardware.id}, follow_redirects=False).status_code == 303
    laptop = db_session.exec(select(Category).where(Category.name == "Laptop")).one()
    assert laptop.parent_id == hardware.id and laptop.team_id is None
    assert [a.detail for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "category"))] == [
        "Hardware",
        "Hardware / Laptop",
    ]


def test_category_creation_refusals(client, db_session):
    desk = create_team(db_session, "Service Desk")
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, None, "Laptop", parent=hardware)
    admin(client)

    refused = (
        {"name": "hardware", "team_id": desk.id},  # duplicate
        {"name": "No team"},
        {"name": "Bad team", "team_id": 9999},
        {"name": "   "},
        {"name": "Other", "team_id": desk.id},
        {"name": "Deep", "parent_id": laptop.id},
        {"name": "Laptop", "parent_id": hardware.id},  # duplicate under the same parent
    )
    for data in refused:
        assert client.post("/admin/categories", data=data).status_code == 400, data
    assert len(db_session.exec(select(Category)).all()) == 2


def test_the_same_name_is_allowed_under_different_parents(client, db_session):
    desk = create_team(db_session, "Service Desk")
    hardware, software = create_category(db_session, desk, "Hardware"), create_category(db_session, desk, "Software")
    admin(client)

    for parent in (hardware, software):
        assert client.post("/admin/categories", data={"name": "Other devices", "parent_id": parent.id}, follow_redirects=False).status_code == 303


def test_only_super_admins_manage_categories(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, desk.id)

    assert client.get("/admin/categories").status_code == 403
    assert client.post("/admin/categories", data={"name": "X", "team_id": desk.id}).status_code == 403
    assert client.get("/admin/category-review").status_code == 403
    assert client.get("/admin/settings").status_code == 403


def test_changing_a_categorys_team_and_switching_it_off(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    hardware = create_category(db_session, desk, "Hardware")
    admin(client)

    client.post(f"/admin/categories/{hardware.id}", data={"team_id": network.id, "is_active": "on"}, follow_redirects=False)
    db_session.refresh(hardware)
    assert hardware.team_id == network.id and hardware.is_active

    client.post(f"/admin/categories/{hardware.id}", data={"team_id": network.id}, follow_redirects=False)  # box unticked
    db_session.refresh(hardware)
    assert hardware.is_active is False
    assert {a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "category"))} == {
        "team_changed",
        "deactivated",
    }
    assert client.post(f"/admin/categories/{hardware.id}", data={"team_id": ""}, follow_redirects=False).status_code == 400
    assert client.post("/admin/categories/9999", data={}).status_code == 404


def test_a_switched_off_category_disappears_from_triage_but_keeps_its_tickets(client, db_session):
    desk = create_team(db_session, "Service Desk")
    old = create_category(db_session, desk, "Old stuff")
    ticket = create_ticket(db_session, subject="x", requester_email="me@example.com", source=TicketSource.EMAIL, team_id=desk.id)
    db_session.commit()
    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, desk.id)
    assert "Old stuff" in client.get(f"/tickets/{ticket.number}").text
    client.post("/auth/logout")

    admin(client)
    client.post(f"/admin/categories/{old.id}", data={"team_id": desk.id}, follow_redirects=False)
    client.post("/auth/logout")

    login_as(client, "agent@example.com")
    assert "Old stuff" not in client.get(f"/tickets/{ticket.number}").text


# ---- Category review --------------------------------------------------------------


def test_an_other_ticket_appears_in_review_only_after_it_is_resolved(client, db_session):
    desk = create_team(db_session, "Service Desk")
    working, _ = other_ticket(client, db_session, desk, status=TicketStatus.IN_PROGRESS)
    admin(client)
    assert "Nothing waiting for review" in client.get("/admin/category-review").text

    working.status = TicketStatus.RESOLVED
    db_session.add(working)
    db_session.commit()
    page = client.get("/admin/category-review").text
    assert working.reference in page and "Odd request" in page


def test_review_assigns_an_existing_category_and_clears_the_flag_without_moving_the_team(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket, _ = other_ticket(client, db_session, desk)
    hardware = create_category(db_session, network, "Hardware")
    admin(client)

    response = client.post(f"/admin/category-review/{ticket.number}", data={"category_id": hardware.id}, follow_redirects=False)

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.category_id == hardware.id and ticket.needs_category_review is False
    assert ticket.team_id == desk.id  # already worked and resolved; only the category changes
    reviewed = db_session.exec(select(TicketEvent).where(TicketEvent.event_type == "category_reviewed")).all()
    assert [(e.from_value, e.to_value) for e in reviewed] == [("Other", "Hardware")]
    assert "Nothing waiting for review" in client.get("/admin/category-review").text


def test_review_can_create_a_new_category(client, db_session):
    desk, network = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    ticket, _ = other_ticket(client, db_session, desk)
    admin(client)

    client.post(
        f"/admin/category-review/{ticket.number}",
        data={"new_category_name": "Printing", "new_category_team_id": network.id},
        follow_redirects=False,
    )

    created = db_session.exec(select(Category).where(Category.name == "Printing")).one()
    db_session.refresh(ticket)
    assert ticket.category_id == created.id and created.team_id == network.id and not ticket.needs_category_review


def test_review_refusals(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket, other = other_ticket(client, db_session, desk)
    off = create_category(db_session, desk, "Retired")
    off.is_active = False
    db_session.add(off)
    db_session.commit()
    admin(client)

    assert client.post(f"/admin/category-review/{ticket.number}", data={}).status_code == 400
    assert client.post(f"/admin/category-review/{ticket.number}", data={"category_id": other.id}).status_code == 400
    assert client.post(f"/admin/category-review/{ticket.number}", data={"category_id": off.id}).status_code == 400
    assert client.post(f"/admin/category-review/{ticket.number}", data={"new_category_name": "No team"}).status_code == 400
    assert client.post("/admin/category-review/9999", data={"category_id": 1}).status_code == 404
    db_session.refresh(ticket)
    assert ticket.needs_category_review is True  # still waiting

    ordinary = create_ticket(db_session, subject="n", requester_email="a@example.com", source=TicketSource.EMAIL)
    db_session.commit()
    good = create_category(db_session, desk, "Real")
    assert client.post(f"/admin/category-review/{ordinary.number}", data={"category_id": good.id}).status_code == 404


# ---- Settings ---------------------------------------------------------------------


def test_the_default_triage_team_setting_is_saved_audited_and_validated(client, db_session):
    desk = create_team(db_session, "Service Desk")
    admin(client)
    assert "Default triage team" in client.get("/admin/settings?group=other").text

    assert client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": str(desk.id)}, follow_redirects=False).status_code == 303
    assert db_session.get(Setting, DEFAULT_TRIAGE_TEAM).value == str(desk.id)
    assert f'value="{desk.id}" selected' in client.get("/admin/settings?group=other").text
    assert [a.action for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "setting"))] == ["changed"]

    client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": str(desk.id)})  # unchanged: not logged again
    assert len(db_session.exec(select(AuditLog).where(AuditLog.entity_type == "setting")).all()) == 1

    assert client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": "9999"}).status_code == 400
    assert client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": "abc"}).status_code == 400
    assert client.post("/admin/settings", data={"key": "not_a_setting", "value": "1"}).status_code == 404
    assert client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": ""}, follow_redirects=False).status_code == 303
    assert db_session.get(Setting, DEFAULT_TRIAGE_TEAM).value == ""

# ---- Review fixes -------------------------------------------------------------------


def test_other_can_be_given_a_team_from_the_page_exactly_as_the_form_posts_it(client, db_session):
    """Regression: Other's row has no tick-box, so the form sends no is_active, and
    that used to be read as 'switch off' and refused. The old test posted the
    field by hand, which hid it."""
    desk = create_team(db_session, "Service Desk")
    admin(client)
    client.get("/admin/categories")  # seeds Other
    other = db_session.exec(select(Category).where(Category.is_other.is_(True))).one()
    assert 'name="is_active"' not in client.get(f"/admin/categories?category={other.id}").text  # the panel (9K) of Other has no tick-box

    response = client.post(f"/admin/categories/{other.id}", data={"team_id": str(desk.id)}, follow_redirects=False)

    assert response.status_code == 303
    db_session.refresh(other)
    assert other.team_id == desk.id and other.is_active is True


def test_other_stays_on_whatever_is_posted(client, db_session):
    admin(client)
    client.get("/admin/categories")
    other = db_session.exec(select(Category).where(Category.is_other.is_(True))).one()

    client.post(f"/admin/categories/{other.id}", data={"team_id": ""}, follow_redirects=False)

    db_session.refresh(other)
    assert other.is_active is True


def test_creating_the_same_main_category_at_once_makes_exactly_one(db_session):
    """A unique constraint cannot do this (the parent is NULL, and Postgres treats
    NULLs as distinct), so creation takes a lock around its duplicate check."""
    import threading

    from sqlmodel import Session

    from app.categories import CategoryError, create_category
    from app.database import engine

    desk = create_team(db_session, "Service Desk")
    barrier, outcomes = threading.Barrier(5), []

    def create():
        with Session(engine) as session:
            barrier.wait()
            try:
                create_category(session, "Hardware", None, desk.id)
                session.commit()
                outcomes.append("created")
            except CategoryError:
                session.rollback()
                outcomes.append("refused")

    threads = [threading.Thread(target=create) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created"] + ["refused"] * 4
    assert len(db_session.exec(select(Category).where(Category.name == "Hardware")).all()) == 1


def test_other_is_seeded_once_even_when_several_requests_ask_at_the_same_time(db_session, monkeypatch):
    """Forces the race rather than hoping for it: every thread reads 'no Other yet'
    before any of them may insert. Without the lock they would all insert."""
    import threading

    from sqlmodel import Session

    from app import categories
    from app.database import engine

    real_get_other = categories.get_other
    barrier, seen = threading.Barrier(5), threading.local()

    def get_other_after_everyone_has_looked(session):
        found = real_get_other(session)
        if not getattr(seen, "waited", False):
            seen.waited = True
            barrier.wait(timeout=10)  # all five have now read the same empty answer
        return found

    monkeypatch.setattr(categories, "get_other", get_other_after_everyone_has_looked)

    def seed():
        with Session(engine) as session:
            categories.ensure_other(session)
            session.commit()

    threads = [threading.Thread(target=seed) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    monkeypatch.setattr(categories, "get_other", real_get_other)
    assert len(db_session.exec(select(Category).where(Category.is_other.is_(True))).all()) == 1

def test_a_setting_with_digits_that_int_rejects_is_refused_and_cannot_break_new_tickets(client, db_session):
    from app.settings_store import get_int_setting

    admin(client)
    for odd in ("²", "٣", "12²"):  # superscript two, Arabic-Indic three
        assert client.post("/admin/settings", data={"key": DEFAULT_TRIAGE_TEAM, "value": odd}).status_code == 400, odd

    # Even if such a value got in some other way, reading it must not crash intake.
    db_session.add(Setting(key=DEFAULT_TRIAGE_TEAM, value="²"))
    db_session.commit()
    assert get_int_setting(db_session, DEFAULT_TRIAGE_TEAM) is None
    create_ticket(db_session, subject="x", requester_email="a@example.com", source=TicketSource.EMAIL)


def test_review_cannot_choose_a_sub_category_of_a_switched_off_category(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket, _ = other_ticket(client, db_session, desk)
    hardware = create_category(db_session, desk, "Hardware")
    laptop = create_category(db_session, None, "Laptop", parent=hardware)
    hardware.is_active = False
    db_session.add(hardware)
    db_session.commit()
    admin(client)

    assert client.post(f"/admin/category-review/{ticket.number}", data={"category_id": laptop.id}).status_code == 400
    db_session.refresh(ticket)
    assert ticket.needs_category_review is True

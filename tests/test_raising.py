import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app.models import AuditLog, Message, MessageDirection, Ticket, TicketSource, TicketStatus, UserRole
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_triage import agent, events

FORMAT = "%Y-%m-%dT%H:%M"
HX = {"HX-Request": "true"}


def raise_it(client, **fields):
    data = {"subject": "Printer on fire", "description": "It is smoking", **fields}
    return client.post("/portal/new", data=data, headers=HX)


def last_ticket(db_session):
    db_session.expire_all()
    return db_session.exec(select(Ticket).order_by(Ticket.id.desc())).first()


def desk(client, db_session, name="Agent Ann", email="agent@example.com"):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    agent_id = login_as(client, email, name)
    set_role(db_session, agent_id, UserRole.AGENT, team.id)
    return team, category, agent_id


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(second=0, microsecond=0)


# ---- For myself -----------------------------------------------------------------------------------


def test_an_end_user_raises_a_request_for_themselves(client, db_session):
    login_as(client, "jo@example.com", "Jo Bloggs")

    response = raise_it(client)

    ticket = last_ticket(db_session)
    assert response.status_code == 204 and response.headers["HX-Redirect"] == f"/portal/{ticket.number}"
    assert (ticket.source, ticket.requester_email, ticket.status) == (TicketSource.PORTAL, "jo@example.com", TicketStatus.NEW)
    assert ticket.created_by_id is not None and ticket.triaged_at is None
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert (message.body, message.direction, message.sender) == ("It is smoking", MessageDirection.INBOUND, "Jo Bloggs")
    assert "Raised in the portal by Jo Bloggs" in client.get(f"/portal/{ticket.number}").text
    assert "It is smoking" in client.get(f"/portal/{ticket.number}").text


def test_without_htmx_the_form_redirects(client, db_session):
    login_as(client, "jo@example.com")
    response = client.post("/portal/new", data={"subject": "s", "description": "d"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/portal/")


def test_an_end_user_cannot_raise_for_someone_else_or_classify(client, db_session):
    login_as(client, "jo@example.com")
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")

    for extra in (
        {"for_email": "victim@example.com"},
        {"category_id": category.id, "impact": "low", "urgency": "low"},
        {"reported_at": ago(days=1).strftime(FORMAT)},
    ):
        assert raise_it(client, **extra).status_code == 400, extra
    assert db_session.exec(select(Ticket)).all() == []


def test_the_form_loads_for_everyone_with_agent_fields_only_for_agents(client, db_session):
    login_as(client, "jo@example.com")
    assert "for_email" not in client.get("/portal/new").text
    client.post("/auth/logout")
    desk(client, db_session)
    assert "for_email" in client.get("/portal/new").text


@pytest.mark.parametrize(
    "fields",
    [{"subject": "  "}, {"description": " "}, {"subject": "s" * 201}, {"description": "d" * 10_001}],
)
def test_bad_input_is_refused(client, db_session, fields):
    login_as(client, "jo@example.com")
    assert raise_it(client, **fields).status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


# ---- For someone else -----------------------------------------------------------------------------


def test_an_agent_raises_a_ticket_for_someone_else(client, db_session):
    team, _, agent_id = desk(client, db_session)

    response = raise_it(client, for_email=" Walk.In@Example.com ")

    ticket = last_ticket(db_session)
    assert response.headers["HX-Redirect"] == f"/tickets/{ticket.number}"
    assert (ticket.source, ticket.requester_email, ticket.created_by_id) == (
        TicketSource.AGENT, "walk.in@example.com", agent_id,
    )
    assert ticket.triaged_at is None  # classifying is optional: it waits in the triage queue
    [message] = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).all()
    assert message.sender == "Logged by Agent Ann on behalf of walk.in@example.com"
    page = client.get(f"/tickets/{ticket.number}").text
    assert "Raised by Agent Ann for walk.in@example.com" in page
    assert events(db_session, ticket, "created")[0].actor_email == "agent@example.com"

    client.post("/auth/logout")
    login_as(client, "walk.in@example.com", "Walk In")
    assert "Raised by Agent Ann for Walk In" in client.get(f"/portal/{ticket.number}").text


def test_a_bad_address_is_refused(client, db_session):
    desk(client, db_session)
    for bad in ("nobody", "a@b", "a b@example.com", "x@@example.com"):
        assert raise_it(client, for_email=bad).status_code == 400, bad
    assert db_session.exec(select(Ticket)).all() == []


def test_an_agent_can_classify_and_assign_in_the_same_step(client, db_session):
    team, category, agent_id = desk(client, db_session)

    response = raise_it(
        client, for_email="walkin@example.com", ticket_type="incident",
        category_id=category.id, impact="high", urgency="high", agent_id=agent_id,
    )

    assert response.status_code == 204
    ticket = last_ticket(db_session)
    assert (ticket.status, ticket.team_id, ticket.priority, ticket.assigned_agent_id) == (
        TicketStatus.OPEN, team.id, "p1", agent_id,
    )
    assert ticket.triaged_at is not None and ticket.resolution_due_at is not None
    assert events(db_session, ticket, "triaged")


@pytest.mark.parametrize(
    "fields",
    [
        {"impact": "low", "urgency": "low"},  # no category
        {"category": True},  # category without impact and urgency
        {"agent": True},  # assignee without category
        {"category": True, "impact": "low", "urgency": "low", "agent": "stranger"},  # not in the team
        {"category": True, "impact": "weird", "urgency": "low"},
    ],
)
def test_incomplete_classification_is_refused_and_nothing_is_created(client, db_session, fields):
    team, category, agent_id = desk(client, db_session)
    other = create_team(db_session, "Network")
    stranger = login_as(client, "stranger@example.com")
    set_role(db_session, stranger, UserRole.AGENT, other.id)
    client.post("/auth/logout")
    login_as(client, "agent@example.com", "Agent Ann")
    data = {k: v for k, v in fields.items() if k not in ("category", "agent")}
    if fields.get("category"):
        data["category_id"] = category.id
    if fields.get("agent") is True:
        data["agent_id"] = agent_id
    if fields.get("agent") == "stranger":
        data["agent_id"] = stranger

    response = raise_it(client, for_email="walkin@example.com", **data)

    assert response.status_code == 400
    assert db_session.exec(select(Ticket)).all() == []  # the half-made ticket is rolled back


# ---- Raising after the fact -----------------------------------------------------------------------


def test_a_ticket_logged_after_the_fact_is_created_backdated_and_resolved(client, db_session):
    team, category, _ = desk(client, db_session)
    reported, resolved = ago(days=3, hours=2), ago(days=3, hours=1)

    response = raise_it(
        client, for_email="walkin@example.com", category_id=category.id, impact="high", urgency="high",
        reported_at=reported.strftime(FORMAT), resolved_at=resolved.strftime(FORMAT),
    )

    assert response.status_code == 204
    ticket = last_ticket(db_session)
    assert ticket.status == TicketStatus.RESOLVED and ticket.is_backdated
    assert (ticket.reported_at, ticket.resolved_at) == (reported, resolved)
    assert ticket.resolution_due_at is None and ticket.created_at > reported


def test_reported_alone_backdates_without_resolving(client, db_session):
    desk(client, db_session)
    raise_it(client, for_email="walkin@example.com", reported_at=ago(days=2).strftime(FORMAT))
    ticket = last_ticket(db_session)
    assert ticket.is_backdated and ticket.status == TicketStatus.NEW


@pytest.mark.parametrize(
    "fields",
    [
        {"reported_at": "{future}"},
        {"reported_at": "{too_old}"},
        {"reported_at": "yesterday"},
        {"resolved_at": "{recent}"},  # resolved needs a category, impact and urgency
    ],
)
def test_bad_dates_are_refused_and_nothing_is_created(client, db_session, fields):
    desk(client, db_session)
    values = {
        "{future}": (datetime.now(timezone.utc) + timedelta(days=1)).strftime(FORMAT),
        "{too_old}": ago(days=31).strftime(FORMAT),
        "{recent}": ago(days=1).strftime(FORMAT),
    }
    data = {k: values.get(v, v) for k, v in fields.items()}

    assert raise_it(client, for_email="walkin@example.com", **data).status_code == 400
    assert db_session.exec(select(Ticket)).all() == []


def test_resolved_before_reported_is_refused(client, db_session):
    team, category, _ = desk(client, db_session)
    response = raise_it(
        client, for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low",
        reported_at=ago(days=1).strftime(FORMAT), resolved_at=ago(days=2).strftime(FORMAT),
    )
    assert response.status_code == 400 and db_session.exec(select(Ticket)).all() == []


# ---- Where it shows ---------------------------------------------------------------------------------


def test_the_list_labels_the_channel_and_filters_by_it(client, db_session):
    desk(client, db_session)
    raise_it(client, subject="From the desk", for_email="walkin@example.com")
    raise_it(client, subject="Mine")
    ticket = db_session.exec(select(Ticket).where(Ticket.subject == "From the desk")).one()
    ticket.source = TicketSource.AGENT  # (already)

    everything = client.get("/tickets").text
    assert "From the desk" in everything and "Mine" in everything
    only_agent = client.get("/tickets", params={"source": "agent"}).text
    assert "From the desk" in only_agent and "Mine" not in only_agent
    only_portal = client.get("/tickets", params={"source": "portal"}).text
    assert "Mine" in only_portal and "From the desk" not in only_portal
    assert client.get("/tickets", params={"source": "carrier-pigeon"}).status_code == 422


def test_the_channel_filter_survives_search_and_paging_links(client, db_session):
    desk(client, db_session)
    raise_it(client, subject="Router reboot", for_email="walkin@example.com")
    page = client.get("/tickets", params={"source": "agent", "q": "router"}).text
    assert "Router reboot" in page
    hrefs = re.findall(r'href="([^"]*)"', page.replace("&amp;", "&"))
    assert any("source=agent" in h and "q=router" in h for h in hrefs)


def test_a_split_part_keeps_who_raised_the_original(client, db_session):
    team, category, agent_id = desk(client, db_session)
    raise_it(client, for_email="walkin@example.com", category_id=category.id, impact="low", urgency="low")
    original = last_ticket(db_session)
    client.post(f"/tickets/{original.number}/split", data={"subject": ["One", "Two"], "details": ["", ""]})

    parts = db_session.exec(select(Ticket).where(Ticket.split_from_id == original.id)).all()
    assert len(parts) == 2 and all(p.created_by_id == agent_id and p.source == TicketSource.AGENT for p in parts)


def test_raising_is_audited(client, db_session):
    login_as(client, "jo@example.com")
    raise_it(client)
    ticket = last_ticket(db_session)
    entry = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "ticket", AuditLog.action == "raised")).one()
    assert (entry.entity_id, entry.detail) == (ticket.id, "portal")


# ---- The one rule: only the raise form makes agent tickets ------------------------------------------


def test_only_the_raise_module_creates_agent_sourced_tickets():
    """Backdating trusts source=agent, so nothing else may set it when creating a ticket."""
    offenders = []
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        if re.search(r"source\s*=\s*TicketSource\.AGENT", text) and path.name != "raising.py":
            offenders.append(str(path))
    assert offenders == []

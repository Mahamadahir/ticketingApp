from sqlmodel import select

from app.models import AuditLog, Ticket, TicketStatus, UserRole
from tests.conftest import create_team, login_as, set_role


def create_ticket(db_session, team_id=None, requester_email="requester@example.com", subject="Test ticket"):
    # Open, not New: untriaged tickets are visible to every agent, which would hide what these tests check.
    ticket = Ticket(subject=subject, requester_email=requester_email, team_id=team_id, status=TicketStatus.OPEN)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def test_agent_only_sees_own_team_tickets(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    create_ticket(db_session, team_id=team_a.id, subject="Team A ticket")
    create_ticket(db_session, team_id=team_b.id, subject="Team B ticket")

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team_a.id)

    response = client.get("/tickets")
    assert "Team A ticket" in response.text
    assert "Team B ticket" not in response.text


def test_super_admin_sees_all_tickets(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    create_ticket(db_session, team_id=team_a.id, subject="Team A ticket")
    create_ticket(db_session, team_id=team_b.id, subject="Team B ticket")

    admin_id = login_as(client, "admin@example.com")

    response = client.get("/tickets")
    assert "Team A ticket" in response.text
    assert "Team B ticket" in response.text


def test_agent_cannot_view_ticket_outside_team(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    other_ticket = create_ticket(db_session, team_id=team_b.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team_a.id)

    response = client.get(f"/tickets/{other_ticket.id}")
    assert response.status_code == 403
    assert "No access" in response.text


def test_agent_self_assigns(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = create_ticket(db_session, team_id=team.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    response = client.post(f"/tickets/{ticket.id}/assign", follow_redirects=False)
    assert response.status_code == 200

    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == agent_id

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "assigned")).all()
    assert len(audit_entries) == 1
    assert audit_entries[0].actor_email == "agent@example.com"


def test_super_admin_assign_rejects_agent_from_other_team(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    ticket = create_ticket(db_session, team_id=team_a.id)

    other_agent_id = login_as(client, "other-agent@example.com")
    set_role(db_session, other_agent_id, UserRole.AGENT, team_b.id)
    client.post("/auth/logout")

    login_as(client, "admin@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/assign", data={"agent_id": other_agent_id}, follow_redirects=False
    )
    assert response.status_code == 400


def test_escalate_ticket_records_reason_and_audit(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = create_ticket(db_session, team_id=team.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    response = client.post(
        f"/tickets/{ticket.id}/escalate",
        data={"reason": "Outage affecting whole floor"},
        follow_redirects=False,
    )
    assert response.status_code == 200

    db_session.refresh(ticket)
    assert ticket.is_escalated is True
    assert ticket.escalation_reason == "Outage affecting whole floor"
    assert ticket.escalated_by_id == agent_id

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "escalated")).all()
    assert len(audit_entries) == 1


def test_escalate_can_reassign_team(client, db_session):
    team_a = create_team(db_session, "Service Desk")
    team_b = create_team(db_session, "Network")
    ticket = create_ticket(db_session, team_id=team_a.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team_a.id)

    client.post(
        f"/tickets/{ticket.id}/escalate",
        data={"reason": "Needs network team", "target_team_id": team_b.id},
    )

    db_session.refresh(ticket)
    assert ticket.team_id == team_b.id
    assert ticket.assigned_agent_id is None


def test_status_change_writes_audit(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = create_ticket(db_session, team_id=team.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    client.post(f"/tickets/{ticket.id}/status", data={"status": "resolved"})

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "status_changed")).all()
    assert len(audit_entries) == 1
    assert "resolved" in audit_entries[0].detail


def test_email_sets_status_awaiting_user_and_is_audited(client, db_session):
    team = create_team(db_session, "Service Desk")
    ticket = create_ticket(db_session, team_id=team.id)

    agent_id = login_as(client, "agent@example.com")
    set_role(db_session, agent_id, UserRole.AGENT, team.id)

    client.post(f"/tickets/{ticket.id}/email", data={"body": "Have you tried rebooting?"})

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_USER
    assert len(ticket.messages) == 1
    assert ticket.messages[0].author_user_id == agent_id

    audit_entries = db_session.exec(select(AuditLog).where(AuditLog.action == "emailed_requester")).all()
    assert len(audit_entries) == 1


def test_end_user_cannot_access_agent_console(client, db_session):
    login_as(client, "enduser@example.com")

    response = client.get("/tickets", follow_redirects=False)
    assert response.status_code == 403

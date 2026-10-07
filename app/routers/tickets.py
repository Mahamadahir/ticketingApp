from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import require_roles
from app.database import get_session
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.models import Message, MessageDirection, Team, Ticket, TicketStatus, User, UserRole
from app.pagination import paginate

router = APIRouter(prefix="/tickets", tags=["tickets"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["max_message_length"] = MAX_MESSAGE_LENGTH

AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)


def _ensure_visible(ticket: Ticket, user: User) -> None:
    if user.role == UserRole.AGENT and ticket.team_id != user.team_id:
        raise HTTPException(status_code=404, detail="Ticket not found")


@router.get("", response_class=HTMLResponse)
def list_tickets(
    request: Request,
    mine: bool = False,
    team_id: Optional[int] = None,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    query = select(Ticket).order_by(Ticket.updated_at.desc())

    if user.role == UserRole.AGENT:
        query = query.where(Ticket.team_id == user.team_id)
    elif team_id is not None:
        query = query.where(Ticket.team_id == team_id)

    if mine:
        query = query.where(Ticket.assigned_agent_id == user.id)

    tickets_page = paginate(session, query, page, request.query_params)
    teams_by_id = {team.id: team for team in session.exec(select(Team)).all()}
    agents_by_id = {agent.id: agent for agent in session.exec(select(User).where(User.role == UserRole.AGENT)).all()}
    teams = session.exec(select(Team)).all() if user.role == UserRole.SUPER_ADMIN else []

    return templates.TemplateResponse(
        request,
        "tickets_list.html",
        {
            "page": tickets_page,
            "teams_by_id": teams_by_id,
            "agents_by_id": agents_by_id,
            "teams": teams,
            "user": user,
            "mine": mine,
            "team_id": team_id,
        },
    )


@router.get("/{ticket_id}", response_class=HTMLResponse)
def view_ticket(
    ticket_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _ensure_visible(ticket, user)

    team = session.get(Team, ticket.team_id) if ticket.team_id else None
    assigned_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    team_agents = (
        session.exec(select(User).where(User.role == UserRole.AGENT, User.team_id == ticket.team_id)).all()
        if ticket.team_id
        else []
    )
    teams = session.exec(select(Team)).all()

    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "ticket": ticket,
            "team": team,
            "assigned_agent": assigned_agent,
            "team_agents": team_agents,
            "teams": teams,
            "user": user,
        },
    )


@router.post("/{ticket_id}/reply", response_class=HTMLResponse)
def reply_to_ticket(
    ticket_id: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _ensure_visible(ticket, user)

    message = Message(
        ticket_id=ticket.id,
        sender=user.display_name,
        body=body,
        direction=MessageDirection.OUTBOUND,
        author_user_id=user.id,
    )
    session.add(message)

    ticket.status = TicketStatus.IN_PROGRESS
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    log_action(session, user.email, "ticket", ticket.id, "commented", body[:200])
    session.commit()
    session.refresh(ticket)

    # TODO: once Graph credentials are wired up, send this reply as an
    # actual email back to ticket.requester_email via graph_client.send_mail
    # so the requester sees it as a normal reply in their inbox.

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/status", response_class=HTMLResponse)
def change_status(
    ticket_id: int,
    request: Request,
    status: TicketStatus = Form(...),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _ensure_visible(ticket, user)

    previous_status = ticket.status
    ticket.status = status
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    log_action(session, user.email, "ticket", ticket.id, "status_changed", f"{previous_status.value} -> {status.value}")
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/assign", response_class=HTMLResponse)
def assign_ticket(
    ticket_id: int,
    request: Request,
    agent_id: Optional[int] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _ensure_visible(ticket, user)

    if user.role == UserRole.AGENT:
        target_agent = user
    else:
        if agent_id is None:
            raise HTTPException(status_code=400, detail="agent_id is required")
        target_agent = session.get(User, agent_id)
        if target_agent is None or target_agent.role != UserRole.AGENT or target_agent.team_id != ticket.team_id:
            raise HTTPException(status_code=400, detail="Agent must belong to the ticket's team")

    ticket.assigned_agent_id = target_agent.id
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    log_action(session, user.email, "ticket", ticket.id, "assigned", target_agent.email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/escalate", response_class=HTMLResponse)
def escalate_ticket(
    ticket_id: int,
    request: Request,
    reason: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    target_team_id: Optional[str] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _ensure_visible(ticket, user)
    parsed_target_team_id = optional_int(target_team_id)

    ticket.is_escalated = True
    ticket.escalation_reason = reason
    ticket.escalated_by_id = user.id
    ticket.escalated_at = datetime.now(timezone.utc)
    if parsed_target_team_id is not None:
        ticket.team_id = parsed_target_team_id
        ticket.assigned_agent_id = None
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    log_action(session, user.email, "ticket", ticket.id, "escalated", reason)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


def _render_ticket_detail(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    team = session.get(Team, ticket.team_id) if ticket.team_id else None
    assigned_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    team_agents = (
        session.exec(select(User).where(User.role == UserRole.AGENT, User.team_id == ticket.team_id)).all()
        if ticket.team_id
        else []
    )
    teams = session.exec(select(Team)).all()

    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "ticket": ticket,
            "team": team,
            "assigned_agent": assigned_agent,
            "team_agents": team_agents,
            "teams": teams,
            "user": user,
        },
    )

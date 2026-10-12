from datetime import datetime, timezone
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import require_roles
from app.database import get_session
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.followers import (
    MAX_EXTERNAL_FOLLOWERS,
    InvalidEmail,
    TooManyFollowers,
    add_follower,
    followers_of,
    remove_follower,
)
from app.categories import CategoryError, create_category, path_of, selectable, team_id_for
from app.messages import add_note, agent_timeline, email_requester, is_triaged
from app.models import Category, Team, Ticket, TicketAlias, TicketStatus, TicketType, User, UserRole
from app.pagination import paginate
from app.ticket_service import EVENT_ASSIGNED, EVENT_ESCALATED, NotTriaged, record_event, set_status
from app.triage import TriageError, reassign_ticket, triage_ticket
from app.visibility import (
    Access,
    NoTicketAccess,
    assignable_agents,
    may_add_note,
    may_self_assign,
    may_triage,
    team_members,
    access_context,
    require_access,
    ticket_access,
    visible_ticket_condition,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])
# Shareable links live outside /tickets: /t/INC-42.
reference_router = APIRouter(tags=["tickets"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["max_message_length"] = MAX_MESSAGE_LENGTH

AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)


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

    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is not None:
        query = query.where(visible)
    if team_id is not None:
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


def _get_ticket(session: Session, ticket_id: int, user: User, needed: Access = Access.WORK) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    require_access(user, ticket, access_context(session, user), needed)
    return ticket


def _agent_options(request: Request, members: list[User], blank_label: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "_agent_options.html", {"members": members, "blank_label": blank_label})


@router.get("/agents-for-team", response_class=HTMLResponse)
def agent_options_for_team(
    request: Request,
    team_id: Optional[int] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The agent choices for a team, as <option>s, for the team picker's htmx call."""
    # Re-assigning with no one chosen leaves the ticket unassigned in that team.
    return _agent_options(request, team_members(session, team_id), "Leave unassigned")


@router.get("/agents-for-category", response_class=HTMLResponse)
def agent_options_for_category(
    request: Request,
    category_id: Optional[int] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The agent choices for the team a category belongs to, for the triage panel."""
    category = session.get(Category, category_id) if category_id is not None else None
    team_id = team_id_for(session, category) if category is not None else None
    # Triage with no one chosen leaves the assignment as it is.
    return _agent_options(request, team_members(session, team_id), "Leave as is")


@router.post("/{ticket_id}/triage", response_class=HTMLResponse)
def triage(
    ticket_id: int,
    request: Request,
    ticket_type: TicketType = Form(...),
    category_id: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
    new_category_name: Optional[str] = Form(None, max_length=200),
    new_category_parent_id: Optional[str] = Form(None),
    new_category_team_id: Optional[str] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Classifies a New ticket: type and category (which picks the team), and
    optionally assigns it. Assignment is not required; the team can pick it up."""
    ticket = _get_ticket(session, ticket_id, user, Access.READ)
    if ticket.triaged_at is not None:
        raise HTTPException(status_code=400, detail="This ticket has already been triaged")
    if not may_triage(user, ticket, access_context(session, user)):
        raise NoTicketAccess()

    try:
        if new_category_name and new_category_name.strip():
            if user.role != UserRole.SUPER_ADMIN:
                raise HTTPException(status_code=403, detail="Only a super admin can create a category")
            category = create_category(
                session, new_category_name, optional_int(new_category_parent_id), optional_int(new_category_team_id)
            )
            log_action(session, user.email, "category", category.id, "created", path_of(session, category))
        else:
            chosen = optional_int(category_id)
            category = session.get(Category, chosen) if chosen is not None else None
            if category is None:
                raise HTTPException(status_code=400, detail="Choose a category")

        chosen_agent = optional_int(agent_id)
        assignee = session.get(User, chosen_agent) if chosen_agent is not None else None
        if chosen_agent is not None and (assignee is None or assignee.role != UserRole.AGENT):
            raise HTTPException(status_code=400, detail="Choose an agent from the list")

        triage_ticket(session, ticket, user, ticket_type=ticket_type, category=category, assignee=assignee)
    except (CategoryError, TriageError) as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))

    log_action(session, user.email, "ticket", ticket.id, "triaged", path_of(session, category))
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/reassign", response_class=HTMLResponse)
def reassign(
    ticket_id: int,
    request: Request,
    team_id: int = Form(...),
    agent_id: Optional[str] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Choose a team, then an agent in it or leave the ticket unassigned there.
    No reason is needed; this is not an escalation."""
    ticket = _get_ticket(session, ticket_id, user)
    team = session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=400, detail="Choose a team")
    chosen_agent = optional_int(agent_id)
    assignee = session.get(User, chosen_agent) if chosen_agent is not None else None
    if chosen_agent is not None and (assignee is None or assignee.role != UserRole.AGENT):
        raise HTTPException(status_code=400, detail="Choose an agent from the list")

    try:
        changed = reassign_ticket(session, ticket, user, team=team, assignee=assignee)
    except TriageError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))

    if changed:
        detail = f"{team.name}: {assignee.email if assignee else 'unassigned'}"
        log_action(session, user.email, "ticket", ticket.id, "reassigned", detail)
    session.commit()
    session.refresh(ticket)

    if ticket_access(user, ticket, access_context(session, user)) < Access.READ:
        # Handing a ticket to a team you are not in takes it out of your view, so
        # say where it went instead of showing a page they can no longer open.
        return HTMLResponse(
            f'<p class="note">{escape(ticket.reference)} was moved to {escape(team.name)}. '
            'You no longer have access to it.</p><p><a href="/tickets">Back to tickets</a></p>'
        )
    return _render_ticket_detail(request, ticket, user, session)

@router.get("/{ticket_id}", response_class=HTMLResponse)
def view_ticket(
    ticket_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    return _render_ticket_detail(request, _get_ticket(session, ticket_id, user, Access.READ), user, session)


@reference_router.get("/t/{reference}", response_class=HTMLResponse)
def view_ticket_by_reference(
    reference: str,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Shareable link, e.g. /t/INC-42."""
    ticket = session.exec(select(Ticket).where(Ticket.reference == reference.upper())).first()
    if ticket is None:
        # A ticket whose type changed at triage keeps its old reference as an alias.
        alias = session.get(TicketAlias, reference.upper())
        ticket = session.get(Ticket, alias.ticket_id) if alias is not None else None
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    require_access(user, ticket, access_context(session, user), Access.READ)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/email", response_class=HTMLResponse)
def email_user(
    ticket_id: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Send the requester a message. Not available until the ticket is triaged."""
    ticket = _get_ticket(session, ticket_id, user)

    try:
        email_requester(session, ticket, user, body)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before emailing the requester")

    # The first email on an open ticket starts the work. Any other status
    # (awaiting user, with MSP, resolved, closed) is the agent's call and an
    # email must not undo it.
    if ticket.status == TicketStatus.OPEN:
        previous_status = ticket.status
        set_status(session, ticket, TicketStatus.IN_PROGRESS, user.email)
        log_action(
            session, user.email, "ticket", ticket.id, "status_changed",
            f"{previous_status.value} -> {TicketStatus.IN_PROGRESS.value}",
        )
    log_action(session, user.email, "ticket", ticket.id, "emailed_requester", body[:200])
    session.commit()
    session.refresh(ticket)

    # TODO: once Graph credentials are wired up, send this as an actual email
    # to ticket.requester_email via graph_client.send_mail, as a reply in the
    # ticket's mail conversation, so the requester sees one chain.

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/note", response_class=HTMLResponse)
def add_ticket_note(
    ticket_id: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An internal note, visible to agents only. Allowed at any stage, including
    on an untriaged ticket the agent has not claimed, and never triages it."""
    ticket = _get_ticket(session, ticket_id, user, Access.READ)
    if not may_add_note(user, ticket, access_context(session, user)):
        raise NoTicketAccess()

    add_note(session, ticket, user, body)
    log_action(session, user.email, "ticket", ticket.id, "note_added", body[:200])
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)

@router.post("/{ticket_id}/status", response_class=HTMLResponse)
def change_status(
    ticket_id: int,
    request: Request,
    status: TicketStatus = Form(...),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, ticket_id, user)
    if status == TicketStatus.NEW:
        raise HTTPException(status_code=400, detail="A ticket only starts in New; it leaves New by being triaged")

    previous_status = ticket.status
    try:
        changed = set_status(session, ticket, status, user.email)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before changing its status")
    if changed:
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
    ticket = _get_ticket(session, ticket_id, user, Access.READ)

    if user.role == UserRole.AGENT:
        if not may_self_assign(user, ticket, access_context(session, user)):
            raise NoTicketAccess()
        target_agent = user
    else:
        if agent_id is None:
            raise HTTPException(status_code=400, detail="agent_id is required")
        target_agent = session.get(User, agent_id)
        if target_agent is None or target_agent.id not in {agent.id for agent in assignable_agents(session, ticket)}:
            raise HTTPException(status_code=400, detail="Agent must be a member of the ticket's team")

    if ticket.assigned_agent_id == target_agent.id:
        return _render_ticket_detail(request, ticket, user, session)

    previous_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    ticket.assigned_agent_id = target_agent.id
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    record_event(
        session, ticket, EVENT_ASSIGNED, user.email,
        from_value=previous_agent.email if previous_agent else None,
        to_value=target_agent.email,
    )
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
    ticket = _get_ticket(session, ticket_id, user)
    parsed_target_team_id = optional_int(target_team_id)

    new_team = None
    if parsed_target_team_id is not None:
        new_team = session.get(Team, parsed_target_team_id)
        if new_team is None:
            raise HTTPException(status_code=400, detail="Target team does not exist")

    previous_team = session.get(Team, ticket.team_id) if ticket.team_id else None
    previous_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    ticket.is_escalated = True
    ticket.escalation_reason = reason
    ticket.escalated_by_id = user.id
    ticket.escalated_at = datetime.now(timezone.utc)
    if new_team is not None:
        ticket.team_id = new_team.id
        ticket.assigned_agent_id = None
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    record_event(
        session, ticket, EVENT_ESCALATED, user.email,
        from_value=previous_team.name if previous_team else None,
        to_value=(new_team or previous_team).name if (new_team or previous_team) else None,
        detail=reason,
    )
    if new_team is not None and previous_agent is not None:
        # Moving to another team unassigns the agent; the history must say so.
        record_event(session, ticket, EVENT_ASSIGNED, user.email, from_value=previous_agent.email, to_value=None)
    # The escalating agent follows the ticket from now on: copied on everything and
    # keeps read access wherever it goes.
    add_follower(session, ticket, user.email, user.email, internal_user=user)
    log_action(session, user.email, "ticket", ticket.id, "escalated", reason)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/followers", response_class=HTMLResponse)
def add_ticket_follower(
    ticket_id: int,
    request: Request,
    email: str = Form(..., max_length=254),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Copy someone on this ticket's emails. They see it under Following in the
    portal if they have an account."""
    ticket = _get_ticket(session, ticket_id, user)
    try:
        follower = add_follower(session, ticket, email, user.email)
    except InvalidEmail:
        raise HTTPException(status_code=400, detail="That is not a valid email address")
    except TooManyFollowers:
        raise HTTPException(status_code=400, detail=f"A ticket can have at most {MAX_EXTERNAL_FOLLOWERS} followers")

    if follower is not None:
        log_action(session, user.email, "ticket", ticket.id, "follower_added", follower.email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{ticket_id}/followers/{follower_id}/remove", response_class=HTMLResponse)
def remove_ticket_follower(
    ticket_id: int,
    follower_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, ticket_id, user)
    removed_email = remove_follower(session, ticket, follower_id, user.email)
    if removed_email is None:
        raise HTTPException(status_code=404, detail="Follower not found")

    log_action(session, user.email, "ticket", ticket.id, "follower_removed", removed_email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


def _render_ticket_detail(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    team = session.get(Team, ticket.team_id) if ticket.team_id else None
    assigned_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    levels = access_context(session, user)
    team_agents = assignable_agents(session, ticket)
    can_work = ticket_access(user, ticket, levels) >= Access.WORK
    can_claim = not can_work and may_self_assign(user, ticket, levels)
    can_triage = may_triage(user, ticket, levels)
    category = session.get(Category, ticket.category_id) if ticket.category_id else None
    teams = session.exec(select(Team)).all()
    timeline = agent_timeline(session, ticket)

    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "ticket": ticket,
            "team": team,
            "assigned_agent": assigned_agent,
            "team_agents": team_agents,
            "teams": teams,
            "timeline": timeline,
            "followers": followers_of(session, ticket),
            "can_email": can_work and is_triaged(ticket),
            "can_edit_followers": can_work,
            "can_note": may_add_note(user, ticket, levels),
            # New is left only by triage, so it is not offered as a status to set.
            "statuses": [s for s in TicketStatus if s != TicketStatus.NEW],
            "can_triage": can_triage,
            "ticket_types": list(TicketType),
            "category_options": selectable(session) if can_triage else [],
            "main_categories": [c for c, _ in selectable(session) if c.parent_id is None and not c.is_other] if can_triage else [],
            "category_path": path_of(session, category) if category else None,
            "can_create_category": user.role == UserRole.SUPER_ADMIN,
            "can_reassign": can_work and not can_triage and ticket.triaged_at is not None,
            "reassign_agents": team_members(session, ticket.team_id),
            "can_work": can_work,
            "can_claim": can_claim,
            "user": user,
        },
    )

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import get_current_user
from app.database import get_session
from app.followers import (
    MAX_EXTERNAL_FOLLOWERS,
    InvalidEmail,
    TooManyFollowers,
    add_follower,
    followers_of,
    is_follower,
    remove_follower,
    tickets_followed_by,
)
from app.backdating import FORMAT as MOMENT_FORMAT, BackdateError, parse_moment
from app.categories import CategoryError, selectable
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.raising import (
    MAX_SUBJECT_LENGTH,
    RaiseError,
    is_agent,
    raise_ticket,
    should_tell_requester,
    source_label,
)
from app.sla import IMPACT_LABELS, URGENCY_LABELS, Level
from app.triage import TriageError
from app.visibility import Access, access_context, ticket_access
from app.messages import about_labels, record_inbound, requester_visible_messages
from app.splitting import has_sub_tickets, refresh_open_sub_tickets, sync_original_status
from app.models import Category, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.ticket_service import get_by_number, set_status
from app.pagination import paginate

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["max_message_length"] = MAX_MESSAGE_LENGTH


def _is_requester(ticket: Ticket, user: User) -> bool:
    return ticket.requester_email.lower() == user.email.lower()


def _get_own_ticket(session: Session, number: int, user: User) -> Ticket:
    """A ticket the user raised. Followers cannot use this: they only read."""
    ticket = get_by_number(session, number)
    if ticket is None or not _is_requester(ticket, user):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _get_commentable_ticket(session: Session, number: int, user: User) -> Ticket:
    """A ticket the user can reply on: one they raised, or one they follow. Internal
    followers are agents and use the agent console, so they cannot comment here."""
    ticket = get_by_number(session, number)
    if ticket is None or not (
        _is_requester(ticket, user) or is_follower(session, ticket, user.email, include_internal=False)
    ):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _get_readable_ticket(session: Session, number: int, user: User) -> Ticket:
    """A ticket the user raised or follows."""
    ticket = get_by_number(session, number)
    if ticket is None or not (_is_requester(ticket, user) or is_follower(session, ticket, user.email)):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _render_new_form(request: Request, user: User, session: Session) -> HTMLResponse:
    agent = is_agent(user)
    return templates.TemplateResponse(
        request,
        "portal_new_ticket.html",
        {
            "user": user,
            "is_agent": agent,
            "ticket_types": list(TicketType),
            "category_options": selectable(session) if agent else [],
            "levels": list(Level),
            "impact_labels": IMPACT_LABELS,
            "urgency_labels": URGENCY_LABELS,
            "moment_format": MOMENT_FORMAT,
            "max_subject_length": MAX_SUBJECT_LENGTH,
            "tell_requester": should_tell_requester(session),
        },
    )


def _render_ticket(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    visible = requester_visible_messages(session, ticket)
    return templates.TemplateResponse(
        request,
        "portal_ticket_detail.html",
        {
            "ticket": ticket,
            "messages": visible,
            "about": about_labels(session, visible),
            "source_label": source_label(session, ticket),
            # Only the requester sees who else is copied; a follower must not learn
            # the other followers' addresses, and internal followers are never shown.
            "followers": (
                [f for f in followers_of(session, ticket) if not f.is_internal]
                if _is_requester(ticket, user)
                else []
            ),
            "is_requester": _is_requester(ticket, user),
            # The requester and ordinary followers can reply; an internal follower is an
            # agent and replies from the agent console.
            "can_comment": _is_requester(ticket, user) or is_follower(session, ticket, user.email, include_internal=False),
            "user": user,
        },
    )


@router.get("", response_class=HTMLResponse)
def list_my_tickets(
    request: Request,
    page: int = 1,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(Ticket).where(Ticket.requester_email == user.email).order_by(Ticket.updated_at.desc())
    tickets_page = paginate(session, query, page, request.query_params)
    following = tickets_followed_by(session, user.email)
    return templates.TemplateResponse(
        request, "portal_tickets_list.html", {"page": tickets_page, "following": following, "user": user}
    )


@router.get("/new", response_class=HTMLResponse)
def new_ticket_form(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _render_new_form(request, user, session)


@router.post("/new")
def raise_new_ticket(
    request: Request,
    subject: str = Form(..., max_length=500),
    description: str = Form(..., max_length=MAX_MESSAGE_LENGTH + 1000),
    for_email: Optional[str] = Form(None, max_length=500),
    ticket_type: TicketType = Form(TicketType.INCIDENT),
    category_id: Optional[str] = Form(None),
    impact: Optional[str] = Form(None),
    urgency: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
    reported_at: Optional[str] = Form(None, max_length=40),
    resolved_at: Optional[str] = Form(None, max_length=40),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Raise a ticket for yourself, or (agents) for someone else. An agent can classify and
    assign it in the same step and say when it was reported and resolved."""
    if len(description.strip()) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"The description can be at most {MAX_MESSAGE_LENGTH} characters")
    try:
        category_choice = optional_int(category_id)
        chosen_category = session.get(Category, category_choice) if category_choice is not None else None
        if category_choice is not None and chosen_category is None:
            raise RaiseError("Choose a category from the list")
        chosen_agent = optional_int(agent_id)
        assignee = session.get(User, chosen_agent) if chosen_agent is not None else None
        if chosen_agent is not None and (assignee is None or assignee.role != UserRole.AGENT):
            raise RaiseError("Choose an agent from the list")
        try:
            level_impact = Level(impact) if impact else None
            level_urgency = Level(urgency) if urgency else None
            moments = (parse_moment(reported_at), parse_moment(resolved_at))
        except (ValueError, BackdateError) as error:
            raise RaiseError(str(error) if isinstance(error, BackdateError) else "Choose impact and urgency from the lists")
        ticket = raise_ticket(
            session, user, subject=subject, description=description, for_email=for_email,
            ticket_type=ticket_type, category=chosen_category, impact=level_impact, urgency=level_urgency,
            assignee=assignee, reported_at=moments[0], resolved_at=moments[1],
        )
        log_action(session, user.email, "ticket", ticket.id, "raised", ticket.source.value)
        session.flush()
    except (RaiseError, TriageError, CategoryError) as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()

    destination = f"/portal/{ticket.number}"
    if ticket.source == TicketSource.AGENT:
        # The agent may have classified it into a team they are not in, which they cannot open.
        can_open = ticket_access(user, ticket, access_context(session, user)) >= Access.READ
        destination = f"/tickets/{ticket.number}" if can_open else "/tickets"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": destination})
    return RedirectResponse(destination, status_code=303)


@router.get("/{number}", response_class=HTMLResponse)
def view_my_ticket(
    number: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _render_ticket(request, _get_readable_ticket(session, number, user), user, session)


@router.post("/{number}/comment", response_class=HTMLResponse)
def add_comment(
    number: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ticket = _get_commentable_ticket(session, number, user)

    record_inbound(session, ticket, sender=user.display_name, body=body, author_user_id=user.id)
    log_action(session, user.email, "ticket", ticket.id, "commented", body[:200])
    # A reply from the requester or a follower hands the ticket back to us: it leaves
    # Awaiting user, which resumes the SLA clock. Any other status is left alone (a
    # reply on a Resolved ticket is the reopen flow, phase 4).
    if has_sub_tickets(session, ticket):
        # The original's status follows its parts, so it is never set here. The system
        # cannot tell which part the reply is about, so each open part sees it, and the
        # original is re-derived from them.
        refresh_open_sub_tickets(session, ticket, user.email)
        sync_original_status(session, ticket.id)
    elif ticket.status == TicketStatus.AWAITING_USER:
        set_status(session, ticket, TicketStatus.IN_PROGRESS, user.email)
        log_action(
            session, user.email, "ticket", ticket.id, "status_changed",
            f"{TicketStatus.AWAITING_USER.value} -> {TicketStatus.IN_PROGRESS.value}",
        )
    session.commit()
    session.refresh(ticket)

    return _render_ticket(request, ticket, user, session)


@router.post("/{number}/followers", response_class=HTMLResponse)
def add_ticket_follower(
    number: int,
    request: Request,
    email: str = Form(..., max_length=254),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The requester copies someone else on updates to their ticket."""
    ticket = _get_own_ticket(session, number, user)
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

    return _render_ticket(request, ticket, user, session)


@router.post("/{number}/followers/{follower_id}/remove", response_class=HTMLResponse)
def remove_ticket_follower(
    number: int,
    follower_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ticket = _get_own_ticket(session, number, user)
    removed_email = remove_follower(session, ticket, follower_id, user.email)
    if removed_email is None:
        raise HTTPException(status_code=404, detail="Follower not found")

    log_action(session, user.email, "ticket", ticket.id, "follower_removed", removed_email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket(request, ticket, user, session)

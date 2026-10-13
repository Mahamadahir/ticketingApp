from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
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
from app.forms import MAX_MESSAGE_LENGTH
from app.messages import record_inbound, requester_visible_messages
from app.models import Ticket, TicketStatus, User
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


def _render_ticket(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "portal_ticket_detail.html",
        {
            "ticket": ticket,
            "messages": requester_visible_messages(session, ticket),
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
    if ticket.status == TicketStatus.AWAITING_USER:
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

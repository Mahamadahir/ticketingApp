from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import get_current_user
from app.database import get_session
from app.followers import (
    MAX_EXTERNAL_FOLLOWERS,
    InvalidEmail,
    NeverFollowListed,
    TooManyFollowers,
    add_follower,
    followers_of,
    remove_follower,
)
from app.approvals import (
    MAX_COMMENT_LENGTH,
    PENDING,
    ApprovalError,
    current_approval,
    withdraw,
    count_pending_for,
    describe_rule,
    pending_for,
    vote_for,
    record_decision,
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
from app.visibility import Access, access_context, may_set_required_by, ticket_access
from app.messages import about_labels, record_inbound, requester_visible_messages
from app.follower_updates import queue_follower_updates
from app.reopening import ReopenError, latest_follow_on, open_follow_on_ticket, related_tickets, reopen_ticket
from app.splitting import apply_requester_reply
from app.request_types import active_request_types, request_type_of
from app.required_by import RequiredByError, offers_required_by, parse_required_by, set_required_by
from app.models import Category, RequestType, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.ticket_service import get_by_number
from app import identity
from app.identity import is_follower_of, is_requester, my_tickets_query
from app.pagination import paginate

router = APIRouter(prefix="/portal", tags=["portal"])


def _is_requester(ticket: Ticket, user: User) -> bool:
    return is_requester(ticket, user)


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
        _is_requester(ticket, user) or is_follower_of(session, ticket, user, include_internal=False)
    ):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _get_readable_ticket(session: Session, number: int, user: User) -> Ticket:
    """A ticket the user raised or follows, or one that is waiting for them to approve. An approver
    reads the request while their answer is wanted; once it is settled the access ends."""
    ticket = get_by_number(session, number)
    if ticket is None or not (
        _is_requester(ticket, user)
        or is_follower_of(session, ticket, user)
        or _my_pending_approval(session, ticket, user) is not None
    ):
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
            "request_types": active_request_types(session),
            "required_by_names": [t.name for t in active_request_types(session) if t.offers_required_by],
        },
    )


def _my_pending_approval(session: Session, ticket: Ticket, user: User):
    """This user's unanswered approval of the ticket, while it is still waiting."""
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        return None
    vote = vote_for(session, ticket, user)
    return vote if vote is not None and vote.decision == PENDING else None


def _approval_rule(session: Session, ticket: Ticket):
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        return None
    approval = current_approval(session, ticket)
    return describe_rule(approval) if approval is not None else None


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
            "request_info": request_type_of(session, ticket),
            "my_approval": _my_pending_approval(session, ticket, user),
            "is_approver": vote_for(session, ticket, user) is not None,
            "approval_rule": _approval_rule(session, ticket),
            # Only the requester sees who else is copied; a follower must not learn
            # the other followers' addresses, and internal followers are never shown.
            "followers": (
                [f for f in followers_of(session, ticket) if not f.is_internal]
                if _is_requester(ticket, user)
                else []
            ),
            "is_requester": _is_requester(ticket, user),
            "can_set_required_by": offers_required_by(session, ticket)
            and may_set_required_by(user, ticket, access_context(session, user)),
            "can_withdraw": _is_requester(ticket, user) and ticket.status == TicketStatus.AWAITING_APPROVAL,
            "can_reopen": _is_requester(ticket, user) and ticket.status == TicketStatus.RESOLVED,
            # The earlier or later request this one is linked to by a reply after closure, if the person can read it.
            "related": [
                (other, which) for other, which in related_tickets(session, ticket)
                if _is_requester(other, user) or is_follower_of(session, other, user)
            ],
            # The requester and ordinary followers can reply; an internal follower is an
            # agent and replies from the agent console.
            "can_comment": _is_requester(ticket, user) or is_follower_of(session, ticket, user, include_internal=False),
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
    tickets_page = paginate(session, my_tickets_query(user), page, request.query_params)
    return templates.TemplateResponse(
        request,
        "portal_tickets_list.html",
        {"page": tickets_page, "my_emails": identity.requester_emails(user), "waiting_on_me": count_pending_for(session, user), "user": user},
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
    request_type_id: Optional[str] = Form(None),
    category_id: Optional[str] = Form(None),
    impact: Optional[str] = Form(None),
    urgency: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
    reported_at: Optional[str] = Form(None, max_length=40),
    resolved_at: Optional[str] = Form(None, max_length=40),
    required_by: Optional[str] = Form(None, max_length=20),
    resolution_notes: Optional[str] = Form(None, max_length=MAX_MESSAGE_LENGTH + 1000),
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
        type_choice = optional_int(request_type_id)
        chosen_request_type = session.get(RequestType, type_choice) if type_choice is not None else None
        if type_choice is not None and chosen_request_type is None:
            raise RaiseError("Choose a kind of request from the list")
        chosen_agent = optional_int(agent_id)
        assignee = session.get(User, chosen_agent) if chosen_agent is not None else None
        if chosen_agent is not None and (assignee is None or assignee.role != UserRole.AGENT):
            raise RaiseError("Choose an agent from the list")
        try:
            level_impact = Level(impact) if impact else None
            level_urgency = Level(urgency) if urgency else None
            moments = (parse_moment(reported_at), parse_moment(resolved_at))
            day = parse_required_by(required_by)
        except (ValueError, BackdateError, RequiredByError) as error:
            raise RaiseError(
                str(error) if isinstance(error, (BackdateError, RequiredByError)) else "Choose impact and urgency from the lists"
            )
        ticket = raise_ticket(
            session, user, subject=subject, description=description, for_email=for_email,
            ticket_type=ticket_type, category=chosen_category, impact=level_impact, urgency=level_urgency,
            assignee=assignee, reported_at=moments[0], resolved_at=moments[1], request_type=chosen_request_type,
            resolution_notes=resolution_notes, required_by=day,
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


@router.get("/approvals", response_class=HTMLResponse)
def my_approvals(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Requests waiting for this person to approve or decline."""
    return templates.TemplateResponse(
        request, "portal_approvals.html", {"waiting": pending_for(session, user), "user": user}
    )


@router.post("/{number}/approval", response_class=HTMLResponse)
def decide_approval(
    number: int,
    request: Request,
    decision: str = Form(...),
    comment: str = Form("", max_length=MAX_COMMENT_LENGTH + 500),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """An approver approves or declines a request waiting for them."""
    ticket = get_by_number(session, number)
    vote = vote_for(session, ticket, user) if ticket is not None else None
    if vote is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    try:
        record_decision(session, vote, decision, comment)
    except ApprovalError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket(request, ticket, user, session)


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

    if ticket.status == TicketStatus.CLOSED:
        # Serialise two replies on the same closed ticket (a double click); if an earlier reply already
        # opened a request, this one belongs on the newest in the chain (or, if that is closed too, after it).
        session.refresh(ticket, with_for_update=True)
        newest = latest_follow_on(session, ticket)
        if newest.id != ticket.id and (_is_requester(newest, user) or is_follower_of(session, newest, user, include_internal=False)):
            ticket = newest  # not someone else's request that the chain happens to end in
    if ticket.status == TicketStatus.CLOSED:
        # A reply after closure opens a new request linked to this one, not a comment on a closed ticket.
        follow_on = open_follow_on_ticket(
            session, ticket, subject=ticket.subject, sender_email=user.email, source=TicketSource.PORTAL,
            actor_email=user.email,
        )
        record_inbound(session, follow_on, sender=user.display_name, body=body, author_user_id=user.id)
        log_action(session, user.email, "ticket", follow_on.id, "commented", body[:200])
        session.commit()
        destination = f"/portal/{follow_on.number}"
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": destination})
        return RedirectResponse(destination, status_code=303)

    message = record_inbound(session, ticket, sender=user.display_name, body=body, author_user_id=user.id, reply_by=user.email)
    log_action(session, user.email, "ticket", ticket.id, "commented", body[:200])
    queue_follower_updates(session, ticket, message, user.email)  # nobody was sent a portal comment
    moved = apply_requester_reply(session, ticket, user.email, text=body)
    if moved:
        log_action(session, user.email, "ticket", ticket.id, "status_changed", f"{moved[0]} -> {moved[1]}")
    session.commit()
    session.refresh(ticket)

    return _render_ticket(request, ticket, user, session)


@router.post("/{number}/reopen")
def reopen_my_ticket(
    number: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The signed-in requester reopens their resolved ticket (no emailed link needed). Followers cannot."""
    ticket = _get_own_ticket(session, number, user)
    try:
        reopen_ticket(session, ticket, user.email, "The requester reopened it from the portal")
    except ReopenError:
        session.rollback()  # reopened or closed since the page was drawn: show them the ticket as it is now
        return RedirectResponse(f"/portal/{ticket.number}", status_code=303)
    session.commit()
    return RedirectResponse(f"/portal/{ticket.number}", status_code=303)


@router.post("/{number}/required-by")
def set_my_required_by(
    number: int,
    required_by: Optional[str] = Form(None, max_length=20),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The requester sets, changes or clears the Required by date until the ticket is triaged."""
    ticket = _get_own_ticket(session, number, user)
    if not may_set_required_by(user, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="The Required by date can no longer be changed here; ask the agent")
    try:
        set_required_by(session, ticket, parse_required_by(required_by), user.email)
    except RequiredByError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    return RedirectResponse(f"/portal/{ticket.number}", status_code=303)


@router.post("/{number}/withdraw")
def withdraw_my_request(
    number: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The signed-in requester withdraws their request while it waits for approval. Followers cannot."""
    ticket = _get_own_ticket(session, number, user)
    try:
        withdraw(session, ticket, user.email)
    except ApprovalError:
        session.rollback()  # decided or closed since the page was drawn: show them the ticket as it is now
        return RedirectResponse(f"/portal/{ticket.number}", status_code=303)
    session.commit()
    return RedirectResponse(f"/portal/{ticket.number}", status_code=303)


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
    except NeverFollowListed as listed:
        raise HTTPException(status_code=400, detail=listed.message())
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

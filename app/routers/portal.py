from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import func
from sqlmodel import Session, select

from app.audit import log_action
from app.config import settings
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
    APPROVED,
    MAX_COMMENT_LENGTH,
    PENDING,
    SELF_APPROVED_COMMENT,
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
from app.forms import MAX_MESSAGE_LENGTH, form_answers, optional_int
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
from app.attachments import files_by_message
from app import reply_files
from app.storage import Storage, get_storage
from app.messages import (
    about_labels,
    attach_files,
    it_support_label,
    record_inbound,
    requester_kind,
    requester_last_update,
    requester_status,
    requester_steps,
    requester_visible_answers,
    requester_visible_messages,
)
from app.follower_updates import queue_follower_updates
from app.reopening import ReopenError, latest_follow_on, open_follow_on_ticket, related_tickets, reopen_ticket
from app.reply_channel import PORTAL, note_requester_wrote
from app.splitting import apply_requester_reply
from app.request_questions import answer_rows, form_rows
from app.request_types import PROBLEM_TITLE, active_request_types, approval_notice, portal_cards, portal_tiles, request_type_of
from app.required_by import RequiredByError, offers_required_by, parse_required_by, set_required_by
from app.models import FINISHED_STATUSES, Category, MessageDirection, RequestType, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.ticket_service import get_by_number
from app import identity
from app.identity import is_follower_of, is_requester, my_tickets_query
from app.pagination import paginate
from app.reply_channel import AGENT
from app.settings_store import INBOUND_MAX_ATTACHMENTS, REQUESTER_UPLOAD_TOTAL_MB, count_setting
from app.time_zones import format_moment, zone_for_user

router = APIRouter(prefix="/portal", tags=["portal"])

OPEN_TAB, FINISHED_TAB = "open", "finished"


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


def _upload_limits(session: Session) -> dict:
    """What a requester may add to a reply or a new request, for the page to say beside the Add a file button."""
    return {
        "max_files": count_setting(session, INBOUND_MAX_ATTACHMENTS),
        "max_file_mb": settings.attachment_max_bytes // (1024 * 1024),
        "max_total_mb": count_setting(session, REQUESTER_UPLOAD_TOTAL_MB),
    }


def _read_files(session: Session, user: User, files: list) -> list:
    """A person's uploaded files, checked: the rules of every stored file for anyone, plus the total-size limit for a
    requester (an agent, replying or raising, is not held to the total). The one place a portal route reads files."""
    read = reply_files.read_uploads if is_agent(user) else reply_files.read_requester_uploads
    return read(session, files)


def _upload_hint(session: Session, user: User) -> dict:
    return {**_upload_limits(session), "show_total": not is_agent(user)}


def _render_new_form(request: Request, user: User, session: Session, chosen_type_id: Optional[int] = None) -> HTMLResponse:
    types = active_request_types(session)
    chosen = next((t for t in types if t.id == chosen_type_id), None)  # a card's kind; one no longer offered is ignored
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
            "cards": portal_cards(session),
            "teams_bot_on": settings.teams_bot_configured,
            "chosen": chosen,
            "chosen_title": chosen.name if chosen else PROBLEM_TITLE,
            "question_rows": form_rows(session, chosen.id) if chosen else [],
            "offers_required_by": chosen is not None and chosen.offers_required_by,
            "approval_notice": approval_notice(session, chosen, user) if chosen else None,
            **_upload_hint(session, user),
        },
    )


def _my_pending_approval(session: Session, ticket: Ticket, user: User):
    """This user's unanswered approval of the ticket, while it is still waiting."""
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        return None
    vote = vote_for(session, ticket, user)
    return vote if vote is not None and vote.decision == PENDING else None


def _self_approved(session: Session, ticket: Ticket, user: User) -> bool:
    """The requester's own approval was recorded automatically (the request type allows it, #167)."""
    vote = vote_for(session, ticket, user) if _is_requester(ticket, user) else None
    return vote is not None and vote.decision == APPROVED and vote.comment == SELF_APPROVED_COMMENT


def _approval_rule(session: Session, ticket: Ticket):
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        return None
    approval = current_approval(session, ticket)
    return describe_rule(approval) if approval is not None and approval.status == PENDING else None


def _conversation(
    session: Session, ticket: Ticket, user: User, emails: set[str], visible: list, files: dict, is_requester_view: bool,
) -> list[dict]:
    """The request page's messages, newest first: who wrote each in the viewer's words ("You", "Sam from IT Support", "Our
    support partner"). Only the messages given (`requester_visible_messages`) and their downloadable files."""
    authors = {
        u.id: u for u in session.exec(select(User).where(User.id.in_({m.author_user_id for m in visible if m.author_user_id}))).all()
    } if any(m.author_user_id for m in visible) else {}
    rows = []
    for message in reversed(visible):
        sender = (message.sender or "").strip()
        if message.direction == MessageDirection.OUTBOUND:
            author = authors.get(message.author_user_id)
            is_it = True
            if message.msp_forward:
                who = "Our support partner"  # never who at the partner wrote
            else:
                who = it_support_label(author.display_name if author is not None else None)
            initial = "IT"
        else:
            is_it = False
            # The person's own words: sent by them, from one of their addresses, or logged for them by an agent (the
            # request's first message, which "How" in About this request says was raised for them).
            mine = (
                message.author_user_id == user.id or sender.lower() in emails or sender.lower() == user.display_name.lower()
                or (message.channel == AGENT and is_requester_view)
            )
            who = "You" if mine else sender or "Someone"
            initial = user.display_name if mine else sender
        rows.append({
            "message": message, "who": who, "initials": initial, "is_it": is_it, "msp": bool(message.msp_forward),
            "files": files.get(message.id, []),
        })
    return rows


def _render_ticket(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    visible = requester_visible_messages(session, ticket)
    mine = _is_requester(ticket, user)
    emails = identity.requester_emails(user)
    zone = zone_for_user(session, user)
    status = requester_status(session, ticket, zone, can_reopen=mine, is_requester=mine)
    request_info = request_type_of(session, ticket)
    assignee = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    # The files an agent attached to an email (open from the portal, as the Teams message says) and the ones this person
    # added to a reply: downloaded through /attachments, which checks the same rule again (visibility.may_view_attachment).
    message_files = {
        message_id: [f for f in files if not f.is_inline]
        for message_id, files in files_by_message(session, user, access_context(session, user), visible).items()
    }
    return templates.TemplateResponse(
        request,
        "portal_ticket_detail.html",
        {
            "ticket": ticket,
            "thread": _conversation(session, ticket, user, emails, visible, message_files, mine),
            "about": about_labels(session, visible),
            "status": status,
            "kind": requester_kind(ticket, request_info[0].name if request_info else None),
            "steps": requester_steps(ticket, status, viewer_is_requester=mine),
            "raised_at": format_moment(ticket.created_at, zone),
            "source_label": source_label(session, ticket),
            "handled_by": it_support_label(assignee.display_name if assignee is not None else None),
            # Without the reopen window, which has its own card
            "last_update": requester_last_update(
                session, ticket, zone, emails, can_reopen=False, viewer_id=user.id, status=replace(status, reopen_note=""),
            ),
            **_upload_hint(session, user),
            "request_info": request_info,
            "answers": requester_visible_answers(session, ticket),
            "my_approval": _my_pending_approval(session, ticket, user),
            "is_approver": vote_for(session, ticket, user) is not None,
            "approval_rule": _approval_rule(session, ticket),
            "self_approved": _self_approved(session, ticket, user),
            # Only the requester sees who else is copied; a follower must not learn
            # the other followers' addresses, and internal followers are never shown.
            "followers": (
                [f for f in followers_of(session, ticket) if not f.is_internal]
                if _is_requester(ticket, user)
                else []
            ),
            "is_requester": _is_requester(ticket, user),
            "can_set_required_by": (offers_required_by(session, ticket) or ticket.required_by is not None)
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
    tab: str = OPEN_TAB,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """My requests: the tiles for the kinds of request people can raise, then the person's requests (raised or followed,
    by any of their addresses) in Open and Finished tabs, each with its status in plain words and a one-line last update."""
    finished = tab == FINISHED_TAB
    query = my_tickets_query(user)
    in_finished = Ticket.status.in_(FINISHED_STATUSES)
    done, total = session.exec(
        select(func.count().filter(in_finished), func.count()).where(Ticket.id.in_(query.with_only_columns(Ticket.id).order_by(None)))
    ).one()
    counts = {OPEN_TAB: total - done, FINISHED_TAB: done}
    tickets_page = paginate(
        session,
        query.where(Ticket.status.in_(FINISHED_STATUSES) if finished else Ticket.status.notin_(FINISHED_STATUSES)),
        page,
        request.query_params,
    )
    emails = identity.requester_emails(user)
    zone = zone_for_user(session, user)
    kinds = {t.id: t.name for t in session.exec(select(RequestType).where(RequestType.id.in_({t.request_type_id for t in tickets_page.items if t.request_type_id}))).all()}
    rows = []
    for ticket in tickets_page.items:
        mine = ticket.requester_email.strip().lower() in emails
        status = requester_status(session, ticket, zone, can_reopen=mine, is_requester=mine)
        rows.append({
            "ticket": ticket,
            "kind": requester_kind(ticket, kinds.get(ticket.request_type_id)),
            "status": status,
            "last_update": requester_last_update(session, ticket, zone, emails, can_reopen=mine, viewer_id=user.id, status=status),
            "following": not mine,
            "can_reopen": mine and ticket.status == TicketStatus.RESOLVED,
        })
    return templates.TemplateResponse(
        request,
        "portal_tickets_list.html",
        {
            "page": tickets_page,
            "rows": rows,
            "tab": FINISHED_TAB if finished else OPEN_TAB,
            "counts": counts,
            "tiles": portal_tiles(session),
            "first_name": (user.display_name.split() or [user.email])[0],
            "teams_bot_on": settings.teams_bot_configured,
            "waiting_on_me": count_pending_for(session, user),
            "user": user,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_ticket_form(
    request: Request,
    request_type_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The raise form. `request_type_id` (a quick-start tile on My requests) chooses the kind of request in advance."""
    return _render_new_form(request, user, session, optional_int(request_type_id))


@router.get("/questions", response_class=HTMLResponse)
def request_questions(
    request: Request,
    request_type_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The form questions of the chosen kind of request, swapped into the raise form when the choice changes."""
    chosen = optional_int(request_type_id)
    request_type = session.get(RequestType, chosen) if chosen is not None else None
    rows = form_rows(session, request_type.id) if request_type is not None and request_type.is_active else []
    return templates.TemplateResponse(
        request, "_request_questions.html", {"div_id": "request-questions", "rows": rows, "oob": False}
    )


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
    answers: dict[int, str] = Depends(form_answers),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """Raise a ticket for yourself, or (agents) for someone else, with files if the person added any (stored on the first
    message, by the same rules as a reply's files). An agent can classify and assign it in the same step and say when it was
    reported and resolved."""
    if len(description.strip()) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"The description can be at most {MAX_MESSAGE_LENGTH} characters")
    try:
        # Before anything is written, so a refusal leaves nothing. An agent is not held to the requester's total.
        new_files = _read_files(session, user, files)
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))
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
            zone = zone_for_user(session, user)  # the form is shown and read in the agent's own time zone
            moments = (parse_moment(reported_at, zone), parse_moment(resolved_at, zone))
            day = parse_required_by(required_by)
        except (ValueError, BackdateError, RequiredByError) as error:
            raise RaiseError(
                str(error) if isinstance(error, (BackdateError, RequiredByError)) else "Choose impact and urgency from the lists"
            )
        ticket = raise_ticket(
            session, user, subject=subject, description=description, for_email=for_email,
            ticket_type=ticket_type, category=chosen_category, impact=level_impact, urgency=level_urgency,
            assignee=assignee, reported_at=moments[0], resolved_at=moments[1], request_type=chosen_request_type,
            resolution_notes=resolution_notes, required_by=day, answers=answers, files=new_files, storage=storage,
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
    zone = zone_for_user(session, user)
    cards = []
    for vote, ticket in pending_for(session, user):
        asker = identity.person_for_address(session, ticket.requester_email)
        approval = current_approval(session, ticket)
        cards.append({
            "ticket": ticket,
            "asked_by": asker.display_name if asker is not None and asker.display_name else ticket.requester_email,
            "asked_by_email": ticket.requester_email,
            "asked_at": vote.created_at,
            "answers": answer_rows(session, ticket),
            "rule": describe_rule(approval) if approval is not None else None,
            "kind": (request_type_of(session, ticket) or (None, None))[0],
        })
    return templates.TemplateResponse(
        request, "portal_approvals.html", {"cards": cards, "max_comment_length": MAX_COMMENT_LENGTH, "zone": zone, "user": user}
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
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """A reply on the request page, with files if the person added any. The files meet the rules of every stored file (the
    agent-side upload path: reply_files.read_uploads, then messages.attach_files), are visible to the agents who can read the
    ticket and to the people who can read the message, and the message is recorded with channel=portal."""
    ticket = _get_commentable_ticket(session, number, user)
    try:
        new_files = _read_files(session, user, files)  # before anything is written, so a refusal leaves nothing
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))

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
        reply = record_inbound(session, follow_on, sender=user.display_name, body=body, author_user_id=user.id, channel=PORTAL)
        attach_files(session, follow_on, reply, user, new_files, storage)
        log_action(session, user.email, "ticket", follow_on.id, "commented", body[:200])
        session.commit()
        destination = f"/portal/{follow_on.number}"
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": destination})
        return RedirectResponse(destination, status_code=303)

    message = record_inbound(
        session, ticket, sender=user.display_name, body=body, author_user_id=user.id, reply_by=user.email, channel=PORTAL,
    )
    attach_files(session, ticket, message, user, new_files, storage)
    note_requester_wrote(session, ticket, user.email, PORTAL)
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
    """The requester sets, changes or clears the Required by date at any time while the request is open."""
    ticket = _get_own_ticket(session, number, user)
    if not may_set_required_by(user, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="The Required by date can no longer be changed here")
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

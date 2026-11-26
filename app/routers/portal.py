from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
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
from app.backdating import FORMAT as MOMENT_FORMAT
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
from app.visibility import ASSIGNEE_ROLES, Access, access_context, may_set_required_by, ticket_access
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
from app.request_types import (
    PROBLEM_TITLE, active_request_types, approval_notice, is_offered, portal_cards, portal_tiles, request_type_of,
)
from app import people_directory, portal_mailbox
from app.mailbox_changes import MailboxChangeError, cancel_by_requester, changes_for_ticket, stop_now
from app.portal_mailbox import AccessForm, ConfirmAccess, ConfirmNoCopy, ForwardingForm, MailboxFormError, TargetRefused
from app.rate_limit import PEOPLE_SEARCH, rate_limited
from app.required_by import RequiredByError, offers_required_by, parse_required_by, set_required_by
from app.models import FINISHED_STATUSES, Category, MessageDirection, RequestType, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.ticket_service import get_by_number
from app import identity
from app.identity import is_follower_of, is_requester, my_tickets_query
from app.pagination import paginate
from app.reply_channel import AGENT
from app.settings_store import INBOUND_MAX_ATTACHMENTS, MAILBOX_CHANGE_MAX_DAYS, REQUESTER_UPLOAD_TOTAL_MB, count_setting
from app.time_zones import MomentError, format_moment, parse_local_moment, zone_for_user

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


def _render_new_form(
    request: Request, user: User, session: Session, chosen_type_id: Optional[int] = None,
    mailbox_form: Optional[ForwardingForm] = None, form_error: str = "", confirm_text: str = "", status_code: int = 200,
) -> HTMLResponse:
    types = active_request_types(session)
    chosen = next((t for t in types if t.id == chosen_type_id), None)  # a card's kind; one no longer offered is ignored
    agent = is_agent(user)
    mailbox_mode = ""  # "forwarding" or "access": that kind's own form; "unavailable": a kind with a mailbox action that cannot be used now
    if chosen is not None and chosen.mailbox_action:
        if not is_offered(chosen):
            mailbox_mode = "unavailable"
        else:
            mailbox_mode = "access" if chosen.mailbox_action in portal_mailbox.ACCESS_ACTIONS else "forwarding"
        if mailbox_mode == "access" and mailbox_form is None:
            # The box the kind is named for starts ticked; the person can tick the other as well.
            mailbox_form = AccessForm(full_access=chosen.mailbox_action == "full_access", send_as=chosen.mailbox_action == "send_as")
    return templates.TemplateResponse(
        request,
        "portal_new_ticket.html",
        {
            "mailbox_mode": mailbox_mode,
            "mailbox_form": mailbox_form or ForwardingForm(),
            "mailbox_error": form_error,
            "mailbox_confirm_text": confirm_text,
            "mailbox_zone": zone_for_user(session, user),
            "quarter_times": portal_mailbox.QUARTER_TIMES,
            "mailbox_max_days": count_setting(session, MAILBOX_CHANGE_MAX_DAYS),
            "mailbox_not_available": portal_mailbox.ACCESS_NOT_AVAILABLE if (
                chosen is not None and chosen.mailbox_action in portal_mailbox.ACCESS_ACTIONS
            ) else portal_mailbox.NOT_AVAILABLE,
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
        status_code=status_code,
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
            "mailboxes": portal_mailbox.views(session, ticket, user) if mine else [],
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


@router.get("/people", response_class=HTMLResponse, dependencies=[Depends(rate_limited(PEOPLE_SEARCH))])
def find_people(
    request: Request,
    q: str = Query("", max_length=100),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The people picker (#309): up to ten people matching what was typed (at least two characters), as a list of buttons the
    form's script turns into a choice. Only enabled members with a mail address, never the person themselves. The object id
    in each button is what the form stores; the server fetches it again on submit."""
    error = ""
    try:
        found = people_directory.search(session, user, q)
    except people_directory.DirectoryError as problem:
        found, error = [], str(problem)
    return templates.TemplateResponse(
        request, "_people_results.html",
        {"people": found, "error": error, "too_short": len(q.strip()) < people_directory.MIN_QUERY_LENGTH},
    )


@router.post("/mailbox-change")
def request_mailbox_change(
    request: Request,
    request_type_id: str = Form(..., max_length=20),
    target_object_id: str = Form("", max_length=100),
    target_name: str = Form("", max_length=200),
    keep_copy: Optional[str] = Form(None, max_length=10),
    start_mode: str = Form(portal_mailbox.NOW, max_length=10),
    start_date: str = Form("", max_length=20),
    start_time: str = Form("", max_length=10),
    end_date: str = Form("", max_length=20),
    end_time: str = Form("", max_length=10),
    confirm_no_copy: Optional[str] = Form(None, max_length=10),
    full_access: Optional[str] = Form(None, max_length=10),
    send_as: Optional[str] = Form(None, max_length=10),
    confirm_access: Optional[str] = Form(None, max_length=10),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ask for the signed-in person's OWN mail to be forwarded (#309), or for someone to be given Full access and/or Send As on it
    (#310). The mailbox is never a field: it is whoever is signed in, and a posted mailbox is ignored. The other person is only ever
    the object id the people picker stored, fetched again here. The kind of request decides which form this is."""
    type_id = optional_int(request_type_id)
    chosen = session.get(RequestType, type_id) if type_id is not None else None
    access = chosen is not None and chosen.mailbox_action in portal_mailbox.ACCESS_ACTIONS
    if chosen is None or not chosen.is_active or not (access or chosen.mailbox_action == "forwarding"):
        raise HTTPException(status_code=400, detail="Choose a kind of request from the list")
    common = dict(
        target_object_id=target_object_id.strip(), target_name=target_name.strip(), keep_copy=keep_copy == "on",
        start_mode=portal_mailbox.LATER if start_mode == portal_mailbox.LATER else portal_mailbox.NOW,
        start_date=start_date, start_time=start_time, end_date=end_date, end_time=end_time, confirm_no_copy=confirm_no_copy == "1",
    )
    form = (
        AccessForm(**common, full_access=full_access == "on", send_as=send_as == "on", confirm_access=confirm_access == "1")
        if access else ForwardingForm(**common)
    )
    try:
        if access:
            ticket = portal_mailbox.submit_access(session, user, chosen, form)
        else:
            ticket = portal_mailbox.submit_forwarding(session, user, chosen, form)
        log_action(session, user.email, "ticket", ticket.id, "raised", ticket.source.value)
        session.flush()
    except (ConfirmNoCopy, ConfirmAccess) as warning:
        session.rollback()
        return _render_new_form(request, user, session, chosen.id, form, confirm_text=str(warning), status_code=200)
    except MailboxFormError as error:
        session.rollback()
        if isinstance(error, TargetRefused):
            form.target_object_id = form.target_name = ""  # asked to choose again
        return _render_new_form(request, user, session, chosen.id, form, form_error=str(error), status_code=400)
    session.commit()
    return RedirectResponse(f"/portal/{ticket.number}", status_code=303)


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
        if chosen_request_type is not None and chosen_request_type.mailbox_action:
            # Only its own form makes the change; raised here it would be a ticket with nothing to do.
            raise RaiseError("This kind of request has its own form: choose it from New request")
        chosen_agent = optional_int(agent_id)
        assignee = session.get(User, chosen_agent) if chosen_agent is not None else None
        if chosen_agent is not None and (assignee is None or assignee.role not in ASSIGNEE_ROLES):
            raise RaiseError("Choose an agent from the list")
        try:
            level_impact = Level(impact) if impact else None
            level_urgency = Level(urgency) if urgency else None
            zone = zone_for_user(session, user)  # the form is shown and read in the agent's own time zone
            moments = (parse_local_moment(reported_at, zone), parse_local_moment(resolved_at, zone))
            day = parse_required_by(required_by)
        except (ValueError, MomentError, RequiredByError) as error:
            raise RaiseError(
                str(error) if isinstance(error, (MomentError, RequiredByError)) else "Choose impact and urgency from the lists"
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


def _own_mailbox_change(session: Session, number: int, user: User, change_id: Optional[str] = None):
    """The ticket and one mailbox change it made, for the person whose mailbox it is; 404 for anyone else, any other ticket or a
    change that belongs to another ticket. A request with several changes (#310) says which in `change_id`; without it, a request
    with one change means that one."""
    ticket = _get_own_ticket(session, number, user)
    mine = identity.requester_emails(user)
    found = [c for c in changes_for_ticket(session, ticket) if c.owner_address in mine]
    wanted = optional_int(change_id)
    if wanted is not None:
        found = [c for c in found if c.id == wanted]
    elif len(found) > 1:
        found = []
    if len(found) != 1:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket, found[0]


@router.post("/{number}/mailbox/cancel")
def cancel_my_mailbox_change(
    number: int,
    change_id: Optional[str] = Form(None, max_length=20),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Cancel the change before it starts (#309). The page asked first (a confirm step); once the start has come it is refused."""
    ticket, change = _own_mailbox_change(session, number, user, change_id)
    try:
        cancel_by_requester(session, change, user.email)
    except MailboxChangeError:
        session.rollback()  # started since the page was drawn: show it as it is now
        return RedirectResponse(f"/portal/{ticket.number}", status_code=303)
    session.commit()
    return RedirectResponse(f"/portal/{ticket.number}", status_code=303)


@router.post("/{number}/mailbox/stop")
def stop_my_mailbox_change(
    number: int,
    change_id: Optional[str] = Form(None, max_length=20),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Stop forwarding now (#309): the end becomes now and the worker's next run removes it."""
    ticket, change = _own_mailbox_change(session, number, user, change_id)
    try:
        stop_now(session, change, user.email)
    except MailboxChangeError:
        session.rollback()
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

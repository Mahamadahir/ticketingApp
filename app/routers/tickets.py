from datetime import datetime, timezone
from html import escape
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlmodel import Session, select

from app.audit import log_action
from app.auth import require_roles
from app.database import get_session
from app.feedback import feedback_for_ticket
from app.forms import MAX_MESSAGE_LENGTH, answers_from_pairs, form_answers, optional_int
from app.followers import (
    MAX_EXTERNAL_FOLLOWERS,
    InvalidEmail,
    NeverFollowListed,
    TooManyFollowers,
    add_follower,
    copyable_followers,
    followers_of,
    remove_follower,
)
from app.backdating import FORMAT as MOMENT_FORMAT, BackdateError, backdate_ticket, may_backdate, parse_moment
from app.categories import CategoryError, create_category, is_selectable, path_of, selectable, team_id_for
from app.msp import MspError, compose as compose_msp, check_can_send, hold_ticket, may_hold, release_hold, send_to_msp
from app.escalation import EscalationError, escalate_ticket as escalate, escalation_target, escalators_of
from app.raising import source_label
from app.request_questions import QuestionError, answer_rows, form_rows, save_answers
from app.request_types import active_request_types, request_type_of
from app.subrequests import TemplateError, after_triage as make_subrequests, answers_changed_note, pending_subjects, preview as preview_subrequests
from app.required_by import (
    RequiredByError,
    apply_form_dates,
    offers_required_by,
    parse_required_by,
    parse_due,
    prepare_scheduled,
    set_due,
    set_required_by,
)
from app.settings_store import REQUIRED_BY_LEAD_DAYS, count_setting
from app.canned import active_canned, fill as fill_canned
from app.approvals import ApprovalError, current_approval, describe_rule, votes_of, waive
from app.notifications import AWAITING_YOU, notify_agent_emailed, notify_assigned, notify_mentions
from app.presence import agent_label
from app.search import normalise, search_condition
from app.splitting import (
    max_sub_tickets,
    SplitError,
    all_sub_tickets_finished,
    check_manual_status,
    has_sub_tickets,
    split_ticket,
    sub_tickets_summary,
)
from app.linking import PARENT_TYPES, LinkError, link_ticket, linked_summary, unlink_ticket
from app.messages import (
    BadStatusChoice,
    EmptyEmail,
    UnknownRecipient,
    add_note,
    agent_timeline,
    email_requester,
    chosen_status,
    default_status_after_email,
    is_triaged,
    preview_email,
    status_choices,
)
from app.models import FINISHED_STATUSES, CannedResponse, Category, RequestType, Team, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.pagination import paginate
from app.team_activity import KIND_LABELS, Kind, describe as describe_activity, feed_query
from app.sla import (
    impact_options,
    urgency_options,
    Level,
    at_risk_percent,
    describe_priority,
    queue_order,
    set_impact_urgency,
    sla_view,
)
from app.reopening import (
    MAX_REASON_LENGTH,
    RECLOSE_REASONS,
    ReopenError,
    check_can_reclose,
    check_can_reopen,
    reclose_ticket,
    related_tickets,
    reopen_ticket,
)
from app.closing import CloseError, check_can_close, close_ticket, preview_closing
from app.resolution import ResolveError, check_can_resolve, preview_resolution, resolve_ticket
from app.ticket_service import AwaitingApproval, ClosingNeedsNotes, EVENT_ASSIGNED, NotTriaged, ResolutionRequired, ScheduledNeedsDate, find_ticket, get_by_number, record_event, set_status
from app.time_zones import zone_for_email, zone_for_user
from app.triage import TriageError, reassign_ticket, triage_ticket
from app.visibility import (
    Access,
    NoTicketAccess,
    assignable_agents,
    may_add_note,
    may_self_assign,
    may_set_due,
    may_edit_answers,
    may_set_required_by,
    may_triage,
    team_members,
    access_context,
    require_access,
    ticket_access,
    visible_ticket_condition,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])
# Shareable links live outside /tickets: /t/1042 (the number; a prefix such as INC-1042 is accepted).
reference_router = APIRouter(tags=["tickets"])

AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)



def _list_link(q: str, source: str = ""):
    """Builds a link to the ticket list that keeps the current search; a filter passed in
    replaces the same filter (false, empty and None leave it out)."""

    def link(**params) -> str:
        wanted = {"q": q, "source": source, **params}
        return "/tickets" + ("?" + urlencode({k: ("true" if v is True else v) for k, v in wanted.items() if v}) if any(wanted.values()) else "")

    return link


@router.get("", response_class=HTMLResponse)
def list_tickets(
    request: Request,
    mine: bool = False,
    team_id: Optional[int] = None,
    q: str = "",
    source: Optional[TicketSource] = None,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    # Untriaged first, then by time to breach, then priority (see sla.queue_order).
    query = select(Ticket).order_by(*queue_order(count_setting(session, REQUIRED_BY_LEAD_DAYS)))

    # Search only narrows what the agent can already see, so visibility is applied first.
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is not None:
        query = query.where(visible)
    q = normalise(q)
    matching = search_condition(session, q)
    if matching is not None:
        query = query.where(matching)
    if team_id is not None:
        query = query.where(Ticket.team_id == team_id)
    if source is not None:
        query = query.where(Ticket.source == source)

    if mine:
        query = query.where(Ticket.assigned_agent_id == user.id)

    tickets_page = paginate(session, query, page, request.query_params)
    teams_by_id = {team.id: team for team in session.exec(select(Team)).all()}
    agents_by_id = {agent.id: agent for agent in session.exec(select(User).where(User.role == UserRole.AGENT)).all()}
    teams = session.exec(select(Team)).all() if user.role == UserRole.SUPER_ADMIN else []
    now = datetime.now(timezone.utc)
    threshold = at_risk_percent(session)

    return templates.TemplateResponse(
        request,
        "tickets_list.html",
        {
            "page": tickets_page,
            "sla": {t.id: sla_view(t, now, threshold) for t in tickets_page.items},
            "teams_by_id": teams_by_id,
            "agents_by_id": agents_by_id,
            "teams": teams,
            "user": user,
            "mine": mine,
            "team_id": team_id,
            "q": q,
            "source": source.value if source else "",
            "sources": [s.value for s in TicketSource],
            "list_link": _list_link(q, source.value if source else ""),
        },
    )


@router.get("/activity", response_class=HTMLResponse)
def team_activity(
    request: Request,
    team_id: Optional[str] = None,
    kind: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Everything that happened on the tickets this agent can open, newest first: ticket
    events, emails and replies, and internal notes. Access is the ticket list's, so a
    ticket moved to another team leaves the feed of everyone outside that team."""
    # The filter form sends a blank value for "all", which is no filter, not an error.
    try:
        team_choice = optional_int(team_id)
        kind_choice = Kind(kind) if kind and kind.strip() else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown team or kind")
    levels = access_context(session, user)
    feed = paginate(
        session, feed_query(visible_ticket_condition(user, levels), team_choice, kind_choice), page,
        request.query_params,
    )
    ids = {row.ticket_id for row in feed.items}
    tickets = {t.id: t for t in session.exec(select(Ticket).where(Ticket.id.in_(ids))).all()} if ids else {}
    all_teams = session.exec(select(Team).order_by(Team.name)).all()
    teams = all_teams if user.role == UserRole.SUPER_ADMIN else [t for t in all_teams if t.id in levels]
    return templates.TemplateResponse(
        request,
        "tickets_activity.html",
        {
            "page": feed,
            "entries": [(row, tickets.get(row.ticket_id), describe_activity(row)) for row in feed.items],
            "teams": teams,
            "teams_by_id": {t.id: t for t in all_teams},
            "team_id": team_choice,
            "kind": kind_choice.value if kind_choice else "",
            "kinds": [(k.value, label) for k, label in KIND_LABELS.items()],
            "user": user,
        },
    )


def _get_ticket(session: Session, number: int, user: User, needed: Access = Access.WORK) -> Ticket:
    ticket = get_by_number(session, number)
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


@router.get("/triage-subrequests", response_class=HTMLResponse)
def triage_subrequests(
    request: Request,
    number: int,
    request_type_id: Optional[str] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The sub-requests list of the triage form (#118): the templates of the chosen kind of request (or the ticket's own
    when "Leave as is"), each with its subject and ticked when the rules produce it for the answers typed so far, or as
    the agent left it. Redrawn whenever the form changes, so a changed answer changes the list."""
    ticket = _get_ticket(session, number, user, Access.READ)
    chosen = optional_int(request_type_id)
    type_id = chosen if chosen is not None else ticket.request_type_id
    pairs = request.query_params.multi_items()
    ticked = [int(v) for k, v in pairs if k == "subrequest_template" and v.isdecimal()]
    listed = [int(v) for k, v in pairs if k == "subrequest_listed" and v.isdecimal()]
    rows = preview_subrequests(session, ticket, type_id, answers_from_pairs(pairs), listed, ticked) if ticket.triaged_at is None else []
    return templates.TemplateResponse(request, "_triage_subrequests.html", {"rows": rows, "ticket": ticket, "max_sub_tickets": max_sub_tickets(session)})


@router.get("/triage-dates", response_class=HTMLResponse)
def triage_dates(
    request: Request,
    request_type_id: Optional[str] = None,
    required_by: str = "",
    due_on: str = "",
    due_reason: str = "",
    has_due: bool = False,
    impact: str = "",
    urgency: str = "",
    category_id: str = "",
    number: Optional[int] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The Required by and Due boxes of the triage form for the chosen kind of request, for its htmx call:
    Required by only when the kind offers it, Due always. What was typed is kept."""
    chosen = optional_int(request_type_id)
    request_type = session.get(RequestType, chosen) if chosen is not None else None
    prefill = _type_defaults(session, request_type, impact, urgency, category_id) if request_type is not None else None
    # The kind's form questions go with it; "Leave as is" shows the ticket's own kind with the answers it has.
    ticket = _get_ticket(session, number, user, Access.READ) if number is not None else None
    type_id = request_type.id if request_type is not None else (ticket.request_type_id if ticket is not None else None)
    question_rows = form_rows(session, type_id, ticket if ticket is not None and type_id == ticket.request_type_id else None)
    return _triage_dates(
        request, request_type is not None and request_type.offers_required_by, required_by, due_on, due_reason, has_due,
        prefill, question_rows,
    )


def _type_defaults(
    session: Session, request_type: RequestType, impact: str = "", urgency: str = "", category_id: str = ""
) -> Optional[dict]:
    """What choosing this kind of request fills in on the triage form (#133): the impact, urgency and category the
    type has defaults for, but only into a box the agent has not already filled in (the form is sent with the call).
    A category that has since been switched off is not offered. None when there is nothing to fill in."""
    category = session.get(Category, request_type.default_category_id) if request_type.default_category_id else None
    if category is not None and (category_id.strip() or not is_selectable(session, category)):
        category = None
    default_impact = None if impact.strip() else request_type.default_impact
    default_urgency = None if urgency.strip() else request_type.default_urgency
    if not (default_impact or default_urgency or category):
        return None
    team_id = team_id_for(session, category) if category is not None else None
    return {
        "impact": default_impact, "urgency": default_urgency,
        "category_id": category.id if category is not None else None,
        "impact_options": impact_options(),
        "urgency_options": urgency_options(),
        "category_options": selectable(session), "members": team_members(session, team_id),
    }


def _triage_dates(
    request: Request, offers: bool, required_by: str, due_on: str, due_reason: str, has_due: bool,
    prefill: Optional[dict] = None, question_rows: Optional[list] = None,
):
    return templates.TemplateResponse(
        request,
        "_triage_dates.html",
        {
            "offers": offers, "required_by": required_by, "due_on": due_on, "due_reason": due_reason,
            "has_due": has_due, "prefill": prefill, "question_rows": question_rows,
        },
    )


@router.post("/{number}/triage", response_class=HTMLResponse)
def triage(
    number: int,
    request: Request,
    ticket_type: TicketType = Form(...),
    impact: Level = Form(...),
    urgency: Level = Form(...),
    category_id: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
    new_category_name: Optional[str] = Form(None, max_length=200),
    new_category_parent_id: Optional[str] = Form(None),
    new_category_team_id: Optional[str] = Form(None),
    request_type_id: Optional[str] = Form(None),
    required_by: Optional[str] = Form(None, max_length=20),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    answers: dict[int, str] = Depends(form_answers),
    subrequest_template: list[int] = Form(default=[]),
    subrequest_listed: list[int] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Classifies a New ticket: type and category (which picks the team), and
    optionally assigns it. Assignment is not required; the team can pick it up."""
    ticket = _get_ticket(session, number, user, Access.READ)
    # Lock the row and re-read it, so two people triaging at once cannot both pass
    # the check below: the second waits, then sees it is already triaged.
    session.refresh(ticket, with_for_update=True)
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

        chosen_type = optional_int(request_type_id)
        request_type = session.get(RequestType, chosen_type) if chosen_type is not None else None
        if chosen_type is not None and request_type is None:
            raise HTTPException(status_code=400, detail="Choose a kind of request from the list")
        triage_ticket(
            session, ticket, user, ticket_type=ticket_type, category=category,
            impact=impact, urgency=urgency, assignee=assignee, request_type=request_type, answers=answers,
        )
        # The dates after triage, as it may have changed the kind of request; the boxes are optional.
        apply_form_dates(session, ticket, user.email, required_by, due_on, due_reason)
        # The sub-requests the agent confirmed (or, with no list shown, everything the rules produce), after the dates
        # as their Due follows Required by. Made now, or once approved.
        make_subrequests(session, ticket, user, subrequest_template, subrequest_listed)
    except (CategoryError, TriageError, RequiredByError, TemplateError) as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))

    log_action(session, user.email, "ticket", ticket.id, "triaged", path_of(session, category))
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/priority", response_class=HTMLResponse)
def change_priority(
    number: int,
    request: Request,
    impact: Level = Form(...),
    urgency: Level = Form(...),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Re-sets impact and urgency on a triaged ticket the agent works. The priority
    follows, and the SLA deadlines are worked out again from the new targets."""
    ticket = _get_ticket(session, number, user)
    if ticket.triaged_at is None:
        raise HTTPException(status_code=400, detail="Triage the ticket first; triage sets its priority")
    if ticket.status in FINISHED_STATUSES:
        # A finished ticket's SLA result must not be rewritable after the fact.
        raise HTTPException(status_code=400, detail="A resolved or closed ticket's priority can no longer be changed")
    if set_impact_urgency(session, ticket, impact, urgency, user.email):
        ticket.updated_at = datetime.now(timezone.utc)
        log_action(session, user.email, "ticket", ticket.id, "priority_changed", describe_priority(ticket))
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/backdate", response_class=HTMLResponse)
def backdate(
    number: int,
    request: Request,
    reported_at: Optional[str] = Form(None, max_length=40),
    resolved_at: Optional[str] = Form(None, max_length=40),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Enter when the problem was reported and/or fixed, for a ticket logged after the
    fact. Up to 30 days back, never in the future. Blank leaves a date as it is."""
    ticket = _get_ticket(session, number, user)
    zone = zone_for_user(session, user)  # the form is shown and read in the agent's own time zone
    try:
        changes = backdate_ticket(
            session, ticket, user.email,
            reported_at=parse_moment(reported_at, zone), resolved_at=parse_moment(resolved_at, zone),
        )
    except BackdateError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changes:
        # What changed, from and to, so the audit log shows what was altered and not just the result.
        detail = "; ".join(
            f"{what} {before:%Y-%m-%d %H:%M} -> {after:%Y-%m-%d %H:%M} UTC" if before else f"{what} -> {after:%Y-%m-%d %H:%M} UTC"
            for what, before, after in changes
        )
        log_action(session, user.email, "ticket", ticket.id, "backdated", detail)
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/answers", response_class=HTMLResponse)
def change_answers(
    number: int,
    request: Request,
    answers: dict[int, str] = Depends(form_answers),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An agent who works the ticket changes the answers to its request type's form questions. Each change is a
    ticket event and an audit entry; nothing else follows from it (it only marks the answers as changed, for the
    automatic sub-requests of #118 part 2)."""
    ticket = _get_ticket(session, number, user, Access.READ)
    session.refresh(ticket, with_for_update=True)  # two saves at once must not both insert the same answer
    if not may_edit_answers(user, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="You cannot change the answers on this ticket")
    try:
        changed = save_answers(session, ticket, answers, user.email)
    except QuestionError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changed:
        log_action(session, user.email, "ticket", ticket.id, "answers_changed", "; ".join(changed))
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/required-by", response_class=HTMLResponse)
def change_required_by(
    number: int,
    request: Request,
    required_by: Optional[str] = Form(None, max_length=20),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An agent sets, changes or clears a service request's Required by date. A past date is
    only accepted on a ticket that has been backdated."""
    ticket = _get_ticket(session, number, user, Access.READ)
    if not may_set_required_by(user, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="You cannot change the Required by date of this ticket")
    try:
        changed = set_required_by(
            session, ticket, parse_required_by(required_by), user.email, backdating=ticket.is_backdated
        )
    except RequiredByError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changed:
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/due", response_class=HTMLResponse)
def change_due(
    number: int,
    request: Request,
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An agent who works the ticket sets or moves its Due date. Moving one needs a reason (a ticket event and an
    audit entry record it); a blank date puts it back to the default from Required by."""
    ticket = _get_ticket(session, number, user, Access.READ)
    if not may_set_due(user, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="You cannot change the Due date of this ticket")
    try:
        changed = set_due(session, ticket, parse_due(due_on), due_reason, user.email)
    except RequiredByError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changed:
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/reassign", response_class=HTMLResponse)
def reassign(
    number: int,
    request: Request,
    team_id: int = Form(...),
    agent_id: Optional[str] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Choose a team, then an agent in it or leave the ticket unassigned there.
    No reason is needed; this is not an escalation."""
    ticket = _get_ticket(session, number, user)
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

    return _render_ticket_detail(request, ticket, user, session)


@router.get("/{number}", response_class=HTMLResponse)
def view_ticket(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    return _render_ticket_detail(request, _get_ticket(session, number, user, Access.READ), user, session)


@reference_router.get("/t/{number}", response_class=HTMLResponse)
def view_ticket_by_number(
    number: str,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Shareable link, e.g. /t/1042. A prefix is accepted and ignored (/t/INC-1042), so
    old links and email subjects still work after a ticket's type changed."""
    ticket = find_ticket(session, number)
    if ticket is None:
        raise HTTPException(status_code=404, detail="No ticket with that number")
    # One canonical address: /tickets/1042 (which also checks access).
    return RedirectResponse(f"/tickets/{ticket.number}", status_code=303)


@router.get("/{number}/canned", response_class=HTMLResponse)
def fill_from_canned(
    number: int,
    request: Request,
    canned_id: Optional[str] = None,
    body: str = "",
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The email box with a canned response added to what the agent has typed so far.
    Only fills the box; the agent edits and sends it through the normal email action."""
    ticket = _get_ticket(session, number, user)
    if not is_triaged(ticket):
        raise HTTPException(status_code=400, detail="Triage the ticket before emailing the requester")
    body = body[:MAX_MESSAGE_LENGTH]
    chosen = optional_int(canned_id)
    if chosen is not None:
        canned = session.get(CannedResponse, chosen)
        if canned is None or not canned.is_active:
            raise HTTPException(status_code=404, detail="That canned response is not available")
        text = fill_canned(canned, ticket, user)
        body = f"{body.rstrip()}\n\n{text}".strip()
        if len(body) > MAX_MESSAGE_LENGTH:
            raise HTTPException(status_code=400, detail="That would make the email too long; shorten what you have written first")
    return templates.TemplateResponse(request, "_email_body.html", {"body": body})


def _ask_for_scheduled_date(
    session: Session, ticket: Ticket, user: User, due_on: Optional[str], due_reason: Optional[str]
) -> None:
    """Every way of choosing Scheduled asks for its date (the Due date) first (app/required_by.py)."""
    try:
        prepare_scheduled(session, ticket, due_on, due_reason, user.email)
    except RequiredByError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))


def _check_scheduled_date_given(ticket: Ticket, status_after: Optional[TicketStatus], due_on: Optional[str]) -> None:
    """The preview's own check, so it refuses what the send would: Scheduled with no date anywhere."""
    if status_after == TicketStatus.SCHEDULED and not (due_on or "").strip() and ticket.due_on is None:
        raise HTTPException(status_code=400, detail="Scheduled needs a date: enter the Due date")


def _set_status_from_message(
    session: Session, ticket: Ticket, user: User, new_status: Optional[TicketStatus],
    due_on: Optional[str] = None, due_reason: Optional[str] = None,
) -> None:
    """Applies the status the agent picked on the Email user or Add note form (None: no change).
    set_status still has the last word (an untriaged or waiting-for-approval ticket is refused)."""
    if new_status is None:
        return
    if new_status == TicketStatus.SCHEDULED:
        _ask_for_scheduled_date(session, ticket, user, due_on, due_reason)
    previous_status = ticket.status
    try:
        changed = set_status(session, ticket, new_status, user.email)
    except ScheduledNeedsDate:
        raise HTTPException(status_code=400, detail="Scheduled needs a date: enter the Due date")
    except (NotTriaged, AwaitingApproval, ResolutionRequired):
        raise HTTPException(status_code=400, detail="That status cannot be set here")
    if changed:
        log_action(
            session, user.email, "ticket", ticket.id, "status_changed",
            f"{previous_status.value} -> {new_status.value}",
        )


@router.post("/{number}/email/preview", response_class=HTMLResponse)
def preview_email_user(
    number: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    next_status: Optional[str] = Form(None),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The "are you sure?" step before an email goes to the requester: who it goes to, who is
    copied, the subject and the text, and what it does to the ticket. Sends and records nothing.
    The checks are the send's own, so what is previewed is what would go."""
    ticket = _get_ticket(session, number, user)
    try:
        preview = preview_email(session, ticket, body, cc_emails=cc if cc_form else None, next_status=next_status)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before emailing the requester")
    except EmptyEmail:
        raise HTTPException(status_code=400, detail="Write the email first")
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    except BadStatusChoice as error:
        raise HTTPException(status_code=400, detail=str(error))
    _check_scheduled_date_given(ticket, preview.status_after, due_on)
    return templates.TemplateResponse(
        request,
        "_email_preview.html",
        {
            "ticket": ticket,
            "preview": preview,
            "cc_form": bool(cc_form),
            "ticked": cc,
            "next_status": next_status,
            "due_on": due_on,
            "due_reason": due_reason,
        },
    )


@router.post("/{number}/email", response_class=HTMLResponse)
def email_user(
    number: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    next_status: Optional[str] = Form(None),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Send the requester a message, copying the followers the agent left ticked.
    Not available until the ticket is triaged. "cc_form" is sent by the page's form
    so that ticking nobody means copying nobody; a request without it copies every
    follower. "next_status" is the status the agent picked in the form (Awaiting user unless they
    chose otherwise); a request without it gets the same default."""
    ticket = _get_ticket(session, number, user)

    try:
        email_requester(session, ticket, user, body, cc_emails=cc if cc_form else None, bell=False)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before emailing the requester")
    except EmptyEmail:
        raise HTTPException(status_code=400, detail="Write the email first")
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")

    # Checked after the message, like the preview, so both give the same error for the same input;
    # a refusal here leaves nothing behind, since the request is not committed.
    try:
        new_status = chosen_status(session, ticket, next_status, default_status_after_email(ticket))
    except BadStatusChoice as error:
        raise HTTPException(status_code=400, detail=str(error))

    status_before = ticket.status
    _set_status_from_message(session, ticket, user, new_status, due_on, due_reason)
    # The requester's bell (#130): when this moved the ticket to Awaiting user, "waiting for your reply" says an agent
    # wrote, so whoever has that on gets only it.
    moved_to_awaiting = status_before != TicketStatus.AWAITING_USER and ticket.status == TicketStatus.AWAITING_USER
    notify_agent_emailed(session, ticket, user.email, instead_of=AWAITING_YOU if moved_to_awaiting else None)
    log_action(session, user.email, "ticket", ticket.id, "emailed_requester", body[:200])
    session.commit()
    session.refresh(ticket)

    # The email itself was queued by email_requester (app/outbox.py) in the transaction above; the
    # worker's mail-out job sends it (app/mail.py).

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/note", response_class=HTMLResponse)
def add_ticket_note(
    number: int,
    request: Request,
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    next_status: Optional[str] = Form(None),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An internal note, visible to agents only. Allowed at any stage, including
    on an untriaged ticket the agent has not claimed, and never triages it. The agent may also pick
    the ticket's next status; left alone, a note never changes it."""
    ticket = _get_ticket(session, number, user, Access.READ)
    levels = access_context(session, user)
    if not may_add_note(user, ticket, levels):
        raise NoTicketAccess()
    try:
        new_status = chosen_status(session, ticket, next_status, ticket.status)
    except BadStatusChoice as error:
        raise HTTPException(status_code=400, detail=str(error))
    if new_status is not None:
        require_access(user, ticket, levels, Access.WORK)  # changing status needs more than reading

    if new_status == TicketStatus.SCHEDULED:
        _ask_for_scheduled_date(session, ticket, user, due_on, due_reason)  # before the note, so a refusal leaves nothing
    note = add_note(session, ticket, user, body)
    log_action(session, user.email, "ticket", ticket.id, "note_added", body[:200])
    _set_status_from_message(session, ticket, user, new_status, due_on, due_reason)
    notify_mentions(session, ticket, note, user)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)

@router.post("/{number}/status", response_class=HTMLResponse)
def change_status(
    number: int,
    request: Request,
    status: TicketStatus = Form(...),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    if status == TicketStatus.NEW:
        raise HTTPException(status_code=400, detail="A ticket only starts in New; it leaves New by being triaged")
    if (
        status == TicketStatus.SCHEDULED and ticket.status != TicketStatus.SCHEDULED and ticket.triaged_at is not None
        and not has_sub_tickets(session, ticket)  # an original follows its parts: check_manual_status says so below
    ):
        # Scheduled is a plan for a day: ask for it first (a reopen to Scheduled included).
        _ask_for_scheduled_date(session, ticket, user, due_on, due_reason)

    if ticket.status == TicketStatus.RESOLVED and status not in FINISHED_STATUSES and not has_sub_tickets(session, ticket):
        # Taking a resolved ticket back to an open status is a reopen: counted, recorded with a reason
        # and the assignee is told (app/reopening.py).
        try:
            reopen_ticket(session, ticket, user.email, "Status set by hand", status=status, tell_assignee=True)
        except ReopenError as error:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(error))
        session.commit()
        session.refresh(ticket)
        return _render_ticket_detail(request, ticket, user, session)
    try:
        check_manual_status(session, ticket, status)
    except SplitError as error:
        raise HTTPException(status_code=400, detail=str(error))
    previous_status = ticket.status
    if (
        status == TicketStatus.CLOSED
        and previous_status not in (TicketStatus.RESOLVED, TicketStatus.AWAITING_APPROVAL, TicketStatus.CLOSED)
        and ticket.triaged_at is not None
    ):
        # Closing directly needs closing notes and emails the requester, with a preview first (#131).
        raise HTTPException(status_code=400, detail="Use the Close action: it needs closing notes and emails the requester")
    try:
        changed = set_status(session, ticket, status, user.email)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before changing its status")
    except ClosingNeedsNotes:
        raise HTTPException(status_code=400, detail="Closing a ticket needs resolution notes")
    except AwaitingApproval:
        raise HTTPException(status_code=400, detail="This request is waiting for its approvers; its status changes when they decide")
    except ResolutionRequired:
        raise HTTPException(status_code=400, detail="Use the Resolve action to resolve a ticket: it needs resolution notes")
    except ScheduledNeedsDate:
        raise HTTPException(status_code=400, detail="Scheduled needs a date: enter the Due date")
    if changed:
        log_action(session, user.email, "ticket", ticket.id, "status_changed", f"{previous_status.value} -> {status.value}")
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/resolve/preview", response_class=HTMLResponse)
def preview_resolve(
    number: int,
    request: Request,
    resolution_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The "are you sure?" step before resolving: the notes, the resolution email (who gets it, who
    is copied, the subject and the text) and what resolving does. Records and sends nothing."""
    ticket = _get_ticket(session, number, user)
    try:
        preview = preview_resolution(session, ticket, resolution_notes, cc_emails=cc if cc_form else None)
    except ResolveError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    return templates.TemplateResponse(
        request,
        "_resolve_preview.html",
        {
            "ticket": ticket,
            "preview": preview,
            "notes": resolution_notes.strip(),
            "cc_form": bool(cc_form),
            "ticked": cc,
        },
    )


@router.post("/{number}/resolve", response_class=HTMLResponse)
def resolve(
    number: int,
    request: Request,
    resolution_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Resolve the ticket with notes (required). Emails the requester, copying the followers the
    agent left ticked, through app/resolution.py."""
    ticket = _get_ticket(session, number, user)
    try:
        resolve_ticket(session, ticket, user, resolution_notes, cc_emails=cc if cc_form else None)
    except ResolveError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    except UnknownRecipient as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/close/preview", response_class=HTMLResponse)
def preview_close(
    number: int,
    request: Request,
    closing_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The "are you sure?" step before closing a ticket that was not resolved: the notes and the closed email
    (who gets it, who is copied, the subject and the text). Records and sends nothing."""
    ticket = _get_ticket(session, number, user)
    try:
        preview = preview_closing(session, ticket, closing_notes, cc_emails=cc if cc_form else None)
    except CloseError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    return templates.TemplateResponse(
        request,
        "_close_preview.html",
        {"ticket": ticket, "preview": preview, "notes": closing_notes.strip(), "cc_form": bool(cc_form), "ticked": cc},
    )


@router.post("/{number}/close", response_class=HTMLResponse)
def close(
    number: int,
    request: Request,
    closing_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Close a ticket that was not resolved, with notes (required). Emails the requester, copying the
    followers the agent left ticked, through app/closing.py."""
    ticket = _get_ticket(session, number, user)
    try:
        close_ticket(session, ticket, user, closing_notes, cc_emails=cc if cc_form else None)
    except CloseError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    except UnknownRecipient as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/reopen", response_class=HTMLResponse)
def reopen(
    number: int,
    request: Request,
    reason: str = Form("", max_length=MAX_REASON_LENGTH + 500),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Reopen a resolved ticket, with the reason. It goes back to In progress (Open if nobody is assigned)
    and the assigned agent is told, through app/reopening.py."""
    ticket = _get_ticket(session, number, user)
    try:
        reopen_ticket(session, ticket, user.email, reason)
    except ReopenError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/reclose", response_class=HTMLResponse)
def reclose(
    number: int,
    request: Request,
    reason: str = Form("", max_length=100),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Close a reopened ticket again with one of the fixed reasons (said thanks, duplicate, raised
    separately, reopened in error)."""
    ticket = _get_ticket(session, number, user)
    try:
        reclose_ticket(session, ticket, user.email, reason)
    except ReopenError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/approval/waive", response_class=HTMLResponse)
def waive_approval(
    number: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: Session = Depends(get_session),
):
    """A super admin lets a request through without waiting for its remaining approvers (one has
    left, say). Agents cannot: approval is a control, so skipping it is a super admin's call."""
    ticket = _get_ticket(session, number, user)
    try:
        waive(session, ticket, user)
    except ApprovalError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, user.email, "ticket", ticket.id, "approval_waived", ticket.tag)
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/assign", response_class=HTMLResponse)
def assign_ticket(
    number: int,
    request: Request,
    agent_id: Optional[int] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user, Access.READ)

    if user.role == UserRole.AGENT:
        levels = access_context(session, user)
        if not may_self_assign(user, ticket, levels):
            if ticket_access(user, ticket, levels) >= Access.WORK and ticket.assigned_agent_id is not None:
                raise HTTPException(status_code=400, detail="This ticket is assigned to someone else. Use Re-assign.")
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
    notify_assigned(session, ticket, target_agent, user.email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/escalate", response_class=HTMLResponse)
def escalate_ticket(
    number: int,
    request: Request,
    reason: str = Form(..., max_length=MAX_MESSAGE_LENGTH),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Sends a triaged ticket to the escalation target of its category. The agent does
    not pick the team (re-assign is for that). Needs a reason."""
    ticket = _get_ticket(session, number, user)
    try:
        escalate(session, ticket, user, reason)
    except EscalationError as error:
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/send-to-msp/preview", response_class=HTMLResponse)
def preview_send_to_msp(
    number: int,
    request: Request,
    reason: str = Form("", max_length=MAX_MESSAGE_LENGTH),
    scope: str = Form("thread", max_length=20),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The "are you sure?" step before sending a ticket to the MSP: the email exactly as it will go, who is copied,
    what the requester is told, and what sending does. Records and sends nothing."""
    ticket = _get_ticket(session, number, user)
    if not reason.strip():
        raise HTTPException(status_code=400, detail="Say why it is being sent to the MSP")
    try:
        preview = compose_msp(session, ticket, scope=scope, reason=reason, actor=user)
    except MspError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return templates.TemplateResponse(
        request, "_msp_preview.html", {"ticket": ticket, "preview": preview, "reason": reason.strip()}
    )


@router.post("/{number}/send-to-msp", response_class=HTMLResponse)
def send_ticket_to_msp(
    number: int,
    request: Request,
    reason: str = Form("", max_length=MAX_MESSAGE_LENGTH),
    scope: str = Form("thread", max_length=20),
    digest: Optional[str] = Form(None, max_length=64),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Sends the ticket to the MSP (the same action as escalating to an MSP-target category), with a reason.
    The ticket stays in its team, With MSP."""
    ticket = _get_ticket(session, number, user)
    try:
        send_to_msp(session, ticket, user, reason, scope, expect_digest=digest)
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/msp-hold", response_class=HTMLResponse)
def msp_hold(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The assigned agent keeps the ticket out of the automatic MSP handoff until it is released."""
    ticket = _get_ticket(session, number, user)
    try:
        hold_ticket(session, ticket, user)
    except MspError as error:
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/msp-hold/release", response_class=HTMLResponse)
def msp_hold_release(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    try:
        release_hold(session, ticket, user)
    except MspError as error:
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/link", response_class=HTMLResponse)
def link(
    number: int,
    request: Request,
    parent_number: str = Form(..., max_length=40),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Links this ticket under a Major Incident or Problem, given by number (1042, or INC-1042). The agent
    works this ticket and must be able to see the parent."""
    ticket = _get_ticket(session, number, user)
    parent = find_ticket(session, parent_number)
    # The same answer whether it does not exist or is not visible to this agent, so
    # references cannot be probed for tickets they may not see.
    if parent is None or ticket_access(user, parent, access_context(session, user)) < Access.READ:
        raise HTTPException(status_code=400, detail="No ticket with that number")
    try:
        link_ticket(session, ticket, parent, user.email)
    except LinkError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    ticket.updated_at = datetime.now(timezone.utc)
    log_action(session, user.email, "ticket", ticket.id, "linked", parent.tag)
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/unlink", response_class=HTMLResponse)
def unlink(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    parent = unlink_ticket(session, ticket, user.email)
    if parent is not None:
        ticket.updated_at = datetime.now(timezone.utc)
        log_action(session, user.email, "ticket", ticket.id, "unlinked", parent.tag)
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


SPLIT_SLOTS = 4  # part boxes on the form; blank subjects are skipped, and a ticket can be split again to add more


@router.post("/{number}/split", response_class=HTMLResponse)
def split(
    number: int,
    request: Request,
    subject: list[str] = Form(default=[]),
    details: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Splits this ticket into sub-tickets, one per part with a subject. Each is triaged
    like the original and can then be amended or assigned on its own page."""
    ticket = _get_ticket(session, number, user)
    details = details + [""] * (len(subject) - len(details))
    parts = [(s, d) for s, d in zip(subject, details) if s.strip() or d.strip()]
    if len(parts) > max_sub_tickets(session) or any(len(s) > 200 or len(d) > 5000 for s, d in parts):
        raise HTTPException(status_code=400, detail="Too many parts, or a subject or details that is too long")
    try:
        made = split_ticket(session, ticket, user, parts)
    except SplitError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, user.email, "ticket", ticket.id, "split", ", ".join(sub.tag for sub in made))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/followers", response_class=HTMLResponse)
def add_ticket_follower(
    number: int,
    request: Request,
    email: str = Form(..., max_length=254),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Copy someone on this ticket's emails. They see it under Following in the
    portal if they have an account."""
    ticket = _get_ticket(session, number, user)
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

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/followers/{follower_id}/remove", response_class=HTMLResponse)
def remove_ticket_follower(
    number: int,
    follower_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    removed_email = remove_follower(session, ticket, follower_id, user.email)
    if removed_email is None:
        raise HTTPException(status_code=404, detail="Follower not found")

    log_action(session, user.email, "ticket", ticket.id, "follower_removed", removed_email)
    session.commit()
    session.refresh(ticket)

    return _render_ticket_detail(request, ticket, user, session)


def _may(check, refusal, *args) -> bool:
    """Whether a service's own check passes (it raises `refusal` when not), so the page and the action agree."""
    try:
        check(*args)
    except refusal:
        return False
    return True


def _may_resolve(session: Session, ticket: Ticket) -> bool:
    """Whether to offer the Resolve form."""
    return _may(check_can_resolve, ResolveError, session, ticket)


def _may_close(session: Session, ticket: Ticket) -> bool:
    """Whether to offer the Close form (a ticket that is not resolved)."""
    return _may(check_can_close, CloseError, session, ticket)


def _render_ticket_detail(request: Request, ticket: Ticket, user: User, session: Session) -> HTMLResponse:
    ticket_approval = current_approval(session, ticket)
    ticket_votes = votes_of(session, ticket_approval)
    team = session.get(Team, ticket.team_id) if ticket.team_id else None
    assigned_agent = session.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
    levels = access_context(session, user)
    if ticket_access(user, ticket, levels) < Access.READ:
        # What the agent just did moved the ticket (triage, re-assign or escalate to
        # another team), and access follows the team, so it is no longer theirs to open.
        where = f" to {escape(team.name)}" if team else ""
        return HTMLResponse(
            f'<p class="note">{escape(ticket.reference)} has been moved{where}. '
            'You no longer have access to it. <a href="/tickets">Back to the list</a></p>'
        )
    team_agents = assignable_agents(session, ticket)
    can_work = ticket_access(user, ticket, levels) >= Access.WORK
    can_self_assign = may_self_assign(user, ticket, levels)
    can_claim = not can_work and can_self_assign
    can_triage = may_triage(user, ticket, levels)
    can_edit_answers = may_edit_answers(user, ticket, levels)
    can_reassign = can_work and not can_triage and ticket.triaged_at is not None
    category = session.get(Category, ticket.category_id) if ticket.category_id else None
    options = selectable(session) if can_triage else []
    teams = session.exec(select(Team)).all()
    escalation_team, escalation_blocked, escalation_is_msp = None, None, False
    if can_work:
        try:
            escalation_team = escalation_target(session, ticket)
            escalation_is_msp = escalation_team is None
        except EscalationError as error:
            escalation_blocked = str(error)
    # Send to MSP is offered on any ticket the MSP could take (not only one whose category escalates there).
    msp_blocked = None
    if can_work:
        try:
            check_can_send(session, ticket)
        except MspError as error:
            msp_blocked = str(error)
    timeline = agent_timeline(session, ticket, viewer=(user, levels))

    # A link is only shown to someone who could open the other ticket anyway.
    parent = session.get(Ticket, ticket.linked_parent_id) if ticket.linked_parent_id else None
    parent_visible = parent is not None and ticket_access(user, parent, levels) >= Access.READ
    linked, linked_total, linked_visible = (
        linked_summary(session, ticket, visible_ticket_condition(user, levels))
        if ticket.type in PARENT_TYPES
        else ([], 0, 0)
    )

    # The same for split: only the original or sub-tickets this viewer could open anyway.
    original = session.get(Ticket, ticket.split_from_id) if ticket.split_from_id else None
    original_visible = original is not None and ticket_access(user, original, levels) >= Access.READ
    # A sub-ticket never has sub-tickets of its own, so skip the queries for it.
    subs, subs_total, subs_visible = (
        ([], 0, 0)
        if ticket.split_from_id is not None
        else sub_tickets_summary(session, ticket, visible_ticket_condition(user, levels))
    )

    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "ticket": ticket,
            "original": original if original_visible else None,
            "has_hidden_original": original is not None and not original_visible,
            "sub_tickets": subs,
            "status_follows_parts": subs_total > 0,
            "all_parts_done": ticket.status not in FINISHED_STATUSES and all_sub_tickets_finished(session, ticket),
            "hidden_sub_count": subs_total - subs_visible,
            "more_sub_count": subs_visible - len(subs),
            "can_split": can_work
            and ticket.triaged_at is not None
            and ticket.split_from_id is None
            and ticket.status not in FINISHED_STATUSES,
            "split_slots": range(SPLIT_SLOTS),
            "team": team,
            "assigned_agent": assigned_agent,
            "team_agents": team_agents,
            "teams": teams,
            "escalation_team": escalation_team,
            "escalation_blocked": escalation_blocked,
            "escalation_is_msp": escalation_is_msp,
            "msp_blocked": msp_blocked,
            "can_hold": can_work and may_hold(ticket, user),
            "can_release_hold": can_work and ticket.msp_hold,
            "timeline": timeline,
            "escalators": escalators_of(session, ticket),
            "followers": followers_of(session, ticket),
            # Who the Email user form offers to copy: everyone except the requester and any never-follow address.
            "cc_candidates": copyable_followers(session, ticket),
            "can_email": can_work and is_triaged(ticket),
            "canned": active_canned(session) if can_work and is_triaged(ticket) else [],
            "can_edit_followers": can_work,
            "can_note": may_add_note(user, ticket, levels),
            # The next-status dropdown on Email user and Add note (empty: not shown). Only for
            # someone who can work the ticket, since a status change needs that.
            "message_statuses": status_choices(session, ticket) if can_work else [],
            "email_default_status": default_status_after_email(ticket),
            # New is left only by triage, so it is not offered as a status to set.
            # Resolved is not a status to set: the Resolve action takes the notes.
            "statuses": [
                s for s in TicketStatus
                if s not in (TicketStatus.NEW, TicketStatus.AWAITING_APPROVAL)
                and (s != TicketStatus.RESOLVED or ticket.status == TicketStatus.RESOLVED)
            ],
            "feedback": feedback_for_ticket(session, user, ticket),
            "resolved_by": session.get(User, ticket.resolved_by_id) if ticket.resolved_by_id else None,
            "can_resolve": can_work and _may_resolve(session, ticket),
            "can_close": can_work and _may_close(session, ticket),
            "can_reopen": can_work and _may(check_can_reopen, ReopenError, ticket),
            "can_reclose": can_work and _may(check_can_reclose, ReopenError, session, ticket),
            "reclose_reasons": RECLOSE_REASONS,
            "max_reason_length": MAX_REASON_LENGTH,
            # Tickets linked by a reply after closure, only those this viewer could open anyway.
            "related": [
                (other, which) for other, which in related_tickets(session, ticket)
                if ticket_access(user, other, levels) >= Access.READ
            ],
            "can_triage": can_triage,
            "can_self_assign": can_self_assign,
            "ticket_types": list(TicketType),
            "parent": parent if parent_visible else None,
            "has_hidden_parent": parent is not None and not parent_visible,
            "linked": linked,
            "hidden_linked_count": linked_total - linked_visible,
            "more_linked_count": linked_visible - len(linked),
            "can_link": can_work and ticket.triaged_at is not None and linked_total == 0,
            "impact_options": impact_options(),
            "urgency_options": urgency_options(),
            "sla": sla_view(ticket, datetime.now(timezone.utc), at_risk_percent(session)),
            "moment_format": MOMENT_FORMAT,
            "can_backdate": may_backdate(ticket),
            "can_set_required_by": (offers_required_by(session, ticket) or ticket.required_by is not None)
            and may_set_required_by(user, ticket, levels),
            "can_set_due": may_set_due(user, ticket, levels),
            "due_on": ticket.due_on,
            "request_types": active_request_types(session) if can_triage else [],
            "offers_required_by": offers_required_by(session, ticket),
            "requester_zone": zone_for_email(session, ticket.requester_email),
            "source_label": source_label(session, ticket),
            "request_info": request_type_of(session, ticket),
            "answers": answer_rows(session, ticket),
            "triage_questions": form_rows(session, ticket.request_type_id, ticket) if can_triage else [],
            "triage_subrequests": preview_subrequests(session, ticket, ticket.request_type_id, {}, [], []) if can_triage else [],
            "max_sub_tickets": max_sub_tickets(session),
            "pending_subrequests": pending_subjects(ticket),
            "answers_changed_after_subrequests": answers_changed_note(session, ticket),
            "edit_questions": form_rows(session, ticket.request_type_id, ticket) if can_edit_answers else [],
            "can_edit_answers": can_edit_answers,
            "answers_changed_at": ticket.answers_changed_at,
            "approvals": ticket_votes,
            "approval_rule": describe_rule(ticket_approval) if ticket_approval else None,
            "approval_status": ticket_approval.status if ticket_approval else None,
            "can_waive": user.role == UserRole.SUPER_ADMIN and ticket.status == TicketStatus.AWAITING_APPROVAL,
            "can_set_priority": can_work and ticket.triaged_at is not None and ticket.status not in FINISHED_STATUSES,
            "category_options": options,
            "main_categories": [c for c, _ in options if c.parent_id is None and not c.is_other],
            "category_path": path_of(session, category) if category else None,
            "can_create_category": user.role == UserRole.SUPER_ADMIN,
            "can_reassign": can_reassign,
            "reassign_agents": team_members(session, ticket.team_id) if can_reassign else [],
            "can_work": can_work,
            "can_claim": can_claim,
            "user": user,
        },
    )

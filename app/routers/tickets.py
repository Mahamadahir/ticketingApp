import functools
import logging
from enum import Enum
from datetime import datetime, timezone
from html import escape
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from app.templating import is_htmx, templates
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app import reply_files
from app.audit import log_action
from app import ticket_presence
from app.auth import get_current_user_passively, require_roles
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
from app.backdating import FORMAT as MOMENT_FORMAT, backdate_ticket, may_backdate
from app.categories import CategoryError, create_category, is_selectable, path_of, selectable, team_id_for
from app.msp import (
    MspError,
    check_can_send,
    compose as compose_msp,
    compose_forward,
    dismiss_forward,
    forward_message,
    hold_ticket,
    may_hold,
    pending_forwards,
    release_hold,
    send_to_msp,
)
from app.msp_actions import (
    OUTCOME_CLOSED,
    OUTCOME_RESOLVED,
    OUTCOME_TAKEN_BACK,
    check_can_take_back,
    compose_release,
    linked_to_mark,
    linked_with_msp_through,
    send_with_linked,
    take_back as take_back_from_msp,
)
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
from app.settings_store import REQUIRED_BY_LEAD_DAYS, SPLIT_EMAIL_ENABLED, count_setting, get_flag
from app.canned import active_canned, fill as fill_canned
from app.approvals import SUPERSEDED, ApprovalError, approval_group_of, current_approval, describe_rule, is_waiting_for_approvers, votes_of, waive
from app.notifications import AWAITING_YOU, mentionable_agents, notify_agent_emailed, notify_assigned, notify_mentions
from app.identity import person_for_address, requester_emails
from app.presence import agent_label
from app.ticket_actions import CARDS, action_chips
from app import mailbox_changes, portal_mailbox
from app.search import normalise, search_condition
from app.splitting import (
    MAX_DETAILS,
    MAX_SUBJECT,
    PREFILLED_PARTS,
    max_sub_tickets,
    prefilled_parts,
    SplitError,
    all_sub_tickets_finished,
    check_manual_status,
    has_sub_tickets,
    split_ticket,
    sub_ticket_count,
    sub_tickets_summary,
)
from app.linking import PARENT_TYPES, LinkError, link_ticket, linked_summary, unlink_ticket
from app.storage import Storage, get_storage
from app.messages import (
    BadStatusChoice,
    EmptyEmail,
    UnknownRecipient,
    add_note,
    add_visible_note,
    agent_timeline,
    delete_note,
    is_note,
    set_note_visible,
    undo_delete_note,
    NoteError,
    email_requester,
    chosen_status,
    default_status_after_email,
    excerpt,
    is_triaged,
    latest_message_id,
    posts_since,
    preview_email,
    status_choices,
)
from app.models import FINISHED_STATUSES, Attachment, CannedResponse, Category, Message, MessageDirection, RequestType, Team, Ticket, TicketSource, TicketStatus, TicketType, User, UserRole
from app.attachments import RenameRefused, files_by_message, picture_copies, rename_attachment, shown_name, ticket_files
from app.pagination import paginate
from app.status_display import AWAITING_ACKNOWLEDGED, NOT_FINISHED, status_options
from app.team_activity import (
    DRAWER_CARDS,
    KIND_LABELS,
    Day,
    Kind,
    count_steps_today,
    load_timeline,
    person_choices,
)
from app.sla import (
    impact_options,
    urgency_options,
    Level,
    at_risk_percent,
    describe_priority,
    breach_order,
    queue_order,
    header_sla,
    queue_sla,
    sla_rows,
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
from app.ticket_service import AwaitingApproval, ClosingNeedsNotes, EVENT_ASSIGNED, EVENT_ATTACHMENT_RENAMED, NotTriaged, ResolutionRequired, ScheduledNeedsDate, find_ticket, get_by_number, record_event, set_status
from app.time_zones import MomentError, parse_local_moment, zone_for_email, zone_for_user
from app.triage import TriageError, change_category, change_type, clean_reason, reassign_ticket, triage_ticket
from app.reactions import acknowledged_condition, acknowledged_views, dismiss_negative, open_negative_ids
from app.thread import build_thread
from app.ticket_emails import SendAnywayError, send_anyway
from app.visibility import (
    ASSIGNEE_ROLES,
    Access,
    NoTicketAccess,
    assignable_agents,
    may_add_note,
    may_manage_note,
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

log = logging.getLogger("app.tickets")
router = APIRouter(prefix="/tickets", tags=["tickets"])
# Shareable links live outside /tickets: /t/1042 (the number; a prefix such as INC-1042 is accepted).
reference_router = APIRouter(tags=["tickets"])

AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)



SORT_BREACH = "breach"  # ?sort=breach; blank is the queue order


def _list_link(q: str, source: str = "", status: str = "", sort: str = ""):
    """Builds a link to the ticket list that keeps the current search, status filter and sort; a filter passed in
    replaces the same filter (false, empty and None leave it out)."""

    def link(**params) -> str:
        wanted = {"q": q, "source": source, "status": status, "sort": sort, **params}
        return "/tickets" + ("?" + urlencode({k: ("true" if v is True else v) for k, v in wanted.items() if v}) if any(wanted.values()) else "")

    return link


def _person_id(value: Optional[str]) -> Optional[int]:
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and int(value) < 2**31 else None


def _count(session: Session, query) -> int:
    return session.exec(select(func.count()).select_from(query.subquery())).one()


def _queue_figures(session: Session, user: User, visible, now: datetime, threshold: int) -> dict:
    """The queue's header and tab counts. Every figure counts only tickets this person can see, with no search or
    filter applied, and each tab's count is the size of the list its link opens."""
    everything = select(Ticket.id)
    if visible is not None:
        everything = everything.where(visible)
    open_tickets = everything.where(Ticket.status.notin_(FINISHED_STATUSES))
    # Breached and at risk are the SLA view's own words for a ticket (app/sla.py), counted over open tickets that have a clock.
    clocked = select(Ticket).where(
        Ticket.status.notin_(FINISHED_STATUSES), Ticket.sla_paused_at.is_(None), Ticket.is_backdated.is_(False),
        or_(Ticket.first_response_due_at.is_not(None), Ticket.resolution_due_at.is_not(None)),
    )
    if visible is not None:
        clocked = clocked.where(visible)
    states = [sla_view(t, now, threshold).state for t in session.exec(clocked).all()]
    untriaged = _count(session, open_tickets.where(Ticket.triaged_at.is_(None)))
    return {
        "breached": states.count("breached"),
        "at_risk": states.count("at_risk"),
        "untriaged": untriaged,
        "tab_mine": _count(session, everything.where(Ticket.assigned_agent_id == user.id)),
        "tab_untriaged": untriaged,
        "tab_all": _count(session, everything),
    }


@router.get("", response_class=HTMLResponse)
def list_tickets(
    request: Request,
    mine: bool = False,
    untriaged: bool = False,
    team_id: Optional[str] = None,
    q: str = "",
    source: Optional[str] = None,
    status: Optional[str] = None,
    sort: Optional[str] = None,
    assignee: Optional[str] = None,
    requester: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    # The filter form sends a blank value for "any", which is no filter, not an error.
    status = (status or "").strip()
    sort = (sort or "").strip()
    try:
        team_id = optional_int(team_id)
        source = TicketSource(source) if source and source.strip() else None
        status_choice = None if not status else (status if status in (NOT_FINISHED, AWAITING_ACKNOWLEDGED) else TicketStatus(status))
        if sort not in ("", SORT_BREACH):
            raise ValueError(sort)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown team, channel, status or sort")
    lead_days = count_setting(session, REQUIRED_BY_LEAD_DAYS)
    # Default: untriaged first, then by time to breach, then priority (see sla.queue_order); "breach" sorts every
    # ticket by time to breach with no pinning (sla.breach_order).
    query = select(Ticket).order_by(*(breach_order(lead_days) if sort == SORT_BREACH else queue_order(lead_days)))

    # Search only narrows what the agent can already see, so visibility is applied first.
    levels = access_context(session, user)
    visible = visible_ticket_condition(user, levels)
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
    if status_choice == NOT_FINISHED:
        query = query.where(Ticket.status.notin_(FINISHED_STATUSES))
    elif status_choice == AWAITING_ACKNOWLEDGED:
        query = query.where(*acknowledged_condition())  # after visibility, like every other filter
    elif status_choice is not None:
        query = query.where(Ticket.status == status_choice)

    if mine:
        query = query.where(Ticket.assigned_agent_id == user.id)
    # Admin > Users links here (#322): one person's assigned tickets, or those raised under any of their addresses.
    # It only narrows what was already made visible above, and a value that names nobody is ignored.
    person_filters = []
    assignee_id, requester_id = _person_id(assignee), _person_id(requester)
    if assignee_id is not None and (assignee_user := session.get(User, assignee_id)) is not None:
        query = query.where(Ticket.assigned_agent_id == assignee_id)
        person_filters.append((f"assigned to {assignee_user.display_name}", "assignee", assignee_id))
    if requester_id is not None and (requester_user := session.get(User, requester_id)) is not None:
        query = query.where(func.lower(func.trim(Ticket.requester_email)).in_(requester_emails(requester_user, session)))
        person_filters.append((f"raised by {requester_user.display_name}", "requester", requester_id))
    if untriaged:  # the sidebar's Untriaged item (app/navigation.py counts the same set)
        query = query.where(Ticket.triaged_at.is_(None), Ticket.status.notin_(FINISHED_STATUSES))

    tickets_page = paginate(session, query, page, request.query_params)
    teams_by_id = {team.id: team for team in session.exec(select(Team)).all()}
    agents_by_id = {agent.id: agent for agent in session.exec(select(User).where(User.role.in_(AGENT_ROLES))).all()}
    teams = sorted(
        (t for t in teams_by_id.values() if user.role == UserRole.SUPER_ADMIN or t.id in levels), key=lambda t: t.name.lower()
    )
    now = datetime.now(timezone.utc)
    threshold = at_risk_percent(session)
    views = {t.id: sla_view(t, now, threshold) for t in tickets_page.items}
    figures = _queue_figures(session, user, visible, now, threshold)
    category_ids = {t.category_id for t in tickets_page.items if t.category_id}
    categories_by_id = (
        {c.id: path_of(session, c) for c in session.exec(select(Category).where(Category.id.in_(category_ids))).all()}
        if category_ids else {}
    )

    return templates.TemplateResponse(
        request,
        "tickets_list.html",
        {
            "page": tickets_page,
            "negative_ids": open_negative_ids(session, [t.id for t in tickets_page.items]),
            "acknowledged": acknowledged_views(session, tickets_page.items, now),
            "sla": {t.id: queue_sla(t, views[t.id], now) for t in tickets_page.items},
            "figures": figures,
            "categories_by_id": categories_by_id,
            "teams_by_id": teams_by_id,
            "agents_by_id": agents_by_id,
            "teams": teams,
            "user": user,
            "mine": mine,
            "untriaged": untriaged,
            "team_id": team_id,
            "q": q,
            "source": source.value if source else "",
            "sources": [s.value for s in TicketSource],
            "status": status,
            "status_options": status_options(),
            "sort": sort,
            "person_filters": person_filters,
            "list_link": _list_link(q, source.value if source else "", status, sort),
        },
    )


ACTIVITY_TABS = (("team", "Whole team"), ("mine", "My activity"))


def _activity_tab(tab: Optional[str]) -> str:
    if tab in (None, "", "team"):
        return "team"
    if tab == "mine":
        return "mine"
    raise HTTPException(status_code=422, detail="Unknown tab")


ACTIVITY_CHIPS = (("", "All"), (Kind.EVENTS.value, "Changes"), (Kind.CONVERSATION.value, "Emails"), (Kind.NOTES.value, "Notes"))


def _activity_filters(team_id: Optional[str], kind: Optional[str]):
    # The filter form sends a blank value for "all", which is no filter, not an error.
    try:
        return optional_int(team_id), (Kind(kind) if kind and kind.strip() else None)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown team or kind")


def _activity_before(before: Optional[str]) -> Optional[datetime]:
    """The "Show earlier" cursor: an ISO moment this feed wrote itself, so anything else is a bad request."""
    if not before:
        return None
    try:
        moment = datetime.fromisoformat(before)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown moment")
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@router.get("/activity", response_class=HTMLResponse)
def team_activity(
    request: Request,
    team_id: Optional[str] = None,
    kind: Optional[str] = None,
    person: Optional[str] = None,
    before: Optional[str] = None,
    tab: Optional[str] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """What happened on the tickets this agent can open, as cards (a session is one card; see app/team_activity.py),
    newest first under day headings. Access is the ticket list's, so a ticket moved to another team leaves the feed of
    everyone outside that team. "Show earlier" loads the next stretch of time (`before`), never splitting a card."""
    team_choice, kind_choice = _activity_filters(team_id, kind)
    tab_choice = _activity_tab(tab)
    me = user.email.lower()
    # "My activity" is the viewer's own steps; the Person filter is for the whole team.
    person_choice = me if tab_choice == "mine" else ((person or "").strip().lower() or None)
    levels = access_context(session, user)
    visible = visible_ticket_condition(user, levels)
    zone = zone_for_user(session, user)
    timeline = load_timeline(
        session, visible, zone, team_id=team_choice, kind=kind_choice, person=person_choice,
        before=_activity_before(before), viewer=me,
    )
    all_teams = session.exec(select(Team).order_by(Team.name)).all()
    teams = all_teams if user.role == UserRole.SUPER_ADMIN else [t for t in all_teams if t.id in levels]
    filters = {
        "team_id": team_choice or "", "kind": kind_choice.value if kind_choice else "",
        "person": "" if tab_choice == "mine" else (person_choice or ""), "tab": "mine" if tab_choice == "mine" else "",
    }
    tab_links = {  # the tabs keep the team and kind filters; the Person filter belongs to the whole team
        value: "/tickets/activity" + (
            "?" + urlencode(q) if (q := {k: v for k, v in {**filters, "person": "", "tab": "mine" if value == "mine" else ""}.items() if v}) else ""
        )
        for value, _label in ACTIVITY_TABS
    }
    latest = "/tickets/activity" + ("?" + urlencode({k: v for k, v in filters.items() if v}) if any(filters.values()) else "")
    earlier = "?" + urlencode({**{k: v for k, v in filters.items() if v}, "before": timeline.earlier.isoformat()}) if timeline.earlier else None
    return templates.TemplateResponse(
        request,
        "tickets_activity.html",
        {
            "timeline": timeline,
            "teams": teams,
            "people": person_choices(session, chosen=None if tab_choice == "mine" else person_choice),
            "team_id": team_choice,
            "kind": filters["kind"],
            "person": "" if tab_choice == "mine" else (person_choice or ""),
            "tab": tab_choice,
            "tabs": ACTIVITY_TABS,
            "tab_links": tab_links,
            "mine_count": count_steps_today(session, visible, me, zone),
            "kinds": [(k.value, label) for k, label in KIND_LABELS.items()],
            "earlier": earlier,
            "latest": latest if before else None,
            "user": user,
        },
    )


@router.get("/activity/latest", response_class=HTMLResponse)
def team_activity_latest(
    request: Request,
    kind: Optional[str] = None,
    tab: Optional[str] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The side drawer's body (htmx, loaded each time it opens): the tabs, the filter chips and about 15 of the latest
    cards, same access and grouping as the page. No frame, never cached, no refreshing on its own."""
    if not is_htmx(request):
        return RedirectResponse("/tickets/activity", status_code=303)  # only the drawer's body, never a page of its own
    _, kind_choice = _activity_filters(None, kind)
    tab_choice = _activity_tab(tab)
    me = user.email.lower()
    levels = access_context(session, user)
    visible = visible_ticket_condition(user, levels)
    zone = zone_for_user(session, user)
    timeline = load_timeline(
        session, visible, zone, kind=kind_choice, person=me if tab_choice == "mine" else None, viewer=me,
        steps=DRAWER_CARDS * 3, min_cards=DRAWER_CARDS,
    )
    days, shown = [], 0  # the first DRAWER_CARDS cards, still under their day headings
    for day in timeline.days:
        if shown >= DRAWER_CARDS:
            break
        days.append(Day(day.label, day.cards[: DRAWER_CARDS - shown]))
        shown += len(days[-1].cards)
    return templates.TemplateResponse(
        request, "tickets_activity_latest.html",
        {
            "days": days, "chips": ACTIVITY_CHIPS, "kind": kind_choice.value if kind_choice else "", "tab": tab_choice,
            "tabs": ACTIVITY_TABS, "mine_count": count_steps_today(session, visible, me, zone),
        },
        headers={"Cache-Control": "no-store"},
    )


def _get_ticket(session: Session, number: int, user: User, needed: Access = Access.WORK) -> Ticket:
    ticket = get_by_number(session, number)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    require_access(user, ticket, access_context(session, user), needed)
    return ticket


def card_errors(card: str):
    """A refused action shows its message inside its own card (#254), not at the top of the page. The route runs as before;
    when it refuses with a 400, 403 or 422 the page is drawn again with that card open, the message in the card and what was
    typed kept. It is the same for htmx (the page swaps into main) and a plain form post (the status stays the refusal's).
    If this ticket no longer offers the card (it was triaged meanwhile), the refusal goes out unchanged. What is refused, and
    why, is decided where it always was; this only decides where the message shows."""

    def wrap(route):
        @functools.wraps(route)
        def run(*args, **kwargs):
            try:
                return route(*args, **kwargs)
            except HTTPException as error:
                if error.status_code not in (400, 403, 422):
                    raise
                request, session, user = kwargs.get("request"), kwargs.get("session"), kwargs.get("user")
                if request is None or session is None or user is None:
                    raise
                session.rollback()
                ticket = get_by_number(session, kwargs["number"])
                if ticket is None:
                    raise
                # Only what was typed: text, numbers, lists of text (the split parts) or of numbers (ticked boxes) and the triage
                # form's answers by question. Never an upload, a flag or a dependency.
                values = {
                    k: v for k, v in kwargs.items()
                    if k not in ("number", "request") and (
                        isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))
                        or (isinstance(v, list) and all(isinstance(i, str) or (isinstance(i, int) and not isinstance(i, bool)) for i in v))
                        or (isinstance(v, dict) and all(isinstance(a, int) and isinstance(b, str) for a, b in v.items()))
                    )
                }
                # A browser cannot refill a file input, so the card says the files must be chosen again. Files held at a preview
                # (`draft`) are kept, if they still are held.
                if any(getattr(i, "filename", None) for v in kwargs.values() if isinstance(v, list) for i in v):  # starlette's UploadFile, not fastapi's subclass
                    values["had_files"] = True
                if values.get("draft"):
                    held = reply_files.held(session, ticket, user, values["draft"])
                    if held:
                        values["held_files"] = [row.filename for row in held]
                    else:
                        values.pop("draft")
                response = _render_ticket_detail(
                    request, ticket, user, session, action=card, card_error=str(error.detail), card_values=values, card_must_open=error,
                )
                if not is_htmx(request):
                    response.status_code = error.status_code
                return response

        return run

    return wrap


def _text(value) -> str:
    """What a form field sent, as text: an enum the route parsed (TicketType, Level) goes back to its value."""
    return str(value.value if isinstance(value, Enum) else (value if value is not None else ""))


def _triage_kept(session: Session, ticket: Ticket, values: dict) -> dict:
    """What a refused Triage save puts back into the redrawn card (#268): every select choice and typed box. A select choice is
    carried only if it is still on offer (the select macros and the checks below match it against what the card lists), so a
    choice that is no longer valid is not silently kept. The category's team members fill Assign to, as the page does when the
    category is picked; the chosen kind of request decides the Required by box, the questions and the sub-requests, as
    /tickets/triage-dates does."""
    category_id = _optional_int(values.get("category_id"))
    category = session.get(Category, category_id) if category_id is not None else None
    if category is not None and not is_selectable(session, category):
        category = None
    members = team_members(session, team_id_for(session, category)) if category is not None else []
    agent_id = _optional_int(values.get("agent_id"))
    chosen = _optional_int(values.get("request_type_id"))
    request_type = next((rt for rt in active_request_types(session) if rt.id == chosen), None)
    type_id = request_type.id if request_type is not None else ticket.request_type_id
    answers = values.get("answers") or {}
    rows = []
    for question, current in form_rows(session, type_id, ticket if type_id == ticket.request_type_id else None):
        given = answers.get(question.id)
        if given is not None:
            if question.kind == "choice" and given not in question.options:
                given = None
            elif question.kind == "yes_no" and given not in ("yes", "no"):
                given = None
        rows.append((question, current if given is None else given))
    offers = request_type.offers_required_by if request_type is not None else offers_required_by(session, ticket)
    return {
        "ticket_type": _text(values.get("ticket_type")),
        "impact": _text(values.get("impact")),
        "urgency": _text(values.get("urgency")),
        "category_id": category.id if category is not None else None,
        "members": members,
        "agent_id": agent_id if any(m.id == agent_id for m in members) else None,
        "request_type_id": request_type.id if request_type is not None else None,
        "offers_required_by": offers,
        "required_by": values.get("required_by") or "",
        "due_on": values.get("due_on") or "",
        "due_reason": values.get("due_reason") or "",
        "question_rows": rows,
        "subrequests": preview_subrequests(
            session, ticket, type_id, {k: v for k, v in answers.items() if v},
            values.get("subrequest_listed") or [], values.get("subrequest_template") or [],
        ),
        "new_category_name": values.get("new_category_name") or "",
        "new_category_parent_id": _optional_int(values.get("new_category_parent_id")),
        "new_category_team_id": _optional_int(values.get("new_category_team_id")),
    }


def _optional_int(value) -> Optional[int]:
    """A select's submitted text as a number, or None when blank or not a number (a kept form must never itself refuse)."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
@card_errors("triage")
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
        if chosen_agent is not None and (assignee is None or assignee.role not in ASSIGNEE_ROLES):
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
    reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Re-sets impact and urgency on a triaged ticket the agent works. The priority
    follows, and the SLA deadlines are worked out again from the new targets. A reason is required (#254): the priority
    decides the SLA clock, so changing it is explained; it is kept on the ticket event and in the audit entry."""
    ticket = _get_ticket(session, number, user)
    if ticket.triaged_at is None:
        raise HTTPException(status_code=400, detail="Triage the ticket first; triage sets its priority")
    if ticket.status in FINISHED_STATUSES:
        # A finished ticket's SLA result must not be rewritable after the fact.
        raise HTTPException(status_code=400, detail="A resolved or closed ticket's priority can no longer be changed")
    try:
        reason = clean_reason(reason, "priority")
    except TriageError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if set_impact_urgency(session, ticket, impact, urgency, user.email, reason):
        ticket.updated_at = datetime.now(timezone.utc)
        log_action(session, user.email, "ticket", ticket.id, "priority_changed", f"{describe_priority(ticket)}: {reason}")
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/category", response_class=HTMLResponse)
def edit_category(
    number: int,
    request: Request,
    category_id: Optional[str] = Form(None),
    reason: Optional[str] = Form(None, max_length=600),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Changes the category of a triaged ticket the agent works, from the Triage box (#254). A reason is required. It does not
    move the ticket: the team and the assignee stay (Re-assign and Escalate move a ticket). A ticket event and an audit entry
    record the old and new category and the reason."""
    ticket = _get_ticket(session, number, user)
    chosen = optional_int(category_id)
    category = session.get(Category, chosen) if chosen is not None else None
    if category is None:
        raise HTTPException(status_code=400, detail="Choose a category")
    previous = session.get(Category, ticket.category_id) if ticket.category_id else None
    before = path_of(session, previous) if previous else "none"
    try:
        changed = change_category(session, ticket, category, user, reason)
    except TriageError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changed:
        why = clean_reason(reason, "category")
        log_action(session, user.email, "ticket", ticket.id, "category_changed", f"{before} -> {path_of(session, category)}: {why}")
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/type", response_class=HTMLResponse)
def edit_type(
    number: int,
    request: Request,
    ticket_type: TicketType = Form(...),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Changes the type of a triaged, unfinished ticket the agent works, from the Triage box (#254). The number never changes
    (only the reference prefix follows the type). A ticket event (`change_type`) and an audit entry record it."""
    ticket = _get_ticket(session, number, user)
    if ticket.triaged_at is None:
        raise HTTPException(status_code=400, detail="Triage the ticket first; triage sets its type")
    if ticket.status in FINISHED_STATUSES:
        raise HTTPException(status_code=400, detail="A resolved or closed ticket's type can no longer be changed")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=400, detail="The type cannot be changed while the request waits for approval")
    previous = ticket.type
    change_type(session, ticket, ticket_type, user.email)
    if ticket.type != previous:
        ticket.updated_at = datetime.now(timezone.utc)
        log_action(session, user.email, "ticket", ticket.id, "type_changed", f"{previous.value} -> {ticket.type.value}")
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/attachments/{attachment_id}/rename", response_class=HTMLResponse)
def rename_file(
    number: int,
    attachment_id: int,
    request: Request,
    name: str = Form("", max_length=400),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Renames a file as the ticket page shows it (#254). For agents who work the ticket. Only the shown name changes: the
    name the file arrived with stays on its row, and the file itself is untouched. A ticket event and an audit entry record
    the old and new names. Nothing is ever deleted."""
    ticket = _get_ticket(session, number, user)
    attachment = session.get(Attachment, attachment_id)
    if attachment is None or attachment.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # An emailed picture is shown once however many copies came (#345), so a rename applies to every copy this person can see.
    old = shown_name(attachment)  # the event records the name the agent saw, whichever copies needed changing
    changed = False
    try:
        for copy in picture_copies(session, user, access_context(session, user), ticket, attachment):
            if rename_attachment(copy, name):
                session.add(copy)
                changed = True
    except RenameRefused as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if changed:
        new = shown_name(attachment)
        ticket.updated_at = datetime.now(timezone.utc)
        session.add(ticket)
        record_event(session, ticket, EVENT_ATTACHMENT_RENAMED, user.email, old, new, detail=f"arrived as {attachment.filename}")
        log_action(session, user.email, "attachment", attachment.id, "renamed", f"{old} -> {new}")
        session.commit()
        session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


def _note_of(session: Session, ticket: Ticket, note_id: int) -> Message:
    note = session.get(Message, note_id)
    if note is None or note.ticket_id != ticket.id or not is_note(note):
        raise HTTPException(status_code=404, detail="Note not found")
    return note


def _note_action(number: int, note_id: int, request: Request, user: User, session: Session, act) -> HTMLResponse:
    """The three actions of a note's ... menu (#255) share this: load the ticket (any agent who can open it), find the note, check
    `may_manage_note` (its author or a super admin), run `act`, commit and draw the page again."""
    ticket = _get_ticket(session, number, user, Access.READ)
    note = _note_of(session, ticket, note_id)
    if not may_manage_note(user, note, ticket, access_context(session, user)):
        raise HTTPException(status_code=403, detail="Only the note's author or a super admin can do that")
    try:
        act(ticket, note)
    except NoteError as error:
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/notes/{note_id}/visibility", response_class=HTMLResponse)
def change_note_visibility(
    number: int,
    note_id: int,
    request: Request,
    visible: str = Form("", max_length=1),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Shows a note to the requester in the portal (visible=1) or hides it again (anything else): the note's author or a super
    admin. Nothing is emailed or sent to Teams and nobody's bell rings; the ticket event and audit entry are the record."""
    return _note_action(number, note_id, request, user, session, lambda t, n: set_note_visible(session, t, n, user, visible == "1"))


@router.post("/{number}/notes/{note_id}/delete", response_class=HTMLResponse)
def remove_note(
    number: int,
    note_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Deletes a note with Undo: hidden at once everywhere, removed by the `note-purge` job after the keep window."""
    return _note_action(number, note_id, request, user, session, lambda t, n: delete_note(session, t, n, user))


@router.post("/{number}/notes/{note_id}/undo", response_class=HTMLResponse)
def restore_note(
    number: int,
    note_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Brings a deleted note back while its row still exists (the purge job has not removed it)."""
    return _note_action(number, note_id, request, user, session, lambda t, n: undo_delete_note(session, t, n, user))


@router.post("/{number}/emails/{email_id}/send-anyway", response_class=HTMLResponse)
def send_email_anyway(
    number: int,
    email_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Sends one email the per-ticket limit on automatic emails held back (#349), for an agent who may work the ticket. That one row
    only; it goes out through the normal outbox. Recorded as an audit entry and a ticket event. A second click does nothing."""
    ticket = _get_ticket(session, number, user, Access.WORK)
    try:
        send_anyway(session, ticket, email_id, user)
    except SendAnywayError as error:
        raise HTTPException(status_code=404, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.get("/{number}/mentions")
def mention_candidates(
    number: int,
    q: str = "",
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The @ list of the Add note composer (#254): the agents the note could mention, who are exactly the ones who can open
    this ticket (`mentionable_agents`), narrowed by what has been typed after the @. For someone who may add a note here."""
    ticket = _get_ticket(session, number, user, Access.READ)
    if not may_add_note(user, ticket, access_context(session, user)):
        raise NoTicketAccess()
    people = mentionable_agents(session, ticket, user, q[:60])
    return JSONResponse(
        {"agents": [{"name": person.display_name or person.email, "email": person.email, "insert": insert} for person, insert in people]},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{number}/backdate", response_class=HTMLResponse)
@card_errors("backdate")
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
            reported_at=parse_local_moment(reported_at, zone), resolved_at=parse_local_moment(resolved_at, zone),
        )
    except MomentError as error:
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
@card_errors("reassign")
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
    if chosen_agent is not None and (assignee is None or assignee.role not in ASSIGNEE_ROLES):
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
    action: Optional[str] = None,
    edit: Optional[str] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The ticket page. `?action=<card>` opens that action's card at the top of the main column and `?edit=<field>` puts one
    box row into its edit form: how the chips and pencils work without scripts (with htmx they fetch this same page and swap
    the card or the box). A name that is not an action this ticket offers this person opens nothing."""
    return _render_ticket_detail(
        request, _get_ticket(session, number, user, Access.READ), user, session, action=action, edit=edit,
    )


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


def _later_posts(session: Session, ticket: Ticket, user: User, seen: Optional[str], confirm: Optional[str]) -> list:
    """The emails to the requester and internal notes other agents posted after the sender's page was loaded (the hidden `seen`
    field), unless the sender has already said to send anyway. A request with no `seen` gets no check: there is nothing to
    compare with. The email and note composers carry it; the resolve and close
    forms (which also email the requester) do not, so they are not checked. Advisory, not a lock: a message that lands between this check and
    the send is not caught."""
    if confirm:
        return []
    last_seen = optional_int(seen)
    if last_seen is None:
        return []
    return posts_since(session, ticket, user, last_seen)


def _send_warning(
    ticket: Ticket, mode: str, later: list, body: str, fields: list[tuple[str, Optional[str]]], files: list[str],
) -> dict:
    """What the page draws when a send was held back: the agent's text (kept in the composer), who posted what since, and the
    form that resubmits the same send with `confirm` (Send anyway). `fields` are the hidden values that form carries."""
    return {
        "mode": mode,
        "body": body,
        "later": [
            {
                "who": m.sender, "at": m.created_at, "excerpt": excerpt(m.body),
                "email": m.direction == MessageDirection.OUTBOUND and not m.visible_note, "visible": m.visible_note,
            }
            for m in later
        ],
        "action": f"/tickets/{ticket.number}/{'email' if mode == 'email' else 'note'}",
        "fields": [(name, value) for name, value in fields if value not in (None, "")] + [("confirm", "1")],
        "files": files,
        "draft": next((value for name, value in fields if name == "draft" and value), None),
    }


def _busy_warning(
    session: Session, ticket: Ticket, user: User, verb: str, confirm: Optional[str], notes: str,
    fields: list[tuple[str, Optional[str]]],
) -> Optional[dict]:
    """The hold on a Resolve or Close while another agent is doing something on the ticket (not just viewing it), read from live
    presence when the form is posted (#246). None when `confirm` says to go ahead, or nobody else is busy. `fields` are the hidden
    values the "anyway" form resubmits unchanged. Advisory, like the send warning. Bulk resolve is not checked."""
    if confirm:
        return None
    busy = ticket_presence.busy_others(session, ticket, user)
    if not busy:
        return None
    return {
        "verb": verb,
        "who": [f"{item.user.display_name} {item.sentence}" for item in busy],
        "notes": notes,
        "action": f"/tickets/{ticket.number}/{verb.lower()}",
        "fields": [(name, value) for name, value in fields if value not in (None, "")] + [("confirm", "1")],
    }


@router.post("/{number}/presence", response_class=HTMLResponse)
def presence_check_in(
    number: int,
    request: Request,
    activity: str = Form("viewing", max_length=20),
    doing: str = Form("", max_length=400),
    shown: str = Form("", max_length=64),
    # Passive: the page checks in by itself every few seconds, so it is not the person being active; an open tab nobody is
    # looking at must not keep a sign-in alive (same reason as the bell's count, app/auth.py).
    user: User = Depends(get_current_user_passively),
    session: Session = Depends(get_session),
):
    """The ticket page's check-in: records what this agent is doing here and answers with the strip of who else is on the ticket,
    or with nothing (204) when it would read the same as what the page already shows (`shown`), so the live region is left
    alone. Agents only, and only for a ticket they can open."""
    if user.role not in ticket_presence.AGENT_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted")
    ticket = _get_ticket(session, number, user, Access.READ)
    ticket_presence.check_in(session, ticket, user, activity, doing)
    session.commit()
    others = ticket_presence.others_here(session, ticket, user)
    now_shown = ticket_presence.digest(others)
    if now_shown == shown:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return templates.TemplateResponse(
        request, "_presence_strip.html", {"others": others, "shown": now_shown}, headers={"Cache-Control": "no-store"}
    )


@router.post("/{number}/presence/leave")
def presence_leave(
    number: int,
    user: User = Depends(get_current_user_passively),  # a beacon sent as the page closes
    session: Session = Depends(get_session),
):
    """The page was closed or left (navigator.sendBeacon on pagehide): this agent's row goes now instead of after the
    gone-after time. It only ever removes the caller's own row, so it needs no access check."""
    if user.role in ticket_presence.AGENT_ROLES:
        ticket = get_by_number(session, number)
        if ticket is not None:
            ticket_presence.leave(session, ticket, user)
            session.commit()
    return Response(status_code=204)


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
    seen: Optional[str] = Form(None, max_length=20),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """The "are you sure?" step before a message goes to the requester: who it goes to (and whether by email or in Teams), who
    is copied, the subject and the text, the files, and what it does to the ticket. Sends and records nothing about the
    ticket; the files are checked (the rules of every stored file, and one email's size) and held with the draft until the
    preview is confirmed, kept editing or expires (app/reply_files.py). The checks are the send's own, so what is
    previewed is what would go."""
    ticket = _get_ticket(session, number, user)
    try:
        preview = preview_email(session, ticket, body, cc_emails=cc if cc_form else None, next_status=next_status, author=user)
    except NotTriaged:
        raise HTTPException(status_code=400, detail="Triage the ticket before emailing the requester")
    except EmptyEmail:
        raise HTTPException(status_code=400, detail="Write the email first")
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    except BadStatusChoice as error:
        raise HTTPException(status_code=400, detail=str(error))
    _check_scheduled_date_given(ticket, preview.status_after, due_on)
    try:
        new_files = reply_files.read_uploads(session, files, email=True)
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))
    draft = reply_files.hold(session, storage, ticket, user, new_files)
    session.commit()  # the draft's files outlive this request; nothing else is recorded
    preview.files = [(item.filename, len(item.data)) for item in new_files]
    return templates.TemplateResponse(
        request,
        "_email_preview.html",
        {
            "ticket": ticket,
            "preview": preview,
            "draft": draft,
            "cc_form": bool(cc_form),
            "ticked": cc,
            "next_status": next_status,
            "due_on": due_on,
            "due_reason": due_reason,
            "seen": seen,
        },
    )


@router.post("/{number}/email/discard", response_class=HTMLResponse)
def discard_email_draft(
    number: int,
    draft: str = Form(..., max_length=100),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Keep editing: the files held for the preview that was just closed are dropped now, not left to expire. Also the send
    warning's Cancel. Only ever the caller's own draft, so reading access is enough (a note-only agent can cancel too)."""
    ticket = _get_ticket(session, number, user, Access.READ)
    reply_files.discard(session, ticket, user, draft)
    session.commit()
    return HTMLResponse("")


@router.post("/{number}/email", response_class=HTMLResponse)
@card_errors("email")
def email_user(
    number: int,
    request: Request,
    body: str = Form("", max_length=MAX_MESSAGE_LENGTH),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    next_status: Optional[str] = Form(None),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    draft: Optional[str] = Form(None, max_length=100),
    seen: Optional[str] = Form(None, max_length=20),
    confirm: Optional[str] = Form(None, max_length=5),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """Send the requester a message, copying the followers the agent left ticked. "draft" names the files held
    at the preview (app/reply_files.py): they are attached to the message and the held copies removed.
    Not available until the ticket is triaged. "cc_form" is sent by the page's form
    so that ticking nobody means copying nobody; a request without it copies every
    follower. "next_status" is the status the agent picked in the form (Awaiting user unless they
    chose otherwise); a request without it gets the same default.

    "seen" is the newest message id the agent's page showed. If another agent has emailed the requester or added a note since,
    nothing is sent: the page comes back with the text kept and a warning (Send anyway resubmits with "confirm")."""
    ticket = _get_ticket(session, number, user)

    later = _later_posts(session, ticket, user, seen, confirm) if is_triaged(ticket) else []
    if later:
        names = [row.filename for row in reply_files.held(session, ticket, user, draft)]
        fields = [("body", body), ("cc_form", cc_form), *[("cc", address) for address in cc], ("next_status", next_status),
                  ("due_on", due_on), ("due_reason", due_reason), ("draft", draft)]
        return _render_ticket_detail(request, ticket, user, session, compose=_send_warning(ticket, "email", later, body, fields, names))

    held_rows = reply_files.held(session, ticket, user, draft, lock=True)
    if draft and not held_rows:
        raise HTTPException(status_code=400, detail="The files for this message are no longer held. Preview the message again.")
    try:
        new_files = reply_files.take(storage, held_rows)
        reply_files.check_email_size(session, [len(item.data) for item in new_files])
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))
    try:
        email_requester(
            session, ticket, user, body, cc_emails=cc if cc_form else None, bell=False, files=new_files, storage=storage,
        )
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
    reply_files.release(session, held_rows)  # the files are attached now, so the stored copies stay
    session.commit()
    session.refresh(ticket)

    # The email itself was queued by email_requester (app/outbox.py) in the transaction above; the
    # worker's mail-out job sends it (app/mail.py).

    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/note", response_class=HTMLResponse)
@card_errors("note")
def add_ticket_note(
    number: int,
    request: Request,
    body: str = Form("", max_length=MAX_MESSAGE_LENGTH),
    next_status: Optional[str] = Form(None),
    due_on: Optional[str] = Form(None, max_length=20),
    due_reason: Optional[str] = Form(None, max_length=600),
    draft: Optional[str] = Form(None, max_length=100),
    seen: Optional[str] = Form(None, max_length=20),
    confirm: Optional[str] = Form(None, max_length=5),
    visible_to_requester: Optional[str] = Form(None, max_length=5),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """An internal note, visible to agents only; its files are too (never in the portal or an email). Allowed at any stage, including
    on an untriaged ticket the agent has not claimed, and never triages it. With "visible_to_requester" (the eye toggle, #254) it is
    instead a note the requester sees in the portal (`add_visible_note`): never emailed, never sent to Teams, no bell, held back
    until the ticket is triaged, and for an agent who works the ticket only. The agent may also pick
    the ticket's next status; left alone, a note never changes it. Like the email, it is held back with a warning when another
    agent emailed the requester or added a note after the agent's page was loaded ("seen"); the files chosen are held with
    the draft (app/reply_files.py) so Send anyway still attaches them."""
    ticket = _get_ticket(session, number, user, Access.READ)
    levels = access_context(session, user)
    if not may_add_note(user, ticket, levels):
        raise NoTicketAccess()
    if not body.strip():
        raise HTTPException(status_code=400, detail="Write the note first")  # a refusal the card shows, not a bare 422
    visible = bool(visible_to_requester)
    if visible and ticket_access(user, ticket, levels) < Access.WORK:
        raise HTTPException(status_code=403, detail="Only an agent who works this ticket can make a note visible to the requester")
    try:
        new_status = chosen_status(session, ticket, next_status, ticket.status)
    except BadStatusChoice as error:
        raise HTTPException(status_code=400, detail=str(error))
    if new_status is not None:
        require_access(user, ticket, levels, Access.WORK)  # changing status needs more than reading

    try:
        new_files = reply_files.read_uploads(session, files)
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))
    held_rows = reply_files.held(session, ticket, user, draft, lock=True)
    if draft and not held_rows:
        raise HTTPException(status_code=400, detail="The files for this note are no longer held. Add the note again.")
    later = _later_posts(session, ticket, user, seen, confirm)
    if later:
        # Only files are held (hold() also drops this agent's other unconfirmed draft, an email preview's files).
        if held_rows and not new_files:
            token = draft
        elif held_rows or new_files:  # files chosen on a second round join the ones already held, under one new token
            try:
                again = reply_files.take(storage, held_rows)
            except reply_files.FilesRefused as error:
                raise HTTPException(status_code=400, detail=str(error))
            token = reply_files.hold(session, storage, ticket, user, again + new_files)
        else:
            token = None
        session.commit()  # the held files outlive this request; nothing else is recorded
        names = [row.filename for row in held_rows] + [item.filename for item in new_files]
        fields = [("body", body), ("next_status", next_status), ("due_on", due_on), ("due_reason", due_reason), ("draft", token),
                  ("visible_to_requester", "1" if visible else None)]
        warning = _send_warning(ticket, "note", later, body, fields, names)
        warning["visible"] = visible
        return _render_ticket_detail(request, ticket, user, session, compose=warning)
    try:
        new_files = reply_files.take(storage, held_rows) + new_files
    except reply_files.FilesRefused as error:
        raise HTTPException(status_code=400, detail=str(error))
    if new_status == TicketStatus.SCHEDULED:
        _ask_for_scheduled_date(session, ticket, user, due_on, due_reason)  # before the note, so a refusal leaves nothing
    if visible:
        note = add_visible_note(session, ticket, user, body, files=new_files, storage=storage)
        log_action(session, user.email, "ticket", ticket.id, "visible_note_added", body[:200])
    else:
        note = add_note(session, ticket, user, body, files=new_files, storage=storage)
        log_action(session, user.email, "ticket", ticket.id, "note_added", body[:200])
    _set_status_from_message(session, ticket, user, new_status, due_on, due_reason)
    notify_mentions(session, ticket, note, user)
    reply_files.release(session, held_rows)  # the files are attached now, so the stored copies stay
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


def _tell_msp(ticked: Optional[str], offered: Optional[str]) -> Optional[bool]:
    """The tick box about telling the MSP: True ticked, False shown and unticked (the form says it was shown), None never shown."""
    return True if ticked else (False if offered else None)


def _msp_offer(session: Session, ticket: Ticket, user: User, outcome: str) -> dict:
    """What a resolve, close or take-back preview offers about the MSP (app/msp_actions.py): the "no longer needed" email, and the
    linked tickets that are With MSP through this one. Each is ticked by default in the preview."""
    return {
        "msp_release": compose_release(session, ticket, outcome, user),
        "msp_linked": linked_with_msp_through(session, ticket, user),
        "msp_outcome": outcome,
    }


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
        preview = preview_resolution(session, ticket, resolution_notes, cc_emails=cc if cc_form else None, author=user)
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
            **_msp_offer(session, ticket, user, OUTCOME_RESOLVED),
        },
    )


@router.post("/{number}/resolve", response_class=HTMLResponse)
@card_errors("resolve")
def resolve(
    number: int,
    request: Request,
    resolution_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    tell_msp: Optional[str] = Form(None),
    msp_offered: Optional[str] = Form(None),
    take_back_linked: Optional[str] = Form(None),
    confirm: Optional[str] = Form(None, max_length=5),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Resolve the ticket with notes (required). Emails the requester, copying the followers the
    agent left ticked, through app/resolution.py. The two MSP tick boxes of the preview (tell the MSP it is no
    longer needed; take back the linked tickets that went With MSP with this one) are app/msp_actions.py's."""
    ticket = _get_ticket(session, number, user)
    hold = _busy_warning(
        session, ticket, user, "Resolve", confirm, resolution_notes.strip(),
        [("resolution_notes", resolution_notes), ("cc_form", cc_form), *[("cc", address) for address in cc], ("tell_msp", tell_msp),
         ("msp_offered", msp_offered), ("take_back_linked", take_back_linked)],
    )
    if hold:
        return _render_ticket_detail(request, ticket, user, session, hold=hold)
    try:
        resolve_ticket(
            session, ticket, user, resolution_notes, cc_emails=cc if cc_form else None,
            tell_msp=_tell_msp(tell_msp, msp_offered), take_back_linked=bool(take_back_linked),
        )
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
        preview = preview_closing(session, ticket, closing_notes, cc_emails=cc if cc_form else None, author=user)
    except CloseError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except UnknownRecipient as error:
        raise HTTPException(status_code=400, detail=f"Not a follower of this ticket: {error}")
    return templates.TemplateResponse(
        request,
        "_close_preview.html",
        {
            "ticket": ticket, "preview": preview, "notes": closing_notes.strip(), "cc_form": bool(cc_form), "ticked": cc,
            **_msp_offer(session, ticket, user, OUTCOME_CLOSED),
        },
    )


@router.post("/{number}/close", response_class=HTMLResponse)
@card_errors("close")
def close(
    number: int,
    request: Request,
    closing_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    cc_form: Optional[str] = Form(None),
    cc: list[str] = Form(default=[]),
    tell_msp: Optional[str] = Form(None),
    msp_offered: Optional[str] = Form(None),
    take_back_linked: Optional[str] = Form(None),
    confirm: Optional[str] = Form(None, max_length=5),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Close a ticket that was not resolved, with notes (required). Emails the requester, copying the
    followers the agent left ticked, through app/closing.py. The MSP tick boxes are as for resolving."""
    ticket = _get_ticket(session, number, user)
    hold = _busy_warning(
        session, ticket, user, "Close", confirm, closing_notes.strip(),
        [("closing_notes", closing_notes), ("cc_form", cc_form), *[("cc", address) for address in cc], ("tell_msp", tell_msp),
         ("msp_offered", msp_offered), ("take_back_linked", take_back_linked)],
    )
    if hold:
        return _render_ticket_detail(request, ticket, user, session, hold=hold)
    try:
        close_ticket(
            session, ticket, user, closing_notes, cc_emails=cc if cc_form else None,
            tell_msp=_tell_msp(tell_msp, msp_offered), take_back_linked=bool(take_back_linked),
        )
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
@card_errors("reopen")
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
@card_errors("reclose")
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
@card_errors("assign")
def assign_ticket(
    number: int,
    request: Request,
    agent_id: Optional[int] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user, Access.READ)

    if user.role == UserRole.AGENT or (user.role == UserRole.SUPER_ADMIN and agent_id is None):
        # Assign to me: an agent always; a super admin when they name nobody (#288), through the same rule.
        levels = access_context(session, user)
        if not may_self_assign(user, ticket, levels):
            if ticket_access(user, ticket, levels) >= Access.WORK and ticket.assigned_agent_id is not None:
                raise HTTPException(status_code=400, detail="This ticket is assigned to someone else. Use Re-assign.")
            if user.role == UserRole.SUPER_ADMIN:
                raise HTTPException(status_code=400, detail="You are not a member of the ticket's team")
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
@card_errors("escalate")
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
    linked = linked_to_mark(session, ticket, user)
    return templates.TemplateResponse(
        request, "_msp_preview.html", {"ticket": ticket, "preview": preview, "reason": reason.strip(), "linked": linked}
    )


@router.post("/{number}/send-to-msp", response_class=HTMLResponse)
@card_errors("msp")
def send_ticket_to_msp(
    number: int,
    request: Request,
    reason: str = Form("", max_length=MAX_MESSAGE_LENGTH),
    scope: str = Form("thread", max_length=20),
    digest: Optional[str] = Form(None, max_length=64),
    mark_linked: Optional[str] = Form(None),
    tell_linked_requesters: Optional[str] = Form(None),
    linked_digest: Optional[str] = Form(None, max_length=64),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Sends the ticket to the MSP (the same action as escalating to an MSP-target category), with a reason.
    The ticket stays in its team, With MSP. For a Major Incident or Problem the preview's two boxes mark the linked tickets
    With MSP and tell their requesters (app/msp_actions.py); `linked_digest` fingerprints the list the preview showed."""
    ticket = _get_ticket(session, number, user)
    try:
        send_with_linked(
            session, ticket, user, reason, scope, expect_digest=digest, mark_linked_too=bool(mark_linked),
            tell_requesters=bool(tell_linked_requesters), expect_linked_digest=linked_digest,
        )
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/take-back-from-msp/preview", response_class=HTMLResponse)
def preview_take_back(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The step before taking a ticket back from the MSP: where it goes, the "no longer needed" email to the MSP
    (ticked by default) and the linked tickets that went With MSP with it. Records and sends nothing."""
    ticket = _get_ticket(session, number, user)
    try:
        check_can_take_back(session, ticket)
    except MspError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return templates.TemplateResponse(
        request, "_msp_take_back_preview.html",
        {
            "ticket": ticket, "goes_to": "In progress" if ticket.assigned_agent_id is not None else "Open",
            **_msp_offer(session, ticket, user, OUTCOME_TAKEN_BACK),
        },
    )


@router.post("/{number}/take-back-from-msp", response_class=HTMLResponse)
@card_errors("takeback")
def take_back_from_msp_route(
    number: int,
    request: Request,
    tell_msp: Optional[str] = Form(None),
    msp_offered: Optional[str] = Form(None),
    take_back_linked: Optional[str] = Form(None),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Takes a With MSP ticket back: In progress if it is assigned, else Open. As ticked, tells the MSP it is no longer
    needed and takes back the linked tickets that went With MSP with it."""
    ticket = _get_ticket(session, number, user)
    try:
        take_back_from_msp(
            session, ticket, user, tell_msp=_tell_msp(tell_msp, msp_offered), take_back_linked_too=bool(take_back_linked)
        )
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/reactions/dismiss", response_class=HTMLResponse)
def dismiss_negative_reaction(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """An agent who can work the ticket dismisses its red Negative reaction chip (#298). It shows again on a new negative reaction."""
    ticket = _get_ticket(session, number, user)
    dismiss_negative(session, ticket, user)
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


def _pending_msp_message(session: Session, ticket: Ticket, message_id: int) -> Message:
    message = session.get(Message, message_id)
    if message is None or message.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


@router.post("/{number}/msp-forward/{message_id}/preview", response_class=HTMLResponse)
def preview_msp_forward(
    number: int,
    message_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The step before passing an MSP message on to a user it left off: the email exactly as it will go."""
    ticket = _get_ticket(session, number, user)
    message = _pending_msp_message(session, ticket, message_id)
    try:
        preview = compose_forward(session, ticket, message)
    except MspError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return templates.TemplateResponse(
        request, "_msp_forward_preview.html", {"ticket": ticket, "message": message, "preview": preview}
    )


@router.post("/{number}/msp-forward/{message_id}", response_class=HTMLResponse)
def msp_forward(
    number: int,
    message_id: int,
    request: Request,
    digest: Optional[str] = Form(None, max_length=64),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    message = _pending_msp_message(session, ticket, message_id)
    try:
        forward_message(session, ticket, message, user, expect_digest=digest)
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/msp-forward/{message_id}/dismiss", response_class=HTMLResponse)
def msp_forward_dismiss(
    number: int,
    message_id: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    ticket = _get_ticket(session, number, user)
    message = _pending_msp_message(session, ticket, message_id)
    try:
        dismiss_forward(session, ticket, message, user)
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    session.refresh(ticket)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/link", response_class=HTMLResponse)
@card_errors("link")
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


def _failed_mailbox_change(session: Session, ticket: Ticket):
    change = mailbox_changes.failed_change_for_ticket(session, ticket)  # the oldest first, one at a time (#310)
    if change is None:
        raise HTTPException(status_code=400, detail="This ticket has no mailbox change waiting for an agent")
    return change


def _settle_mailbox_change(session: Session, number: int, user: User, action):
    """Runs one of the agent's actions on a failed mailbox change (app/mailbox_changes.py) and commits, or refuses with the
    action's own message. `action(session, change, user)` returns True when the worker should look now (Try again)."""
    ticket = _get_ticket(session, number, user)
    change = _failed_mailbox_change(session, ticket)
    try:
        wake = action(session, change, user)
    except mailbox_changes.MailboxChangeError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if wake:
        from app.scheduled_jobs import SCHEDULED_JOBS, run_now  # the worker's next tick starts the job, not this route

        run_now(session, next(job for job in SCHEDULED_JOBS if job.name == "mailbox-changes"), user.email)
    ticket.updated_at = datetime.now(timezone.utc)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


@router.post("/{number}/mailbox/try-again", response_class=HTMLResponse)
@card_errors("mbx_retry")
def mailbox_try_again(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """One more attempt at a mailbox change that failed for good: the worker makes (or removes) it on its next run."""
    ticket = _settle_mailbox_change(session, number, user, lambda s, change, agent: mailbox_changes.try_again(s, change, agent) or True)
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/mailbox/done-by-hand", response_class=HTMLResponse)
@card_errors("mbx_done")
def mailbox_done_by_hand(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The agent made (or removed) the change in the Exchange admin centre: record it. A hand-made start still gets its
    automatic removal at the end time."""
    ticket = _settle_mailbox_change(session, number, user, lambda s, change, agent: mailbox_changes.done_by_hand(s, change, agent))
    return _render_ticket_detail(request, ticket, user, session)


@router.post("/{number}/mailbox/cancel", response_class=HTMLResponse)
@card_errors("mbx_cancel")
def mailbox_cancel(
    number: int,
    request: Request,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Drops a mailbox change that could not be started: the request is resolved as cancelled and the requester told."""
    ticket = _settle_mailbox_change(session, number, user, lambda s, change, agent: mailbox_changes.cancel(s, change, agent))
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


@router.post("/{number}/split", response_class=HTMLResponse)
@card_errors("split")
def split(
    number: int,
    request: Request,
    subject: list[str] = Form(default=[]),
    details: list[str] = Form(default=[]),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Splits this ticket into sub-tickets, one per part with a subject (the card starts with the original's text in each, so
    nothing is made until the agent confirms what is in the boxes). Each part of a triaged ticket is triaged like the original
    and can then be amended or assigned on its own page; each part of an untriaged one is untriaged, for the queue. The
    requester is emailed once about it (app/messages.record_split_notice)."""
    ticket = _get_ticket(session, number, user)
    details = details + [""] * (len(subject) - len(details))
    parts = [(s, d) for s, d in zip(subject, details) if s.strip() or d.strip()]
    if len(parts) > max_sub_tickets(session) or any(len(s) > MAX_SUBJECT or len(d) > MAX_DETAILS for s, d in parts):
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


# The boxes of the right-hand column whose rows have a pencil, and the keys `?edit=` takes ("file-<id>" renames a file).
EDIT_FIELDS = ("type", "category", "priority", "status", "due", "required_by")

# Where a ticket came from, short, for the header's "Reported by ..." (source_label has the long form).
REPORTED_BY = {
    TicketSource.EMAIL: "email", TicketSource.PORTAL: "portal", TicketSource.TEAMS: "Teams", TicketSource.AGENT: "an agent",
}


def _render_ticket_detail(
    request: Request, ticket: Ticket, user: User, session: Session, compose: Optional[dict] = None, hold: Optional[dict] = None,
    action: Optional[str] = None, edit: Optional[str] = None, card_error: Optional[str] = None,
    card_values: Optional[dict] = None, card_must_open: Optional[HTTPException] = None,
) -> HTMLResponse:
    """`compose` is set when a send was held back (`_send_warning`): the composer's card then opens with the text kept.
    `action` names the card to open at the top of the main column (app/ticket_actions.py) and `edit` the box row to show as
    a form; each is honoured only when this ticket offers it to this person."""
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
    message_files = files_by_message(session, user, levels, [i.message for i in timeline if i.message is not None])
    requester_person = person_for_address(session, ticket.requester_email)
    thread = build_thread(
        session, ticket, timeline, user, levels, requester_person.display_name if requester_person else None, message_files,
    )

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

    now = datetime.now(timezone.utc)
    at_risk = at_risk_percent(session)
    sla_now = sla_view(ticket, now, at_risk)
    presence_others = ticket_presence.others_here(session, ticket, user, now)

    # What the header offers (app/ticket_actions.py) and which card is open. The same answers feed the page's dict below.
    triaged = ticket.triaged_at is not None
    triaged_actions = triaged and ticket.status != TicketStatus.AWAITING_APPROVAL
    unfinished = ticket.status not in FINISHED_STATUSES
    can_resolve = can_work and _may_resolve(session, ticket)
    can_close = can_work and _may_close(session, ticket)
    can_reopen = can_work and _may(check_can_reopen, ReopenError, ticket)
    can_reclose = can_work and _may(check_can_reclose, ReopenError, session, ticket)
    can_link = can_work and triaged and linked_total == 0 and ticket.linked_parent_id is None
    can_split = can_work and ticket.split_from_id is None and unfinished  # before triage too (#259): the parts are untriaged
    can_take_back = can_work and ticket.status == TicketStatus.WITH_MSP and not has_sub_tickets(session, ticket)
    can_backdate = may_backdate(ticket)
    mailbox_change = mailbox_changes.failed_change_for_ticket(session, ticket)
    mailbox_phase = mailbox_changes.failed_phase(mailbox_change) if mailbox_change is not None else None
    chips = action_chips(
        can_work=can_work, can_note=may_add_note(user, ticket, levels), triaged=triaged, triaged_actions=triaged_actions,
        can_triage=can_triage, is_super_admin=user.role == UserRole.SUPER_ADMIN, can_self_assign=can_self_assign,
        assigned=assigned_agent is not None, can_reassign=can_reassign, with_msp=ticket.status == TicketStatus.WITH_MSP,
        can_take_back=can_take_back, can_link=can_link, can_split=can_split, can_resolve=can_resolve, can_reopen=can_reopen,
        can_reclose=can_reclose, can_close=can_close, can_backdate=can_backdate, mailbox_phase=mailbox_phase,
    )
    if compose:
        card = compose["mode"]  # a held-back send reopens its own composer with the text kept
    else:
        card = action if action in chips.keys() else None
    if card_must_open is not None and card is None:
        raise card_must_open  # the card is no longer on offer, so there is nowhere in it to show the message
    can_edit_triage = can_work and triaged and unfinished
    can_edit_status = can_work and triaged_actions
    can_set_required_by = (offers_required_by(session, ticket) or ticket.required_by is not None) and may_set_required_by(user, ticket, levels)
    can_set_due = may_set_due(user, ticket, levels)
    editable = {
        "type": can_edit_triage and ticket.status != TicketStatus.AWAITING_APPROVAL, "category": can_edit_triage, "priority": can_edit_triage, "status": can_edit_status,
        "due": can_set_due, "required_by": can_set_required_by,
    }
    files = ticket_files(session, user, levels, ticket)
    editing = None
    if edit in EDIT_FIELDS and editable[edit]:
        editing = edit
    elif edit and edit.startswith("file-") and can_work and edit[5:].isdecimal() and int(edit[5:]) in {f.attachment.id for f in files}:
        editing = edit
    options = selectable(session) if (card == "triage" or editing == "category") else []
    split_parts, split_shown, split_existing, split_room = [], 0, 0, 0
    if card == "split":
        # The boxes the ticket can still take (the limit is for the whole family): the first PREFILLED_PARTS shown and the
        # rest hidden until the agent adds them. A refused split comes back with every part that was typed, shown.
        split_existing = sub_ticket_count(session, ticket)
        split_room = max(max_sub_tickets(session) - split_existing, 0)
        typed_subjects, typed_details = (card_values or {}).get("subject"), (card_values or {}).get("details") or []
        if typed_subjects is not None:
            split_parts = [(sub, typed_details[n] if n < len(typed_details) else "") for n, sub in enumerate(typed_subjects)]
            filled = [n + 1 for n, (sub, det) in enumerate(split_parts) if sub.strip() or det.strip()]
            split_shown = max(PREFILLED_PARTS, filled[-1] if filled else 0)
        else:
            split_parts = prefilled_parts(session, ticket, PREFILLED_PARTS)
            split_shown = PREFILLED_PARTS
        split_parts += [("", "")] * (max(split_room, split_shown) - len(split_parts))
    card_title, card_doing = CARDS.get(card, ("", ""))
    kept = {}
    if card == "triage" and card_error:
        try:
            kept = _triage_kept(session, ticket, card_values or {})
        except Exception:  # keeping the form is a courtesy: never turn the refusal into a server error
            log.exception("could not keep the triage form for ticket %s", ticket.number)

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
            "can_split": can_split,
            "chips": chips,
            "mailbox_change": mailbox_change if mailbox_phase else None,
            "mailbox_declined": mailbox_changes.DECLINED_LINE if mailbox_changes.was_declined(session, ticket) else "",
            "mailbox_phase": mailbox_phase,
            "card": card,
            "card_title": card_title,
            "card_doing": card_doing,
            "editing": editing,
            "editable": editable,
            "files": files,
            "requester_person": requester_person,
            "reported_by": REPORTED_BY.get(ticket.source, "someone"),
            "triaged_actions": triaged_actions,
            "split_shown": split_shown, "split_existing": split_existing, "split_room": split_room,
            "split_parts": split_parts,
            "split_email_on": get_flag(session, SPLIT_EMAIL_ENABLED),
            "team": team,
            "assigned_agent": assigned_agent,
            "team_agents": team_agents,
            "teams": teams,
            "escalation_team": escalation_team,
            "escalation_blocked": escalation_blocked,
            "escalation_is_msp": escalation_is_msp,
            "msp_blocked": msp_blocked,
            "msp_pending": pending_forwards(session, ticket) if can_work else [],
            "can_take_back_from_msp": can_take_back,
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
            "can_resolve": can_resolve,
            "can_close": can_close,
            "can_reopen": can_reopen,
            "can_reclose": can_reclose,
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
            "can_link": can_link,
            "impact_options": impact_options(),
            "urgency_options": urgency_options(),
            "sla": sla_now,
            "sla_chip": header_sla(ticket, sla_now, now),
            "sla_rows": sla_rows(ticket, now, at_risk),
            "message_files": message_files,
            "thread": thread,
            "negative_reaction": ticket.id in open_negative_ids(session, [ticket.id]),
            "acknowledged": acknowledged_views(session, [ticket], now).get(ticket.id),
            "moment_format": MOMENT_FORMAT,
            "can_backdate": can_backdate,
            "can_set_required_by": can_set_required_by,
            "can_set_due": can_set_due,
            "due_on": ticket.due_on,
            "request_types": active_request_types(session) if card == "triage" else [],
            "offers_required_by": kept["offers_required_by"] if kept else offers_required_by(session, ticket),
            "requester_zone": zone_for_email(session, ticket.requester_email),
            "source_label": source_label(session, ticket),
            "request_info": request_type_of(session, ticket),
            "answers": answer_rows(session, ticket),
            "triage_questions": kept["question_rows"] if kept else (form_rows(session, ticket.request_type_id, ticket) if card == "triage" else []),
            "triage_subrequests": kept["subrequests"] if kept else (preview_subrequests(session, ticket, ticket.request_type_id, {}, [], []) if card == "triage" else []),
            "triage_kept": kept,
            "max_sub_tickets": max_sub_tickets(session),
            "pending_subrequests": pending_subjects(ticket),
            "answers_changed_after_subrequests": answers_changed_note(session, ticket),
            "edit_questions": form_rows(session, ticket.request_type_id, ticket) if can_edit_answers else [],
            "can_edit_answers": can_edit_answers,
            "answers_changed_at": ticket.answers_changed_at,
            "approvals": ticket_votes,
            "approval_waiting": is_waiting_for_approvers(session, ticket),
            "approval_waiting_group": (approval_group_of(session, ticket) if is_waiting_for_approvers(session, ticket) else None),
            "approval_rule": describe_rule(ticket_approval) if ticket_approval and ticket_approval.status != SUPERSEDED else None,
            "approval_status": ticket_approval.status if ticket_approval else None,
            "approval_note": portal_mailbox.approval_note(session, ticket),
            "can_waive": user.role == UserRole.SUPER_ADMIN and ticket.status == TicketStatus.AWAITING_APPROVAL,
            "can_set_priority": can_edit_triage,
            "category_options": options,
            "main_categories": [c for c, _ in options if c.parent_id is None and not c.is_other],
            "category_path": path_of(session, category) if category else None,
            "can_create_category": user.role == UserRole.SUPER_ADMIN,
            "can_reassign": can_reassign,
            "reassign_agents": team_members(session, ticket.team_id) if can_reassign else [],
            "can_work": can_work,
            "can_claim": can_claim,
            "user": user,
            # Who else is on this ticket (app/ticket_presence.py): drawn with the page, then refreshed by its check-in.
            "presence_others": presence_others,
            "presence_shown": ticket_presence.digest(presence_others),
            "presence_interval": ticket_presence.check_in_seconds(session),
            # The newest message this page shows; the send compares it with the ticket (_later_posts).
            "latest_post_id": latest_message_id(session, ticket),
            "compose": compose,
            "hold": hold,
            "card_error": card_error,
            "card_values": card_values or {},
        },
    )

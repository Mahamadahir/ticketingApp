from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.activity import (
    ACCESS_DENIED,
    ACCOUNT_LOCKED,
    LOGIN_FAILED,
    LOGIN_SUCCESS,
    LOGOUT,
    PROBLEM_EVENTS,
    REQUEST,
    SSO_FAILED,
)
from app.bank_holidays import REGIONS as BANK_HOLIDAY_REGIONS
from app import emergency, linked_identities as links, outage
from app.audit import log_action
from app.auth import hash_password, require_permission, require_roles
from app.permissions import ASSIGNABLE_ROLES, MANAGE_REQUEST_TEMPLATES, PERMISSIONS
from app.sla import impact_options, urgency_options
from app.categories import (
    CategoryError,
    create_category,
    ensure_other,
    is_selectable,
    parse_escalation,
    path_of,
    selectable,
)
from app.approval_groups import (
    MAX_NAME_LENGTH as MAX_GROUP_NAME_LENGTH,
    ApprovalGroupError,
    add_member as add_group_member,
    all_groups,
    create_group,
    members_of,
    remove_member as remove_group_member,
    rename_group,
    types_using,
)
from app.request_questions import (
    KINDS as QUESTION_KINDS,
    QuestionError,
    add_question,
    delete_question,
    all_questions_by_type,
    answered_question_ids,
    move_question,
    update_question,
)
from app.subrequests import (
    TemplateError as SubTemplateError,
    all_templates_by_type,
    delete_template as delete_sub_template,
    describe_template,
    format_conditions,
    move_template,
    save_template as save_sub_template,
)
from app.request_types import (
    MAX_DESCRIPTION_LENGTH as MAX_REQUEST_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH as MAX_REQUEST_NAME_LENGTH,
    RequestTypeError,
    TypeDefaults,
    all_request_types,
    self_triage_defaults,
    create_request_type,
    update_request_type,
)
from app.canned import (
    MAX_TITLE_LENGTH,
    PLACEHOLDERS,
    CannedError,
    all_canned,
    create_canned,
    update_canned,
)
from app.config import settings
from app.email_templates import (
    MAX_BODY_LENGTH as MAX_EMAIL_BODY_LENGTH,
    MAX_SUBJECT_LENGTH as MAX_EMAIL_SUBJECT_LENGTH,
    REGISTRY as EMAIL_TEMPLATES,
    TemplateError,
    current_text,
    definition as email_template_def,
    is_customised,
    render_preview,
    reset_template,
    save_template,
)
from app.database import get_session
from app.feedback import feedback_page, resolvers_with_feedback
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.followers import InvalidEmail, normalise_email
from app.thanks_rules import (
    MATCH_TYPES as THANKS_MATCH_TYPES,
    MAX_PATTERN_LENGTH as THANKS_MAX_PATTERN,
    MAX_RULE_LENGTH as THANKS_MAX_LENGTH,
    ThanksRuleError,
    add_rule as add_thanks_rule,
    all_rules as all_thanks_rules,
    candidate_to_rule,
    describe as describe_thanks_rule,
    dismiss_candidate,
    pending_candidates,
    set_rule_enabled as set_thanks_rule_enabled,
    update_rule as update_thanks_rule,
)
from app.never_follow import MAX_REASON_LENGTH, NeverFollowError, add_listed, all_listed, remove_listed
from app.models import (
    FINISHED_STATUSES,
    ActivityLog,
    ApprovalGroup,
    AuditLog,
    CannedResponse,
    Category,
    GraphNotification,
    GraphSubscription,
    InboundMail,
    LinkedIdentity,
    MailSyncState,
    OutboundEmail,
    RequestType,
    RequestSubTemplate,
    RequestTypeQuestion,
    ScheduledJob,
    MembershipLevel,
    Team,
    TeamMembership,
    Ticket,
    User,
    UserPermission,
    UserRole,
)
from app.pagination import paginate
from app.scheduled_jobs import SCHEDULED_JOBS, status_line, timing
from app.settings_store import (
    BANK_HOLIDAY_REGION,
    APPROVAL_DEFAULT_PASS_PERCENT,
    DUE_DAYS_BEFORE_REQUIRED_BY,
    REQUIRED_BY_LEAD_DAYS,
    JOB_LATE_MINUTES,
    MSP_BUSINESS_END,
    MSP_BUSINESS_START,
    REGISTRY,
    get_setting,
    set_setting,
)
from app.msp import compile_subject_pattern
from app.approvals import drop_votes_of, start_waiting_rounds
from app.sessions import live_session_counts, revoke_user_sessions
from app.inbound import IGNORED_FLOOD
from app.subscriptions import health_warnings, mail_folder, subscription_resource
from app.teams import MAX_ATTEMPTS
from app.time_zones import parse_clock, parse_dates, valid_zone
from app.ticket_service import EVENT_CATEGORY_REVIEWED, get_by_number, record_event

router = APIRouter(prefix="/admin", tags=["admin"])

require_super_admin = require_roles(UserRole.SUPER_ADMIN)
require_request_templates = require_permission(MANAGE_REQUEST_TEMPLATES)


@router.get("/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    page: int = 1,
    inactive: Optional[str] = None,
    link_error: str = "",
    aliases: int = 0,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    query = select(User).order_by(User.email)
    # Inactive agents who still hold open tickets: the tickets need a new assignee (#158).
    only_stranded = inactive == "open_tickets"
    if only_stranded:
        query = query.where(
            User.is_active.is_(False),
            select(Ticket.id)
            .where(Ticket.assigned_agent_id == User.id, Ticket.status.not_in(FINISHED_STATUSES))
            .exists(),
        )
    users_page = paginate(session, query, page, request.query_params)
    teams = session.exec(select(Team).order_by(Team.name)).all()
    teams_by_id = {team.id: team for team in teams}

    user_ids = [u.id for u in users_page.items]
    memberships: dict[int, list[TeamMembership]] = {}
    if user_ids:
        rows = session.exec(select(TeamMembership).where(TeamMembership.user_id.in_(user_ids))).all()
        for row in rows:
            memberships.setdefault(row.user_id, []).append(row)

    granted: dict[int, set[str]] = {}
    if user_ids:
        for row in session.exec(select(UserPermission).where(UserPermission.user_id.in_(user_ids))).all():
            granted.setdefault(row.user_id, set()).add(row.permission)

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "only_stranded": only_stranded,
            "permissions": PERMISSIONS,
            "assignable_roles": ASSIGNABLE_ROLES,
            "granted": granted,
            "page": users_page,
            "teams": teams,
            "teams_by_id": teams_by_id,
            "memberships": memberships,
            "levels": list(MembershipLevel),
            "roles": list(UserRole),
            "now": datetime.now(timezone.utc),
            "live_sessions": live_session_counts(session, user_ids),
            "pending_links": links.rows_for(session, links.PENDING),
            "active_links": links.rows_for(session, links.ACTIVE, aliases=bool(aliases), limit=200),
            "show_aliases": bool(aliases),
            "merged_into": {
                link.secondary_user_id: link.main_user_id
                for link in session.exec(
                    select(LinkedIdentity).where(LinkedIdentity.status == links.ACTIVE, LinkedIdentity.secondary_user_id.in_(user_ids))
                ).all()
            } if user_ids else {},
            "link_error": link_error,
            "user": user,
        },
    )


def _users_with_link_error(message: str) -> RedirectResponse:
    return RedirectResponse("/admin/users?" + urlencode({"link_error": message}), status_code=303)


@router.get("/links/preview", response_class=HTMLResponse)
def preview_link(
    request: Request,
    main_email: str,
    other_email: str,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Both accounts side by side (roles, teams, open tickets) before a super admin confirms a link. The main account
    is the one named first; the page says what is carried over and what is not."""
    main = session.exec(select(User).where(func.lower(User.email) == main_email.strip().lower())).first()
    secondary = session.exec(select(User).where(func.lower(User.email) == other_email.strip().lower())).first()
    if main is None or secondary is None:
        return _users_with_link_error("Both addresses must belong to existing accounts (an account exists once its person has signed in).")
    try:
        links.check_pair(session, main, secondary)
    except links.LinkError as refusal:
        return _users_with_link_error(str(refusal))
    main_summary, secondary_summary = links.summarise(session, main), links.summarise(session, secondary)
    return templates.TemplateResponse(
        request,
        "admin_link_confirm.html",
        {
            "user": admin, "main": main_summary, "secondary": secondary_summary,
            "not_carried": links.not_carried_over(main_summary, secondary_summary),
        },
    )


@router.post("/links")
def create_link(
    main_id: int = Form(...),
    secondary_id: int = Form(...),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    main, secondary = session.get(User, main_id), session.get(User, secondary_id)
    if main is None or secondary is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        links.link_accounts(session, main=main, secondary=secondary, admin=admin)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _users_with_link_error(str(refusal))
    return RedirectResponse("/admin/users", status_code=303)


def _link_or_404(session: Session, link_id: int) -> LinkedIdentity:
    link = session.get(LinkedIdentity, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return link


@router.post("/links/{link_id}/approve")
def approve_link(link_id: int, admin: User = Depends(require_super_admin), session: Session = Depends(get_session)):
    link = _link_or_404(session, link_id)
    try:
        links.approve(session, link, admin)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _users_with_link_error(str(refusal))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/links/{link_id}/decline")
def decline_link(link_id: int, admin: User = Depends(require_super_admin), session: Session = Depends(get_session)):
    link = _link_or_404(session, link_id)
    try:
        links.decline(session, link, admin)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _users_with_link_error(str(refusal))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/links/{link_id}/unlink")
def unlink_link(link_id: int, admin: User = Depends(require_super_admin), session: Session = Depends(get_session)):
    link = _link_or_404(session, link_id)
    try:
        links.unlink(session, link, admin)
        session.commit()
    except links.LinkError as refusal:
        session.rollback()
        return _users_with_link_error(str(refusal))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}")
def update_user(
    user_id: int,
    role: UserRole = Form(...),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if target.is_break_glass and role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="A break-glass account stays a super admin; deactivate it instead")
    if target.role != role:
        log_action(session, admin.email, "user", target.id, "role_changed", f"{target.role.value} -> {role.value}")
        target.role = role
        # A changed role (including losing the agent role) ends every session of that person, so they sign in again.
        ended = revoke_user_sessions(session, target.id)
        if ended:
            log_action(session, admin.email, "user", target.id, "sessions_revoked", f"role changed; {ended} ended")
        if not emergency.may_hold_one(target) and target.emergency_password_hash:
            # End users get no outage-mode fallback (#164); the sign-in refuses them anyway, this leaves no hash behind.
            target.emergency_password_hash = target.emergency_password_set_at = target.emergency_password_used_at = None
            log_action(session, admin.email, "user", target.id, "emergency_password_invalidated", "role changed")
        if role not in ASSIGNABLE_ROLES:
            # Grants only mean something for a role that may hold them; drop them rather than leave them dormant.
            for row in session.exec(select(UserPermission).where(UserPermission.user_id == target.id)).all():
                log_action(session, admin.email, "user", target.id, "permission_revoked", row.permission)
                session.delete(row)

    session.add(target)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/permissions")
def set_user_permissions(
    user_id: int,
    permissions: list[str] = Form(default=[]),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Saves the ticked boxes as this person's extra permissions (app/permissions.py): grants the new
    ones, revokes the unticked ones, one audit entry each."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    wanted = set(permissions)
    if not wanted <= set(PERMISSIONS):
        raise HTTPException(status_code=400, detail="Unknown permission")
    if wanted and target.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail="Only agents can be given extra permissions")
    rows = session.exec(select(UserPermission).where(UserPermission.user_id == target.id)).all()
    held = {row.permission: row for row in rows}
    if not target.is_active and wanted - set(held):
        raise HTTPException(status_code=400, detail="An inactive account cannot be given extra permissions")
    for key in sorted(wanted - set(held)):
        try:
            with session.begin_nested():  # a double submit may have added it already: not an error
                session.add(UserPermission(user_id=target.id, permission=key, granted_by=admin.email))
                session.flush()
        except IntegrityError:
            continue
        log_action(session, admin.email, "user", target.id, "permission_granted", key)
    for key, row in held.items():
        if key not in wanted:
            session.delete(row)
            log_action(session, admin.email, "user", target.id, "permission_revoked", key)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/memberships")
def set_membership(
    user_id: int,
    team_id: int = Form(...),
    level: MembershipLevel = Form(...),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Adds the user to a team, or changes their level if already a member."""
    target = session.get(User, user_id)
    team = session.get(Team, team_id)
    if target is None or team is None:
        raise HTTPException(status_code=404, detail="User or team not found")
    if target.role == UserRole.END_USER:
        # Team access only applies to agents; a row here would do nothing now and
        # quietly take effect if the person is made an agent later.
        raise HTTPException(status_code=400, detail="Make the user an agent before adding them to a team")

    membership = session.get(TeamMembership, (user_id, team_id))
    if membership is None:
        session.add(TeamMembership(user_id=user_id, team_id=team_id, level=level))
        log_action(session, admin.email, "user", target.id, "membership_added", f"{team.name}: {level.value}")
    elif membership.level != level:
        log_action(
            session, admin.email, "user", target.id, "membership_changed",
            f"{team.name}: {membership.level.value} -> {level.value}",
        )
        membership.level = level
        session.add(membership)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/memberships/{team_id}/remove")
def remove_membership(
    user_id: int,
    team_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    membership = session.get(TeamMembership, (user_id, team_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")

    team = session.get(Team, team_id)
    log_action(
        session, admin.email, "user", user_id, "membership_removed",
        f"{team.name if team else team_id}: {membership.level.value}",
    )
    session.delete(membership)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/active")
def set_user_active(
    user_id: int,
    is_active: bool = Form(...),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """An inactive person is no longer polled for out-of-office, cannot sign in, and every session of theirs
    ends at once (#157)."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if is_active and links.link_of(session, target) is not None:
        raise HTTPException(status_code=400, detail="This account was merged into another by a link; unlink it to use it again")
    if target.is_active != is_active:
        log_action(session, admin.email, "user", target.id, "activated" if is_active else "deactivated", "")
        target.is_active = is_active
        session.add(target)
        if not is_active:
            ended = revoke_user_sessions(session, target.id)
            if ended:
                log_action(session, admin.email, "user", target.id, "sessions_revoked", f"deactivated; {ended} ended")
            drop_votes_of(session, target, admin.email)  # their unanswered approval votes are dropped (#161)
        else:
            start_waiting_rounds(session, admin.email)  # a request waiting for approvers may now have one (#161)
        session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/sign-out")
def sign_out_user_everywhere(
    user_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Ends every signed-in session of this person (a lost laptop, a leaver). They can sign in again."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    ended = revoke_user_sessions(session, target.id)
    log_action(session, admin.email, "user", target.id, "signed_out_everywhere", f"by a super admin; {ended} ended")
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/clear-oid")
def clear_user_object_id(
    user_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Un-binds the Entra object id, so the next Entra sign-in with this email binds the new directory
    account (a re-hire, a recreated account). Their sessions end too. Never automatic (#157)."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.entra_object_id is None:
        raise HTTPException(status_code=400, detail="This user has no stored object id")
    log_action(session, admin.email, "user", target.id, "oid_cleared", f"was {target.entra_object_id}")
    target.entra_object_id = None
    session.add(target)
    revoke_user_sessions(session, target.id)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/unlock")
def unlock_user(
    user_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Clears the failed-attempt count and any lock on a local account."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    log_action(
        session, admin.email, "user", target.id, "unlocked",
        f"failed attempts {target.failed_login_count}, locked until {target.locked_until}",
    )
    target.failed_login_count = 0
    target.locked_until = None
    session.add(target)
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users")
def create_local_user(
    email: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    role: UserRole = Form(...),
    team_id: Optional[str] = Form(None),
    level: MembershipLevel = Form(MembershipLevel.MEMBER),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    existing = session.exec(select(User).where(User.email == email.lower())).first()
    if existing:
        raise HTTPException(status_code=400, detail="A user with that email already exists")

    parsed_team_id = optional_int(team_id)
    team = session.get(Team, parsed_team_id) if parsed_team_id is not None else None
    if parsed_team_id is not None and team is None:
        raise HTTPException(status_code=400, detail="Team does not exist")
    if team is not None and role == UserRole.END_USER:
        raise HTTPException(status_code=400, detail="Team access only applies to agents")

    local_user = User(
        email=email.lower(),
        display_name=display_name,
        role=role,
        password_hash=hash_password(password),
    )
    session.add(local_user)
    session.flush()
    log_action(session, admin.email, "user", local_user.id, "created", f"local account, role={role}")
    if team is not None:
        session.add(TeamMembership(user_id=local_user.id, team_id=team.id, level=level))
        log_action(session, admin.email, "user", local_user.id, "membership_added", f"{team.name}: {level.value}")
    session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/teams", response_class=HTMLResponse)
def list_teams(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    teams = session.exec(select(Team).order_by(Team.name)).all()
    member_counts = dict(
        session.exec(select(TeamMembership.team_id, func.count(TeamMembership.user_id)).group_by(TeamMembership.team_id)).all()
    )
    ticket_counts = dict(
        session.exec(select(Ticket.team_id, func.count(Ticket.id)).group_by(Ticket.team_id)).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_teams.html",
        {
            "teams": teams,
            "teams_by_id": {team.id: team for team in teams},
            "member_counts": member_counts,
            "ticket_counts": ticket_counts,
            "user": user,
        },
    )

@router.post("/teams/{team_id}/msp-handoff")
def set_team_msp_handoff(
    team_id: int,
    included: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Includes a team in (or leaves it out of) the automatic MSP handoff (app/msp.py)."""
    team = session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    want = bool(included)
    if team.msp_handoff_included != want:
        team.msp_handoff_included = want
        session.add(team)
        log_action(session, admin.email, "team", team.id, "msp_handoff_included" if want else "msp_handoff_excluded", team.name)
        session.commit()
    return RedirectResponse("/admin/teams", status_code=303)


@router.post("/teams")
def create_team(
    name: str = Form(...),
    parent_team_id: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    existing = session.exec(select(Team).where(Team.name == name)).first()
    if existing:
        raise HTTPException(status_code=400, detail="A team with that name already exists")

    parent_id = optional_int(parent_team_id)
    parent = session.get(Team, parent_id) if parent_id is not None else None
    if parent_id is not None and parent is None:
        raise HTTPException(status_code=400, detail="Parent team does not exist")
    if parent is not None and parent.parent_team_id is not None:
        raise HTTPException(status_code=400, detail="Teams go one level deep: a sub-team cannot have its own sub-teams")

    team = Team(name=name, parent_team_id=parent_id)
    session.add(team)
    session.flush()
    log_action(session, admin.email, "team", team.id, "created", f"{name} (sub-team of {parent.name})" if parent else name)
    session.commit()
    return RedirectResponse("/admin/teams", status_code=303)


@router.get("/feedback", response_class=HTMLResponse)
def view_feedback(
    request: Request,
    agent: Optional[str] = None,
    rating: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    agent_id, chosen_rating = optional_int(agent), optional_int(rating)
    entries = feedback_page(session, user, page, request.query_params, agent_id=agent_id, rating=chosen_rating)
    # Current agents plus anyone who has feedback (a resolver since demoted still has to be filterable).
    agents = resolvers_with_feedback(session)
    names = {u.id: u for u in agents}
    return templates.TemplateResponse(
        request,
        "admin_feedback.html",
        {"page": entries, "agents": agents, "names": names, "agent": agent_id, "rating": chosen_rating, "user": user},
    )


AUDIT_ENTITY_TYPES = ["ticket", "user", "team", "category", "setting", "email_template"]


@router.get("/audit-log", response_class=HTMLResponse)
def view_audit_log(
    request: Request,
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
    subject_user_id: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    parsed_subject_user_id = optional_int(subject_user_id)

    query = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if actor:
        query = query.where(AuditLog.actor_email == actor)
    if parsed_subject_user_id is not None:
        query = query.where(AuditLog.entity_type == "user", AuditLog.entity_id == parsed_subject_user_id)

    entries_page = paginate(session, query, page, request.query_params)
    actors = session.exec(select(AuditLog.actor_email).distinct().order_by(AuditLog.actor_email)).all()
    users = session.exec(select(User).order_by(User.email)).all()

    return templates.TemplateResponse(
        request,
        "admin_audit_log.html",
        {
            "page": entries_page,
            "entity_types": AUDIT_ENTITY_TYPES,
            "actors": actors,
            "users": users,
            "entity_type": entity_type,
            "actor": actor,
            "subject_user_id": parsed_subject_user_id,
            "user": user,
        },
    )


# ---- Categories ----------------------------------------------------------------


@router.get("/categories", response_class=HTMLResponse)
def list_categories(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    ensure_other(session)
    session.commit()
    categories = session.exec(select(Category).order_by(Category.name)).all()
    teams = session.exec(select(Team).order_by(Team.name)).all()
    mains = sorted((c for c in categories if c.parent_id is None), key=lambda c: (c.is_other, c.name.lower()))
    subs: dict[int, list[Category]] = {}
    for category in categories:
        if category.parent_id is not None:
            subs.setdefault(category.parent_id, []).append(category)
    return templates.TemplateResponse(
        request,
        "admin_categories.html",
        {
            "mains": mains,
            "subs": subs,
            "teams": teams,
            "teams_by_id": {team.id: team for team in teams},
            "user": user,
        },
    )


@router.post("/categories")
def add_category(
    name: str = Form(..., max_length=200),
    parent_id: Optional[str] = Form(None),
    team_id: Optional[str] = Form(None),
    escalation: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        category = create_category(
            session, name, optional_int(parent_id), optional_int(team_id), escalation or None
        )
    except CategoryError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "category", category.id, "created", path_of(session, category))
    default = "inherit" if category.parent_id is not None else "msp"
    if escalation and escalation != default:
        log_action(
            session, admin.email, "category", category.id, "escalation_changed",
            f"{path_of(session, category)}: {_escalation_label(session, category)}",
        )
    session.commit()
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/categories/{category_id}")
def update_category(
    category_id: int,
    team_id: Optional[str] = Form(None),
    escalation: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Sets a main category's team and where it escalates to, or a sub-category's
    escalation (inherit, MSP or a team), or switches a category on or off. Switching
    off hides it from triage; tickets already in it are untouched."""
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    if escalation is not None:
        _set_escalation(session, admin, category, escalation)

    if category.parent_id is None:
        new_team = optional_int(team_id)
        if new_team is not None and session.get(Team, new_team) is None:
            raise HTTPException(status_code=400, detail="That team does not exist")
        if new_team is None and not category.is_other:
            raise HTTPException(status_code=400, detail="A main category needs a team")
        if new_team != category.team_id:
            log_action(session, admin.email, "category", category.id, "team_changed", f"{category.team_id} -> {new_team}")
            category.team_id = new_team

    # Other has no tick-box (it is always on), so a missing field must not read as
    # 'switch off'. Anything else is on only when its box is ticked.
    active = True if category.is_other else is_active == "on"
    if active != category.is_active:
        log_action(session, admin.email, "category", category.id, "activated" if active else "deactivated", path_of(session, category))
        category.is_active = active

    session.add(category)
    session.commit()
    return RedirectResponse("/admin/categories", status_code=303)


def _escalation_label(session: Session, category: Category) -> str:
    """The audit wording for a category's escalation target: inherit, MSP or the team's name."""
    if category.escalation_inherits and category.parent_id is not None:
        return "inherit"
    return session.get(Team, category.escalation_team_id).name if category.escalation_team_id else "MSP"


def _set_escalation(session: Session, admin: User, category: Category, choice: str) -> None:
    """choice is "inherit" (sub-categories only), "msp", or a team id."""
    try:
        inherits, team_id = parse_escalation(session, category, choice)
    except CategoryError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if (category.escalation_inherits, category.escalation_team_id) != (inherits, team_id):
        category.escalation_inherits, category.escalation_team_id = inherits, team_id
        session.add(category)
        log_action(
            session, admin.email, "category", category.id, "escalation_changed",
            f"{path_of(session, category)}: {_escalation_label(session, category)}",
        )


# ---- Canned responses -----------------------------------------------------------


@router.get("/canned", response_class=HTMLResponse)
def list_canned(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "admin_canned.html",
        {
            "responses": all_canned(session),
            "placeholders": PLACEHOLDERS,
            "max_title_length": MAX_TITLE_LENGTH,
            "max_message_length": MAX_MESSAGE_LENGTH,
            "user": user,
        },
    )


@router.post("/canned")
def add_canned(
    title: str = Form(..., max_length=500),
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH + 1000),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        canned = create_canned(session, title, body)
    except CannedError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "canned_response", canned.id, "created", canned.title)
    session.commit()
    return RedirectResponse("/admin/canned", status_code=303)


@router.post("/canned/{canned_id}")
def edit_canned(
    canned_id: int,
    title: str = Form(..., max_length=500),
    body: str = Form(..., max_length=MAX_MESSAGE_LENGTH + 1000),
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    canned = session.get(CannedResponse, canned_id)
    if canned is None:
        raise HTTPException(status_code=404, detail="Canned response not found")
    was_active = canned.is_active
    old_title, old_body = canned.title, canned.body
    try:
        update_canned(session, canned, title, body, is_active == "on")
    except CannedError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if (canned.title, canned.body) != (old_title, old_body):
        log_action(
            session, admin.email, "canned_response", canned.id, "edited",
            f"{old_title!r} -> {canned.title!r}" if canned.title != old_title else canned.title,
        )
    if canned.is_active != was_active:
        log_action(
            session, admin.email, "canned_response", canned.id,
            "activated" if canned.is_active else "deactivated", canned.title,
        )
    session.commit()
    return RedirectResponse("/admin/canned", status_code=303)


# ---- Never-follow list (4B-2) ----------------------------------------------------------


@router.get("/never-follow", response_class=HTMLResponse)
def list_never_follow(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "admin_never_follow.html",
        {"rows": all_listed(session), "max_reason_length": MAX_REASON_LENGTH, "user": user},
    )


@router.post("/never-follow")
def add_never_follow(
    email: str = Form(..., max_length=254),
    reason: str = Form("", max_length=1000),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        address = normalise_email(email)
        row = add_listed(session, address, reason, admin.email)
    except InvalidEmail:
        raise HTTPException(status_code=400, detail="That is not a valid email address")
    except NeverFollowError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "never_follow", row.id, "added", f"{row.email}: {row.reason}" if row.reason else row.email)
    session.commit()
    return RedirectResponse("/admin/never-follow", status_code=303)


@router.post("/never-follow/{row_id}/remove")
def remove_never_follow(
    row_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    email = remove_listed(session, row_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Address not on the list")
    log_action(session, admin.email, "never_follow", row_id, "removed", email)
    session.commit()
    return RedirectResponse("/admin/never-follow", status_code=303)


# ---- Thank-you rules and review queue (4E-2) ---------------------------------------------


@router.get("/thanks-rules", response_class=HTMLResponse)
def list_thanks_rules(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "admin_thanks_rules.html",
        {
            "rules": all_thanks_rules(session), "candidates": pending_candidates(session),
            "match_types": THANKS_MATCH_TYPES, "max_pattern": THANKS_MAX_PATTERN, "max_length": THANKS_MAX_LENGTH,
            "user": user,
        },
    )


@router.post("/thanks-rules")
def add_thanks_rule_route(
    match_type: str = Form(..., max_length=20),
    pattern: str = Form(..., max_length=1000),
    max_length: str = Form(..., max_length=10),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        rule = add_thanks_rule(session, match_type, pattern, max_length, admin.email)
    except ThanksRuleError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "thanks_rule", rule.id, "added", describe_thanks_rule(rule))
    session.commit()
    return RedirectResponse("/admin/thanks-rules", status_code=303)


@router.post("/thanks-rules/candidates/{candidate_id}/rule")
def thanks_candidate_to_rule(
    candidate_id: int,
    match_type: str = Form(..., max_length=20),
    pattern: str = Form(..., max_length=1000),
    max_length: str = Form(..., max_length=10),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        made = candidate_to_rule(session, candidate_id, match_type, pattern, max_length, admin.email)
    except ThanksRuleError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if made is None:
        raise HTTPException(status_code=404, detail="That reply is not waiting for review")
    _, rule = made
    log_action(session, admin.email, "thanks_rule", rule.id, "added_from_review", describe_thanks_rule(rule))
    session.commit()
    return RedirectResponse("/admin/thanks-rules", status_code=303)


@router.post("/thanks-rules/candidates/{candidate_id}/dismiss")
def dismiss_thanks_candidate(
    candidate_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    row = dismiss_candidate(session, candidate_id, admin.email)
    if row is None:
        raise HTTPException(status_code=404, detail="That reply is not waiting for review")
    log_action(session, admin.email, "thanks_candidate", row.id, "dismissed", row.normalised_text[:200])
    session.commit()
    return RedirectResponse("/admin/thanks-rules", status_code=303)


@router.post("/thanks-rules/{rule_id}")
def edit_thanks_rule(
    rule_id: int,
    match_type: str = Form(..., max_length=20),
    pattern: str = Form(..., max_length=1000),
    max_length: str = Form(..., max_length=10),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        rule = update_thanks_rule(session, rule_id, match_type, pattern, max_length)
    except ThanksRuleError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    log_action(session, admin.email, "thanks_rule", rule.id, "edited", describe_thanks_rule(rule))
    session.commit()
    return RedirectResponse("/admin/thanks-rules", status_code=303)


@router.post("/thanks-rules/{rule_id}/{action}")
def toggle_thanks_rule(
    rule_id: int,
    action: str,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    if action not in ("enable", "disable"):
        raise HTTPException(status_code=404, detail="Not found")
    rule = set_thanks_rule_enabled(session, rule_id, action == "enable")
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    log_action(session, admin.email, "thanks_rule", rule.id, action + "d", describe_thanks_rule(rule))
    session.commit()
    return RedirectResponse("/admin/thanks-rules", status_code=303)


MAX_FORM_TEXT = 50000  # a generous cap on posted template text; the real limits are in app/email_templates.py


# ---- Email templates (4T) --------------------------------------------------------------


def _email_templates_page(
    request: Request, user: User, session: Session, error: Optional[str] = None,
    draft_key: Optional[str] = None, draft_subject: str = "", draft_body: str = "",
):
    """The list page. After a refused save the admin's own text is shown again in that template's
    form, with the reason, so nothing typed is lost."""
    rows = []
    for defn in EMAIL_TEMPLATES.values():
        subject, body = current_text(session, defn.key)
        subject = subject or ""
        if defn.key == draft_key:
            subject, body = draft_subject, draft_body
        try:
            preview, problem = render_preview(defn, subject, body), None
        except TemplateError as exc:
            preview, problem = None, str(exc)
        rows.append({
            "defn": defn, "subject": subject, "body": body, "customised": is_customised(session, defn.key),
            "preview": preview, "error": problem if defn.key == draft_key else None,
        })
    return templates.TemplateResponse(
        request,
        "admin_email_templates.html",
        {"rows": rows, "max_subject_length": MAX_EMAIL_SUBJECT_LENGTH, "max_body_length": MAX_EMAIL_BODY_LENGTH, "user": user},
        status_code=400 if error or draft_key else 200,
    )


@router.get("/email-templates", response_class=HTMLResponse)
def list_email_templates(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return _email_templates_page(request, user, session)


@router.post("/email-templates/{key}/preview", response_class=HTMLResponse)
def preview_email_template(
    request: Request,
    key: str,
    subject: str = Form("", max_length=MAX_FORM_TEXT),
    body: str = Form("", max_length=MAX_FORM_TEXT),
    user: User = Depends(require_super_admin),
):
    """The wording rendered with sample data, as the admin types. Saves nothing."""
    try:
        defn = email_template_def(key)
        preview, error = render_preview(defn, subject, body), None
    except TemplateError as problem:
        preview, error = None, str(problem)
    return templates.TemplateResponse(request, "_email_template_preview.html", {"preview": preview, "error": error})


@router.post("/email-templates/{key}")
def save_email_template(
    request: Request,
    key: str,
    subject: str = Form("", max_length=MAX_FORM_TEXT),
    body: str = Form(..., max_length=MAX_FORM_TEXT),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    if key not in EMAIL_TEMPLATES:
        raise HTTPException(status_code=404, detail="Email template not found")
    before = current_text(session, key)
    try:
        save_template(session, key, subject, body, admin.email)
    except TemplateError as error:
        session.rollback()
        return _email_templates_page(request, admin, session, str(error), key, subject, body)
    after = current_text(session, key)
    if after != before:
        changed = [name for name, a, b in (("subject", before[0], after[0]), ("text", before[1], after[1])) if a != b]
        log_action(session, admin.email, "email_template", 0, "edited", f"{key}: {' and '.join(changed)} changed")
    session.commit()
    return RedirectResponse("/admin/email-templates", status_code=303)


@router.post("/email-templates/{key}/reset")
def reset_email_template(
    key: str,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    if key not in EMAIL_TEMPLATES:
        raise HTTPException(status_code=404, detail="Email template not found")
    if reset_template(session, key):
        log_action(session, admin.email, "email_template", 0, "reset", f"{key}: back to the default")
    session.commit()
    return RedirectResponse("/admin/email-templates", status_code=303)


# ---- Approval groups --------------------------------------------------------------


@router.get("/approval-groups", response_class=HTMLResponse)
def list_approval_groups(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "admin_approval_groups.html",
        {
            "groups": [(g, members_of(session, g.id), types_using(session, g.id)) for g in all_groups(session)],
            "max_name_length": MAX_GROUP_NAME_LENGTH,
            "user": user,
        },
    )


@router.post("/approval-groups")
def add_approval_group(
    name: str = Form(..., max_length=500),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        group = create_group(session, name)
    except ApprovalGroupError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "approval_group", group.id, "created", group.name)
    session.commit()
    return RedirectResponse("/admin/approval-groups", status_code=303)


@router.post("/approval-groups/{group_id}/rename")
def rename_approval_group(
    group_id: int,
    name: str = Form(..., max_length=500),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    group = session.get(ApprovalGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Approval group not found")
    before = group.name
    try:
        rename_group(session, group, name)
    except ApprovalGroupError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if before != group.name:
        log_action(session, admin.email, "approval_group", group.id, "renamed", f"{before!r} -> {group.name!r}")
    session.commit()
    return RedirectResponse("/admin/approval-groups", status_code=303)


@router.post("/approval-groups/{group_id}/members")
def add_approval_group_member(
    group_id: int,
    email: str = Form(..., max_length=254),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    group = session.get(ApprovalGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Approval group not found")
    try:
        member = add_group_member(session, group, email)
    except ApprovalGroupError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "approval_group", group.id, "member_added", member.email)
    start_waiting_rounds(session, admin.email)  # a request waiting for approvers starts its round at once (#161)
    session.commit()
    return RedirectResponse("/admin/approval-groups", status_code=303)


@router.post("/approval-groups/{group_id}/members/{user_id}/remove")
def remove_approval_group_member(
    group_id: int,
    user_id: int,
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    group = session.get(ApprovalGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Approval group not found")
    try:
        member = remove_group_member(session, group, user_id)
    except ApprovalGroupError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "approval_group", group.id, "member_removed", member.email)
    session.commit()
    return RedirectResponse("/admin/approval-groups", status_code=303)


# ---- Request types (Service Requests) --------------------------------------------


def _type_snapshot(session: Session, request_type: RequestType) -> tuple:
    group = session.get(ApprovalGroup, request_type.approval_group_id) if request_type.approval_group_id else None
    return (
        request_type.name, request_type.description, group.name if group else "(none)",
        request_type.pass_percent, request_type.offers_required_by,
        request_type.due_days_before, request_type.queue_lead_days,
        _defaults_text(session, request_type), request_type.subrequests_need_own_approval,
        request_type.approvers_may_self_approve,
    )


def _defaults_text(session: Session, request_type: RequestType) -> str:
    team = session.get(Team, request_type.default_team_id) if request_type.default_team_id else None
    category = session.get(Category, request_type.default_category_id) if request_type.default_category_id else None
    return "team {}, category {}, impact {}, urgency {}".format(
        team.name if team else "(none)", path_of(session, category) if category else "(none)",
        request_type.default_impact or "(none)", request_type.default_urgency or "(none)",
    )


def _stale_default_categories(session: Session, types: list[RequestType]) -> dict[int, str]:
    """A type's default category that has been switched off, by id, so the edit form still shows it as chosen
    (saving then says why it cannot be kept) instead of quietly clearing it."""
    stale = {}
    for request_type in types:
        category = session.get(Category, request_type.default_category_id) if request_type.default_category_id else None
        if category is not None and not is_selectable(session, category):
            stale[category.id] = f"{path_of(session, category)} (switched off)"
    return stale


def _defaults_from_form(team_id, category_id, impact, urgency) -> TypeDefaults:
    return TypeDefaults(optional_int(team_id), optional_int(category_id), (impact or "").strip() or None, (urgency or "").strip() or None)


@router.get("/request-types", response_class=HTMLResponse)
def list_request_types(
    request: Request,
    user: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    types = all_request_types(session)
    sub_templates = all_templates_by_type(session)
    all_sub = [t for group in sub_templates.values() for t in group]
    question_labels = {q.id: q.label for group in all_questions_by_type(session).values() for q in group}
    return templates.TemplateResponse(
        request,
        "admin_request_types.html",
        {
            "types": types,
            "groups": all_groups(session),
            "default_pass": get_setting(session, APPROVAL_DEFAULT_PASS_PERCENT),
            "default_due_days": get_setting(session, DUE_DAYS_BEFORE_REQUIRED_BY),
            "default_queue_days": get_setting(session, REQUIRED_BY_LEAD_DAYS),
            "max_name_length": MAX_REQUEST_NAME_LENGTH,
            "max_description_length": MAX_REQUEST_DESCRIPTION_LENGTH,
            "teams": session.exec(select(Team).order_by(Team.name)).all(),
            "category_options": selectable(session),
            "impact_options": impact_options(),
            "urgency_options": urgency_options(),
            "stale_categories": _stale_default_categories(session, types),
            "self_triages": {t.id for t in types if self_triage_defaults(session, t) is not None},
            "questions": all_questions_by_type(session),
            "answered_questions": answered_question_ids(session),
            "question_kinds": QUESTION_KINDS,
            "sub_templates": sub_templates,
            "condition_text": {t.id: format_conditions(session, t.condition_groups, question_labels) for t in all_sub},
            "agents": session.exec(select(User).where(User.role == UserRole.AGENT, User.is_active).order_by(User.display_name)).all(),
            "user": user,
        },
    )


@router.post("/request-types")
def add_request_type(
    name: str = Form(..., max_length=500),
    description: str = Form("", max_length=5000),
    approval_group_id: Optional[str] = Form(None),
    pass_percent: int = Form(..., ge=1, le=100),
    is_active: Optional[str] = Form(None),
    offers_required_by: Optional[str] = Form(None),
    due_days_before: Optional[str] = Form(None),
    queue_lead_days: Optional[str] = Form(None),
    default_team_id: Optional[str] = Form(None),
    default_category_id: Optional[str] = Form(None),
    default_impact: Optional[str] = Form(None, max_length=20),
    default_urgency: Optional[str] = Form(None, max_length=20),
    subrequests_need_own_approval: Optional[str] = Form(None),
    approvers_may_self_approve: Optional[str] = Form(None),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    try:
        request_type = create_request_type(
            session, name, description, optional_int(approval_group_id), pass_percent, is_active == "on",
            offers_required_by == "on", optional_int(due_days_before), optional_int(queue_lead_days),
            defaults=_defaults_from_form(default_team_id, default_category_id, default_impact, default_urgency),
            subrequests_need_own_approval=subrequests_need_own_approval == "on",
            approvers_may_self_approve=approvers_may_self_approve == "on",
        )
    except RequestTypeError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "request_type", request_type.id, "created", request_type.name)
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/{type_id}")
def edit_request_type(
    type_id: int,
    name: str = Form(..., max_length=500),
    description: str = Form("", max_length=5000),
    approval_group_id: Optional[str] = Form(None),
    pass_percent: int = Form(..., ge=1, le=100),
    is_active: Optional[str] = Form(None),
    offers_required_by: Optional[str] = Form(None),
    due_days_before: Optional[str] = Form(None),
    queue_lead_days: Optional[str] = Form(None),
    default_team_id: Optional[str] = Form(None),
    default_category_id: Optional[str] = Form(None),
    default_impact: Optional[str] = Form(None, max_length=20),
    default_urgency: Optional[str] = Form(None, max_length=20),
    subrequests_need_own_approval: Optional[str] = Form(None),
    approvers_may_self_approve: Optional[str] = Form(None),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    request_type = session.get(RequestType, type_id)
    if request_type is None:
        raise HTTPException(status_code=404, detail="Request type not found")
    before = _type_snapshot(session, request_type)
    was_active = request_type.is_active
    try:
        update_request_type(
            session, request_type, name, description, optional_int(approval_group_id),
            pass_percent, is_active == "on", offers_required_by == "on",
            optional_int(due_days_before), optional_int(queue_lead_days),
            defaults=_defaults_from_form(default_team_id, default_category_id, default_impact, default_urgency),
            subrequests_need_own_approval=subrequests_need_own_approval == "on",
            approvers_may_self_approve=approvers_may_self_approve == "on",
        )
    except RequestTypeError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.flush()
    start_waiting_rounds(session, admin.email)  # a request waiting for approvers may have a group with someone to ask now (#161)
    after = _type_snapshot(session, request_type)
    if before != after:
        changes = []
        if before[0] != after[0]:
            changes.append(f"name {before[0]!r} -> {after[0]!r}")
        if before[1] != after[1]:
            changes.append("description changed")
        if before[2] != after[2]:
            changes.append(f"approval group {before[2]} -> {after[2]}")
        if before[3] != after[3]:
            changes.append(f"pass {before[3]}% -> {after[3]}%")
        if before[4] != after[4]:
            changes.append(f"Required by date offered {before[4]} -> {after[4]}")
        if before[5] != after[5]:
            changes.append(f"Due days before Required by {before[5]} -> {after[5]}")
        if before[6] != after[6]:
            changes.append(f"queue lead days {before[6]} -> {after[6]}")
        if before[7] != after[7]:
            changes.append(f"defaults {before[7]} -> {after[7]}")
        if before[8] != after[8]:
            changes.append(f"sub-requests of this type need their own approval {before[8]} -> {after[8]}")
        if before[9] != after[9]:
            changes.append(f"approvers may approve their own requests {before[9]} -> {after[9]}")
        log_action(session, admin.email, "request_type", request_type.id, "edited", "; ".join(changes))
    if request_type.is_active != was_active:
        log_action(
            session, admin.email, "request_type", request_type.id,
            "activated" if request_type.is_active else "deactivated", request_type.name,
        )
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


def _get_question(session: Session, question_id: int) -> RequestTypeQuestion:
    question = session.get(RequestTypeQuestion, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


def _template_args(
    subject_pattern, team_id, category_id, sub_request_type_id, impact, urgency, assignee, due_days_before,
    conditions, is_active,
) -> dict:
    """The form fields of a sub-request template as save_sub_template takes them. The assignee box holds an agent's id,
    "parent" (the parent's assignee) or nothing."""
    parent = (assignee or "").strip() == "parent"
    return dict(
        subject_pattern=subject_pattern, team_id=optional_int(team_id), category_id=optional_int(category_id),
        sub_request_type_id=optional_int(sub_request_type_id), impact=(impact or "").strip() or None,
        urgency=(urgency or "").strip() or None, assignee_id=None if parent else optional_int(assignee),
        assign_to_parent_assignee=parent, due_days_before=optional_int(due_days_before),
        conditions_text=conditions, is_active=is_active == "on",
    )


@router.post("/request-types/{type_id}/subrequest-templates")
def add_subrequest_template(
    type_id: int,
    subject_pattern: str = Form("", max_length=1000),
    team_id: Optional[str] = Form(None),
    category_id: Optional[str] = Form(None),
    sub_request_type_id: Optional[str] = Form(None),
    impact: Optional[str] = Form(None, max_length=20),
    urgency: Optional[str] = Form(None, max_length=20),
    assignee: Optional[str] = Form(None, max_length=20),
    due_days_before: Optional[str] = Form(None),
    conditions: str = Form("", max_length=5000),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    request_type = session.get(RequestType, type_id)
    if request_type is None:
        raise HTTPException(status_code=404, detail="Request type not found")
    try:
        template = save_sub_template(session, request_type, None, **_template_args(
            subject_pattern, team_id, category_id, sub_request_type_id, impact, urgency, assignee, due_days_before,
            conditions, "on",
        ))
    except SubTemplateError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "request_type", request_type.id, "subrequest_template_added", describe_template(session, template))
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


def _get_template(session: Session, template_id: int) -> RequestSubTemplate:
    template = session.get(RequestSubTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.post("/request-types/subrequest-templates/{template_id}")
def edit_subrequest_template(
    template_id: int,
    subject_pattern: str = Form("", max_length=1000),
    team_id: Optional[str] = Form(None),
    category_id: Optional[str] = Form(None),
    sub_request_type_id: Optional[str] = Form(None),
    impact: Optional[str] = Form(None, max_length=20),
    urgency: Optional[str] = Form(None, max_length=20),
    assignee: Optional[str] = Form(None, max_length=20),
    due_days_before: Optional[str] = Form(None),
    conditions: str = Form("", max_length=5000),
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    template = _get_template(session, template_id)
    request_type = session.get(RequestType, template.request_type_id)
    before = describe_template(session, template)
    try:
        save_sub_template(session, request_type, template, **_template_args(
            subject_pattern, team_id, category_id, sub_request_type_id, impact, urgency, assignee, due_days_before,
            conditions, is_active,
        ))
    except SubTemplateError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(
        session, admin.email, "request_type", request_type.id, "subrequest_template_edited",
        f"{before} -> {describe_template(session, template)}, available {template.is_active}",
    )
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/subrequest-templates/{template_id}/move")
def move_subrequest_template(
    template_id: int,
    direction: str = Form(..., max_length=10),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    template = _get_template(session, template_id)
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="Choose up or down")
    if move_template(session, template, direction == "up"):
        log_action(session, admin.email, "request_type", template.request_type_id, "subrequest_template_moved", f"{template.subject_pattern} {direction}")
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/subrequest-templates/{template_id}/delete")
def delete_subrequest_template(
    template_id: int,
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    template = _get_template(session, template_id)
    detail, type_id = describe_template(session, template), template.request_type_id
    delete_sub_template(session, template)
    log_action(session, admin.email, "request_type", type_id, "subrequest_template_deleted", detail)
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/{type_id}/questions")
def add_type_question(
    type_id: int,
    label: str = Form("", max_length=1000),
    kind: str = Form(..., max_length=20),
    options: str = Form("", max_length=5000),
    is_required: Optional[str] = Form(None),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    request_type = session.get(RequestType, type_id)
    if request_type is None:
        raise HTTPException(status_code=404, detail="Request type not found")
    try:
        question = add_question(session, request_type, label, kind, options, is_required == "on")
    except QuestionError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(
        session, admin.email, "request_type", request_type.id, "question_added",
        f"{question.label} ({QUESTION_KINDS[question.kind]}{', required' if question.is_required else ''})",
    )
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/questions/{question_id}")
def edit_type_question(
    question_id: int,
    label: str = Form("", max_length=1000),
    options: str = Form("", max_length=5000),
    is_required: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    question = _get_question(session, question_id)
    before = (question.label, list(question.options), question.is_required, question.is_active)
    try:
        update_question(session, question, label, options, is_required == "on", is_active == "on")
    except QuestionError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    if before != (question.label, list(question.options), question.is_required, question.is_active):
        log_action(
            session, admin.email, "request_type", question.request_type_id, "question_edited",
            f"{before[0]!r} -> {question.label!r}, required {question.is_required}, available {question.is_active}",
        )
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/questions/{question_id}/move")
def move_type_question(
    question_id: int,
    direction: str = Form(..., max_length=10),
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    question = _get_question(session, question_id)
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="Choose up or down")
    if move_question(session, question, direction == "up"):
        log_action(session, admin.email, "request_type", question.request_type_id, "question_moved", f"{question.label} {direction}")
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


@router.post("/request-types/questions/{question_id}/delete")
def delete_type_question(
    question_id: int,
    admin: User = Depends(require_request_templates),
    session: Session = Depends(get_session),
):
    question = _get_question(session, question_id)
    label, type_id = question.label, question.request_type_id
    try:
        delete_question(session, question)
    except QuestionError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "request_type", type_id, "question_deleted", label)
    session.commit()
    return RedirectResponse("/admin/request-types", status_code=303)


# ---- Category review ("Other" tickets, after resolution) ------------------------


def _review_queue(session: Session) -> list[Ticket]:
    return list(
        session.exec(
            select(Ticket)
            .where(Ticket.needs_category_review.is_(True), Ticket.status.in_(FINISHED_STATUSES))
            .order_by(Ticket.resolved_at.desc(), Ticket.id.desc())
        ).all()
    )


@router.get("/category-review", response_class=HTMLResponse)
def category_review(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    teams = session.exec(select(Team).order_by(Team.name)).all()
    options = [(c, label) for c, label in selectable(session) if not c.is_other]
    return templates.TemplateResponse(
        request,
        "admin_category_review.html",
        {
            "tickets": _review_queue(session),
            "category_options": options,
            "main_categories": [c for c, _ in options if c.parent_id is None],
            "teams": teams,
            "user": user,
        },
    )


@router.post("/category-review/{number}")
def review_category(
    number: int,
    category_id: Optional[str] = Form(None),
    new_category_name: Optional[str] = Form(None, max_length=200),
    new_category_parent_id: Optional[str] = Form(None),
    new_category_team_id: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Gives a resolved 'Other' ticket a real category: an existing one or a new
    one. The ticket's team is not changed (it is already done); only the category,
    so the next ticket like it has a home and reports are accurate."""
    ticket = get_by_number(session, number)
    if ticket is None or not ticket.needs_category_review or ticket.status not in FINISHED_STATUSES:
        raise HTTPException(status_code=404, detail="That ticket is not waiting for a category review")

    try:
        if new_category_name and new_category_name.strip():
            category = create_category(
                session, new_category_name, optional_int(new_category_parent_id), optional_int(new_category_team_id)
            )
            log_action(session, admin.email, "category", category.id, "created", path_of(session, category))
        else:
            chosen = optional_int(category_id)
            category = session.get(Category, chosen) if chosen is not None else None
            if category is None or category.is_other or not is_selectable(session, category):
                raise HTTPException(status_code=400, detail="Choose a category, or create a new one")
    except CategoryError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))

    previous = session.get(Category, ticket.category_id) if ticket.category_id else None
    ticket.category_id = category.id
    ticket.needs_category_review = False
    session.add(ticket)
    record_event(
        session, ticket, EVENT_CATEGORY_REVIEWED, admin.email,
        from_value=path_of(session, previous) if previous else None, to_value=path_of(session, category),
    )
    log_action(session, admin.email, "ticket", ticket.id, "category_reviewed", path_of(session, category))
    session.commit()
    return RedirectResponse("/admin/category-review", status_code=303)


# ---- Settings -------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def view_settings(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "definitions": list(REGISTRY.values()),
            "values": {key: get_setting(session, key) for key in REGISTRY},
            "teams": session.exec(select(Team).order_by(Team.name)).all(),
            "user": user,
        },
    )


@router.post("/settings")
def save_setting(
    key: str = Form(...),
    value: str = Form("", max_length=2000),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    definition = REGISTRY.get(key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown setting")

    value = value.strip()
    if len(value) > (500 if definition.kind == "regex" else 200) and definition.kind != "dates":
        raise HTTPException(status_code=400, detail="That is too long (200 characters at most)")
    if definition.kind == "email" and value:
        try:
            value = normalise_email(value)
        except InvalidEmail:
            raise HTTPException(status_code=400, detail="Enter one email address, or leave it blank")
    if definition.kind == "dates" and value:
        try:
            value = ", ".join(d.isoformat() for d in sorted(set(parse_dates(value))))
        except ValueError:
            raise HTTPException(status_code=400, detail="Enter dates like 2026-12-25, separated by commas or spaces")
    if definition.kind == "region" and value not in BANK_HOLIDAY_REGIONS:
        raise HTTPException(status_code=400, detail="Choose england-and-wales, scotland or northern-ireland")
    if definition.kind == "team" and value:
        if not (value.isascii() and value.isdigit()) or session.get(Team, int(value)) is None:
            raise HTTPException(status_code=400, detail="That team does not exist")

    if definition.kind == "minutes" and value:
        # Up to a year; a blank value means the target is not tracked.
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 525600:
            raise HTTPException(status_code=400, detail="Enter a whole number of minutes, 1 to 525600, or leave it blank")
    if definition.kind == "count":
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 1000:
            raise HTTPException(status_code=400, detail="Enter a whole number from 1 to 1000")
    if definition.kind == "off_minutes":
        if not (value.isascii() and value.isdigit()) or not 0 <= int(value) <= 525600:
            raise HTTPException(status_code=400, detail="Enter a whole number of minutes, 0 to 525600 (0 is off)")
    if definition.kind == "regex" and value:  # blank means the default pattern
        try:
            compile_subject_pattern(value)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    if definition.kind == "hour":
        if not (value.isascii() and value.isdigit()) or not 0 <= int(value) <= 23:
            raise HTTPException(status_code=400, detail="Enter a whole number from 0 to 23")
    if definition.kind == "clock":
        if parse_clock(value) is None:
            raise HTTPException(status_code=400, detail="Enter a time of day like 09:00 or 17:30")
        value = f"{parse_clock(value):%H:%M}"
        other_key = {MSP_BUSINESS_START: MSP_BUSINESS_END, MSP_BUSINESS_END: MSP_BUSINESS_START}.get(key)
        other = parse_clock(get_setting(session, other_key)) if other_key else None
        if other is not None and ((key == MSP_BUSINESS_START and value >= f"{other:%H:%M}") or (key == MSP_BUSINESS_END and value <= f"{other:%H:%M}")):
            raise HTTPException(status_code=400, detail="Business hours must end after they start")
    if definition.kind == "time_zone" and value and valid_zone(value) is None:
        raise HTTPException(status_code=400, detail="Enter an IANA time zone name such as Europe/London")
    if definition.kind == "flag" and value not in ("on", "off"):
        raise HTTPException(status_code=400, detail="Choose on or off")
    if definition.kind == "vote_percent":
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 100:
            raise HTTPException(status_code=400, detail="Enter a whole number from 1 to 100")
    if definition.kind == "percent":
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 99:
            raise HTTPException(status_code=400, detail="Enter a whole number from 1 to 99")

    previous = get_setting(session, key)
    if previous != value:
        set_setting(session, key, value)
        log_action(session, admin.email, "setting", 0, "changed", f"{key}: {previous!r} -> {value!r}")
        if key == BANK_HOLIDAY_REGION:  # fetch the new region at the next tick, not at the next slot
            job = session.get(ScheduledJob, "bank-holidays")
            if job is not None:
                job.next_run_at = datetime.now(timezone.utc)
                session.add(job)
    session.commit()
    return RedirectResponse("/admin/settings", status_code=303)

# ---- Activity log ---------------------------------------------------------------

ACTIVITY_EVENT_TYPES = [
    LOGIN_SUCCESS,
    LOGIN_FAILED,
    ACCOUNT_LOCKED,
    LOGOUT,
    SSO_FAILED,
    ACCESS_DENIED,
    REQUEST,
]


def _parse_day(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates look like 2026-10-31")


@router.get("/activity-log", response_class=HTMLResponse)
def view_activity_log(
    request: Request,
    event_type: Optional[str] = None,
    actor: Optional[str] = None,
    problems: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Sign-ins, failed sign-ins, lockouts, refused access and every state-changing
    request, newest first. 'Problems only' shows failures, lockouts, refusals and
    any request that came back as an error."""
    query = select(ActivityLog).order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
    if event_type:
        query = query.where(ActivityLog.event_type == event_type)
    if actor and actor.strip():
        # Part of an address is enough; wildcards the person typed are matched literally.
        needle = actor.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(ActivityLog.actor_email.like(f"%{needle}%", escape="\\"))
    if problems:
        query = query.where(or_(ActivityLog.event_type.in_(PROBLEM_EVENTS), ActivityLog.status_code >= 400))
    start, end = _parse_day(date_from), _parse_day(date_to)
    if start is not None and end is not None and end < start:
        raise HTTPException(status_code=400, detail="'To' date is before the 'From' date")
    if start is not None:
        query = query.where(ActivityLog.created_at >= start)
    if end is not None:
        query = query.where(ActivityLog.created_at < end + timedelta(days=1))

    rows_page = paginate(session, query, page, request.query_params)
    return templates.TemplateResponse(
        request,
        "admin_activity_log.html",
        {
            "page": rows_page,
            "event_types": ACTIVITY_EVENT_TYPES,
            "event_type": event_type,
            "actor": actor or "",
            "problems": problems,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "user": user,
        },
    )


# ---- Outbound mail --------------------------------------------------------------

@router.get("/outbound-mail", response_class=HTMLResponse)
def view_outbound_mail(
    request: Request,
    waiting: bool = False,
    page: int = 1,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Emails queued from the shared mailbox, newest first: what was sent (or in dev mode, would
    have been), to whom, and why a failed one failed. Approval emails show the text with the answer
    link left out. 'Waiting only' shows those not sent yet."""
    query = select(OutboundEmail).order_by(OutboundEmail.id.desc())
    if waiting:
        query = query.where(OutboundEmail.sent_at.is_(None))
    rows_page = paginate(session, query, page, request.query_params)
    tickets = {
        t.id: t
        for t in session.exec(select(Ticket).where(Ticket.id.in_({r.ticket_id for r in rows_page.items}))).all()
    } if rows_page.items else {}
    return templates.TemplateResponse(
        request,
        "admin_outbound_mail.html",
        {
            "page": rows_page, "tickets": tickets, "waiting": waiting, "mode": settings.mail_sending,
            "max_attempts": MAX_ATTEMPTS, "user": user,
        },
    )


# ---- Mail intake ----------------------------------------------------------------------------------

@router.get("/mail-intake", response_class=HTMLResponse)
def view_mail_intake(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Is mail reaching us? The Graph subscription (when it expires, when it was last renewed, the last
    error), the catch-up sync, and how many received notifications still wait to be processed. Tokens
    and the clientState are never shown."""
    now = datetime.now(timezone.utc)
    subscription = session.exec(
        select(GraphSubscription).where(GraphSubscription.resource == subscription_resource())
    ).first()
    sync = session.exec(select(MailSyncState).where(MailSyncState.folder == mail_folder())).first()
    waiting = session.exec(
        select(func.count()).select_from(GraphNotification).where(
            GraphNotification.processed_at.is_(None), GraphNotification.lifecycle_event.is_(None)
        )
    ).one()
    oldest = session.exec(
        select(func.min(GraphNotification.received_at)).where(
            GraphNotification.processed_at.is_(None), GraphNotification.lifecycle_event.is_(None)
        )
    ).one()
    flood_since = now - timedelta(hours=24)
    flood_day = session.exec(
        select(func.count()).select_from(InboundMail).where(
            InboundMail.outcome == IGNORED_FLOOD, InboundMail.created_at >= flood_since
        )
    ).one()
    flood_total = session.exec(
        select(func.count()).select_from(InboundMail).where(InboundMail.outcome == IGNORED_FLOOD)
    ).one()
    flood_recent = session.exec(
        select(InboundMail).where(InboundMail.outcome == IGNORED_FLOOD).order_by(InboundMail.created_at.desc()).limit(10)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_mail_intake.html",
        {
            "subscription": subscription, "sync": sync, "waiting": waiting, "oldest": oldest, "now": now,
            "flood_day": flood_day, "flood_total": flood_total, "flood_recent": flood_recent,
            "warnings": health_warnings(session, now),
            "user": user,
        },
    )


# ---- Recurring jobs -------------------------------------------------------------------------------

@router.get("/scheduled-jobs", response_class=HTMLResponse)
def view_scheduled_jobs(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """The recurring jobs driven by the scheduled_job table: when each is next due, when it last worked,
    and why it last failed, under one status line."""
    now = datetime.now(timezone.utc)
    rows = session.exec(select(ScheduledJob).order_by(ScheduledJob.name)).all()
    return templates.TemplateResponse(
        request,
        "admin_scheduled_jobs.html",
        {"rows": rows, "status": status_line(rows, now, timing(session, JOB_LATE_MINUTES), {job.name for job in SCHEDULED_JOBS}), "now": now, "user": user},
    )


@router.get("/outage", response_class=HTMLResponse)
def outage_page(
    request: Request, message: str = "", user: User = Depends(require_super_admin), session: Session = Depends(get_session),
):
    """Outage mode (#164, app/outage.py): the state, who may switch it on, and which agents have no emergency password yet."""
    agents = session.exec(
        select(User).where(User.role.in_([UserRole.AGENT, UserRole.SUPER_ADMIN]), User.is_active, User.is_break_glass.is_(False)).order_by(User.email)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_outage.html",
        {
            "user": user, "message": message, "status": outage.status(session),
            "without": [a for a in agents if not a.emergency_password_hash], "with_count": sum(1 for a in agents if a.emergency_password_hash),
        },
    )


@router.post("/outage/on")
def outage_on(user: User = Depends(require_super_admin), session: Session = Depends(get_session)):
    try:
        outage.turn_on(session, user)
    except outage.OutageError as refusal:
        session.rollback()
        return RedirectResponse("/admin/outage?" + urlencode({"message": str(refusal)}), status_code=303)
    session.commit()
    return RedirectResponse("/admin/outage?" + urlencode({"message": "Outage mode is on."}), status_code=303)


@router.post("/outage/off")
def outage_off(user: User = Depends(require_super_admin), session: Session = Depends(get_session)):
    try:
        result = outage.turn_off(session, user)
    except outage.OutageError as refusal:
        session.rollback()
        return RedirectResponse("/admin/outage?" + urlencode({"message": str(refusal)}), status_code=303)
    session.commit()
    message = f"Outage mode is off. {result.sessions_ended} emergency session(s) ended, {result.passwords_invalidated} emergency password(s) cleared."
    return RedirectResponse("/admin/outage?" + urlencode({"message": message}), status_code=303)

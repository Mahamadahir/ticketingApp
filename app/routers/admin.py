from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import or_
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
from app.audit import log_action
from app.auth import hash_password, require_roles
from app.categories import (
    CategoryError,
    create_category,
    ensure_other,
    is_selectable,
    parse_escalation,
    path_of,
    selectable,
)
from app.request_types import (
    MAX_APPROVERS,
    MAX_DESCRIPTION_LENGTH as MAX_REQUEST_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH as MAX_REQUEST_NAME_LENGTH,
    RequestTypeError,
    all_request_types,
    approvers_of,
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
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.models import (
    FINISHED_STATUSES,
    ActivityLog,
    AuditLog,
    CannedResponse,
    Category,
    GraphNotification,
    GraphSubscription,
    MailSyncState,
    OutboundEmail,
    RequestType,
    ScheduledJob,
    MembershipLevel,
    Team,
    TeamMembership,
    Ticket,
    User,
    UserRole,
)
from app.pagination import paginate
from app.scheduled_jobs import status_line
from app.settings_store import REGISTRY, get_setting, set_setting
from app.subscriptions import health_warnings, mail_folder, subscription_resource
from app.teams import MAX_ATTEMPTS
from app.ticket_service import EVENT_CATEGORY_REVIEWED, get_by_number, record_event

router = APIRouter(prefix="/admin", tags=["admin"])

require_super_admin = require_roles(UserRole.SUPER_ADMIN)


@router.get("/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    page: int = 1,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    users_page = paginate(session, select(User).order_by(User.email), page, request.query_params)
    teams = session.exec(select(Team).order_by(Team.name)).all()
    teams_by_id = {team.id: team for team in teams}

    user_ids = [u.id for u in users_page.items]
    memberships: dict[int, list[TeamMembership]] = {}
    if user_ids:
        rows = session.exec(select(TeamMembership).where(TeamMembership.user_id.in_(user_ids))).all()
        for row in rows:
            memberships.setdefault(row.user_id, []).append(row)

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "page": users_page,
            "teams": teams,
            "teams_by_id": teams_by_id,
            "memberships": memberships,
            "levels": list(MembershipLevel),
            "roles": list(UserRole),
            "now": datetime.now(timezone.utc),
            "user": user,
        },
    )


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

    if target.role != role:
        log_action(session, admin.email, "user", target.id, "role_changed", f"{target.role.value} -> {role.value}")
        target.role = role

    session.add(target)
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


# ---- Request types (Service Requests) --------------------------------------------


@router.get("/request-types", response_class=HTMLResponse)
def list_request_types(
    request: Request,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    types = all_request_types(session)
    return templates.TemplateResponse(
        request,
        "admin_request_types.html",
        {
            "types": [(t, ", ".join(a.email for a in approvers_of(session, t))) for t in types],
            "max_name_length": MAX_REQUEST_NAME_LENGTH,
            "max_description_length": MAX_REQUEST_DESCRIPTION_LENGTH,
            "max_approvers": MAX_APPROVERS,
            "user": user,
        },
    )


@router.post("/request-types")
def add_request_type(
    name: str = Form(..., max_length=500),
    description: str = Form("", max_length=5000),
    approvers: str = Form("", max_length=5000),
    approvals_required: int = Form(1, ge=1, le=100),
    declines_required: int = Form(1, ge=1, le=100),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        request_type = create_request_type(session, name, description, approvers, approvals_required, declines_required)
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
    approvers: str = Form("", max_length=5000),
    approvals_required: int = Form(1, ge=1, le=100),
    declines_required: int = Form(1, ge=1, le=100),
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    request_type = session.get(RequestType, type_id)
    if request_type is None:
        raise HTTPException(status_code=404, detail="Request type not found")
    before = (
        request_type.name, request_type.description, sorted(a.email for a in approvers_of(session, request_type)),
        (request_type.approvals_required, request_type.declines_required),
    )
    was_active = request_type.is_active
    try:
        update_request_type(
            session, request_type, name, description, approvers, is_active == "on",
            approvals_required, declines_required,
        )
    except RequestTypeError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.flush()
    after = (
        request_type.name, request_type.description, sorted(a.email for a in approvers_of(session, request_type)),
        (request_type.approvals_required, request_type.declines_required),
    )
    if before != after:
        changes = []
        if before[0] != after[0]:
            changes.append(f"name {before[0]!r} -> {after[0]!r}")
        if before[1] != after[1]:
            changes.append("description changed")
        if before[2] != after[2]:
            changes.append(f"approvers {before[2]} -> {after[2]}")
        if before[3] != after[3]:
            changes.append(f"needs {before[3][0]} approvals / {before[3][1]} declines -> {after[3][0]} / {after[3][1]}")
        log_action(session, admin.email, "request_type", request_type.id, "edited", "; ".join(changes))
    if request_type.is_active != was_active:
        log_action(
            session, admin.email, "request_type", request_type.id,
            "activated" if request_type.is_active else "deactivated", request_type.name,
        )
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
    value: str = Form("", max_length=200),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    definition = REGISTRY.get(key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown setting")

    value = value.strip()
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
    if definition.kind == "flag" and value not in ("on", "off"):
        raise HTTPException(status_code=400, detail="Choose on or off")
    if definition.kind == "percent":
        if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= 99:
            raise HTTPException(status_code=400, detail="Enter a whole number from 1 to 99")

    previous = get_setting(session, key)
    if previous != value:
        set_setting(session, key, value)
        log_action(session, admin.email, "setting", 0, "changed", f"{key}: {previous!r} -> {value!r}")
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
    return templates.TemplateResponse(
        request,
        "admin_mail_intake.html",
        {
            "subscription": subscription, "sync": sync, "waiting": waiting, "oldest": oldest, "now": now,
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
        {"rows": rows, "status": status_line(rows, now), "now": now, "user": user},
    )

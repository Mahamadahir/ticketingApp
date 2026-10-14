from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    path_of,
    selectable,
)
from app.canned import (
    MAX_TITLE_LENGTH,
    PLACEHOLDERS,
    CannedError,
    all_canned,
    create_canned,
    update_canned,
)
from app.database import get_session
from app.forms import MAX_MESSAGE_LENGTH, optional_int
from app.models import (
    FINISHED_STATUSES,
    ActivityLog,
    AuditLog,
    CannedResponse,
    Category,
    MembershipLevel,
    Team,
    TeamMembership,
    Ticket,
    User,
    UserRole,
)
from app.pagination import paginate
from app.settings_store import REGISTRY, get_setting, set_setting
from app.ticket_service import EVENT_CATEGORY_REVIEWED, get_by_number, record_event

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")

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


AUDIT_ENTITY_TYPES = ["ticket", "user", "team", "category", "setting"]


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
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    try:
        category = create_category(session, name, optional_int(parent_id), optional_int(team_id))
    except CategoryError as error:
        raise HTTPException(status_code=400, detail=str(error))
    log_action(session, admin.email, "category", category.id, "created", path_of(session, category))
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


def _set_escalation(session: Session, admin: User, category: Category, choice: str) -> None:
    """choice is "inherit" (sub-categories only), "msp", or a team id."""
    inherits, team_id = False, None
    if choice == "inherit":
        if category.parent_id is None:
            raise HTTPException(status_code=400, detail="A main category sets its own escalation target")
        inherits = True
    elif choice != "msp":
        team_id = optional_int(choice)
        if team_id is None or session.get(Team, team_id) is None:
            raise HTTPException(status_code=400, detail="That team does not exist")
    if category.parent_id is None:
        inherits = category.escalation_inherits  # unused for a main category; leave as it was
    if (category.escalation_inherits, category.escalation_team_id) != (inherits, team_id):
        log_action(
            session, admin.email, "category", category.id, "escalation_changed",
            f"{path_of(session, category)}: {choice}",
        )
        category.escalation_inherits = inherits
        category.escalation_team_id = team_id
        session.add(category)


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

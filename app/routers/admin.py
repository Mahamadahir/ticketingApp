from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.audit import log_action
from app.auth import hash_password, require_roles
from app.categories import (
    CategoryError,
    create_category,
    ensure_other,
    path_of,
    selectable,
)
from app.database import get_session
from app.forms import optional_int
from app.models import (
    FINISHED_STATUSES,
    AuditLog,
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
from app.ticket_service import EVENT_CATEGORY_REVIEWED, record_event

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
            "member_counts": member_counts,
            "ticket_counts": ticket_counts,
            "user": user,
        },
    )

@router.post("/teams")
def create_team(
    name: str = Form(...),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    existing = session.exec(select(Team).where(Team.name == name)).first()
    if existing:
        raise HTTPException(status_code=400, detail="A team with that name already exists")

    team = Team(name=name)
    session.add(team)
    session.flush()
    log_action(session, admin.email, "team", team.id, "created", name)
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
    is_active: Optional[str] = Form(None),
    admin: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Sets a main category's team, or switches a category on or off. Switching
    off hides it from triage; tickets already in it are untouched."""
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    if category.parent_id is None:
        new_team = optional_int(team_id)
        if new_team is not None and session.get(Team, new_team) is None:
            raise HTTPException(status_code=400, detail="That team does not exist")
        if new_team is None and not category.is_other:
            raise HTTPException(status_code=400, detail="A main category needs a team")
        if new_team != category.team_id:
            log_action(session, admin.email, "category", category.id, "team_changed", f"{category.team_id} -> {new_team}")
            category.team_id = new_team

    active = is_active == "on"
    if category.is_other and not active:
        raise HTTPException(status_code=400, detail="'Other' cannot be switched off")
    if active != category.is_active:
        log_action(session, admin.email, "category", category.id, "activated" if active else "deactivated", path_of(session, category))
        category.is_active = active

    session.add(category)
    session.commit()
    return RedirectResponse("/admin/categories", status_code=303)


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
    return templates.TemplateResponse(
        request,
        "admin_category_review.html",
        {
            "tickets": _review_queue(session),
            "category_options": [(c, label) for c, label in selectable(session) if not c.is_other],
            "main_categories": [c for c, _ in selectable(session) if c.parent_id is None and not c.is_other],
            "teams": teams,
            "user": user,
        },
    )


@router.post("/category-review/{ticket_id}")
def review_category(
    ticket_id: int,
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
    ticket = session.get(Ticket, ticket_id)
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
            if category is None or category.is_other or not category.is_active:
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
        if not value.isdigit() or session.get(Team, int(value)) is None:
            raise HTTPException(status_code=400, detail="That team does not exist")

    previous = get_setting(session, key)
    if previous != value:
        set_setting(session, key, value)
        log_action(session, admin.email, "setting", 0, "changed", f"{key}: {previous!r} -> {value!r}")
    session.commit()
    return RedirectResponse("/admin/settings", status_code=303)
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.audit import log_action
from app.auth import hash_password, require_roles
from app.database import get_session
from app.forms import optional_int
from app.models import AuditLog, MembershipLevel, Team, TeamMembership, Ticket, User, UserRole
from app.pagination import paginate

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


AUDIT_ENTITY_TYPES = ["ticket", "user", "team"]


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

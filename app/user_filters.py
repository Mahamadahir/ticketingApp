"""The filters of Admin > Users (9I): name or email, role, team and status, applied to a `select(User)`.
A value that means nothing (an unknown role, a team id that is not a number) is ignored."""

from sqlalchemy import or_
from sqlmodel import select

from app.models import TeamMembership, User, UserRole

USER_STATUS_LABELS = {"active": "Active", "inactive": "Inactive", "local": "Local account", "break_glass": "Break-glass"}


def filter_users(query, q: str, role: str, team: str, status: str):
    """Narrows the Users list by the plain-GET filters (a value that means nothing is ignored)."""
    q = q.strip()
    if q:
        query = query.where(or_(User.email.icontains(q, autoescape=True), User.display_name.icontains(q, autoescape=True)))
    if role in {r.value for r in UserRole}:
        query = query.where(User.role == UserRole(role))
    team = team.strip()
    if team.isascii() and team.isdigit() and int(team) < 2**31:
        query = query.where(
            select(TeamMembership.user_id).where(TeamMembership.user_id == User.id, TeamMembership.team_id == int(team)).exists()
        )
    if status == "active":
        query = query.where(User.is_active.is_(True))
    elif status == "inactive":
        query = query.where(User.is_active.is_(False))
    elif status == "local":
        query = query.where(User.password_hash.is_not(None), User.is_break_glass.is_(False))
    elif status == "break_glass":
        query = query.where(User.is_break_glass.is_(True))
    return query

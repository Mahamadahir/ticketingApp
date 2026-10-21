"""Approval groups (phase 4C-1): named sets of people who vote on requests.

A request type points at one group (app/request_types.py); when a request of that type is triaged,
the group's members become its voters (app/approvals.py). Admins manage groups under Admin >
Approval groups. A member needs an account (they sign in once first) and need not be an agent:
approvers are often managers. Groups are never deleted, so an approval keeps the group it was asked of.
"""

from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.followers import InvalidEmail, normalise_email
from app.models import ApprovalGroup, ApprovalGroupMember, RequestType, User

MAX_NAME_LENGTH = 100


class ApprovalGroupError(ValueError):
    """A reason a group cannot be saved, worded for the admin."""


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ApprovalGroupError("A group needs a name")
    if len(name) > MAX_NAME_LENGTH:
        raise ApprovalGroupError(f"The name can be at most {MAX_NAME_LENGTH} characters")
    return name


def _name_taken(session: Session, name: str, except_id: Optional[int]) -> bool:
    query = select(ApprovalGroup.id).where(func.lower(ApprovalGroup.name) == name.lower())
    if except_id is not None:
        query = query.where(ApprovalGroup.id != except_id)
    return session.exec(query).first() is not None


def _flush(session: Session) -> None:
    """Writes now, so two admins using the same name or adding the same person at once get the
    friendly error (the database constraint is the last line of defence)."""
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ApprovalGroupError("That was already done by someone else")


def create_group(session: Session, name: str) -> ApprovalGroup:
    name = _clean_name(name)
    if _name_taken(session, name, None):
        raise ApprovalGroupError("There is already an approval group with that name")
    group = ApprovalGroup(name=name)
    session.add(group)
    _flush(session)
    return group


def rename_group(session: Session, group: ApprovalGroup, name: str) -> ApprovalGroup:
    name = _clean_name(name)
    if _name_taken(session, name, group.id):
        raise ApprovalGroupError("There is already an approval group with that name")
    group.name = name
    session.add(group)
    _flush(session)
    return group


def all_groups(session: Session) -> list[ApprovalGroup]:
    return list(session.exec(select(ApprovalGroup).order_by(func.lower(ApprovalGroup.name))).all())


def members_of(session: Session, group_id: Optional[int]) -> list[User]:
    if group_id is None:
        return []
    return list(
        session.exec(
            select(User)
            .join(ApprovalGroupMember, ApprovalGroupMember.user_id == User.id)
            .where(ApprovalGroupMember.group_id == group_id)
            .order_by(User.email)
        ).all()
    )


def is_member(session: Session, group_id: int, user_id: int) -> bool:
    return session.exec(
        select(ApprovalGroupMember.id).where(
            ApprovalGroupMember.group_id == group_id, ApprovalGroupMember.user_id == user_id
        )
    ).first() is not None


def add_member(session: Session, group: ApprovalGroup, email: str) -> User:
    """Adds the person with this address. They must already have an account, so a typo cannot
    silently leave a request without its voter."""
    try:
        email = normalise_email(email)
    except InvalidEmail:
        raise ApprovalGroupError(f"{(email or '').strip()} is not a valid email address")
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        raise ApprovalGroupError(f"{email} has no account yet: they need to sign in once first")
    if is_member(session, group.id, user.id):
        raise ApprovalGroupError(f"{email} is already in this group")
    session.add(ApprovalGroupMember(group_id=group.id, user_id=user.id))
    _flush(session)
    return user


def remove_member(session: Session, group: ApprovalGroup, user_id: int) -> User:
    """Takes someone out. They stay a voter on approvals already asked (the number of voters is fixed
    then) but can no longer answer them; a super admin can waive a request that can never be decided."""
    row = session.exec(
        select(ApprovalGroupMember).where(
            ApprovalGroupMember.group_id == group.id, ApprovalGroupMember.user_id == user_id
        )
    ).first()
    user = session.get(User, user_id)
    if row is None or user is None:
        raise ApprovalGroupError("That person is not in this group")
    in_use = group_is_used_by_active_type(session, group.id)
    if in_use and len(members_of(session, group.id)) == 1:
        raise ApprovalGroupError(f"{in_use} is available on the request form and uses this group: it cannot be left empty")
    session.delete(row)
    session.flush()
    return user


def group_is_used_by_active_type(session: Session, group_id: int) -> Optional[str]:
    """The name of an active request type that uses the group, or None."""
    return session.exec(
        select(RequestType.name).where(RequestType.approval_group_id == group_id, RequestType.is_active).limit(1)
    ).first()


def types_using(session: Session, group_id: int) -> list[RequestType]:
    return list(
        session.exec(
            select(RequestType).where(RequestType.approval_group_id == group_id).order_by(func.lower(RequestType.name))
        ).all()
    )

"""The request catalogue (phase 1H): the kinds of service request people can raise, and who
approves each. Admins manage it; the request form lists the active types.

A type points at one approval group (app/approval_groups.py) and carries the two percentages that
settle a request (app/approvals.py): how much of the group must approve for it to pass, and how much
must decline for it to fail. An active type must have a group with at least one member.
"""

from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.approval_groups import members_of
from app.approvals import PercentError, check_percentages
from app.models import ApprovalGroup, RequestType, User

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000


class RequestTypeError(ValueError):
    """A reason a request type cannot be saved, worded for the admin."""


def _clean(name: str, description: str) -> tuple[str, str]:
    name, description = (name or "").strip(), (description or "").strip()
    if not name:
        raise RequestTypeError("A request type needs a name")
    if len(name) > MAX_NAME_LENGTH:
        raise RequestTypeError(f"The name can be at most {MAX_NAME_LENGTH} characters")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise RequestTypeError(f"The description can be at most {MAX_DESCRIPTION_LENGTH} characters")
    return name, description


def _name_taken(session: Session, name: str, except_id: Optional[int]) -> bool:
    query = select(RequestType.id).where(func.lower(RequestType.name) == name.lower())
    if except_id is not None:
        query = query.where(RequestType.id != except_id)
    return session.exec(query).first() is not None


def _checked(session: Session, group_id: Optional[int], pass_percent: int, fail_percent: int, is_active: bool) -> None:
    if group_id is not None and session.get(ApprovalGroup, group_id) is None:
        raise RequestTypeError("That approval group does not exist")
    try:
        check_percentages(pass_percent, fail_percent)
    except PercentError as error:
        raise RequestTypeError(str(error))
    if is_active and not members_of(session, group_id):
        raise RequestTypeError(
            "An active request type needs an approval group with at least one member (switch it off to save it without one)"
        )


def approvers_of(session: Session, request_type: RequestType) -> list[User]:
    """The members of the type's group today."""
    return members_of(session, request_type.approval_group_id)


def _flush(session: Session) -> None:
    """Writes now, so two admins saving the same name at once get the friendly error
    (the database index is the last line of defence) rather than a server error."""
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise RequestTypeError("There is already a request type with that name")


def create_request_type(
    session: Session, name: str, description: str, group_id: Optional[int],
    pass_percent: int, fail_percent: int, is_active: bool = True,
) -> RequestType:
    name, description = _clean(name, description)
    if _name_taken(session, name, None):
        raise RequestTypeError("There is already a request type with that name")
    _checked(session, group_id, pass_percent, fail_percent, is_active)
    request_type = RequestType(
        name=name, description=description, is_active=is_active, approval_group_id=group_id,
        pass_percent=pass_percent, fail_percent=fail_percent,
    )
    session.add(request_type)
    _flush(session)
    return request_type


def update_request_type(
    session: Session, request_type: RequestType, name: str, description: str, group_id: Optional[int],
    pass_percent: int, fail_percent: int, is_active: bool,
) -> RequestType:
    name, description = _clean(name, description)
    if _name_taken(session, name, request_type.id):
        raise RequestTypeError("There is already a request type with that name")
    _checked(session, group_id, pass_percent, fail_percent, is_active)
    request_type.name, request_type.description, request_type.is_active = name, description, is_active
    request_type.approval_group_id = group_id
    request_type.pass_percent, request_type.fail_percent = pass_percent, fail_percent
    session.add(request_type)
    _flush(session)
    return request_type


def request_type_of(session: Session, ticket) -> Optional[tuple[RequestType, Optional[ApprovalGroup]]]:
    """The request type a ticket was raised as, with its approval group, or None."""
    if ticket.request_type_id is None:
        return None
    request_type = session.get(RequestType, ticket.request_type_id)
    if request_type is None:
        return None
    group = session.get(ApprovalGroup, request_type.approval_group_id) if request_type.approval_group_id else None
    return request_type, group


def all_request_types(session: Session) -> list[RequestType]:
    return list(session.exec(select(RequestType).order_by(func.lower(RequestType.name))).all())


def active_request_types(session: Session) -> list[RequestType]:
    return list(
        session.exec(
            select(RequestType).where(RequestType.is_active).order_by(func.lower(RequestType.name))
        ).all()
    )

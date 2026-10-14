"""The request catalogue (phase 1H): the kinds of service request people can raise, and who
approves each. Admins manage it; the request form lists the active types.

Approvers are configured here and shown on the ticket. Asking them to approve, and holding
the work until they have, is the next step (1H-ii).
"""

from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.followers import InvalidEmail, normalise_email
from app.models import RequestType, RequestTypeApprover, User

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_APPROVERS = 10


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


def parse_approvers(session: Session, text: str) -> list[User]:
    """The accounts named by a list of email addresses (commas, semicolons or new lines).
    Every address must belong to an account, so a typo cannot silently leave a request
    without its approver; someone who has never signed in has no account yet."""
    emails = []
    for part in (text or "").replace(";", ",").replace("\n", ",").split(","):
        if not part.strip():
            continue
        try:
            email = normalise_email(part)
        except InvalidEmail:
            raise RequestTypeError(f"{part.strip()} is not a valid email address")
        if email not in emails:
            emails.append(email)
    if len(emails) > MAX_APPROVERS:
        raise RequestTypeError(f"A request type can have at most {MAX_APPROVERS} approvers")
    users = []
    for email in emails:
        user = session.exec(select(User).where(User.email == email)).first()
        if user is None:
            raise RequestTypeError(f"{email} has no account yet: they need to sign in once first")
        users.append(user)
    return users


def approvers_of(session: Session, request_type: RequestType) -> list[User]:
    rows = session.exec(
        select(User)
        .join(RequestTypeApprover, RequestTypeApprover.user_id == User.id)
        .where(RequestTypeApprover.request_type_id == request_type.id)
        .order_by(User.email)
    ).all()
    return list(rows)


def _set_approvers(session: Session, request_type: RequestType, users: list[User]) -> None:
    wanted = {u.id for u in users}
    existing = session.exec(
        select(RequestTypeApprover).where(RequestTypeApprover.request_type_id == request_type.id)
    ).all()
    for row in existing:
        if row.user_id not in wanted:
            session.delete(row)
    have = {row.user_id for row in existing}
    for user_id in wanted - have:
        session.add(RequestTypeApprover(request_type_id=request_type.id, user_id=user_id))


def create_request_type(
    session: Session, name: str, description: str, approvers_text: str
) -> RequestType:
    name, description = _clean(name, description)
    if _name_taken(session, name, None):
        raise RequestTypeError("There is already a request type with that name")
    users = parse_approvers(session, approvers_text)
    request_type = RequestType(name=name, description=description)
    session.add(request_type)
    session.flush()
    _set_approvers(session, request_type, users)
    session.flush()
    return request_type


def update_request_type(
    session: Session, request_type: RequestType, name: str, description: str, approvers_text: str, is_active: bool
) -> RequestType:
    name, description = _clean(name, description)
    if _name_taken(session, name, request_type.id):
        raise RequestTypeError("There is already a request type with that name")
    users = parse_approvers(session, approvers_text)
    request_type.name, request_type.description, request_type.is_active = name, description, is_active
    session.add(request_type)
    _set_approvers(session, request_type, users)
    session.flush()
    return request_type


def request_type_of(session: Session, ticket) -> Optional[tuple[RequestType, list[User]]]:
    """The request type a ticket was raised as, with its approvers, or None."""
    if ticket.request_type_id is None:
        return None
    request_type = session.get(RequestType, ticket.request_type_id)
    return (request_type, approvers_of(session, request_type)) if request_type is not None else None


def all_request_types(session: Session) -> list[RequestType]:
    return list(session.exec(select(RequestType).order_by(func.lower(RequestType.name))).all())


def active_request_types(session: Session) -> list[RequestType]:
    return list(
        session.exec(
            select(RequestType).where(RequestType.is_active).order_by(func.lower(RequestType.name))
        ).all()
    )
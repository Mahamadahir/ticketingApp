"""The request catalogue (phase 1H): the kinds of service request people can raise, and who
approves each. Admins manage it; the request form lists the active types.

A type points at one approval group (app/approval_groups.py) and carries the two percentages that
settle a request (app/approvals.py): how much of the group must approve for it to pass, and how much
must decline for it to fail. An active type must have a group with at least one member.

A type can also carry defaults: a team, a category, an impact and an urgency (#133). The admin gives them the same
checks as triage (an active category that belongs to the team). With all four set, a portal request of the type
triages itself when raised (`self_triage`); with some set they only prefill the triage form.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.approval_groups import members_of
from app.approvals import PercentError, check_percentages
from app.audit import log_action
from app.categories import is_selectable, path_of, team_id_for
from app.models import ApprovalGroup, Category, RequestType, Team, Ticket, TicketSource, TicketType
from app.request_questions import missing_required
from app.sla import Level
from app.triage import triage_ticket

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


def _checked(session: Session, group_id: Optional[int], pass_percent: int, is_active: bool) -> None:
    if group_id is not None and session.get(ApprovalGroup, group_id) is None:
        raise RequestTypeError("That approval group does not exist")
    try:
        check_percentages(pass_percent)
    except PercentError as error:
        raise RequestTypeError(str(error))
    if is_active and not members_of(session, group_id, active_only=True):
        raise RequestTypeError(
            "An active request type needs an approval group with at least one member (switch it off to save it without one)"
        )


MAX_LEAD_DAYS = 365


@dataclass(frozen=True)
class TypeDefaults:
    """What an admin chose for a type's defaults (all optional)."""

    team_id: Optional[int] = None
    category_id: Optional[int] = None
    impact: Optional[str] = None
    urgency: Optional[str] = None


def _check_defaults(session: Session, defaults: TypeDefaults) -> None:
    """The same checks as triage: the team exists, the category is in use (and so is its parent) and, with both
    chosen, the category belongs to that team; impact and urgency are real levels."""
    team = session.get(Team, defaults.team_id) if defaults.team_id is not None else None
    if defaults.team_id is not None and team is None:
        raise RequestTypeError("That team does not exist")
    category = session.get(Category, defaults.category_id) if defaults.category_id is not None else None
    if defaults.category_id is not None:
        if category is None or not is_selectable(session, category):
            raise RequestTypeError("That category is no longer in use")
        category_team = team_id_for(session, category)
        if category_team is None:
            raise RequestTypeError(
                "That category has no team yet. Ask a super admin to give it one "
                "(for 'Other', set a default triage team under Settings)."
            )
        if team is not None and category_team != team.id:
            raise RequestTypeError(f"That category belongs to a different team than {team.name}")
    for value, label in ((defaults.impact, "impact"), (defaults.urgency, "urgency")):
        if value is not None and value not in {level.value for level in Level}:
            raise RequestTypeError(f"Choose the {label} from the list")


def self_triage_defaults(session: Session, request_type: RequestType) -> Optional[Category]:
    """The category a portal request of this type is triaged into by itself, or None when the type's defaults are
    not all set or no longer hold (the category was switched off, or moved to another team): such a request
    waits in New for an agent, as it always did."""
    if None in (
        request_type.default_team_id, request_type.default_category_id,
        request_type.default_impact, request_type.default_urgency,
    ):
        return None
    try:
        defaults = TypeDefaults(
            request_type.default_team_id, request_type.default_category_id,
            request_type.default_impact, request_type.default_urgency,
        )
        _check_defaults(session, defaults)
    except RequestTypeError:
        return None
    return session.get(Category, request_type.default_category_id)


def self_triage(session: Session, ticket: Ticket, request_type: RequestType) -> bool:
    """Triages a just-raised portal request through the real `triage_ticket` with the system as the actor, when its
    type's defaults are all set: the type becomes Service Request, the type's team (through its category) and
    priority apply, the SLA starts, and approvals start as for an agent's triage (Awaiting approval with a group,
    else Open unassigned in the team queue). Returns whether it did. Only when the type's required form questions
    are answered (app/request_questions.py). An email- or Teams-raised ticket never comes
    here. Records the triaged event (naming the type) and a `triaged_automatically` audit entry. Does not commit."""
    if ticket.triaged_at is not None or ticket.source != TicketSource.PORTAL:
        return False
    category = self_triage_defaults(session, request_type)
    if category is None:
        return False
    if missing_required(session, ticket):
        # Cannot happen from the portal form, which enforces them; a request without its answers waits for an agent.
        return False
    triage_ticket(
        session, ticket, None, ticket_type=TicketType.SERVICE_REQUEST, category=category,
        impact=Level(request_type.default_impact), urgency=Level(request_type.default_urgency),
        request_type=request_type, automatic_from=request_type,
    )
    log_action(
        session, "system", "ticket", ticket.id, "triaged_automatically",
        f"{request_type.name}: {path_of(session, category)}",
    )
    # No agent step: everything the rules produce is made (now, or once approved). Imported here because
    # app.subrequests builds on the splitting machinery, which builds on app.triage.
    from app.subrequests import after_triage

    after_triage(session, ticket, None)
    return True


def _check_days(*counts: Optional[int]) -> None:
    for count in counts:
        if count is not None and not 0 <= count <= MAX_LEAD_DAYS:
            raise RequestTypeError(f"A number of days must be from 0 to {MAX_LEAD_DAYS}, or blank to use the setting")


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
    pass_percent: int, is_active: bool = True, offers_required_by: bool = False,
    due_days_before: Optional[int] = None, queue_lead_days: Optional[int] = None,
    defaults: Optional[TypeDefaults] = None, subrequests_need_own_approval: bool = False,
    approvers_may_self_approve: bool = False,
) -> RequestType:
    name, description = _clean(name, description)
    _check_days(due_days_before, queue_lead_days)
    defaults = defaults or TypeDefaults()
    _check_defaults(session, defaults)
    if _name_taken(session, name, None):
        raise RequestTypeError("There is already a request type with that name")
    _checked(session, group_id, pass_percent, is_active)
    request_type = RequestType(
        name=name, description=description, is_active=is_active, approval_group_id=group_id,
        pass_percent=pass_percent, offers_required_by=offers_required_by,
        due_days_before=due_days_before, queue_lead_days=queue_lead_days,
        default_team_id=defaults.team_id, default_category_id=defaults.category_id,
        default_impact=defaults.impact, default_urgency=defaults.urgency,
        subrequests_need_own_approval=subrequests_need_own_approval, approvers_may_self_approve=approvers_may_self_approve,
    )
    session.add(request_type)
    _flush(session)
    return request_type


def update_request_type(
    session: Session, request_type: RequestType, name: str, description: str, group_id: Optional[int],
    pass_percent: int, is_active: bool, offers_required_by: Optional[bool] = None,
    due_days_before: Optional[int] = None, queue_lead_days: Optional[int] = None,
    defaults: Optional[TypeDefaults] = None, subrequests_need_own_approval: Optional[bool] = None,
    approvers_may_self_approve: Optional[bool] = None,
) -> RequestType:
    """Blank (None) day counts mean "use the setting". `defaults` None leaves the type's defaults as they are, and so
    does `subrequests_need_own_approval` None."""
    name, description = _clean(name, description)
    _check_days(due_days_before, queue_lead_days)
    if defaults is not None:
        _check_defaults(session, defaults)
    if _name_taken(session, name, request_type.id):
        raise RequestTypeError("There is already a request type with that name")
    _checked(session, group_id, pass_percent, is_active)
    request_type.name, request_type.description, request_type.is_active = name, description, is_active
    request_type.approval_group_id = group_id
    request_type.pass_percent = pass_percent
    if offers_required_by is not None:
        request_type.offers_required_by = offers_required_by
    request_type.due_days_before, request_type.queue_lead_days = due_days_before, queue_lead_days
    if subrequests_need_own_approval is not None:
        request_type.subrequests_need_own_approval = subrequests_need_own_approval
    if approvers_may_self_approve is not None:
        request_type.approvers_may_self_approve = approvers_may_self_approve
    if defaults is not None:
        request_type.default_team_id, request_type.default_category_id = defaults.team_id, defaults.category_id
        request_type.default_impact, request_type.default_urgency = defaults.impact, defaults.urgency
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

"""Reading the requester's feedback (phase 4G). Writing it is app/action_links.py.

Who may read which row is decided in app/visibility.py (`feedback_condition`): admins all of it,
an agent only the feedback about tickets they resolved. Feedback comments are personal data: the
Phase 7 anonymisation work must cover `ticket_feedback.comment` (see docs/build-plan.md)."""

from typing import Mapping, Optional

from sqlmodel import Session, select

from app.models import Ticket, TicketFeedback, User
from app.pagination import Page, paginate
from app.visibility import feedback_condition


def feedback_query(user: User, agent_id: Optional[int] = None, rating: Optional[int] = None):
    """Newest first, rows (feedback, ticket), restricted to what `user` may read. The agent filter
    can only narrow what the person could already see."""
    query = (
        select(TicketFeedback, Ticket)
        .join(Ticket, Ticket.id == TicketFeedback.ticket_id)
        .where(feedback_condition(user))
        .order_by(TicketFeedback.created_at.desc(), TicketFeedback.id.desc())
    )
    if agent_id is not None:
        query = query.where(TicketFeedback.resolved_by_id == agent_id)
    if rating is not None:
        query = query.where(TicketFeedback.rating == rating)
    return query


def feedback_page(
    session: Session, user: User, page_number: int, query_params: Mapping[str, str],
    agent_id: Optional[int] = None, rating: Optional[int] = None,
) -> Page:
    return paginate(session, feedback_query(user, agent_id, rating), page_number, query_params)


def feedback_for_ticket(session: Session, user: User, ticket: Ticket) -> list[TicketFeedback]:
    """The feedback on one ticket that `user` may read (none for most agents)."""
    return list(session.exec(
        select(TicketFeedback)
        .where(TicketFeedback.ticket_id == ticket.id, feedback_condition(user))
        .order_by(TicketFeedback.created_at, TicketFeedback.id)
    ).all())

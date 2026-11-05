"""Closing a ticket that was not resolved first (#131). The agent's Close action.

Closing from any status but Resolved needs closing notes (#122) and, like resolving (app/resolution.py),
emails the requester with the admin template "closed", copying the followers the agent left ticked.
`preview_closing` is the "are you sure?" step (nothing is recorded), then `close_ticket`.

The notes are stored as the ticket's resolution notes and audited as "closed". The closed email has no
reopen link (only a Resolved ticket reopens; a reply to a Closed ticket opens a new linked ticket) but
keeps the feedback link.

No email, and not this module, for the other ways a ticket closes: closing a Resolved ticket (it already had
its resolution email; the status buttons do it, and so does the auto-close), a decline or withdrawal
(app/approvals.py has its own emails), re-closing a reopened ticket (app/reopening.py), a split original whose
status follows its parts, and a ticket raised already closed (app/raising.py only resolves).

Nothing here commits. Every refusal is a `CloseError` raised before anything is changed.
"""

from typing import Optional

from sqlmodel import Session

from app.action_links import FEEDBACK_MARKER
from app.audit import log_action
from app.email_templates import render_template, ticket_values
from app.forms import MAX_MESSAGE_LENGTH
from app.messages import EmailPreview, email_requester, is_triaged, preview_email
from app.models import FINISHED_STATUSES, Ticket, TicketStatus, User
from app.notifications import TICKET_CLOSED, notify_agent_emailed
from app.splitting import SplitError, check_manual_status, lock_ticket
from app.ticket_service import set_status

AUDIT_CLOSED = "closed"


class CloseError(ValueError):
    """A reason the ticket cannot be closed this way (or the notes are not acceptable)."""


def clean_closing_notes(notes: Optional[str]) -> str:
    notes = (notes or "").replace("\r\n", "\n").strip()
    if not notes:
        raise CloseError("Write the closing notes: why the ticket is being closed")
    if len(notes) > MAX_MESSAGE_LENGTH:
        raise CloseError(f"The closing notes can be at most {MAX_MESSAGE_LENGTH} characters")
    return notes


def check_can_close(session: Session, ticket: Ticket) -> None:
    """The rules that do not depend on the notes. Raises CloseError."""
    if ticket.status in FINISHED_STATUSES:
        raise CloseError("This ticket is already resolved or closed")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise CloseError("This request is waiting for its approvers; it cannot be closed until they decide")
    if not is_triaged(ticket):
        raise CloseError("Triage the ticket before closing it")
    try:
        check_manual_status(session, ticket, TicketStatus.CLOSED)
    except SplitError as error:
        raise CloseError(str(error))


def closed_email_body(session: Session, ticket: Ticket, notes: str) -> str:
    """The text of the closed email: the admin template with the notes put in (the feedback link is put in as it is sent)."""
    return render_template(session, "closed", {
        **ticket_values(ticket), "closing_notes": notes, "feedback_link": FEEDBACK_MARKER,
    }).body


def preview_closing(
    session: Session, ticket: Ticket, notes: str, cc_emails: Optional[list[str]] = None
) -> EmailPreview:
    """What closing would email, for the "are you sure?" step. Runs the same checks as close_ticket and
    records nothing. Raises CloseError (or UnknownRecipient for a bad copy)."""
    notes = clean_closing_notes(notes)
    check_can_close(session, ticket)
    return preview_email(session, ticket, closed_email_body(session, ticket, notes), cc_emails=cc_emails)


def close_ticket(
    session: Session, ticket: Ticket, actor: User, notes: str, *, cc_emails: Optional[list[str]] = None,
    tell_msp: Optional[bool] = None, take_back_linked: bool = False,
) -> None:
    """Closes the ticket: stores the notes and who closed it, moves it to Closed, writes the status event
    and the audit entry, and queues the closed email (an agent's email, so it counts as a response). tell_msp and take_back_linked are the
    preview's tick boxes about the MSP (app/msp_actions.py)."""
    notes = clean_closing_notes(notes)
    # A double submit or two agents at once: the second waits, re-reads the ticket and is refused as already closed.
    if ticket.split_from_id is not None:
        lock_ticket(session, ticket.split_from_id)
    session.flush()
    session.refresh(ticket, with_for_update=True)
    check_can_close(session, ticket)
    body = closed_email_body(session, ticket, notes)
    # Validates the copy list before anything changes, so a bad address leaves the ticket as it was.
    preview_email(session, ticket, body, cc_emails=cc_emails)

    previous = ticket.status
    # Imported here: app.msp_actions builds on app.msp, which builds on app.mail, which imports this module.
    from app.msp_actions import OUTCOME_CLOSED, finish_with_msp

    finish_with_msp(session, ticket, actor, OUTCOME_CLOSED, tell_msp=tell_msp, take_back_linked_too=take_back_linked)
    ticket.resolution_notes = notes
    ticket.resolved_by_id = actor.id
    set_status(session, ticket, TicketStatus.CLOSED, actor.email)
    session.add(ticket)
    log_action(session, actor.email, "ticket", ticket.id, "status_changed", f"{previous.value} -> closed")
    log_action(session, actor.email, "ticket", ticket.id, AUDIT_CLOSED, notes[:200])

    email_requester(session, ticket, actor, body, cc_emails=cc_emails, bell=False)
    # Whoever has "closed" on is told by that entry (made by set_status); the others by "emailed you" (#130).
    notify_agent_emailed(session, ticket, actor.email, instead_of=TICKET_CLOSED)

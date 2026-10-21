"""Resolving a ticket (phase 4D). The one place a ticket becomes Resolved.

`set_status` refuses Resolved unless it is told the change comes from here, so every route and
job that resolves goes through `resolve_ticket`. How callers use it:

- An agent resolving in the web app: `preview_resolution` for the "are you sure?" step (nothing is
  recorded), then `resolve_ticket(session, ticket, agent, notes, cc_emails=...)`. The resolution email
  goes to the requester (an agent's email, so it counts as a first response), copying the followers
  the agent left ticked, with the admin template "resolution".
- A backdated ticket raised already resolved (app/raising.py): `resolve_ticket(..., send_email=False)`.
  It is logged after the fact, so nobody is emailed; backdating then sets the dates.
- Automatic resolves (4H auto-resolve, 4I bulk resolve): pass `actor=None` for no human, and the text
  from `auto_resolution_note` (the admin template "auto_resolve_note"). The email is then an automatic
  notice and copies every follower.

Nothing here commits. The change, its event, the audit entry and the queued email land in the
caller's transaction. Every refusal is a `ResolveError` worded for whoever is resolving, raised
before anything is changed (a bad copy address is `UnknownRecipient`, also before any change).
"""

from typing import Optional

from sqlmodel import Session

from app.action_links import FEEDBACK_MARKER, REOPEN_MARKER
from app.audit import log_action
from app.email_templates import render_template, ticket_values
from app.forms import MAX_MESSAGE_LENGTH
from app.messages import EmailPreview, email_requester, is_triaged, preview_email, record_notice
from app.models import FINISHED_STATUSES, Ticket, TicketStatus, User
from app.splitting import SplitError, check_manual_status, lock_ticket
from app.ticket_service import set_status

AUDIT_RESOLVED = "resolved"


class ResolveError(ValueError):
    """A reason the ticket cannot be resolved (or the notes are not acceptable)."""


def clean_notes(notes: Optional[str]) -> str:
    notes = (notes or "").replace("\r\n", "\n").strip()
    if not notes:
        raise ResolveError("Write the resolution notes: what was done to fix it")
    if len(notes) > MAX_MESSAGE_LENGTH:
        raise ResolveError(f"The resolution notes can be at most {MAX_MESSAGE_LENGTH} characters")
    return notes


def check_can_resolve(session: Session, ticket: Ticket) -> None:
    """The rules that do not depend on the notes. Raises ResolveError."""
    if ticket.status in FINISHED_STATUSES:
        raise ResolveError("This ticket is already resolved or closed")
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        raise ResolveError("This request is waiting for its approvers; it cannot be resolved until they decide")
    if not is_triaged(ticket):
        raise ResolveError("Triage the ticket before resolving it")
    try:
        check_manual_status(session, ticket, TicketStatus.RESOLVED)
    except SplitError as error:
        raise ResolveError(str(error))


def resolution_email_body(session: Session, ticket: Ticket, notes: str) -> str:
    """The text of the resolution email: the admin template with the notes put in."""
    # The links are put in as the email is built (app/action_links.py), so the stored text holds markers.
    return render_template(session, "resolution", {
        **ticket_values(ticket), "resolution_notes": notes,
        "feedback_link": FEEDBACK_MARKER, "reopen_link": REOPEN_MARKER,
    }).body


def auto_resolution_note(session: Session, ticket: Ticket, days: int) -> str:
    """The notes for a ticket resolved with no agent (4H, 4I): the admin template "auto_resolve_note"."""
    return render_template(session, "auto_resolve_note", {**ticket_values(ticket), "days": str(days)}).body


def preview_resolution(
    session: Session, ticket: Ticket, notes: str, cc_emails: Optional[list[str]] = None
) -> EmailPreview:
    """What resolving would email, for the "are you sure?" step. Runs the same checks as
    resolve_ticket and records nothing. Raises ResolveError (or UnknownRecipient for a bad copy)."""
    notes = clean_notes(notes)
    check_can_resolve(session, ticket)
    return preview_email(session, ticket, resolution_email_body(session, ticket, notes), cc_emails=cc_emails)


def resolve_ticket(
    session: Session,
    ticket: Ticket,
    actor: Optional[User],
    notes: str,
    *,
    cc_emails: Optional[list[str]] = None,
    send_email: bool = True,
    detail: Optional[str] = None,
) -> None:
    """Resolves the ticket: stores the notes and who resolved it, moves it to Resolved (which stops
    its SLA clock and re-derives a split original's status), writes the status event and the audit
    entry, and queues the resolution email unless send_email is False.

    actor is the agent, or None for an automatic resolve. cc_emails is the addresses to copy
    (None: every follower, the same rule as an agent's email). detail is added to the status event."""
    notes = clean_notes(notes)
    # Two agents (or a double submit) resolving at once: the second waits here, re-reads the ticket as
    # the first left it and is refused as already resolved, so there is one email and one set of notes.
    if ticket.split_from_id is not None:
        lock_ticket(session, ticket.split_from_id)  # the original first, like everything in a family
    session.flush()
    session.refresh(ticket, with_for_update=True)
    check_can_resolve(session, ticket)
    body = resolution_email_body(session, ticket, notes) if send_email else None
    if body is not None:
        # Validates the copy list before anything changes, so a bad address leaves the ticket as it was.
        preview_email(session, ticket, body, cc_emails=cc_emails)

    actor_email = actor.email if actor else None
    set_status(session, ticket, TicketStatus.RESOLVED, actor_email, detail=detail, by_resolution=True)
    ticket.resolution_notes = notes
    ticket.resolved_by_id = actor.id if actor else None
    session.add(ticket)
    log_action(session, actor_email or "system", "ticket", ticket.id, AUDIT_RESOLVED, notes[:200])

    if body is not None:
        if actor is not None:
            email_requester(session, ticket, actor, body, cc_emails=cc_emails)
        else:
            record_notice(session, ticket, body, copy_followers=True)

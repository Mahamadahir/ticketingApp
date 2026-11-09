"""The rules every way in shares when a person's words become a ticket or a reply: email (app/inbound.py) and Teams
(app/teams_intake.py). Each channel decides who the person is and which ticket the words belong to; what is the same
for both is here, so the two cannot drift:

- cleaning what a person wrote (control characters out, one-line subject, body cut to the same length);
- the never-follow list: words from a listed address (a list, the mailbox's alias, a no-reply) are ignored whole, with an
  audit entry that stops at the per-sender hourly limit;
- recording the first message of a ticket and a reply on one (`record_first_message`, `record_reply`), a reply going
  through `apply_requester_reply` so the status effect, the thanks rules and the reopen rule are the portal's;
- keeping files: `store_files` is the one place a file is checked against app/attachments.py, and a file that is
  refused, too large or beyond the limit leaves an event on the ticket, never an error.

Nothing here commits; the caller does.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, func, select

from app.attachments import AttachmentRejected, store_attachment
from app.audit import log_action
from app.forms import MAX_MESSAGE_LENGTH
from app.messages import record_inbound
from app.models import AuditLog, Message, Ticket
from app.never_follow import listed_entry
from app.raising import MAX_SUBJECT_LENGTH
from app.settings_store import INBOUND_MAX_PER_SENDER_HOUR, count_setting
from app.splitting import apply_requester_reply
from app.storage import Storage, get_storage
from app.ticket_service import record_event

NO_SUBJECT = "(no subject)"
NO_TEXT = "(no message text)"
CUT = "\n\n[cut short]"
MAX_AUDIT_DETAIL = 500  # of an audit entry about ignored words
EVENT_ATTACHMENT_SKIPPED = "attachment_skipped"


def clean_text(value) -> str:
    """Text safe to store: Postgres refuses a NUL, and other control characters are only noise."""
    if not isinstance(value, str):
        return ""
    # Only control characters (NUL included) and lone surrogates go; spaces such as U+00A0 and joiners stay.
    return "".join(c for c in value if c in "\n\t\r" or unicodedata.category(c) not in ("Cc", "Cs"))


def clean_subject(subject: str) -> str:
    subject = " ".join(subject.split())
    return subject[:MAX_SUBJECT_LENGTH] if subject else NO_SUBJECT


def clean_body(body: str) -> str:
    body = body.strip()
    if not body:
        return NO_TEXT
    return body if len(body) <= MAX_MESSAGE_LENGTH else body[: MAX_MESSAGE_LENGTH - len(CUT)].rstrip() + CUT


def ignored_as_never_follow(session: Session, address: str, what: str, now: datetime) -> bool:
    """True when the address is on the never-follow list: nothing is made from words it sent. The audit log keeps who
    and what, so an admin can see what was dropped; the entries stop at the per-sender hourly limit so a listed address
    caught in a loop cannot fill the audit log."""
    listed = listed_entry(session, address)
    if listed is None:
        return False
    logged = session.exec(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_type == "never_follow", AuditLog.entity_id == listed.id, AuditLog.action == "mail_ignored",
            AuditLog.created_at >= now - timedelta(hours=1),
        )
    ).one()
    if logged < count_setting(session, INBOUND_MAX_PER_SENDER_HOUR):
        log_action(session, address, "never_follow", listed.id, "mail_ignored", f"{address}: {what}"[:MAX_AUDIT_DETAIL])
    return True


def record_first_message(
    session: Session, ticket: Ticket, *, sender: str, body: str, author_id: Optional[int], action: str,
    graph_message_id: Optional[str] = None,
) -> Message:
    """The words a ticket was opened with: a public message from the requester, which tells nobody, and the audit entry."""
    message = record_inbound(session, ticket, sender=sender, body=body, author_user_id=author_id)
    message.graph_message_id = graph_message_id
    session.flush()
    log_action(session, sender, "ticket", ticket.id, action)
    return message


def record_reply(
    session: Session, ticket: Ticket, *, sender: str, body: str, author_id: Optional[int], text: str,
    has_attachment: bool, action: str, how: str, graph_message_id: Optional[str] = None,
) -> Message:
    """A reply on an open or resolved ticket from someone who may write on it. Records it (the assignee is told), applies
    its effect on the status (`apply_requester_reply`: Awaiting user goes back to In progress; a Resolved ticket reopens
    unless the reply is a thank-you; `text` is the new words only) and writes the audit entries. The caller then keeps
    the files and updates the followers."""
    message = record_inbound(session, ticket, sender=sender, body=body, author_user_id=author_id, reply_by=sender)
    message.graph_message_id = graph_message_id
    session.flush()
    moved = apply_requester_reply(session, ticket, sender, text=text, has_attachment=has_attachment)
    log_action(session, sender, "ticket", ticket.id, action, how)
    if moved:
        log_action(session, sender, "ticket", ticket.id, "status_changed", f"{moved[0]} -> {moved[1]}")
    return message


@dataclass
class IncomingAttachment:
    filename: str
    data: Optional[bytes] = None  # None when skipped
    inline: bool = False
    skipped: Optional[str] = None  # why it was not fetched


def store_files(
    session: Session, storage: Optional[Storage], ticket: Ticket, message: Message, files: list[IncomingAttachment]
) -> list[str]:
    """Keeps the files that came with a message, each through `store_attachment`. A file that was skipped when it was
    fetched, or is refused now, leaves an `attachment_skipped` event naming it and why. Returns "name (why)" for each
    one not kept, so a channel that can answer straight away can say so."""
    storage = storage or (get_storage() if any(item.data is not None for item in files) else None)
    not_kept: list[str] = []
    for item in files:
        reason = item.skipped
        if reason is None and item.data is not None:
            try:
                store_attachment(
                    session, storage, ticket=ticket, filename=item.filename, data=item.data,
                    message=message, inline=item.inline,
                )
            except AttachmentRejected as error:
                reason = str(error)
        if reason is not None:
            record_event(session, ticket, EVENT_ATTACHMENT_SKIPPED, None, to_value=item.filename[:200], detail=reason)
            not_kept.append(f"{item.filename} ({reason})")
    return not_kept

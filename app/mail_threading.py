"""Mail threading (issue #350): which earlier email a ticket email is sent as a reply to, and how the Graph id of each email we
sent is learned.

Every email to a requester or follower about a ticket goes, where it can, as a Graph `reply` in the ticket's mail conversation
(app/mail.py, `send_through_graph`), so Outlook, Gmail and phones stack it like a normal thread and the earlier conversation is
quoted below the new text. `reply` needs Mail.Send only. This module decides the one thing the sender cannot: the message to
reply to.

The rule, kept in one place: the latest email on this ticket that EVERY recipient of the new email was already on (as sender, To
or Cc), because Exchange quotes the replied-to message to everyone the reply goes to. The candidates are
- the mail that reached the shared mailbox for this ticket (`InboundMail`: its Graph id, sender, To and Cc), never one from the
  MSP (`Message.msp_forward` is set on every message that came from them) and never an agents-only message; and
- the emails this app sent about the ticket whose Sent Items id is known (`OutboundEmail.graph_message_id`), of the kinds that
  are mail to a requester or follower (`THREADED_KINDS`: not approvals, not the MSP, not mail about no ticket).
Nothing qualifies for a ticket raised in the portal or Teams, or for a first email to a new follower: the email then goes as a
fresh `sendMail` and starts the conversation. A target that Graph refused or no longer has (`OutboundEmail.thread_note`) is not
tried again.

`sendMail` and `reply` return no message id, so the id of what we sent is read from Sent Items afterwards: straight after the send
(`claim_sent_copy`, from the newest Sent Items messages) and again by the email-reactions job over the Sent Items delta
(`app/email_reactions.claim_sent_item`), which catches any the first lookup missed. Both match by ticket subject, recipients and
time, as the reactions job already did.

Nothing here commits or calls Graph.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app import outbox
from app.identity import addresses_of
from app.models import InboundMail, Message, MessageDirection, MessageVisibility, OutboundEmail
from app.subscriptions import parse_graph_time

# The kinds of email that go to a requester or follower about their ticket. Approval emails are their own conversation per approver,
# the MSP's thread is its own, and an email about no ticket (or to the other person of a mailbox change) has nothing to reply to.
THREADED_KINDS = (
    outbox.REPLY, outbox.NOTICE, outbox.RAISED_FOR, outbox.ACKNOWLEDGEMENT, outbox.CHASE, outbox.SPLIT_NOTICE, outbox.FOLLOWER_UPDATE,
)
AMBIGUOUS_GAP = timedelta(seconds=30)
SENT_MATCH_WINDOW = timedelta(minutes=15)  # how far Sent Items' time may be from the time the outbox says it sent the email
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|aw|sv|vs)\s*:\s*)+", re.IGNORECASE)


def norm_subject(subject: Optional[str]) -> str:
    """The subject without the "RE: " Exchange puts on a reply, so a reply's Sent Items copy matches the ticket subject we asked for."""
    return _REPLY_PREFIX.sub("", subject or "").strip()


def recipients_of(item: dict) -> set[str]:
    """The lower-case addresses a sent message went to (to and cc)."""
    found: set[str] = set()
    for key in ("toRecipients", "ccRecipients"):
        for entry in item.get(key) or []:
            address = entry.get("emailAddress", {}).get("address") if isinstance(entry, dict) and isinstance(entry.get("emailAddress"), dict) else None
            if isinstance(address, str) and address.strip():
                found.add(address.strip().lower())
    return found


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _addresses(*values: Optional[str]) -> set[str]:
    return {a.strip().lower() for value in values for a in (value or "").split(",") if a.strip()}




def reply_target(session: Session, row: OutboundEmail, recipients: list[str]) -> Optional[str]:
    """The Graph id of the message to send this email as a reply to, or None for a fresh email. `recipients` are all the addresses
    it goes to (to and cc, exactly as the outbox decided); each must have been on the target."""
    if row.kind not in THREADED_KINDS or row.ticket_id is None or not recipients:
        return None
    refused = set(
        session.exec(
            select(OutboundEmail.reply_to_graph_id).where(
                OutboundEmail.ticket_id == row.ticket_id, OutboundEmail.thread_note.is_not(None), OutboundEmail.reply_to_graph_id.is_not(None)
            )
        ).all()
    )
    people = [addresses_of(session, recipient) for recipient in recipients]  # each person's addresses (app/identity.py), once
    candidates: list[tuple[datetime, str, set[str]]] = []
    # What reached the mailbox about this ticket. An MSP message and an agents-only one are never candidates; neither is a deleted note.
    for mail, message in session.exec(
        select(InboundMail, Message)
        .join(Message, Message.id == InboundMail.message_id)
        .where(
            Message.ticket_id == row.ticket_id,
            Message.direction == MessageDirection.INBOUND,
            Message.visibility == MessageVisibility.PUBLIC,
            Message.msp_forward.is_(None),
            Message.deleted_at.is_(None),
        )
    ).all():
        if mail.graph_message_id:
            candidates.append((_aware(mail.received_at or mail.created_at), mail.graph_message_id, _addresses(mail.sender, mail.to_addresses, mail.cc_addresses)))
    # What this app sent about it, once Sent Items has told us the id.
    for sent in session.exec(
        select(OutboundEmail).where(
            OutboundEmail.ticket_id == row.ticket_id,
            OutboundEmail.id != row.id,
            OutboundEmail.kind.in_(THREADED_KINDS),
            OutboundEmail.sent_at.is_not(None),
            OutboundEmail.graph_message_id.is_not(None),
        )
    ).all():
        candidates.append((_aware(sent.sent_at), sent.graph_message_id, _addresses(sent.to_address, sent.cc)))
    for _, graph_id, on in sorted(candidates, key=lambda c: c[0], reverse=True):
        if graph_id not in refused and all(on & person for person in people):
            return graph_id
    return None


def claim_sent_copy(session: Session, row: OutboundEmail, items: list[dict]) -> bool:
    """Records which Sent Items message is this row's copy: the one with its ticket subject (a reply's copy has an "RE: " in front),
    to its recipient, sent closest to when the row says it went, and not already some other email's copy. Returns whether one was found."""
    if row.graph_message_id or not row.subject or row.sent_at is None:
        return False
    wanted, to, sent_at = norm_subject(row.subject), row.to_address.strip().lower(), _aware(row.sent_at)
    ids = [item["id"] for item in items if isinstance(item, dict) and item.get("id")]
    taken = set(session.exec(select(OutboundEmail.graph_message_id).where(OutboundEmail.graph_message_id.in_(ids))).all()) if ids else set()
    gaps: list[tuple[timedelta, str]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or item["id"] in taken or norm_subject(item.get("subject")) != wanted:
            continue
        when = parse_graph_time(item.get("sentDateTime"))
        if to not in recipients_of(item) or when is None or abs(when - sent_at) > SENT_MATCH_WINDOW:
            continue
        gaps.append((abs(when - sent_at), item["id"]))
    gaps.sort()
    if not gaps or (len(gaps) > 1 and gaps[1][0] - gaps[0][0] < AMBIGUOUS_GAP):
        return False  # none, or two copies too close in time to tell apart: the reactions job (which has the same guard) decides later
    row.graph_message_id = gaps[0][1]
    session.add(row)
    return True

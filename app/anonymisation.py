"""Requester anonymisation (issue #176): keep the ticket, remove the person. Irreversible.

`anonymise` does the whole database part on the caller's session without committing, so it is one transaction: either
every table below is changed or none is. `preview` runs the same function inside a transaction it rolls back and reports
what it counted, so the page shows exactly what would change, not a description of it.

What the person is, and every table that holds their data, comes from `app/person_data.py` (`find_person`, `COLUMNS`).
Each column there carries an `anon` decision; `STEPS` below holds the code for every decision that is not "keep", and
`tests/test_anonymisation.py` fails when a column has a decision with no step or a step has no decision.

The result for a person:

- their tickets stay (number, type, category, team, dates, status, events, so the reports still count them); the
  requester becomes `anonymised-N@invalid` and the account (if any) "Anonymised requester #N", inactive;
- their files are deleted: the rows now, the stored bytes after the commit (see `purge_pending_blobs`);
- what they wrote, and the agent emails sent to them, become "[removed]"; their form answers, rating comments and
  approval comments are blanked (the rating and the vote are kept);
- follower rows, copy addresses, time zone, preferences, sessions, linked identities, link codes and bell entries go;
- everywhere an address of theirs sits inside kept text (audit and activity entries, events, the ticket's subject and
  resolution, internal notes) it becomes the placeholder address;
- the mail thread ids on their tickets are cleared, so mail from the old address starts a fresh ticket.

Kept on purpose: the ticket subject and resolution notes (after the address is scrubbed), internal notes (after the
address is scrubbed), staff names, the other people on a shared ticket. A name written into free text is not searched
for: only addresses are. These are listed on the confirm page.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.audit import log_action
from app.closing import CloseError, check_can_close
from app.config import settings
from app.models import (
    ActionToken,
    ActivityLog,
    ApprovalGroupMember,
    ApprovalVote,
    Attachment,
    AuditLog,
    InboundMail,
    LinkCode,
    LinkedIdentity,
    Message,
    MessageDirection,
    MessageVisibility,
    MspUnmatchedMail,
    NeverFollowAddress,
    Notification,
    NotificationPreference,
    OutboundEmail,
    PendingBlobDelete,
    AnonymisedRequester,
    RequesterTimeZone,
    TeamMembership,
    TeamsConversation,
    TeamsPost,
    ThanksCandidate,
    Ticket,
    TicketAnswer,
    TicketApproval,
    TicketEvent,
    TicketFeedback,
    TicketFollower,
    TicketStatus,
    User,
    UserPermission,
    UserRole,
    UserSession,
)
from app.person_data import (
    BLANK, COLUMNS, DELETE, KEEP, NULL, REMOVE, REPLACE, SCRUB, WITH_ROW, Person, find_person, list_matches, mentions,
    PLACEHOLDER_DOMAIN, object_ids, placeholder_address,
)
from app.attachments import keys_in_use, try_lock_blob
from app.storage import Storage
from app.ticket_service import set_status

AUDIT_ENTITY = "anonymisation"
AUDIT_ACTION = "requester_anonymised"
REMOVED = "[removed]"
CLOSE_NOTE = "Closed because the requester was anonymised (data protection)"


def placeholder_name(n: int) -> str:
    return f"Anonymised requester #{n}"


def address_hash(address: str) -> str:
    """A keyed one-way hash of the address, the only trace of it that is kept. Keyed with the session secret so the
    table cannot be matched against a list of guessed addresses without it; rotating that secret means a repeat of
    an old request is no longer recognised (the request still works)."""
    key = settings.session_secret_key.encode("utf-8")
    return hmac.new(key, b"anonymise-requester:" + address.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


class Refused(Exception):
    """The action may not go ahead; the message is shown to the admin."""


def _in(column, values):
    values = list(values)
    return column.in_(values) if values else column.is_(None) & column.is_not(None)


@dataclass
class Context:
    session: Session
    person: Person
    n: int
    actor: User
    ticket_ids: list[int]
    counts: dict[str, int] = field(default_factory=dict)
    blob_keys: set[str] = field(default_factory=set)
    shared_files_kept: int = 0
    _pattern: Optional[re.Pattern] = None
    _own_messages: Optional[list] = None

    @property
    def placeholder(self) -> str:
        return placeholder_address(self.n)

    def count(self, label: str, amount: int = 1) -> None:
        if amount:
            self.counts[label] = self.counts.get(label, 0) + amount

    def scrub(self, text: Optional[str]) -> Optional[str]:
        """The text with each of the person's addresses replaced by the placeholder address."""
        if not text:
            return text
        if self._pattern is None:
            # Whole addresses only: "bigbob@x.com" is someone else when the person is bob@x.com.
            alternatives = "|".join(re.escape(a) for a in sorted(self.person.addresses, key=len, reverse=True))
            self._pattern = re.compile(rf"(?<![\w.+%'-])(?:{alternatives})(?![\w-]|\.\w)", re.I)
        return self._pattern.sub(self.placeholder, text)

    def without_addresses(self, text: Optional[str]) -> Optional[str]:
        """A comma-separated list of addresses with the person's taken out."""
        if not text:
            return text
        kept = [part for part in text.split(",") if not mentions(part, self.person)]
        return ", ".join(p.strip() for p in kept) or None


# ---- Steps. Each takes the Context, changes rows on ctx.session, counts what it did -----------------------------


def _scrub_column(model, column: str, label: str) -> Callable[[Context], None]:
    attribute = getattr(model, column)

    def step(ctx: Context) -> None:
        rows = ctx.session.exec(select(model).where(list_matches(attribute, ctx.person))).all()
        changed = 0
        for row in rows:
            old = getattr(row, column)
            new = ctx.scrub(old)
            if new != old:
                setattr(row, column, new)
                ctx.session.add(row)
                changed += 1
        ctx.count(label, changed)

    return step


def _remove_column(model, column: str, label: str) -> Callable[[Context], None]:
    attribute = getattr(model, column)

    def step(ctx: Context) -> None:
        rows = ctx.session.exec(select(model).where(list_matches(attribute, ctx.person))).all()
        changed = 0
        for row in rows:
            old = getattr(row, column)
            if mentions(old, ctx.person):
                setattr(row, column, ctx.without_addresses(old))
                ctx.session.add(row)
                changed += 1
        ctx.count(label, changed)

    return step


def _their_messages(ctx: Context) -> list[Message]:
    """Messages they wrote, on any ticket: by one of their accounts, or from one of their addresses. Found once, before
    any sender is replaced, and the same list is used by every step."""
    if ctx._own_messages is None:
        rows = ctx.session.exec(
            select(Message).where(or_(_in(Message.author_user_id, ctx.person.user_ids), list_matches(Message.sender, ctx.person)))
        ).all()
        ctx._own_messages = [m for m in rows if m.author_user_id in ctx.person.user_ids or mentions(m.sender, ctx.person)]
    return ctx._own_messages


def _agent_emails_to_them(ctx: Context) -> list[Message]:
    return list(
        ctx.session.exec(
            select(Message).where(
                _in(Message.ticket_id, ctx.ticket_ids),
                Message.direction == MessageDirection.OUTBOUND,
                Message.visibility == MessageVisibility.PUBLIC,
            )
        ).all()
    )


def step_message_body(ctx: Context) -> None:
    """What they wrote and what agents emailed to them become "[removed]"; an address in any other message (an
    internal note, a follower's reply) becomes the placeholder address."""
    wrote = _their_messages(ctx)
    sent = _agent_emails_to_them(ctx)
    gone = {m.id: m for m in wrote + sent}
    for message in gone.values():
        if message.body != REMOVED:
            message.body = REMOVED
            ctx.session.add(message)
    ctx.count("Messages they wrote, removed", len({m.id for m in wrote}))
    ctx.count("Agent emails sent to them, removed", len({m.id for m in sent} - {m.id for m in wrote}))
    others = ctx.session.exec(select(Message).where(list_matches(Message.body, ctx.person))).all()
    changed = 0
    for message in others:
        if message.id in gone:
            continue
        new = ctx.scrub(message.body)
        if new != message.body:
            message.body = new
            ctx.session.add(message)
            changed += 1
    ctx.count("Other messages naming the address, address replaced", changed)


def step_message_sender(ctx: Context) -> None:
    for message in _their_messages(ctx):
        message.sender = placeholder_name(ctx.n)
        ctx.session.add(message)


def step_message_cc(ctx: Context) -> None:
    _remove_column(Message, "cc", "Copy lists on messages, address taken out")(ctx)


def step_held_files(ctx: Context) -> None:
    """Files held while the Teams bot asks which request a message is for, on questions about their tickets (8F): the
    held rows go and the stored files nothing else uses are queued for deletion. Runs before `step_attachments`, so a file
    the person also attached somewhere is still counted as used here and queued by that step."""
    from app.reply_files import release_for_tickets as release_reply_files
    from app.teams_pending import release_for_tickets

    ctx.count("Held Teams files deleted", release_for_tickets(ctx.session, sorted(ctx.ticket_ids)))
    ctx.count("Files held for an agent's reply deleted", release_reply_files(ctx.session, sorted(ctx.ticket_ids)))


def step_attachments(ctx: Context) -> None:
    """Every file on their tickets, and every file they sent or uploaded on other tickets: the rows go now; a stored
    file is queued for deletion when no remaining row uses it (identical files share one blob)."""
    session = ctx.session
    own_messages = [m.id for m in _their_messages(ctx)]
    rows = session.exec(
        select(Attachment).where(
            or_(
                _in(Attachment.ticket_id, ctx.ticket_ids),
                _in(Attachment.uploaded_by_id, ctx.person.user_ids),
                _in(Attachment.message_id, own_messages),
            )
        )
    ).all()
    keys = {row.blob_key for row in rows}
    for row in rows:
        session.delete(row)
    session.flush()
    ctx.count("Files deleted", len(rows))
    still_used = set(session.exec(select(Attachment.blob_key).where(Attachment.blob_key.in_(keys))).all()) if keys else set()
    ctx.shared_files_kept = len(still_used)
    for key in keys - still_used:
        ctx.blob_keys.add(key)
        if session.get(PendingBlobDelete, key) is None:
            session.add(PendingBlobDelete(blob_key=key))
    # An email still waiting to go to the MSP must not point at files that are gone.
    for email in session.exec(select(OutboundEmail).where(_in(OutboundEmail.ticket_id, ctx.ticket_ids), OutboundEmail.sent_at.is_(None))).all():
        if email.attachment_ids:
            email.attachment_ids = None
            session.add(email)


def step_ticket_requester(ctx: Context) -> None:
    for ticket in ctx.session.exec(select(Ticket).where(_in(Ticket.id, ctx.ticket_ids))).all():
        ticket.requester_email = ctx.placeholder
        ticket.graph_conversation_id = None
        ctx.session.add(ticket)
    ctx.count("Tickets kept, requester replaced", len(ctx.ticket_ids))


def step_thread_ids(ctx: Context) -> None:
    """Mail threading: a reply from the old address must not match a ticket by its conversation or Message-ID."""
    rows = ctx.session.exec(
        select(InboundMail).where(
            or_(
                _in(InboundMail.ticket_id, ctx.ticket_ids),
                list_matches(InboundMail.sender, ctx.person),
            )
        )
    ).all()
    changed = 0
    for row in rows:
        if row.ticket_id not in ctx.ticket_ids and not mentions(row.sender, ctx.person):
            continue
        if row.conversation_id or row.internet_message_id:
            row.conversation_id = None
            row.internet_message_id = None
            ctx.session.add(row)
            changed += 1
    ctx.count("Received mails, thread ids cleared", changed)


def step_inbound_sender(ctx: Context) -> None:
    rows = ctx.session.exec(select(InboundMail).where(list_matches(InboundMail.sender, ctx.person))).all()
    changed = 0
    for row in rows:
        if mentions(row.sender, ctx.person):
            row.sender = ctx.placeholder
            ctx.session.add(row)
            changed += 1
    ctx.count("Received mails, sender replaced", changed)


def step_outbound_to(ctx: Context) -> None:
    """Emails not yet sent to them are cancelled; sent ones keep their row (so the outbox history stays) with the
    address replaced and the body removed."""
    session = ctx.session
    rows = session.exec(select(OutboundEmail).where(func.lower(func.trim(OutboundEmail.to_address)).in_(ctx.person.addresses))).all()
    cancelled = replaced = 0
    about = set()
    for row in rows:
        if row.sent_at is None:
            session.delete(row)
            cancelled += 1
        else:
            # One follower update per person per message is unique (message, address): two of their addresses on the
            # same message must not both become the same placeholder.
            if row.about_message_id is not None and row.about_message_id in about:
                row.to_address = f"anonymised-{ctx.n}-{row.id}@{PLACEHOLDER_DOMAIN}"
            else:
                row.to_address = ctx.placeholder
            about.add(row.about_message_id)
            row.body = REMOVED if row.body else row.body
            session.add(row)
            replaced += 1
    # An email to someone else (a follower, the MSP) that carries one of their messages is cancelled too: the message
    # is about to read "[removed]", and that is what would be sent.
    own = [m.id for m in _their_messages(ctx)]
    carrying = session.exec(
        select(OutboundEmail).where(
            OutboundEmail.sent_at.is_(None), or_(_in(OutboundEmail.message_id, own), _in(OutboundEmail.about_message_id, own))
        )
    ).all()
    for row in carrying:
        session.delete(row)
    ctx.count("Waiting emails to others that carry their messages, cancelled", len(carrying))
    ctx.count("Emails waiting to be sent to them, cancelled", cancelled)
    ctx.count("Emails already sent to them, address replaced and body removed", replaced)


def step_outbound_body(ctx: Context) -> None:
    """The text of emails sent about their tickets (follower updates carry what they wrote) is removed."""
    rows = ctx.session.exec(
        select(OutboundEmail).where(_in(OutboundEmail.ticket_id, ctx.ticket_ids), OutboundEmail.sent_at.is_not(None))
    ).all()
    changed = 0
    for row in rows:
        if row.body and row.body != REMOVED:
            row.body = REMOVED
            ctx.session.add(row)
            changed += 1
    ctx.count("Emails sent about their tickets, body removed", changed)
    rest = ctx.session.exec(select(OutboundEmail).where(list_matches(OutboundEmail.body, ctx.person))).all()
    scrubbed = 0
    for row in rest:
        new = ctx.scrub(row.body)
        if new != row.body:
            row.body = new
            ctx.session.add(row)
            scrubbed += 1
    ctx.count("Other waiting emails naming the address, address replaced", scrubbed)


def step_answers(ctx: Context) -> None:
    rows = ctx.session.exec(select(TicketAnswer).where(_in(TicketAnswer.ticket_id, ctx.ticket_ids))).all()
    changed = 0
    for row in rows:
        if row.value:
            row.value = ""
            ctx.session.add(row)
            changed += 1
    ctx.count("Form answers blanked", changed)


def step_feedback_comment(ctx: Context) -> None:
    rows = ctx.session.exec(select(TicketFeedback).where(_in(TicketFeedback.ticket_id, ctx.ticket_ids))).all()
    changed = 0
    for row in rows:
        if row.comment:
            row.comment = ""
            ctx.session.add(row)
            changed += 1
    ctx.count("Rating comments blanked (the rating is kept)", changed)
    step_expire_links(ctx)  # the feedback and reopen links belong with the rating


def step_vote_comment(ctx: Context) -> None:
    rows = ctx.session.exec(select(ApprovalVote).where(_in(ApprovalVote.voter_id, ctx.person.user_ids))).all()
    changed = 0
    for row in rows:
        if row.comment:
            row.comment = ""
            ctx.session.add(row)
            changed += 1
    ctx.count("Approval comments blanked (the vote is kept)", changed)


def step_expire_links(ctx: Context) -> None:
    """The emailed feedback and reopen links on their tickets stop working."""
    now = datetime.now(timezone.utc)
    rows = ctx.session.exec(
        select(ActionToken).where(_in(ActionToken.ticket_id, ctx.ticket_ids), ActionToken.used_at.is_(None))
    ).all()
    for row in rows:
        row.used_at = now
        ctx.session.add(row)
    ctx.count("Emailed links switched off", len(rows))


def step_followers(ctx: Context) -> None:
    rows = ctx.session.exec(
        select(TicketFollower).where(
            or_(func.lower(func.trim(TicketFollower.email)).in_(ctx.person.addresses), _in(TicketFollower.user_id, ctx.person.user_ids))
        )
    ).all()
    for row in rows:
        ctx.session.delete(row)
    ctx.count("Follower and copy rows removed", len(rows))


def step_notifications(ctx: Context) -> None:
    session = ctx.session
    rows = session.exec(select(Notification).where(_in(Notification.user_id, ctx.person.user_ids))).all()
    ids = [r.id for r in rows]
    if ids:
        for post in session.exec(select(TeamsPost).where(TeamsPost.notification_id.in_(ids))).all():
            session.delete(post)
    for row in rows:
        session.delete(row)
    session.flush()
    ctx.count("Bell entries deleted", len(rows))


def _delete_rows(model, condition: Callable[[Context], object], label: str) -> Callable[[Context], None]:
    def step(ctx: Context) -> None:
        rows = ctx.session.exec(select(model).where(condition(ctx))).all()
        for row in rows:
            ctx.session.delete(row)
        ctx.count(label, len(rows))

    return step


def step_activity_network(ctx: Context) -> None:
    """Their own sign-in and activity rows lose the network address and browser."""
    rows = ctx.session.exec(
        select(ActivityLog).where(
            or_(_in(ActivityLog.actor_user_id, ctx.person.user_ids), func.lower(func.trim(ActivityLog.actor_email)).in_(ctx.person.addresses))
        )
    ).all()
    changed = 0
    for row in rows:
        if row.ip or row.user_agent:
            row.ip = None
            row.user_agent = None
            ctx.session.add(row)
            changed += 1
    ctx.count("Activity entries, network address and browser cleared", changed)


def step_accounts(ctx: Context) -> None:
    """Their accounts are kept (tickets and messages point at them) but nothing is left that identifies them, and
    they are inactive, so nobody can sign in as them."""
    users = ctx.session.exec(select(User).where(_in(User.id, ctx.person.user_ids)).order_by(User.id)).all()
    for index, user in enumerate(users):
        user.email = ctx.placeholder if index == 0 else f"anonymised-{ctx.n}-{user.id}@invalid"
        user.display_name = placeholder_name(ctx.n)
        user.entra_object_id = None
        user.password_hash = None
        user.totp_secret = None
        user.emergency_password_hash = None
        user.emergency_password_set_at = None
        user.emergency_password_used_at = None
        user.locked_until = None
        user.time_zone = None
        user.time_zone_source = None
        user.theme = None
        user.teams_delay_minutes = None
        user.out_of_office = False
        user.is_active = False
        ctx.session.add(user)
    ctx.count("Accounts anonymised and made inactive", len(users))


_delete_teams_conversations = _delete_rows(
    TeamsConversation,
    lambda c: or_(_in(TeamsConversation.user_id, c.person.user_ids), _in(TeamsConversation.entra_object_id, object_ids(c.session, c.person))),
    "Teams bot conversations deleted",
)

# One step for every decision that is not KEEP or WITH_ROW, keyed by the column (person_data.COLUMNS). Several
# columns of one table can share a step; it runs once. Order matters: messages are read before their senders are
# replaced, files are found before the messages' rows change, accounts last, so what identifies the person is still
# readable to every step that needs to find their rows.
STEPS: dict[str, Callable[[Context], None]] = {
    # files and the messages they hang off
    "teams_held_file.filename": step_held_files,
    "reply_held_file.filename": step_held_files,
    "attachment.uploaded_by_id": step_attachments,
    "message.body": step_message_body,
    "message.sender": step_message_sender,
    "message.cc": step_message_cc,
    # mail records
    "inboundmail.conversation_id": step_thread_ids,
    "inboundmail.internet_message_id": step_thread_ids,
    "inboundmail.sender": step_inbound_sender,
    "inboundmail.to_addresses": _remove_column(InboundMail, "to_addresses", "Received mails, address taken out of To"),
    "inboundmail.cc_addresses": _remove_column(InboundMail, "cc_addresses", "Received mails, address taken out of Cc"),
    "outboundemail.to_address": step_outbound_to,
    "outboundemail.cc": _remove_column(OutboundEmail, "cc", "Emails sent, address taken out of Cc"),
    "outboundemail.subject": _scrub_column(OutboundEmail, "subject", "Email subjects, address replaced"),
    "outboundemail.body": step_outbound_body,
    "outboundemail.last_error": _scrub_column(OutboundEmail, "last_error", "Email failure notes, address replaced"),
    "mspunmatchedmail.sender": _scrub_column(MspUnmatchedMail, "sender", "MSP mail, address replaced"),
    "mspunmatchedmail.to_addresses": _remove_column(MspUnmatchedMail, "to_addresses", "MSP mail, address taken out of To"),
    "mspunmatchedmail.cc_addresses": _remove_column(MspUnmatchedMail, "cc_addresses", "MSP mail, address taken out of Cc"),
    "mspunmatchedmail.subject": _scrub_column(MspUnmatchedMail, "subject", "MSP mail subjects, address replaced"),
    "mspunmatchedmail.body": _scrub_column(MspUnmatchedMail, "body", "MSP mail bodies, address replaced"),
    # the ticket
    "ticket.requester_email": step_ticket_requester,
    "ticket.graph_conversation_id": step_ticket_requester,
    "ticket.subject": _scrub_column(Ticket, "subject", "Ticket subjects, address replaced"),
    "ticket.escalation_reason": _scrub_column(Ticket, "escalation_reason", "Escalation reasons, address replaced"),
    "ticket.resolution_notes": _scrub_column(Ticket, "resolution_notes", "Resolution notes, address replaced"),
    "ticketanswer.value": step_answers,
    "ticketfeedback.comment": step_feedback_comment,
    "approvalvote.comment": step_vote_comment,
    "ticketfollower.email": step_followers,
    "ticketfollower.added_by_email": _scrub_column(TicketFollower, "added_by_email", "Follower rows, added-by address replaced"),
    # histories
    "ticketevent.actor_email": _scrub_column(TicketEvent, "actor_email", "Ticket events, address replaced (actor)"),
    "ticketevent.from_value": _scrub_column(TicketEvent, "from_value", "Ticket events, address replaced (from)"),
    "ticketevent.to_value": _scrub_column(TicketEvent, "to_value", "Ticket events, address replaced (to)"),
    "ticketevent.detail": _scrub_column(TicketEvent, "detail", "Ticket events, address replaced (detail)"),
    "activitylog.ip": step_activity_network,
    "activitylog.user_agent": step_activity_network,
    "activitylog.actor_email": _scrub_column(ActivityLog, "actor_email", "Activity entries, address replaced"),
    "activitylog.detail": _scrub_column(ActivityLog, "detail", "Activity entries, address replaced (detail)"),
    "auditlog.actor_email": _scrub_column(AuditLog, "actor_email", "Earlier audit entries, address replaced (actor)"),
    "auditlog.detail": _scrub_column(AuditLog, "detail", "Earlier audit entries, address replaced (detail)"),
    "notification.user_id": step_notifications,
    "notification.actor_email": _scrub_column(Notification, "actor_email", "Bell entries of others, address replaced (actor)"),
    "notification.text": _scrub_column(Notification, "text", "Bell entries of others, address replaced (text)"),
    "thankscandidate.sample_text": _scrub_column(ThanksCandidate, "sample_text", "Thanks-rule samples, address replaced"),
    "neverfollowaddress.email": _delete_rows(
        NeverFollowAddress, lambda c: func.lower(func.trim(NeverFollowAddress.email)).in_(c.person.addresses), "Never-follow entries removed"
    ),
    # the person's own settings and sign-in
    "user_session.user_id": _delete_rows(UserSession, lambda c: _in(UserSession.user_id, c.person.user_ids), "Sign-in sessions deleted"),
    "userpermission.user_id": _delete_rows(UserPermission, lambda c: _in(UserPermission.user_id, c.person.user_ids), "Extra permissions deleted"),
    "teammembership.user_id": _delete_rows(TeamMembership, lambda c: _in(TeamMembership.user_id, c.person.user_ids), "Team memberships deleted"),
    "approvalgroupmember.user_id": _delete_rows(
        ApprovalGroupMember, lambda c: _in(ApprovalGroupMember.user_id, c.person.user_ids), "Approval group memberships deleted"
    ),
    "notificationpreference.user_id": _delete_rows(
        NotificationPreference, lambda c: _in(NotificationPreference.user_id, c.person.user_ids), "Notification preferences deleted"
    ),
    # Their chat with the Teams bot is found by their account or by any of their object ids: one step serves both columns.
    "teams_conversation.user_id": _delete_teams_conversations,
    "teams_conversation.entra_object_id": _delete_teams_conversations,
    "requestertimezone.email": _delete_rows(
        RequesterTimeZone, lambda c: func.lower(func.trim(RequesterTimeZone.email)).in_(c.person.addresses), "Mailbox time zones deleted"
    ),
    "linked_identity.main_user_id": _delete_rows(
        LinkedIdentity,
        lambda c: or_(
            _in(LinkedIdentity.main_user_id, c.person.user_ids),
            _in(LinkedIdentity.secondary_user_id, c.person.user_ids),
            func.lower(func.trim(LinkedIdentity.email)).in_(c.person.addresses),
        ),
        "Linked identities deleted",
    ),
    "link_code.user_id": _delete_rows(
        LinkCode,
        lambda c: or_(_in(LinkCode.user_id, c.person.user_ids), func.lower(func.trim(LinkCode.address)).in_(c.person.addresses)),
        "Link codes deleted",
    ),
    "user.email": step_accounts,
    "user.display_name": step_accounts,
    "user.entra_object_id": step_accounts,
}


def expected_step_keys() -> set[str]:
    """The columns whose decision needs a step (everything but keep and goes-with-its-row)."""
    return {key for key, use in COLUMNS.items() if use.anon not in (KEEP, WITH_ROW)}


# ---- Refusals and the plan ---------------------------------------------------------------------------------------


def _ticket_ids(session: Session, person: Person) -> list[int]:
    return list(
        session.exec(
            select(Ticket.id).where(func.lower(func.trim(Ticket.requester_email)).in_(person.addresses)).order_by(Ticket.id)
        ).all()
    )


def open_tickets(session: Session, person: Person) -> list[Ticket]:
    """Their tickets that are not Closed. Resolved counts: it can still be reopened and is still emailed."""
    return list(
        session.exec(
            select(Ticket)
            .where(func.lower(func.trim(Ticket.requester_email)).in_(person.addresses), Ticket.status != TicketStatus.CLOSED)
            .order_by(Ticket.number)
        ).all()
    )


def refusal(session: Session, person: Person) -> Optional[str]:
    """Why this person cannot be anonymised, or None."""
    mailbox = settings.graph_shared_mailbox.strip().lower()
    if mailbox and mailbox in person.addresses:
        return "That is the shared mailbox, not a person."
    accounts = session.exec(select(User).where(_in(User.id, person.user_ids))).all()
    for account in accounts:
        if account.role != UserRole.END_USER or account.is_break_glass:
            return (
                "That address belongs to an agent, super admin or break-glass account. They leave through the normal "
                "route (deactivate the account on Admin > Users), not by anonymisation."
            )
    pending = session.exec(
        select(ApprovalVote, Ticket.number)
        .join(TicketApproval, TicketApproval.id == ApprovalVote.approval_id)
        .join(Ticket, Ticket.id == TicketApproval.ticket_id)
        .where(_in(ApprovalVote.voter_id, person.user_ids), ApprovalVote.decision == "pending", TicketApproval.status == "pending")
    ).first()
    if pending is not None:
        return f"They still have an approval vote waiting on ticket #{pending[1]}. Settle or withdraw that approval first."
    return None


def blockers_to_close(session: Session, tickets: list[Ticket]) -> list[str]:
    """Open tickets that cannot be closed here, with the reason (the admin deals with those first)."""
    problems = []
    for ticket in tickets:
        if ticket.status == TicketStatus.RESOLVED:
            continue
        if ticket.status == TicketStatus.WITH_MSP:
            problems.append(f"{ticket.reference}: it is with the MSP. Close it from its page, where the MSP can be told.")
            continue
        try:
            check_can_close(session, ticket)
        except CloseError as error:
            problems.append(f"{ticket.reference}: {error}")
    return problems


def _close(session: Session, ticket: Ticket, actor: User) -> None:
    previous = ticket.status
    if previous != TicketStatus.RESOLVED:
        ticket.resolution_notes = CLOSE_NOTE
        ticket.resolved_by_id = actor.id
    set_status(session, ticket, TicketStatus.CLOSED, actor.email, detail=CLOSE_NOTE)
    session.add(ticket)
    log_action(session, actor.email, "ticket", ticket.id, "status_changed", f"{previous.value} -> closed (requester anonymised)")


@dataclass
class Outcome:
    n: int
    tickets: int
    closed: int
    counts: dict[str, int]
    blob_keys: set[str]
    shared_files_kept: int
    repeat: bool
    hash: str
    fingerprint: str  # of what was counted; the confirm step refuses when the data changed since the preview


def anonymise(session: Session, person: Person, actor: User, *, n: int, close_open: bool) -> Outcome:
    """The database part, on `session`, without committing. Raises Refused. `n` is the placeholder number
    (`AnonymisedRequester.id`); a preview passes 0 and rolls everything back."""
    problem = refusal(session, person)
    if problem:
        raise Refused(problem)
    still_open = open_tickets(session, person)
    if still_open and not close_open:
        raise Refused(
            f"{len(still_open)} of their tickets are not closed ({', '.join(t.reference for t in still_open[:8])}"
            f"{'...' if len(still_open) > 8 else ''}). Close them first, or tick \"close open tickets first\"."
        )
    blocked = blockers_to_close(session, still_open)
    if blocked:
        raise Refused("These open tickets cannot be closed here, so nothing was changed:\n" + "\n".join(blocked))
    for ticket in still_open:
        _close(session, ticket, actor)
    session.flush()

    ctx = Context(session, person, n, actor, _ticket_ids(session, person))
    for step in dict.fromkeys(STEPS.values()):
        step(ctx)
        session.flush()
    if not ctx.counts:
        raise Refused("Nothing is held under that address.")
    digest = address_hash(person.address)
    repeat = session.exec(
        select(AnonymisedRequester.id).where(AnonymisedRequester.address_hash == digest, AnonymisedRequester.id != n)
    ).first() is not None
    fingerprint = hashlib.sha256(
        repr((sorted(ctx.ticket_ids), len(still_open), sorted(ctx.counts.items()), sorted(ctx.blob_keys))).encode()
    ).hexdigest()
    return Outcome(n, len(ctx.ticket_ids), len(still_open), ctx.counts, ctx.blob_keys, ctx.shared_files_kept, repeat, digest, fingerprint)


@dataclass
class Preview:
    person: Person
    outcome: Optional[Outcome]
    refused: Optional[str]
    tickets: list[tuple[str, str, str]]  # reference, status, subject
    open_references: list[str]
    earlier: Optional[AnonymisedRequester]
    waiting_deletes: int


def preview(session: Session, address: str, actor: User, *, close_open: bool) -> Preview:
    """What anonymising would do, worked out by doing it in a transaction that is rolled back."""
    person = find_person(session, address)
    tickets = [
        (t.reference, t.status.value, t.subject)
        for t in session.exec(
            select(Ticket).where(func.lower(func.trim(Ticket.requester_email)).in_(person.addresses)).order_by(Ticket.number)
        ).all()
    ]
    open_refs = [t.reference for t in open_tickets(session, person)]
    outcome = refused = None
    try:
        outcome = anonymise(session, person, actor, n=0, close_open=close_open)
    except Refused as error:
        refused = str(error)
    finally:
        session.rollback()
    earlier = session.exec(
        select(AnonymisedRequester).where(AnonymisedRequester.address_hash == address_hash(person.address)).order_by(AnonymisedRequester.id.desc())
    ).first()
    waiting = len(session.exec(select(PendingBlobDelete.blob_key)).all())
    return Preview(person, outcome, refused, tickets, open_refs, earlier, waiting)


def run(session: Session, address: str, actor: User, *, close_open: bool, expected: Optional[str] = None) -> Outcome:
    """Anonymises for real: one transaction, committed here. The stored files are deleted afterwards by the caller
    (`purge_pending_blobs`), once this has committed. Raises Refused with nothing changed. `expected` is the preview's
    fingerprint: when what would change is no longer what the admin was shown, nothing is done."""
    person = find_person(session, address)
    digest = address_hash(person.address)
    record = AnonymisedRequester(address_hash=digest)
    session.add(record)
    session.flush()
    try:
        outcome = anonymise(session, person, actor, n=record.id, close_open=close_open)
        if expected is not None and outcome.fingerprint != expected:
            raise Refused("What would change is no longer what the preview showed (the data changed). Nothing was done; look at it again.")
        # The audit entry holds the hash, never the address. Written after the earlier entries were rewritten.
        log_action(
            session, actor.email, AUDIT_ENTITY, record.id, AUDIT_ACTION,
            f"{outcome.tickets} tickets kept, {outcome.closed} closed first, {len(outcome.blob_keys)} stored files queued "
            f"for deletion, repeat request: {'yes' if outcome.repeat else 'no'}, address hash {digest}",
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return outcome


# ---- Stored files ------------------------------------------------------------------------------------------------


def purge_pending_blobs(session: Session, storage: Storage, keys: Optional[set[str]] = None) -> tuple[int, int]:
    """Deletes the stored files waiting in `pending_blob_delete` (just `keys`, or the oldest 500). Returns (deleted,
    failed). A file that an attachment row uses again by now (the same bytes uploaded since) is left alone and the
    entry dropped. Each key is locked (`attachments.try_lock_blob`) while it is checked and deleted, the lock an upload
    of the same bytes also takes, so an upload cannot slip between the check and the delete; a key an upload holds is
    left for the next run. A failure is recorded on the entry and retried by the `blob-cleanup` job; nothing is lost, and
    the database never points at a file that is not meant to be there. The caller commits (which releases the locks)."""
    query = select(PendingBlobDelete).order_by(PendingBlobDelete.created_at).limit(500)
    if keys is not None:
        query = select(PendingBlobDelete).where(PendingBlobDelete.blob_key.in_(keys))
    # Lock every key first (the lock an upload of the same bytes also takes), then ask which are used: a row committed
    # before the lock is seen here, and an upload that comes after waits until this transaction ends.
    entries = [e for e in session.exec(query).all() if try_lock_blob(session, e.blob_key)]
    used = keys_in_use(session, {e.blob_key for e in entries})
    done = failed = 0
    for entry in entries:
        if entry.blob_key in used:
            session.delete(entry)  # used again: the file stays
            continue
        try:
            storage.delete(entry.blob_key)
        except Exception as error:  # a storage outage, a missing permission: try again later
            entry.attempts += 1
            entry.last_error = f"{type(error).__name__}: {error}"[:300]
            session.add(entry)
            failed += 1
            continue
        done += 1
        session.delete(entry)
    return done, failed


def run_blob_cleanup(session: Session) -> None:
    """The recurring job `blob-cleanup` (app/scheduled_jobs.py): retries stored-file deletes that failed."""
    from app.storage import get_storage

    purge_pending_blobs(session, get_storage())

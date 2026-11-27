"""The activity thread of the agent ticket page (#255): the boxes, newest first.

`app.messages.agent_timeline` stays the one source of what is on a ticket (messages, notes and events, oldest first, with the
rules for split requests). This module turns that list into the boxes the page draws, and decides nothing about who may see
what: the requester's view is still only `messages.requester_visible_messages`, and this thread is for agents.

- A message is one box. An email to the requester, a reply, an internal note, a note shown to the requester, a deleted note's
  place ("Note deleted. Undo").
- An event is one box, except that the events one save wrote (`TicketEvent.group_id`, see `ticket_service.event_group`) are one
  box: a triage is the "Triaged" box that lists its fields, a resolve or close is the "Resolved" or "Closed" box, anything else
  is a "Changed" box with a sentence per change.
- Every sentence is written here, in plain words: names, never addresses or raw stored values.
- A requester's emailed reply has its quoted earlier conversation split off (`quoted_text.split_quoted`) so the page can fold it.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.messages import TimelineItem, is_note
from app.models import (
    Message,
    MessageDirection,
    Ticket,
    TicketEvent,
    User,
)
from app.quoted_text import split_quoted
from app.reactions import NEGATIVE, POSITIVE, display_of, reactions_for_ticket
from app.reply_channel import AGENT, EMAIL, PORTAL, SYSTEM, TEAMS_CHANNEL
from app.ticket_emails import Delivery, describe, ticket_emails
from app.visibility import Access, AccessContext, may_manage_note, ticket_access

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")

# Kinds of box. The template draws each one; the chip words and tones are here so a test and the page agree.
EMAIL_OUT, EMAIL_IN, NOTE, VISIBLE_NOTE, NOTE_DELETED = "email_out", "email_in", "note", "visible_note", "note_deleted"
CHANGE, TRIAGE, RESOLVED, SYSTEM_BOX = "change", "triage", "resolved", "system"
REACTION = "reaction"  # a reaction to a bot message that is not one of the ticket's messages (an approval card, a notice)

CHIPS = {
    EMAIL_OUT: ("Email", "blue"),
    EMAIL_IN: ("Email", "gray"),
    NOTE: ("Internal note · agents only", "amber"),
    VISIBLE_NOTE: ("Note · visible to the requester", "blue"),
    NOTE_DELETED: ("Note deleted", "gray"),
    CHANGE: ("Change", "gray"),
    TRIAGE: ("Triage", "green"),
    RESOLVED: ("Resolved", "green"),
    SYSTEM_BOX: ("System", "gray"),
    REACTION: ("Reaction", "gray"),
}
REACTION_TONES = {POSITIVE: "green", NEGATIVE: "red"}  # a neutral reaction is gray
REACTED_TO = {
    "resolution": "the resolution message", "awaiting_user": "the request for a reply", "approval": "the approval request",
}


@dataclass
class ReactionLine:
    """A reaction as the thread draws it: the emoji or label as received, who, when, and whether it was taken back."""

    text: str
    who: str
    at: datetime
    tone: str
    channel: str
    removed_at: Optional[datetime] = None
    sentiment: str = "neutral"
    no_effect: bool = False  # not the requester or a follower: recorded and shown, nothing more


@dataclass
class Box:
    id: str
    kind: str
    at: datetime
    num: int  # the message or event id, to order boxes at the same moment
    who: str  # the name shown; "System" for an automatic step
    who_email: Optional[str]  # shown on hover only
    title: str  # what they did, in plain words ("Emailed the requester")
    chip: str
    tone: str
    lines: list[str] = field(default_factory=list)  # a sentence per change
    fields: list[tuple[str, str]] = field(default_factory=list)  # a triage box: label and value
    message: Optional[Message] = None
    body: Optional[str] = None  # what the sender wrote (an emailed reply: without its quoted history)
    quoted: Optional[str] = None  # the folded quoted history, or None
    files: list = field(default_factory=list)
    notes: Optional[str] = None  # a resolved or closed box: the resolution notes
    source: Optional[Ticket] = None  # another ticket of a split request this item belongs to
    extra: list[str] = field(default_factory=list)  # small muted lines (copied to, MSP state)
    can_manage: bool = False  # the note menu is shown (author or super admin)
    is_visible_note: bool = False
    deleted_by: Optional[str] = None
    deleted_at: Optional[datetime] = None
    alert: Optional[str] = None  # a red chip beside the kind chip (MSP mail whose sender was not verified)
    waits_for_triage: bool = False  # a visible note on an untriaged ticket: the requester sees it once triaged
    reactions: list = field(default_factory=list)  # ReactionLines: reactions to this message (#298)
    delivery: Optional[Delivery] = None  # the email this box is or carries: its kind, recipients and state (#349)


# ---- names -----------------------------------------------------------------------------------------------------


class Names:
    """Display names for the addresses the thread mentions: an account's name, the requester's name, else the part of the
    address before the @ written as a name. Used so a sentence never shows an address."""

    def __init__(self, session: Session, ticket: Ticket, requester_name: Optional[str], emails: set[str]):
        self.requester = ticket.requester_email.lower()
        self.requester_name = requester_name or self._plain(self.requester)
        self.by_email: dict[str, str] = {}
        lowered = {e.lower() for e in emails if e}
        if lowered:
            from sqlalchemy import func

            for user in session.exec(select(User).where(func.lower(User.email).in_(lowered))).all():
                if user.display_name:
                    self.by_email[user.email.lower()] = user.display_name

    @staticmethod
    def _plain(email: str) -> str:
        local = email.split("@")[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
        return local.title() or "Someone"

    def of(self, email: Optional[str]) -> str:
        if not email:
            return "System"
        key = email.strip().lower()
        if key in self.by_email:
            return self.by_email[key]
        if key == self.requester:
            return self.requester_name
        return self._plain(key)

    def scrub(self, text: Optional[str]) -> str:
        """The text with every address in it replaced by the person's name."""
        return _EMAIL.sub(lambda m: self.of(m.group(0)), text or "")

    def many(self, text: Optional[str]) -> str:
        """A comma-separated list of addresses as names."""
        parts = [p.strip() for p in (text or "").split(",") if p.strip()]
        return ", ".join(self.of(p) if "@" in p else p for p in parts)


def _words(value: Optional[str]) -> str:
    """A stored status or type as words: awaiting_user -> Awaiting user, with MSP in capitals."""
    text = (value or "none").replace("_", " ").strip().capitalize()
    return text.replace("Msp", "MSP").replace("msp", "MSP")


def _reason(detail: Optional[str]) -> str:
    return f" Reason: {detail}" if detail else ""


# ---- one event as one sentence ---------------------------------------------------------------------------------


def _sentence(e: TicketEvent, n: Names) -> str:
    """What an event says, as a plain sentence. Unknown kinds fall back to their name in words, with the detail."""
    t, frm, to, detail = e.event_type, e.from_value, e.to_value, n.scrub(e.detail)
    if t == "status_changed":
        text = f"Changed the status from {_words(frm)} to {_words(to)}."
        return text + (f" {detail}." if detail and not detail.endswith(".") else (f" {detail}" if detail else ""))
    if t == "assigned":
        if to and frm:
            return f"Re-assigned the ticket from {n.of(frm)} to {n.of(to)}."
        return f"Assigned the ticket to {n.of(to)}." if to else (f"Unassigned the ticket (it was {n.of(frm)}'s)." if frm else "Left the ticket unassigned.")
    if t == "escalated":
        return f"Escalated the ticket from {frm or 'no team'} to {to}.{_reason(detail)}"
    if t == "team_changed":
        return f"Moved the ticket from {frm or 'no team'} to {to}.{_reason(detail)}"
    if t == "type_changed":
        return f"Changed the type from {_words(frm)} to {_words(to)}."
    if t == "category_changed":
        return f"Changed the category from {frm or 'none'} to {to or 'none'}.{_reason(detail)}"
    if t == "category_reviewed":
        return f"Reviewed the category: {frm or 'Other'} became {to or 'none'}."
    if t == "priority_changed":
        return f"Changed the priority from {frm or 'none'} to {to or 'none'}.{_reason(detail)}"
    if t == "due_changed":
        return f"Changed the Due date from {frm or 'none'} to {to or 'none'}.{_reason(detail)}"
    if t == "sla_start_changed":
        return f"Changed when the SLA clock starts from {frm} to {to}."
    if t == "required_by_changed":
        return f"Changed Required by from {frm or 'none'} to {to or 'none'}."
    if t == "attachment_renamed":
        return f"Renamed a file from {frm} to {to}."
    if t == "answer_changed":
        return f"Updated the answer to {detail or 'a question'}" + (f": {to}." if to else " (it was cleared).")
    if t == "note_visibility_changed":
        return "Made a note visible to the requester." if to == "visible" else "Hid a note from the requester."
    if t == "created":
        return f"Created the ticket{' (' + detail + ')' if detail else ''}."
    if t == "request_type":
        return f"Set the kind of request to {to}." if to else f"Cleared the kind of request (it was {frm})."
    if t in ("subrequest_created",):
        return f"Made the sub-request {to}."
    if t == "subrequest_skipped":
        return f"Did not make the sub-request {to}.{_reason(detail)}"
    if t == "subrequests_planned":
        return f"Planned {to} sub-requests, to be made once approved."
    if t == "approval_requested":
        return f"Asked {n.many(to)} to approve." + (f" {detail}." if detail else "")
    if t == "approval_decided":
        return f"{'Approved' if to == 'approved' else 'Declined'} the request." + (f" {detail}" if detail else "")
    if t == "approval_outcome":
        return f"The request was {to}."
    if t == "approval_waiting":
        return f"Waiting for approvers: {detail}." if detail else "Waiting for approvers."
    if t == "approval_vote_removed":
        return f"Dropped {n.of(to)}'s unanswered vote."
    if t == "approval_self_approved":
        return "The requester is an approver, so the request approved itself."
    if t == "approval_withdrawn":
        return "The requester withdrew the request while it waited for approval."
    if t == "backdated":
        return f"Backdated the {detail or 'ticket'} from {frm or 'none'} to {to or 'none'} (UTC)."
    if t == "requester_changed":
        return f"Changed the end user from {n.of(frm)} to {n.of(to)}." + (f" ({detail})" if detail else "")
    if t == "follower_added":
        return f"Added {n.of(to)} as a follower."
    if t == "follower_removed":
        return f"Removed {n.of(frm)} as a follower."
    if t == "follower_added_from_mail":
        return f"{n.of(to)} now follows the ticket." if to else (detail + "." if detail else "A follower was added from mail.")
    if t == "linked":
        return f"Linked the ticket under {to}."
    if t == "unlinked":
        return f"Unlinked the ticket from {frm}." if frm else "Unlinked the ticket."
    if t == "child_linked":
        return f"{to} was linked under this ticket."
    if t == "child_unlinked":
        return f"{frm} was unlinked from this ticket."
    if t == "split":
        return f"Split off {to}."
    if t == "split_from":
        return f"Split from {to}."
    if t == "email_sent_anyway":
        return f"Sent an email anyway that the limit on automatic emails had held back: {detail or 'an email'}."
    if t == "sent_to_msp":
        how = {"thread": "the whole thread", "original": "the original request", "automatic": "automatically"}.get(to or "", None)
        return f"Sent the ticket to the MSP{' (' + how + ')' if how else ''}.{_reason(detail)}"
    if t == "msp_hold":
        return "Kept the ticket out of the automatic MSP handoff." + (f" {detail}." if detail and "kept out" not in detail else "")
    if t == "msp_hold_released":
        return "Released the hold on the MSP handoff."
    if t == "msp_mail_received":
        return f"Mail came in from the MSP. {detail}." if detail else "Mail came in from the MSP."
    if t == "msp_forwarded":
        return detail + "." if detail else "Passed the MSP's mail on to the user."
    if t == "msp_forward_dismissed":
        return detail + "." if detail else "Chose not to pass the MSP's mail on."
    if t == "msp_told_no_longer_needed":
        return "Told the MSP the ticket is no longer needed." if to != "taken_back" else "Told the MSP the ticket was taken back."
    if t == "msp_not_told":
        return "Did not tell the MSP the ticket is finished."
    if t == "msp_acknowledged":
        return detail + "." if detail else f"The MSP acknowledged it as ticket {to}."
    if t == "with_msp_through":
        return f"With the MSP through {to}."
    if t == "taken_back_from_msp":
        return f"Took the ticket back from the MSP; it is now {_words(to)}.{_reason(detail)}"
    if t == "linked_taken_back_from_msp":
        return f"Took {to} linked tickets back from the MSP."
    if t == "requester_notice":
        return f"The requester will {'be told' if to == 'will be told' else 'not be told'} about this ticket.{_reason(detail)}"
    if t == "reopened":
        return f"Reopened the ticket. {detail}." if detail else "Reopened the ticket."
    if t == "reclosed":
        return f"Closed the ticket again: {detail or to}."
    if t == "follows":
        return f"This ticket follows {to}."
    if t == "followed_by":
        return f"{to} follows this ticket."
    if t == "message_moved":
        return f"A message was moved {'to ' + to if to else 'from ' + (frm or 'another ticket')}." + (f" {detail}." if detail else "")
    if t == "thanks_ignored":
        return f'A thank-you reply did not reopen the ticket: "{detail}"' if detail else "A thank-you reply did not reopen the ticket."
    if t == "reply_not_reopened":
        return "A reply to this finished mailbox request did not reopen it (that would change nothing in Exchange)" + (f': "{detail}"' if detail else ".")
    if t == "mail_reference_not_used":
        return (detail + ".") if detail else "A reply named this ticket but was not matched to it."
    if t in ("attachments_pending", "attachments_added", "attachments_failed") and detail:
        return detail if detail.endswith(".") else detail + "."
    if t == "attachment_skipped":
        return f"A file was not saved: {to}." + (f" {detail}." if detail else "")
    if t == "chased":
        return f"Chased the requester. {detail}." if detail else "Chased the requester."
    if t == "auto_closed":
        return f"Closed the ticket automatically. {detail}." if detail else "Closed the ticket automatically."
    text = _words(t.replace("_", " ")) + "."
    return text + (f" {detail}" if detail else "")


_TITLES = {
    "assigned": "Assigned", "escalated": "Escalated", "team_changed": "Re-assigned", "type_changed": "Changed",
    "category_changed": "Changed", "priority_changed": "Changed", "due_changed": "Changed", "required_by_changed": "Changed", "sla_start_changed": "Changed",
    "status_changed": "Changed", "reopened": "Reopened", "reclosed": "Closed again", "created": "Created",
    "note_visibility_changed": "Changed a note", "attachment_renamed": "Renamed a file", "sent_to_msp": "Sent to the MSP",
    "taken_back_from_msp": "Took back from the MSP", "linked": "Linked", "unlinked": "Unlinked", "split": "Split",
    "split_from": "Split", "backdated": "Backdated", "follower_added": "Added a follower", "follower_removed": "Removed a follower",
    "approval_requested": "Asked for approval", "approval_decided": "Decided", "approval_outcome": "Approval",
    "chased": "Chased", "requester_changed": "Changed the end user", "auto_closed": "Closed", "answer_changed": "Updated an answer",
}


# ---- messages -------------------------------------------------------------------------------------------------


def _sender_name(sender: str) -> tuple[str, Optional[str]]:
    """(name, address) of a message's sender: 'Name <a@b.c>', 'Name' or 'a@b.c'. The address is for the hover only."""
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", sender or "")
    name, address = (match.group(1).strip().strip('"'), match.group(2)) if match else ((sender or "").strip(), None)
    if "@" in name and not address:
        address, name = name, name.split("@")[0].replace(".", " ").replace("_", " ").title()
    return name or (address or "Someone"), address


def _inbound_title(message: Message, first: bool, follower: bool) -> str:
    where = {EMAIL: "by email", PORTAL: "in the portal", TEAMS_CHANNEL: "in Teams"}.get(message.channel or "", "")
    if message.channel == AGENT:
        return "Logged by an agent for the requester"
    verb = "Raised" if first else "Replied"
    return f"{verb} {where}".strip() if not follower else f"Follower replied {where}".strip()


def _outbound_title(message: Message) -> str:
    return {
        EMAIL: "Emailed the requester", TEAMS_CHANNEL: "Replied to the requester in Teams", SYSTEM: "Sent an automatic message",
        PORTAL: "Replied to the requester in the portal",
    }.get(message.channel or "", "Messaged the requester")


def _message_box(
    item: TimelineItem, ticket: Ticket, n: Names, *, first_inbound_id: Optional[int], files: dict, viewer: User, levels: AccessContext,
    deleted_names: dict[int, str],
) -> Box:
    m = item.message
    name, address = _sender_name(m.sender)
    base = dict(id=f"m{m.id}", num=m.id or 0, at=m.created_at, who=name, who_email=address, message=m, source=item.source, files=files.get(m.id, []))
    if m.deleted_at is not None:
        chip, tone = CHIPS[NOTE_DELETED]
        by = deleted_names.get(m.deleted_by_id or 0)
        return Box(kind=NOTE_DELETED, title="Note deleted", chip=chip, tone=tone, deleted_at=m.deleted_at, deleted_by=by,
                   can_manage=may_manage_note(viewer, m, ticket, levels), **base)
    manage = is_note(m) and m.ticket_id == ticket.id and may_manage_note(viewer, m, ticket, levels)
    if m.direction == MessageDirection.INTERNAL:
        chip, tone = CHIPS[NOTE]
        return Box(kind=NOTE, title="Added an internal note", chip=chip, tone=tone, body=m.body, can_manage=manage, **base)
    if m.visible_note:
        chip, tone = CHIPS[VISIBLE_NOTE]
        return Box(
            kind=VISIBLE_NOTE, title="Added a note", chip=chip, tone=tone, body=m.body, can_manage=manage, is_visible_note=True,
            lines=["Shown in the portal. Not sent by email."], waits_for_triage=not ticket.triaged_at, **base,
        )
    if m.msp_forward:
        # MSP mail is stored as an outbound message (app/msp.py), so it is told apart by `msp_forward`, before direction.
        extra = {
            "pending": ["The user was not on it; agents only until forwarded."], "forwarded": ["Forwarded to the user."],
            "dismissed": ["Not forwarded."], "internal": ["An acknowledgement; agents only."],
        }.get(m.msp_forward, [])
        return Box(
            kind=EMAIL_IN, title="MSP mail", chip="From the MSP", tone="blue", body=m.body, extra=extra,
            alert="sender not verified" if m.msp_unverified else None, **base,
        )
    if m.direction == MessageDirection.OUTBOUND:
        chip, tone = CHIPS[EMAIL_OUT]
        extra = [f"Copied: {m.cc}"] if m.cc else []
        return Box(kind=EMAIL_OUT, title=_outbound_title(m), chip=chip, tone=tone, body=m.body, extra=extra, **base)
    # Inbound: the requester or a follower.
    chip, tone = CHIPS[EMAIL_IN]
    title = _inbound_title(m, m.id == first_inbound_id, item.sender_kind == "follower")
    body, quoted = split_quoted(m.body) if m.channel == EMAIL else (m.body, None)
    return Box(kind=EMAIL_IN, title=title, chip=chip, tone=tone, body=body, quoted=quoted, **base)


# ---- events ---------------------------------------------------------------------------------------------------


def _event_who(events: list[TicketEvent], n: Names) -> tuple[str, Optional[str]]:
    actor = events[0].actor_email
    return n.of(actor), (actor if actor and "@" in actor else None)


def _triage_fields(events: list[TicketEvent], ticket: Ticket, n: Names) -> tuple[list[tuple[str, str]], list[str]]:
    """The triage box: its fields, and sentences for anything else the same save changed."""
    triaged = next(e for e in events if e.event_type == "triaged")
    detail = triaged.detail or ""
    automatic = None
    if "; triaged automatically from request type " in detail:
        detail, automatic = detail.split("; triaged automatically from request type ", 1)
    match = re.match(r"^(?P<type>[a-z_]+), team (?P<team>.*), (?P<priority>P[1-4] \(.*\))$", detail)
    fields: list[tuple[str, str]] = []
    if match:
        fields.append(("Type", _words(match["type"])))
    fields.append(("Category", triaged.to_value or "Not set"))
    if match:
        fields.append(("Team", match["team"]))
        fields.append(("Priority", match["priority"]))
    else:  # an older or unusual detail: show it as it is rather than guess
        fields.append(("Details", n.scrub(detail)))
    assigned = next((e for e in events if e.event_type == "assigned"), None)
    fields.append(("Assigned to", n.of(assigned.to_value) if assigned and assigned.to_value else "Unassigned"))
    if automatic:
        fields.append(("Triaged", f"Automatically, from the request type {automatic}"))
    rest = [
        _sentence(e, n) for e in events
        if e.event_type not in ("triaged", "team_changed", "assigned")
        and not (e.event_type == "status_changed" and e.from_value == "new")
    ]
    return fields, rest


def _event_box(
    events: list[TicketEvent], ticket: Ticket, n: Names, source: Optional[Ticket], *, newest_finish_id: Optional[int],
) -> Box:
    """One box for the events one save wrote (or one event)."""
    who, who_email = _event_who(events, n)
    at = events[0].created_at
    box_id = f"e{events[0].id}"
    num = events[0].id or 0
    triaged = any(e.event_type == "triaged" for e in events)
    finish = next(
        (e for e in events if e.event_type == "status_changed" and e.to_value in ("resolved", "closed")), None
    )
    automatic = events[0].actor_email is None
    if triaged:
        fields, rest = _triage_fields(events, ticket, n)
        chip, tone = CHIPS[TRIAGE]
        return Box(id=box_id, num=num, kind=TRIAGE, at=at, who=who, who_email=who_email, title="Triaged this ticket", chip=chip, tone=tone,
                   fields=fields, lines=rest, source=source)
    if finish is not None:
        word = "Resolved" if finish.to_value == "resolved" else "Closed"
        chip, tone = CHIPS[RESOLVED] if word == "Resolved" else ("Closed", "gray")
        rest = [_sentence(e, n) for e in events if e is not finish]
        # The notes are kept on the ticket only while it stays finished, so they show on its latest resolve or close.
        notes = ticket.resolution_notes if finish.id == newest_finish_id and ticket.resolution_notes else None
        detail = n.scrub(finish.detail)
        if detail:
            rest.insert(0, detail if detail.endswith(".") else detail + ".")
        return Box(id=box_id, num=num, kind=RESOLVED, at=at, who=who, who_email=who_email, title=f"{word} the ticket" if not automatic else f"{word} automatically",
                   chip=chip, tone=tone, lines=rest, notes=notes, source=source)
    chip, tone = CHIPS[SYSTEM_BOX] if automatic else CHIPS[CHANGE]
    lines = [_sentence(e, n) for e in events]
    kind = SYSTEM_BOX if automatic else CHANGE
    if len(events) == 1:
        title = _TITLES.get(events[0].event_type, "Updated the ticket") if not automatic else "Automatic update"
    else:
        title = "Changed" if not automatic else "Automatic update"
    return Box(id=box_id, num=num, kind=kind, at=at, who=who, who_email=who_email, title=title, chip=chip, tone=tone, lines=lines, source=source)


def build_thread(
    session: Session, ticket: Ticket, items: list[TimelineItem], viewer: User, levels: AccessContext, requester_name: Optional[str], files: dict,
) -> list[Box]:
    """The boxes of the thread, newest first, from `agent_timeline`'s items (oldest first)."""
    events = [i for i in items if i.event is not None]
    messages = [i for i in items if i.message is not None]
    emails: set[str] = set()
    for i in events:
        e = i.event
        for value in (e.actor_email, e.from_value, e.to_value, e.detail):
            emails.update(_EMAIL.findall(value or ""))
    names = Names(session, ticket, requester_name, emails)

    deleted_by = {m.message.deleted_by_id for m in messages if m.message.deleted_by_id}
    deleted_names = (
        {u.id: (u.display_name or names.of(u.email)) for u in session.exec(select(User).where(User.id.in_(deleted_by))).all()}
        if deleted_by else {}
    )
    own_inbound = [i.message for i in messages if i.source is None and i.message.direction == MessageDirection.INBOUND and not i.message.msp_forward]
    first_inbound_id = min(own_inbound, key=lambda m: (m.created_at, m.id or 0)).id if own_inbound else None

    boxes: list[Box] = []
    for i in messages:
        boxes.append(_message_box(i, ticket, names, first_inbound_id=first_inbound_id, files=files, viewer=viewer, levels=levels,
                                  deleted_names=deleted_names))

    own_events = [i for i in events if i.source is None]
    finish_ids = [
        i.event.id for i in own_events if i.event.event_type == "status_changed" and i.event.to_value in ("resolved", "closed")
    ]
    newest_finish_id = max(finish_ids) if finish_ids and ticket.status.value in ("resolved", "closed") else None
    grouped: dict[tuple, list[TimelineItem]] = {}
    order: list[tuple] = []
    for i in own_events:
        e = i.event
        key = ("g", e.ticket_id, e.group_id) if e.group_id else ("e", e.id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(i)
    for key in order:
        group = [i.event for i in sorted(grouped[key], key=lambda i: (i.event.created_at, i.event.id or 0))]
        boxes.append(_event_box(group, ticket, names, None, newest_finish_id=newest_finish_id))
    for i in events:
        if i.source is not None:  # progress of a part of a split request, shown on its original, one box each
            boxes.append(_event_box([i.event], ticket, names, i.source, newest_finish_id=None))

    # Newest first. At the same moment an event comes before the message that caused it (the oldest-first order of
    # `agent_timeline` reversed), and the later row before the earlier.
    _add_emails(session, ticket, boxes, ticket_access(viewer, ticket, levels) >= Access.WORK)
    _add_reactions(session, ticket, boxes)
    boxes.sort(key=lambda b: (b.at, 0 if b.message is not None else 1, b.num), reverse=True)
    return boxes


def _add_emails(session: Session, ticket: Ticket, boxes: list[Box], can_work: bool) -> None:
    """Every email sent or queued about the ticket (#349), one entry each. An email that carries one of the ticket's own messages (an
    agent's reply, an automatic notice) shows as the delivery line of that message's box; any other (acknowledgement, chase, approval
    mail, follower update, MSP mail, ...) is a box of its own in the automatic-message format, at the time it was queued."""
    rows = ticket_emails(session, ticket)
    if not rows:
        return
    by_message = {b.message.id: b for b in boxes if b.message is not None and b.source is None and b.kind != NOTE_DELETED}
    chip, tone = CHIPS[EMAIL_OUT]
    for row in rows:
        delivery = describe(row, can_work)
        target = by_message.get(row.message_id) if row.message_id else None
        if target is not None and target.delivery is None:
            target.delivery = delivery
            continue
        boxes.append(Box(
            id=f"o{row.id}", kind=EMAIL_OUT, at=row.created_at, num=row.id or 0, who="IT Support (automatic)", who_email=None,
            title="Sent an automatic message", chip=chip, tone=tone, delivery=delivery,
        ))


def _add_reactions(session: Session, ticket: Ticket, boxes: list[Box]) -> None:
    """Reactions (#298): a small line under the message they were made to, or a box of their own when that is not a message
    on the ticket (an approval card, a notice). Agents only; the portal is unchanged."""
    found = reactions_for_ticket(session, ticket)
    if not found:
        return
    user_ids = {r.user_id for r in found if r.user_id}
    names = {u.id: u.display_name or u.email for u in session.exec(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    by_message = {b.message.id: b for b in boxes if b.message is not None and b.source is None}
    for r in found:
        who = names.get(r.user_id or 0) or r.reactor_name or "Someone"
        line = ReactionLine(
            text=display_of(r.raw_type, r.emoji), who=who, at=r.reacted_at, tone=REACTION_TONES.get(r.sentiment, "gray"), channel=r.channel,
            removed_at=r.removed_at, sentiment=r.sentiment, no_effect=not r.by_party,
        )
        target = by_message.get(r.message_id) if r.message_id else None
        if target is not None and target.kind != NOTE_DELETED:
            target.reactions.append(line)
            continue
        chip, tone = CHIPS[REACTION]
        where = REACTED_TO.get(r.message_kind, "a message from the bot")
        boxes.append(Box(
            id=f"r{r.id}", kind=REACTION, at=r.reacted_at, num=r.id or 0, who=who, who_email=None,
            title=f"Reacted to {where} in {'Teams' if r.channel == 'teams' else 'an email'}", chip=chip, tone=tone, reactions=[line],
        ))

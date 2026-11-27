"""Reactions in Outlook (issue #299): the recurring job that reads them from the shared mailbox's Sent Items.

There is no documented Graph API for Outlook reactions, but Outlook keeps them on the message as MAPI extended properties
(property set 41F28F13-83F4-4114-A584-EEDB5A6B0BFF), which Graph can read: `ReactionsCount`, and `MapiReactionsBlob`, the base64
of plain JSON `{"Version": 1, "Reactions": [{"IsBcc", "ST", "Name", "Email", "Type", "DateTime"}]}`. A reaction changes the
message's `lastModifiedDateTime`, so a delta query on Sent Items sees it. Every email the app sends is saved to Sent Items
(`saveToSentItems`), so that folder holds every message a requester could have reacted to.

The job (`run_email_reactions`, registered as `email-reactions`, its interval the setting `email_reactions_poll_minutes`):
- keeps its delta link in `MailSyncState` (folder `.../mailFolders/sentitems`), like the inbound catch-up, and moves it only
  together with what it recorded (one transaction: the runner commits);
- matches a changed message to its ticket and to what it was (`message_kind`). Graph's `sendMail` returns no message id, so the
  Sent Items copy cannot be matched on internetMessageId; it is matched to the outbox row that sent it by ticket, subject,
  recipient and sent time (`outbox_row_for`), and the kind comes from `OutboundEmail.reaction_kind`, fixed by the mail job when the email was sent
  (the same rule as a Teams message: `reactions.kind_of_requester_message`). No row (or an old one) is kind `other`. The ticket
  is the row's ticket, else the `[INC-n]` reference in the subject through `ticket_service.find_ticket` (number only);
- takes the message as about the ticket only if it went to the requester or a follower (to, cc);
- matches the reactor's address to a person through app/identity.py (`person_for_address`, never a raw comparison); a reaction by
  someone with no account here is skipped, not recorded;
- records what is new with `reactions.record_reaction` and marks what has gone from the blob removed with
  `reactions.remove_reaction`: the model and every effect are the Teams ones, unchanged (app/reactions.py). A reaction with
  `IsBcc` true is recorded and has no effect; a `Type` we do not know is kept raw and is neutral through the admin mapping;
- fails safe: Microsoft can change these undocumented properties without notice. A message whose blob is missing (with a count
  above 0), is not base64, not JSON, or has a version other than 1 records nothing and removes nothing; the run logs ONE warning
  (counts only, never an address or the blob) and stores it on the sync row, where Admin > Mail intake shows "reaction format not
  understood" until a later run reads every blob it meets.

Nothing here sends mail, rings a bell or changes a status. The run does not commit; the runner does.
"""

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlmodel import Session, select

from app import reactions
from app.config import settings
from app.graph_client import REACTIONS_BLOB_ID, REACTIONS_COUNT_ID, graph_client
from app.identity import addresses_of, person_for_address
from app.mail_threading import THREADED_KINDS, norm_subject, recipients_of
from app.run_summary import RunSummary
from app.models import MailSyncState, OutboundEmail, Ticket, TicketFollower, TicketReaction
from app.settings_store import EMAIL_REACTIONS_BACKFILL_DAYS, count_setting
from app.subscriptions import MAX_PAGES_PER_RUN, SubscriptionError, _lock_row, describe_failure, parse_graph_time, sent_folder
from app.ticket_service import find_ticket

log = logging.getLogger("app.email_reactions")

SUPPORTED_VERSION = 1
MAX_REACTIONS_PER_MESSAGE = 200  # an Outlook message does not carry more than a handful; more than this is not what we read
SENT_MATCH_WINDOW = timedelta(minutes=15)  # how far the Sent Items time may be from the time the outbox says it sent the email
FORMAT_WARNING = "reaction format not understood"
REFERENCE = re.compile(r"\[([A-Za-z]{2,4}-\d{1,9})\]")
# Only fetch a changed message again for its reactions when it was changed well after it was sent (see `_properties`).
CHANGED_AFTER_SENT = timedelta(minutes=1)
AMBIGUOUS_GAP = timedelta(seconds=30)  # two different emails this close in time cannot be told apart by when they were sent


class FormatNotUnderstood(Exception):
    """The reaction properties are not in a shape this code reads. Carries no content (it is logged)."""


# ---- Decoding ----------------------------------------------------------------------------------------------------


def _normal_id(property_id: object) -> str:
    return " ".join(str(property_id or "").lower().split())


def properties_of(item: dict) -> tuple[Optional[int], Optional[str]]:
    """(ReactionsCount, the base64 blob) from a message's `singleValueExtendedProperties`; either is None when absent."""
    count, blob = None, None
    for entry in item.get("singleValueExtendedProperties") or []:
        if not isinstance(entry, dict):
            continue
        name = _normal_id(entry.get("id"))
        if name == _normal_id(REACTIONS_COUNT_ID):
            try:
                count = int(str(entry.get("value")).strip())
            except ValueError:
                count = None
        elif name == _normal_id(REACTIONS_BLOB_ID) and isinstance(entry.get("value"), str):
            blob = entry["value"]
    return count, blob


@dataclass(frozen=True)
class Reaction:
    address: str  # lower case
    name: Optional[str]
    type: str
    at: Optional[datetime]
    is_bcc: bool


def decode_blob(blob: str) -> list[Reaction]:
    """The reactions in a MapiReactionsBlob. Raises FormatNotUnderstood for anything that is not base64 of JSON with
    `Version` 1 and a `Reactions` list; an entry that is not usable (no address, no type) is left out, not an error."""
    try:
        raw = base64.b64decode(blob, validate=False)
        data = json.loads(raw.decode("utf-8-sig"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise FormatNotUnderstood("not base64 of JSON") from None
    if not isinstance(data, dict) or data.get("Version") != SUPPORTED_VERSION or isinstance(data.get("Version"), bool):
        raise FormatNotUnderstood("unknown version")
    entries = data.get("Reactions")
    if not isinstance(entries, list):
        raise FormatNotUnderstood("no reactions list")
    found: list[Reaction] = []
    for entry in entries[:MAX_REACTIONS_PER_MESSAGE]:
        if not isinstance(entry, dict) or not isinstance(entry.get("Email"), str) or not isinstance(entry.get("Type"), str):
            continue
        address, kind = entry["Email"].strip().lower(), entry["Type"].replace("\x00", "").strip()
        if not address or not kind:
            continue
        name = entry.get("Name") if isinstance(entry.get("Name"), str) else None
        found.append(Reaction(address, name, kind[: reactions.MAX_TYPE], parse_graph_time(entry.get("DateTime")), entry.get("IsBcc") is True))
    return found


# ---- Matching ----------------------------------------------------------------------------------------------------


def outbox_row_for(
    session: Session, subject: str, sent_at: Optional[datetime], recipients: set[str], unclaimed_only: bool = False
) -> Optional[OutboundEmail]:
    """The email the outbox sent that this Sent Items message is: same subject and recipient, sent at (nearly) the same time, the
    closest in time when there are several. None when none matches (an email sent by hand from the mailbox, or before the app kept
    what it sent), and None when the two closest are different kinds of email sent within AMBIGUOUS_GAP of each other (a reply and
    then the resolution, seconds apart: `sent_at` is the app's clock after Graph answered, so their order could be swapped), because a
    wrong kind would be worse than the plain `other`. A reply's copy carries an "RE: " the row's subject does not (#350), so it is
    matched on the subject without it. `unclaimed_only` looks only at emails whose Sent Items id is not yet known."""
    if not subject or sent_at is None:
        return None
    query = select(OutboundEmail).where(
        OutboundEmail.subject.in_({subject, norm_subject(subject)}), OutboundEmail.ticket_id.is_not(None), OutboundEmail.sent_at.is_not(None),
        OutboundEmail.sent_at >= sent_at - SENT_MATCH_WINDOW, OutboundEmail.sent_at <= sent_at + SENT_MATCH_WINDOW,
    )
    if unclaimed_only:
        query = query.where(OutboundEmail.graph_message_id.is_(None), OutboundEmail.kind.in_(THREADED_KINDS))
    rows = session.exec(query).all()
    rows = sorted(
        (row for row in rows if (row.to_address or "").strip().lower() in recipients), key=lambda row: abs(_aware(row.sent_at) - sent_at),
    )
    if not rows:
        return None
    if len(rows) > 1 and rows[0].reaction_kind != rows[1].reaction_kind:
        if abs(_aware(rows[1].sent_at) - sent_at) - abs(_aware(rows[0].sent_at) - sent_at) < AMBIGUOUS_GAP:
            return None
    return rows[0]


def claim_sent_item(session: Session, item: dict) -> bool:
    """Notes this Sent Items message's id on the email it is the copy of, if that email's copy was not yet found (#350: the next email
    to the same people replies to it). The same match as the reactions use, and nothing is claimed when two kinds of email are too close
    to tell apart. Returns whether an email was claimed."""
    subject = item.get("subject") if isinstance(item.get("subject"), str) else ""
    row = outbox_row_for(session, subject, parse_graph_time(item.get("sentDateTime")), recipients_of(item), unclaimed_only=True)
    if row is None or session.exec(select(OutboundEmail.id).where(OutboundEmail.graph_message_id == item["id"])).first() is not None:
        return False
    row.graph_message_id = item["id"]
    session.add(row)
    return True


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def went_to_a_party(session: Session, ticket: Ticket, recipients: set[str]) -> bool:
    """Whether the email went to the ticket's requester (under any of their addresses) or to a follower."""
    mine = addresses_of(session, ticket.requester_email)
    mine |= {a.strip().lower() for a in session.exec(select(TicketFollower.email).where(TicketFollower.ticket_id == ticket.id)).all()}
    return bool(recipients & mine)


# ---- One run -----------------------------------------------------------------------------------------------------


@dataclass
class RunCounts:
    changed: int = 0
    recorded: int = 0
    removed: int = 0
    skipped: int = 0  # no ticket, not sent to the requester or a follower, or a reactor with no account here
    unreadable: set = field(default_factory=set)  # message refs whose reaction properties were not understood
    understood: int = 0  # messages whose blob was read


def email_reactions_job(session: Session) -> RunSummary:
    """The registered job: `run_email_reactions`, reported as a summary: read (Sent Items messages whose reactions were read),
    recorded (reactions added or taken back)."""
    counts = run_email_reactions(session)
    return RunSummary.of(read=counts.understood, recorded=counts.recorded + counts.removed)


def run_email_reactions(session: Session, graph=None, now: Optional[datetime] = None) -> RunCounts:
    """One run of the job: reads the Sent Items messages changed since the stored delta link and brings each one's reactions in
    line with its blob. Returns the counts. Does not commit (the runner does), so the delta link moves only with what was
    recorded. A failure raises, the runner rolls back and retries, and the failure shows on Admin > Recurring jobs and the card
    on Admin > Mail intake."""
    counts = RunCounts()
    if not (settings.graph_configured and settings.graph_shared_mailbox):
        return counts
    graph = graph or graph_client
    now = now or datetime.now(timezone.utc)
    state = _lock_row(session, MailSyncState, "folder", sent_folder())
    if state is None:
        return counts  # another run has it
    try:
        _sync(session, graph, state, now, counts)
    except Exception as error:
        session.rollback()
        message = describe_failure(error)
        failed = _lock_row(session, MailSyncState, "folder", sent_folder())
        if failed is not None:
            failed.last_error, failed.last_error_at = message, now
            session.add(failed)
        session.commit()
        log.error("email reactions: %s", message)
        raise SubscriptionError(message) from None
    return counts


def _sync(session: Session, graph, state: MailSyncState, now: datetime, counts: RunCounts) -> None:
    link = state.resume_link or state.delta_link
    since = None
    if link is None:
        since = state.starting_point_at = now - timedelta(days=count_setting(session, EMAIL_REACTIONS_BACKFILL_DAYS))
    restarted = False
    expand = True
    for _ in range(MAX_PAGES_PER_RUN):
        try:
            page = graph.sent_items_delta_page(link, since if link is None else None, expand=expand)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 400 and link is None and expand:
                # Graph refused `$expand` on a delta query: ask for the matching fields only, and read each changed message on its own.
                expand = False
                log.warning("email reactions: Graph refused $expand on the Sent Items delta; reading changed messages one by one")
                continue
            if status != 410 or link is None or restarted:
                raise
            # Graph no longer remembers our place: start again from the backfill window (what is recorded is never recorded twice).
            state.delta_link = state.resume_link = link = None
            since = state.starting_point_at = now - timedelta(days=count_setting(session, EMAIL_REACTIONS_BACKFILL_DAYS))
            restarted = True
            continue
        items = [item for item in page.get("value", []) if isinstance(item, dict) and item.get("id") and "@removed" not in item]
        standing = _standing_on(session, [_ref_of(item) for item in items])
        for item in items:
            counts.changed += 1
            claim_sent_item(session, item)
            _one_message(session, graph, item, counts, now, standing.get(_ref_of(item), []))
        next_link, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
        if next_link:
            state.resume_link = link = next_link
            continue
        if not delta_link:
            raise SubscriptionError("Graph returned neither a next page nor a delta link")
        state.delta_link, state.resume_link = delta_link, None
        state.last_synced_at, state.last_error, state.last_error_at = now, None, None
        state.last_handed_over = counts.recorded
        _note_format(state, counts, now)
        session.add(state)
        log.info(
            "email reactions: %s changed message(s), %s reaction(s) recorded, %s removed, %s skipped, %s not understood",
            counts.changed, counts.recorded, counts.removed, counts.skipped, len(counts.unreadable),
        )
        return
    _note_format(state, counts, now)
    session.add(state)  # out of pages for this run: the resume link is kept


def _ref_of(item: dict) -> str:
    """How the message is named on a reaction row: its internetMessageId, else its Graph id."""
    ref = (item.get("internetMessageId") if isinstance(item.get("internetMessageId"), str) else "").strip() or item["id"]
    return ref[: reactions.MAX_TEXT]


def _standing_on(session: Session, refs: list[str]) -> dict[str, list[TicketReaction]]:
    """The email reactions that still stand on these messages, by message, in one query for a whole page."""
    found: dict[str, list[TicketReaction]] = {}
    if refs:
        for row in session.exec(
            select(TicketReaction).where(
                TicketReaction.channel == reactions.EMAIL, TicketReaction.message_ref.in_(refs), TicketReaction.removed_at.is_(None)
            )
        ).all():
            found.setdefault(row.message_ref, []).append(row)
    return found


def _note_format(state: MailSyncState, counts: RunCounts, now: datetime) -> None:
    """One warning for the whole run, kept on the sync row for Admin > Mail intake. It clears only when a run read a blob and met
    no unreadable one: a run that saw no reactions at all says nothing about whether the format is understood now."""
    if counts.unreadable:
        noun = "message" if len(counts.unreadable) == 1 else "messages"
        state.last_warning, state.last_warning_at = f"{FORMAT_WARNING}: {len(counts.unreadable)} sent {noun} at the last run", now
        log.warning("email reactions: %s (%s sent %s; Microsoft may have changed the undocumented properties)", FORMAT_WARNING, len(counts.unreadable), noun)
    elif counts.understood:
        state.last_warning = state.last_warning_at = None


def _changed_since_sent(item: dict) -> bool:
    """Whether the message was changed well after it was sent, which is what a reaction does to it."""
    sent, changed = parse_graph_time(item.get("sentDateTime")), parse_graph_time(item.get("lastModifiedDateTime"))
    return sent is not None and changed is not None and changed - sent > CHANGED_AFTER_SENT


def _properties(graph, item: dict) -> Optional[tuple[Optional[int], Optional[str]]]:
    """The reaction properties of a changed message as (count, blob), either None when the message has none: a message with no
    reaction comes back without them. None (not a pair) means they could not be read at all, and nothing may then be concluded
    from it (above all, no reaction may be taken as removed). A delta query is meant to carry them (`$expand`); when the item has
    none and was changed well after it was sent (a reaction changes it, a plain send does not), the message is read once on its own."""
    if "singleValueExtendedProperties" in item:
        return properties_of(item)
    sent, changed = parse_graph_time(item.get("sentDateTime")), parse_graph_time(item.get("lastModifiedDateTime"))
    if sent is None or changed is None:
        return None
    if changed - sent > CHANGED_AFTER_SENT:
        full = graph.get_sent_message_reactions(item["id"])
        return properties_of(full) if isinstance(full, dict) else None  # gone (moved or deleted): unknown
    return None, None  # never touched since it was sent: no reactions


def _one_message(session: Session, graph, item: dict, counts: RunCounts, now: datetime, standing: list[TicketReaction]) -> None:
    subject = item.get("subject") if isinstance(item.get("subject"), str) else ""
    message_ref = _ref_of(item)
    if not standing and "singleValueExtendedProperties" not in item and not _changed_since_sent(item):
        return  # the usual sent message: nothing reacted to it and nothing is recorded
    properties = _properties(graph, item)
    if properties is None:
        return  # could not be read: leave whatever is recorded alone
    count, blob = properties
    if not count and blob is None and not standing:
        return
    try:
        if blob is None:
            if count:
                raise FormatNotUnderstood("no blob")
            found: list[Reaction] = []  # the count is 0 or absent: every reaction has been taken back
        else:
            found = decode_blob(blob)
            counts.understood += 1
    except FormatNotUnderstood:
        counts.unreadable.add(message_ref)
        return  # record nothing, remove nothing

    # Taken back: what stands but is no longer in the blob. Each is removed on its own ticket, so this holds even when the message can
    # no longer be matched (its outbox row was purged, say).
    still_there = {(reaction.address, reaction.type[: reactions.MAX_TYPE]) for reaction in found}
    for gone in standing:
        if (gone.reactor_ref, gone.raw_type) not in still_there:
            ticket = session.get(Ticket, gone.ticket_id)
            if ticket is not None and reactions.remove_reaction(
                session, ticket, channel=reactions.EMAIL, message_ref=message_ref, reactor_ref=gone.reactor_ref, raw_type=gone.raw_type, at=now,
            ):
                counts.removed += 1
    if not found:
        return

    recipients = recipients_of(item)
    sent_at = parse_graph_time(item.get("sentDateTime"))
    row = outbox_row_for(session, subject, sent_at, recipients)
    ticket = session.get(Ticket, row.ticket_id) if row is not None else None
    if ticket is None:
        match = REFERENCE.search(subject)
        ticket = find_ticket(session, match.group(1)) if match else None
    if ticket is None or not went_to_a_party(session, ticket, recipients):
        counts.skipped += 1
        return
    kind = (row.reaction_kind if row is not None else None) or reactions.OTHER
    for reaction in found:
        person = person_for_address(session, reaction.address)
        if person is None:
            counts.skipped += 1
            continue
        made = reactions.record_reaction(
            session, ticket, channel=reactions.EMAIL, message_kind=kind, message_ref=message_ref, raw_type=reaction.type,
            # A Bcc reaction is recorded but never counts as the requester's or a follower's: no person, so no effect.
            reactor=reactions.Reactor(ref=reaction.address, name=reaction.name, address=reaction.address, user=None if reaction.is_bcc else person),
            message_id=row.message_id if row is not None else None, message_sent_at=sent_at, at=min(reaction.at or now, now),
        )
        if made is not None:
            counts.recorded += 1

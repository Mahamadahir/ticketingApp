"""Thank-you rules and their review queue (phase 4E-2). Nothing here commits.

A reply on a Resolved ticket normally reopens it (app/reopening.py). The exception: a short thank-you.
An admin keeps a list of rules; when the NEW text of a reply (the mail's uniqueBody, or the portal
comment, never the quoted thread) matches an enabled rule and the reply has no attachment, the reply is
recorded and the ticket stays Resolved. The one place that decides is `apply_requester_reply`
(app/splitting.py), which asks `is_thanks_reply`; mail and portal comments both go through it.

A rule has a match type (equals, starts with, contains, ends with), a pattern that may use `*` as a
wildcard, and a maximum length. Both the reply and the pattern are normalised first: lower case, and
punctuation, symbols and emoji turned into spaces (apostrophes dropped), runs of spaces collapsed, so
"Thanks!!  :)" is "thanks" and the maximum length counts those normalised characters. There are no
regular expressions: user text is never compiled into anything. A pattern is split on `*` and its pieces
are found in the text one after another with plain `str.startswith`, `endswith` and `find`, so the work is
linear in the text, which is cut off at MAX_TEXT_SCAN before it is looked at.

Review queue: when an agent recloses a reopened ticket as "User said thanks" (app/reopening.py), the
reply that reopened it is added to the queue (`record_candidate`), once per distinct normalised text with
a count. An admin turns a candidate into a rule (`candidate_to_rule`) or dismisses it (`dismiss_candidate`),
and a dismissed text is not queued again.
"""

import unicodedata
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import Message, MessageDirection, MessageVisibility, ThanksCandidate, ThanksRule, Ticket, TicketEvent

MATCH_TYPES: dict[str, str] = {
    "equals": "Equals",
    "starts_with": "Starts with",
    "contains": "Contains",
    "ends_with": "Ends with",
}

MAX_PATTERN_LENGTH = 100  # as typed, wildcards included
MAX_WILDCARDS = 5
MAX_RULE_LENGTH = 200  # the largest "maximum length" a rule can have
MAX_TEXT_SCAN = 1000  # a reply longer than this (as written) is never a thank-you, so it is not scanned
MAX_SAMPLE_LENGTH = 200

CANDIDATE_PENDING = "pending"
CANDIDATE_DISMISSED = "dismissed"
CANDIDATE_RULE = "rule"

EVENT_THANKS_IGNORED = "thanks_ignored"  # detail: what the reply said
EVENT_REOPENED_BY_REPLY = "The requester replied"  # the reason apply_requester_reply gives reopen_ticket


class ThanksRuleError(ValueError):
    """A rule that cannot be saved, worded for the admin."""


def normalise_text(text: str) -> str:
    """Lower case, punctuation, symbols and emoji turned into spaces (apostrophes dropped), spaces collapsed."""
    kept = []
    for char in unicodedata.normalize("NFKC", text or "").casefold():
        if char in "'’‘`":
            continue
        kept.append(char if char.isalnum() else " ")
    return " ".join("".join(kept).split())


def _parts(pattern: str) -> tuple[bool, list[str], bool]:
    """A stored or typed pattern as (starts with a wildcard, normalised pieces between the wildcards,
    ends with a wildcard). Empty pieces (two wildcards in a row) are dropped."""
    stripped = (pattern or "").strip()
    pieces = [piece for piece in (normalise_text(p) for p in stripped.split("*")) if piece]
    return stripped.startswith("*"), pieces, stripped.endswith("*")


def normalise_pattern(pattern: str) -> str:
    lead, pieces, trail = _parts(pattern)
    if not pieces:
        return ""
    return ("*" if lead else "") + "*".join(pieces) + ("*" if trail else "")


def clean_rule(match_type: str, pattern: str, max_length) -> tuple[str, str, int]:
    """Validates a rule and returns (match type, normalised pattern, maximum length). Raises ThanksRuleError."""
    if match_type not in MATCH_TYPES:
        raise ThanksRuleError("Choose a match type from the list")
    pattern = (pattern or "").strip()
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ThanksRuleError(f"The pattern can be at most {MAX_PATTERN_LENGTH} characters")
    if pattern.count("*") > MAX_WILDCARDS:
        raise ThanksRuleError(f"The pattern can have at most {MAX_WILDCARDS} wildcards (*)")
    cleaned = normalise_pattern(pattern)
    if not cleaned:
        raise ThanksRuleError("The pattern needs some words or letters, not just punctuation or wildcards")
    try:
        longest = int(max_length)
    except (TypeError, ValueError):
        raise ThanksRuleError("The maximum length must be a whole number")
    if not 1 <= longest <= MAX_RULE_LENGTH:
        raise ThanksRuleError(f"The maximum length must be between 1 and {MAX_RULE_LENGTH}")
    return match_type, cleaned, longest


def rule_matches(match_type: str, pattern: str, max_length: int, normalised: str) -> bool:
    """Whether a normalised reply matches one rule. No regular expressions: the pattern's pieces are found in
    order with startswith, endswith and find. A bad rule (no pieces) matches nothing."""
    if not normalised or len(normalised) > max_length:
        return False
    lead, pieces, trail = _parts(pattern)
    if not pieces:
        return False
    anchor_start = match_type in ("equals", "starts_with") and not lead
    anchor_end = match_type in ("equals", "ends_with") and not trail
    position = 0
    last = len(pieces) - 1
    for index, piece in enumerate(pieces):
        if index == 0 and anchor_start:
            if not normalised.startswith(piece):
                return False
            position = len(piece)
            if index == last and anchor_end:
                return len(normalised) == len(piece)
        elif index == last and anchor_end:
            return len(normalised) - len(piece) >= position and normalised.endswith(piece)
        else:
            found = normalised.find(piece, position)
            if found < 0:
                return False
            position = found + len(piece)
    return True


def enabled_rules(session: Session) -> list[ThanksRule]:
    return list(session.exec(select(ThanksRule).where(ThanksRule.enabled == True).order_by(ThanksRule.id)).all())  # noqa: E712


def is_thanks_reply(session: Session, text: Optional[str], has_attachment: bool = False) -> bool:
    """Whether a reply is a thank-you that should be recorded without reopening: its new text matches an
    enabled rule and it has no attachment. `text` must be the new text only (never the quoted thread)."""
    if has_attachment or not text or len(text) > MAX_TEXT_SCAN:
        return False
    normalised = normalise_text(text)
    if not normalised:
        return False
    return any(rule_matches(r.match_type, r.pattern, r.max_length, normalised) for r in enabled_rules(session))


# ---- Rules (admin) ----------------------------------------------------------------------------


def all_rules(session: Session) -> list[ThanksRule]:
    return list(session.exec(select(ThanksRule).order_by(ThanksRule.id)).all())


def add_rule(session: Session, match_type: str, pattern: str, max_length, created_by: str) -> ThanksRule:
    match_type, pattern, longest = clean_rule(match_type, pattern, max_length)
    row = ThanksRule(match_type=match_type, pattern=pattern, max_length=longest, created_by=created_by)
    session.add(row)
    session.flush()
    return row


def update_rule(session: Session, rule_id: int, match_type: str, pattern: str, max_length) -> Optional[ThanksRule]:
    """Changes a rule, or returns None if it is gone."""
    row = session.get(ThanksRule, rule_id)
    if row is None:
        return None
    row.match_type, row.pattern, row.max_length = clean_rule(match_type, pattern, max_length)
    session.add(row)
    return row


def set_rule_enabled(session: Session, rule_id: int, enabled: bool) -> Optional[ThanksRule]:
    row = session.get(ThanksRule, rule_id)
    if row is None:
        return None
    row.enabled = enabled
    session.add(row)
    return row


def describe(rule: ThanksRule) -> str:
    return f"{MATCH_TYPES.get(rule.match_type, rule.match_type)} \"{rule.pattern}\" up to {rule.max_length} characters"


# ---- The review queue -------------------------------------------------------------------------


def pending_candidates(session: Session) -> list[ThanksCandidate]:
    return list(
        session.exec(
            select(ThanksCandidate).where(ThanksCandidate.status == CANDIDATE_PENDING)
            .order_by(ThanksCandidate.occurrences.desc(), ThanksCandidate.id)
        ).all()
    )


def _reply_that_reopened(session: Session, ticket: Ticket) -> Optional[Message]:
    """The requester's message that reopened this ticket, if it was reopened by a reply (not by someone
    pressing Reopen): the newest public inbound message at or before the latest reopened event."""
    event = session.exec(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "reopened")
        .order_by(TicketEvent.id.desc()).limit(1)
    ).first()
    if event is None or event.detail != EVENT_REOPENED_BY_REPLY:
        return None
    return session.exec(
        select(Message).where(
            Message.ticket_id == ticket.id, Message.direction == MessageDirection.INBOUND,
            Message.visibility == MessageVisibility.PUBLIC, Message.created_at <= event.created_at,
        ).order_by(Message.id.desc()).limit(1)
    ).first()


def record_candidate(session: Session, ticket: Ticket) -> Optional[ThanksCandidate]:
    """Called when a reopened ticket is reclosed as "said thanks": queues the reply that reopened it for an
    admin to review. Nothing is queued for a ticket reopened by a button, for an empty or long text, for a
    text a rule already covers (it would not have reopened) or one an admin already dismissed or ruled on
    (a pending one only counts up)."""
    message = _reply_that_reopened(session, ticket)
    if message is None or len(message.body) > MAX_TEXT_SCAN:
        return None
    normalised = normalise_text(message.body)
    if not normalised or len(normalised) > MAX_RULE_LENGTH:
        return None
    existing = session.exec(select(ThanksCandidate).where(ThanksCandidate.normalised_text == normalised)).first()
    if existing is None:
        row = ThanksCandidate(
            normalised_text=normalised, sample_text=message.body.strip()[:MAX_SAMPLE_LENGTH], first_ticket_id=ticket.id
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            return row
        except IntegrityError:  # another reclose queued the same text a moment ago
            existing = session.exec(select(ThanksCandidate).where(ThanksCandidate.normalised_text == normalised)).first()
            if existing is None:
                return None
    if existing.status == CANDIDATE_PENDING:
        existing.occurrences += 1
        session.add(existing)
        return existing
    return None


def dismiss_candidate(session: Session, candidate_id: int, admin_email: str) -> Optional[ThanksCandidate]:
    row = session.get(ThanksCandidate, candidate_id)
    if row is None or row.status != CANDIDATE_PENDING:
        return None
    row.status = CANDIDATE_DISMISSED
    row.decided_by = admin_email
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    return row


def candidate_to_rule(
    session: Session, candidate_id: int, match_type: str, pattern: str, max_length, admin_email: str
) -> Optional[tuple[ThanksCandidate, ThanksRule]]:
    """Makes a rule from a pending candidate (the admin may edit the suggested rule first) and marks the
    candidate done. None if the candidate is gone or already decided. Raises ThanksRuleError for a bad rule."""
    row = session.get(ThanksCandidate, candidate_id)
    if row is None or row.status != CANDIDATE_PENDING:
        return None
    rule = add_rule(session, match_type, pattern, max_length, admin_email)
    row.status = CANDIDATE_RULE
    row.decided_by = admin_email
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    return row, rule

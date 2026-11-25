"""Reactions to messages about a ticket (issue #298, Teams; #299 reuses all of it for Outlook email reactions).

A reaction is a soft signal. It is recorded, shown on the ticket and in Team activity, and it can do three small things,
all decided HERE and nowhere else (`apply_effects`), so every channel gets the same rules:

- feedback: a positive or negative reaction on a resolution message is a quick rating (5 or 1, `TicketFeedback.from_reaction`).
  A real answer through the feedback link always replaces it (`action_links.submit_feedback` calls `drop_soft_feedback`),
  and taking the reaction back removes it.
- acknowledgement: a positive reaction on a resolution message, or on a message asking the requester to reply (Awaiting
  user), counts as the requester answering for the chase: it stops the reminders and the auto-resolve that follows one
  (`acknowledged_at`, asked by app/auto_close_chase.py). It never closes or resolves anything, and auto-close keeps its
  timer. It is computed from the live reaction rows, so taking the reaction back ends it with no cleanup.
  It is shown (an "Acknowledged by reaction" chip, `acknowledged_views`) and has a backstop (#303): `acknowledged_wait_resolve_days`
  after the reaction, with no reply, the ordinary auto-resolve runs (the job does that, it is not a reaction effect).
- negative flag: a negative reaction shows a red "Negative reaction" chip on the ticket and in the queue until an agent
  dismisses it (`open_negative_ids`, `dismiss_negative`).

A reaction never changes a ticket's status and never rings a bell or sends a message. Only the requester's reactions (and
a follower's) have effects: whoever else reacts is recorded and shown, nothing more (`by_party`, from `is_party`). What a
reaction means comes from the admin setting `reaction_sentiments` (reaction type to positive, negative or neutral);
anything not in it is neutral, kept exactly as received.

How a channel uses this module: write a `TicketReaction` with `record_reaction(...)` when someone reacts and
`remove_reaction(...)` when they take it back; give it the ticket, the channel (`TEAMS` or `EMAIL`), the kind of message
(`RESOLUTION`, `AWAITING_USER`, `APPROVAL`, `OTHER`), the platform's id of that message, the type exactly as received and
who it was. Everything else follows. The Teams side is app/teams_reactions.py; the bot's own sent messages are remembered
here too (`record_bot_message`) because Teams names the message reacted to by its id only.

Nothing here commits; the caller does.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import delete, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.identity import is_follower_of, is_requester
from app.models import BotMessage, Ticket, TicketEvent, TicketFeedback, TicketReaction, TicketStatus, User
from app.settings_store import ACKNOWLEDGED_WAIT_RESOLVE_DAYS, REACTION_SENTIMENTS, count_setting, get_setting

log = logging.getLogger("app.reactions")

TEAMS, EMAIL = "teams", "email"
CHANNELS = (TEAMS, EMAIL)
RESOLUTION, AWAITING_USER, APPROVAL, OTHER = "resolution", "awaiting_user", "approval", "other"
MESSAGE_KINDS = (RESOLUTION, AWAITING_USER, APPROVAL, OTHER)
POSITIVE, NEGATIVE, NEUTRAL = "positive", "negative", "neutral"
SENTIMENTS = (POSITIVE, NEGATIVE, NEUTRAL)

MAX_TYPE = 64  # a longer "type" is not a reaction we know how to read; it is cut, never refused
MAX_TEXT = 200
RATING_POSITIVE, RATING_NEGATIVE = 5, 1
AUDIT_DISMISSED = "reaction_dismissed"

# How the six standard Teams reactions are shown. Anything else is shown as received (its emoji if the platform sent one,
# else the raw type).
STANDARD_EMOJI = {"like": "\U0001F44D", "heart": "❤️", "laugh": "\U0001F606", "surprised": "\U0001F62E", "sad": "\U0001F622", "angry": "\U0001F621"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- The mapping -------------------------------------------------------------------------------------------------


def parse_mapping(text: str) -> dict[str, str]:
    """`like=positive, sad=negative` (commas or new lines) as {type: sentiment}; types are lower case. Raises ValueError, with
    the words to show an admin, for a pair that is not `type=positive|negative|neutral`."""
    mapping: dict[str, str] = {}
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, equals, sentiment = part.partition("=")
        name, sentiment = name.strip().lower(), sentiment.strip().lower()
        if not equals or not name or len(name) > MAX_TYPE or sentiment not in SENTIMENTS:
            raise ValueError("Write each reaction as name=positive, name=negative or name=neutral, separated by commas")
        mapping[name] = sentiment
    return mapping


def mapping(session: Session) -> dict[str, str]:
    """The admin mapping; a stored value that cannot be read gives the default rather than an error."""
    try:
        return parse_mapping(get_setting(session, REACTION_SENTIMENTS))
    except ValueError:
        from app.settings_store import REGISTRY

        return parse_mapping(REGISTRY[REACTION_SENTIMENTS].default)


def sentiment_of(session: Session, raw_type: str) -> str:
    """What the reaction means now. A type that is not in the mapping is neutral."""
    return mapping(session).get((raw_type or "").strip().lower(), NEUTRAL)


def display_of(raw_type: str, emoji: Optional[str] = None) -> str:
    """The reaction as people read it: the emoji the platform sent, else the standard one for the type with the type's name
    ("👍 like"), else the type exactly as received."""
    if emoji:
        return emoji
    standard = STANDARD_EMOJI.get((raw_type or "").strip().lower())
    return f"{standard} {raw_type}" if standard else raw_type


# ---- What the bot sent -------------------------------------------------------------------------------------------


def kind_of_notification(notification_kind: str) -> str:
    """The kind of message a bot notification card is, from the notification it carries."""
    from app.notifications import APPROVAL_WAITING, AWAITING_YOU, TICKET_CLOSED, TICKET_RESOLVED

    if notification_kind in (TICKET_RESOLVED, TICKET_CLOSED):
        return RESOLUTION
    if notification_kind == AWAITING_YOU:
        return AWAITING_USER
    if notification_kind == APPROVAL_WAITING:
        return APPROVAL
    return OTHER


def kind_of_requester_message(ticket: Ticket) -> str:
    """The kind of an agent's or the system's message to the requester, from where the ticket stands when it is sent: a
    message on a resolved or closed ticket is the resolution; one on an Awaiting user ticket asks for a reply."""
    if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        return RESOLUTION
    if ticket.status == TicketStatus.AWAITING_USER:
        return AWAITING_USER
    return OTHER


def record_bot_message(
    session: Session, ticket_id: Optional[int], activity_id: Optional[str], conversation_id: str, kind: str, message_id: Optional[int] = None,
) -> Optional[BotMessage]:
    """Remembers a message or card the bot sent about a ticket, so a reaction to it can be found. Nothing is kept for a send
    that returned no id or is not about a ticket. Sending twice with the same id keeps one row."""
    if not ticket_id or not activity_id or not conversation_id or kind not in MESSAGE_KINDS:
        return None
    activity_id, conversation_id = activity_id[:MAX_TEXT], conversation_id[:512]
    existing = session.exec(
        select(BotMessage).where(BotMessage.conversation_id == conversation_id, BotMessage.activity_id == activity_id)
    ).first()
    if existing is not None:
        return existing
    row = BotMessage(activity_id=activity_id, conversation_id=conversation_id, ticket_id=ticket_id, message_id=message_id, kind=kind)
    session.add(row)
    return row


def find_bot_message(session: Session, activity_id: str, conversation_id: str, reactor_conversation_id: Optional[str] = None) -> Optional[BotMessage]:
    """The message of ours a reaction was made to: by the activity id in the conversation. Teams ids are only unique within a
    chat, so when the conversation id Teams sent differs from the one stored, an id that matches exactly one stored message
    in the reacting person's own chat is still accepted (that chat is `reactor_conversation_id`)."""
    exact = session.exec(
        select(BotMessage).where(BotMessage.activity_id == activity_id, BotMessage.conversation_id == conversation_id)
    ).first()
    if exact is not None or not reactor_conversation_id:
        return exact
    return session.exec(
        select(BotMessage).where(BotMessage.activity_id == activity_id, BotMessage.conversation_id == reactor_conversation_id)
    ).first()


# ---- Recording ---------------------------------------------------------------------------------------------------


def is_party(session: Session, ticket: Ticket, user: Optional[User]) -> bool:
    """Whether the person is the ticket's requester or one of its followers (through all their addresses and linked
    identities: app/identity.py). Only a party's reactions can have an effect."""
    return user is not None and (is_requester(ticket, user) or is_follower_of(session, ticket, user))


@dataclass(frozen=True)
class Reactor:
    """Who reacted, as the platform said (`ref`: the Entra object id for Teams, the address for email) and the account it
    matched through app/identity.py, if any."""

    ref: str
    name: Optional[str] = None
    address: Optional[str] = None
    user: Optional[User] = None


def _clip(value: Optional[str], limit: int = MAX_TEXT) -> Optional[str]:
    value = (value or "").replace("\x00", "").strip()
    return value[:limit] or None


def _standing(session: Session, channel: str, message_ref: str, reactor_ref: str, raw_type: str) -> Optional[TicketReaction]:
    """The reaction by this person of this type to this message that has not been taken back, if there is one."""
    return session.exec(
        select(TicketReaction).where(
            TicketReaction.channel == channel, TicketReaction.message_ref == message_ref[:MAX_TEXT], TicketReaction.reactor_ref == reactor_ref[:512],
            TicketReaction.raw_type == raw_type, TicketReaction.removed_at.is_(None),
        )
    ).first()


def record_reaction(
    session: Session, ticket: Ticket, *, channel: str, message_kind: str, message_ref: str, raw_type: str, reactor: Reactor,
    emoji: Optional[str] = None, message_id: Optional[int] = None, message_sent_at: Optional[datetime] = None, at: Optional[datetime] = None,
) -> Optional[TicketReaction]:
    """Records one reaction and applies its effects. Returns the row, or None when it is already recorded and still standing
    (the platform delivered it twice, even at the same moment) or is not usable (no type, no one). The type is kept exactly as
    received. `message_sent_at` is when the message reacted to was sent, when the channel knows it."""
    raw = (raw_type or "").replace("\x00", "").strip()[:MAX_TYPE]
    ref = _clip(reactor.ref, 512)
    if channel not in CHANNELS or not raw or not ref or not message_ref:
        return None
    if message_kind not in MESSAGE_KINDS:
        message_kind = OTHER
    if _standing(session, channel, message_ref, ref, raw) is not None:
        return None
    row = TicketReaction(
        ticket_id=ticket.id, channel=channel, message_kind=message_kind, message_ref=message_ref[:MAX_TEXT], message_id=message_id,
        message_sent_at=message_sent_at, user_id=reactor.user.id if reactor.user is not None else None, reactor_ref=ref,
        reactor_name=_clip(reactor.name), reactor_address=_clip(reactor.address), raw_type=raw, emoji=_clip(emoji, 32),
        sentiment=sentiment_of(session, raw), by_party=is_party(session, ticket, reactor.user), reacted_at=at or _now(),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None  # the same reaction arrived at the same moment through another request: it is recorded
    apply_effects(session, ticket)
    return row


def remove_reaction(
    session: Session, ticket: Ticket, *, channel: str, message_ref: str, reactor_ref: str, raw_type: str, at: Optional[datetime] = None,
) -> Optional[TicketReaction]:
    """Marks the standing reaction removed (the row and its history stay). Returns it, or None when there was none standing.
    Whatever it did stops counting; nothing already sent is undone."""
    row = _standing(session, channel, message_ref or "", (reactor_ref or "").strip(), (raw_type or "").strip()[:MAX_TYPE])
    if row is None:
        return None
    row.removed_at = at or _now()
    session.add(row)
    session.flush()
    apply_effects(session, ticket)
    return row


# ---- The effects: the one place --------------------------------------------------------------------------------


def apply_effects(session: Session, ticket: Ticket) -> None:
    """Brings what reactions do to the ticket in line with its live reactions. Run after every recording and removal, so a
    reaction taken back stops counting by the same code that counted it. The acknowledgement and the negative chip are read
    from the rows when they are needed (`acknowledged_since`, `open_negative_ids`); the feedback is a row, kept here."""
    _refresh_feedback(session, ticket)


def _cycle_start(ticket: Ticket) -> Optional[datetime]:
    return ticket.resolved_at if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) else None


def _of_this_round(since: datetime):
    """SQL: the reaction is not about a message sent before `since` (a message of an earlier resolution or wait). A channel that
    does not know when the message was sent (`message_sent_at` empty) is taken as this round's."""
    return or_(TicketReaction.message_sent_at.is_(None), TicketReaction.message_sent_at >= since)


def _refresh_feedback(session: Session, ticket: Ticket) -> None:
    start = _cycle_start(ticket)
    if start is None:
        # Not resolved now (reopened): nothing new can be rated, but a quick rating whose reaction has been taken back goes.
        gone = select(TicketReaction.id).where(TicketReaction.id == TicketFeedback.reaction_id, TicketReaction.removed_at.is_(None))
        session.execute(delete(TicketFeedback).where(
            TicketFeedback.ticket_id == ticket.id, TicketFeedback.from_reaction.is_(True), ~gone.exists(),
        ))
        return
    in_cycle = (TicketFeedback.ticket_id == ticket.id, TicketFeedback.created_at >= start)
    soft = list(session.exec(select(TicketFeedback).where(*in_cycle, TicketFeedback.from_reaction.is_(True)).order_by(TicketFeedback.id)).all())
    answered = session.exec(select(TicketFeedback.id).where(*in_cycle, TicketFeedback.from_reaction.is_(False)).limit(1)).first()
    latest = None
    if answered is None:
        latest = session.exec(
            select(TicketReaction).where(
                TicketReaction.ticket_id == ticket.id, TicketReaction.by_party.is_(True), TicketReaction.removed_at.is_(None),
                TicketReaction.message_kind == RESOLUTION, TicketReaction.sentiment.in_((POSITIVE, NEGATIVE)),
                TicketReaction.reacted_at >= start, _of_this_round(start),
            ).order_by(TicketReaction.reacted_at.desc(), TicketReaction.id.desc()).limit(1)
        ).first()
    if latest is None:
        for row in soft:  # a real answer exists, or no reaction stands any more
            session.delete(row)
        return
    rating = RATING_POSITIVE if latest.sentiment == POSITIVE else RATING_NEGATIVE
    if soft:
        keep, extra = soft[0], soft[1:]
        keep.rating, keep.reaction_id = rating, latest.id
        session.add(keep)
        for row in extra:
            session.delete(row)
        return
    session.add(TicketFeedback(
        ticket_id=ticket.id, token_id=None, rating=rating, comment="", resolved_by_id=ticket.resolved_by_id, from_reaction=True,
        reaction_id=latest.id,
    ))


def drop_soft_feedback(session: Session, ticket: Ticket) -> None:
    """A real feedback answer replaces the quick rating a reaction made for this resolution: the soft rows of this round go (the
    real answer is added by the caller, and `_refresh_feedback` never adds a soft one while a real one exists). An earlier
    round's quick rating stays as history."""
    conditions = [TicketFeedback.ticket_id == ticket.id, TicketFeedback.from_reaction.is_(True)]
    if ticket.resolved_at is not None:
        conditions.append(TicketFeedback.created_at >= ticket.resolved_at)
    session.execute(delete(TicketFeedback).where(*conditions))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _standing_positive():
    return (
        TicketReaction.by_party.is_(True), TicketReaction.removed_at.is_(None), TicketReaction.sentiment == POSITIVE,
        TicketReaction.message_kind.in_((RESOLUTION, AWAITING_USER)),
    )


def acknowledged_since(session: Session, ticket_id: int, since: datetime) -> bool:
    """Whether the requester (or a follower) has acknowledged since `since` with a positive reaction that still stands, on a
    resolution message or on a message asking for a reply."""
    return session.exec(
        select(TicketReaction.id).where(
            TicketReaction.ticket_id == ticket_id, *_standing_positive(), TicketReaction.reacted_at >= since, _of_this_round(since),
        ).limit(1)
    ).first() is not None


def _latest_wait_start():
    """SQL: the latest status event of this ticket to Awaiting user (None when there is none)."""
    from app.ticket_service import EVENT_STATUS_CHANGED
    return (
        select(func.max(TicketEvent.created_at)).where(
            TicketEvent.ticket_id == Ticket.id, TicketEvent.event_type == EVENT_STATUS_CHANGED,
            TicketEvent.to_value == TicketStatus.AWAITING_USER.value,
        ).scalar_subquery()
    )


def _acknowledging_this_wait():
    """SQL: a standing positive reaction by the requester (or a follower) on this ticket since its current Awaiting user wait
    began (the same start as `waiting_since`)."""
    since = func.coalesce(_latest_wait_start(), Ticket.updated_at)
    return (
        TicketReaction.ticket_id == Ticket.id, *_standing_positive(), TicketReaction.reacted_at >= since,
        or_(TicketReaction.message_sent_at.is_(None), TicketReaction.message_sent_at >= since),
    )


def acknowledged_condition():
    """SQL conditions for a query on Ticket: Awaiting user, and the current wait is acknowledged by a reaction (the queue's filter)."""
    return (Ticket.status == TicketStatus.AWAITING_USER, select(TicketReaction.id).where(*_acknowledging_this_wait()).exists())


def acknowledged_times(session: Session, ticket_ids: Iterable[int]) -> dict[int, datetime]:
    """The tickets among these whose current Awaiting user wait is acknowledged, each with when (the earliest standing
    acknowledging reaction of this wait). THE one answer to "is this wait acknowledged, and since when": the chase job, the
    ticket page, the queue and its filter all read it. A reply, a status change (a new wait starts) or a reaction taken back
    ends it with no cleanup, because it is read from the live rows."""
    ids = list(ticket_ids)
    if not ids:
        return {}
    rows = session.exec(
        select(Ticket.id, func.min(TicketReaction.reacted_at)).where(
            Ticket.id.in_(ids), Ticket.status == TicketStatus.AWAITING_USER, *_acknowledging_this_wait(),
        ).group_by(Ticket.id)
    ).all()
    return {ticket_id: _aware(at) for ticket_id, at in rows}


def acknowledged_at(session: Session, ticket: Ticket) -> Optional[datetime]:
    """When the ticket's current wait was acknowledged by a reaction, or None (also when it is not Awaiting user)."""
    return acknowledged_times(session, [ticket.id]).get(ticket.id)


def waiting_since(session: Session, ticket: Ticket) -> datetime:
    """When the ticket went into Awaiting user this time: its latest status event to that status (a ticket that left and came
    back starts again)."""
    moment = session.exec(select(_latest_wait_start()).where(Ticket.id == ticket.id)).first()
    return _aware(moment or ticket.updated_at)


def backstop_days(session: Session) -> int:
    """`acknowledged_wait_resolve_days`: 0 is never."""
    return count_setting(session, ACKNOWLEDGED_WAIT_RESOLVE_DAYS)


def backstop_due(since: datetime, now: datetime, days: int) -> bool:
    """Whether an acknowledged wait has had no reply for `days` whole days since the reaction (never when `days` is 0)."""
    return days > 0 and now >= _aware(since) + timedelta(days=days)


@dataclass(frozen=True)
class AcknowledgedView:
    """What the pages say about an acknowledged wait: the chip text and (on the ticket page) the line about what happens next."""
    since: datetime
    chip: str
    line: str


def _days(count: int) -> str:
    return "today" if count == 0 else (f"{count} day" if count == 1 else f"{count} days")


def acknowledged_views(session: Session, tickets: Iterable[Ticket], now: datetime) -> dict[int, AcknowledgedView]:
    """The chip for each of these tickets whose wait is acknowledged (tickets that are not are missing from the result)."""
    times = acknowledged_times(session, [t.id for t in tickets])
    if not times:
        return {}
    limit = backstop_days(session)
    out = {}
    for ticket_id, since in times.items():
        since = min(since, now)  # never in the future
        elapsed = (now - since).days
        if limit == 0:
            line = "Chases stopped. Will not auto-resolve."
        elif backstop_due(since, now, limit):
            line = "Chases stopped. Auto-resolves at the next run if there is no reply."
        else:
            left = -((now - since - timedelta(days=limit)) // timedelta(days=1))  # whole days left, rounded up
            line = f"Chases stopped. Auto-resolves in {_days(left)} if there is no reply."
        out[ticket_id] = AcknowledgedView(since=since, chip=f"Acknowledged by reaction · {_days(elapsed)}", line=line)
    return out


def _open_negative():
    return (
        TicketReaction.sentiment == NEGATIVE, TicketReaction.by_party.is_(True), TicketReaction.removed_at.is_(None),
        TicketReaction.dismissed_at.is_(None),
    )


def open_negative_ids(session: Session, ticket_ids: Iterable[int]) -> set[int]:
    """Which of these tickets show the red Negative reaction chip (the queue asks for its page in one query)."""
    ids = list(ticket_ids)
    if not ids:
        return set()
    return set(session.exec(select(TicketReaction.ticket_id).where(TicketReaction.ticket_id.in_(ids), *_open_negative()).distinct()).all())


def dismiss_negative(session: Session, ticket: Ticket, agent: User) -> int:
    """An agent dismisses the chip: every negative reaction showing it is marked, with who and when. Returns how many. A
    later negative reaction shows the chip again."""
    rows = session.exec(select(TicketReaction).where(TicketReaction.ticket_id == ticket.id, *_open_negative())).all()
    for row in rows:
        row.dismissed_at, row.dismissed_by_id = _now(), agent.id
        session.add(row)
    if rows:
        log_action(session, agent.email, "ticket", ticket.id, AUDIT_DISMISSED, f"{len(rows)} negative reaction(s) dismissed")
    return len(rows)


def reactions_for_ticket(session: Session, ticket: Ticket) -> list[TicketReaction]:
    """Every reaction on the ticket, oldest first, removed ones included."""
    return list(session.exec(select(TicketReaction).where(TicketReaction.ticket_id == ticket.id).order_by(TicketReaction.reacted_at, TicketReaction.id)).all())


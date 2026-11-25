"""Reactions in Teams (issue #298): what the `messageReaction` activity does.

Teams pushes it to the bot when a person adds or takes back a reaction to a message in their chat with the bot. It carries
`replyToId` (the message reacted to: its id only, never its text), `reactionsAdded` and `reactionsRemoved` (each entry has a
`type`, and perhaps more, which is kept as received) and `from` (the person: Entra object id, Teams id and name). The id is
looked up among the messages the bot sent about a ticket (`BotMessage`, app/reactions.py); a reaction to anything else is
ignored. The person is matched by object id (app/identity.py `person_for_object_id`), never by name or address.

Everything a reaction does is decided in app/reactions.py (shared with the email reactions). This module only reads the
activity and turns it into `record_reaction` and `remove_reaction` calls. Only the personal chat is served.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app import reactions
from app.identity import normal_oid, person_for_object_id
from app.models import Message, Ticket, TeamsConversation, User
from app.teams_bot import _is_personal, _person, _sub, _text

log = logging.getLogger("app.teams_bot")

MAX_PER_ACTIVITY = 20  # an activity does not carry more than a handful; more than this is not Teams


def _when(activity: dict) -> datetime:
    """When it happened, from the activity's timestamp when it is a usable one, else now."""
    raw = _text(activity.get("timestamp"))
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return min(moment, datetime.now(timezone.utc))  # never later than now: a clock that runs fast must not hide a reaction


def _entries(activity: dict, key: str) -> list[dict]:
    value = activity.get(key)
    return [item for item in value[:MAX_PER_ACTIVITY] if isinstance(item, dict)] if isinstance(value, list) else []


def _emoji(entry: dict) -> Optional[str]:
    """Any emoji or unicode the platform sent with the type, as received."""
    for key in ("emoji", "unicode", "text"):
        if isinstance(entry.get(key), str) and entry[key].strip():
            return entry[key]
    return None


def _log(entry: dict, what: str) -> None:
    """The real `type` strings Teams sends are a staging check (docs/build-plan.md), so the type is logged at debug level, with the
    names of any other fields it carries; nothing else the platform sent (a person's text or emoji) is written to the log."""
    log.debug("Teams reaction %s: type=%r other fields=%s", what, _text(entry.get("type"))[:64], sorted(k for k in entry if k != "type")[:8])


def reactor_of(session: Session, activity: dict) -> Optional[reactions.Reactor]:
    """Who reacted: the object id Teams sent, the name it gave and the account the id matches. A person with no account here
    yet but a stored chat that was matched to one (`TeamsConversation.user_id`) is that account."""
    oid = normal_oid(_person(activity)[0])
    if oid is None:
        return None
    user = person_for_object_id(session, oid)
    if user is None:
        row = session.exec(select(TeamsConversation).where(TeamsConversation.entra_object_id == oid)).first()
        user = session.get(User, row.user_id) if row is not None and row.user_id else None
    return reactions.Reactor(ref=oid, name=_text(_sub(activity, "from", "name")) or None, user=user)


def handle(session: Session, activity: dict) -> None:
    """Records the reactions added and marks the ones removed. Commits its own work."""
    if not _is_personal(activity):
        return
    activity_id = _text(activity.get("replyToId")).strip()
    conversation_id = _text(_sub(activity, "conversation", "id"))
    reactor = reactor_of(session, activity)
    if not activity_id or not conversation_id or reactor is None:
        return
    own_chat = session.exec(select(TeamsConversation.conversation_id).where(TeamsConversation.entra_object_id == reactor.ref)).first()
    sent = reactions.find_bot_message(session, activity_id, conversation_id, own_chat)
    if sent is None:
        log.debug("Teams reaction to a message we do not know (id %s): ignored", activity_id[:40])
        return
    ticket = session.get(Ticket, sent.ticket_id)
    if ticket is None:
        return
    at = _when(activity)
    message_id = sent.message_id if sent.message_id and session.get(Message, sent.message_id) is not None else None
    for entry in _entries(activity, "reactionsAdded"):
        _log(entry, "added")
        reactions.record_reaction(
            session, ticket, channel=reactions.TEAMS, message_kind=sent.kind, message_ref=sent.activity_id, raw_type=_text(entry.get("type")),
            reactor=reactor, emoji=_emoji(entry), message_id=message_id, message_sent_at=sent.created_at, at=at,
        )
    for entry in _entries(activity, "reactionsRemoved"):
        _log(entry, "removed")
        reactions.remove_reaction(
            session, ticket, channel=reactions.TEAMS, message_ref=sent.activity_id, reactor_ref=reactor.ref, raw_type=_text(entry.get("type")), at=at,
        )
    session.commit()

"""Which channel an agent's reply (or an automatic notice) to the requester travels by (phase 8F, #193).

One rule, used by every public message to the requester (`messages.email_requester`, `messages.record_notice`):

- Each ticket remembers the channel its requester last wrote in: email, portal or teams (`Ticket.last_requester_channel`).
  Until they write again it is the channel the ticket arrived by (its source; an agent-raised ticket is reached by email).
  A Teams message on an email ticket switches it to Teams, and a later email reply switches it back. Only the requester
  moves it: a follower's reply never does. `note_requester_wrote` is the one place that sets it.
- A reply goes by email unless the channel is Teams. For Teams it goes to the requester's chat with the bot, and also by
  email when they chose "Teams and email" in My settings (`User.teams_replies_also_email`), or when the message copies
  followers (the copy cannot be sent without the requester on it, and followers' email updates are not changed).
- When the bot cannot reach the requester (it is off, or they have no chat) the reply goes by email. When the Teams message
  is sent later and the bot gives up on it (the app was removed, Teams refuses it, or the retries ran out) the email is
  queued then (`email_instead`), so a reply is never lost.

The email itself, its checks and its sending are unchanged (app/outbox.py, app/mail.py); so is the Teams message
(`teams.requester_message_post`). This module only decides which of the two to queue.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, func, select

from app.identity import person_for_address
from app.models import Message, OutboundEmail, TeamsPost, Ticket, TicketSource
from app.outbox import queue_message

EMAIL = "email"
PORTAL = "portal"
TEAMS_CHANNEL = "teams"
CHANNELS = (EMAIL, PORTAL, TEAMS_CHANNEL)

SOURCE_CHANNEL = {
    TicketSource.EMAIL: EMAIL,
    TicketSource.PORTAL: PORTAL,
    TicketSource.TEAMS: TEAMS_CHANNEL,
    TicketSource.AGENT: EMAIL,
}


def requester_channel(ticket: Ticket) -> str:
    """The channel the requester last wrote in, or the one the ticket arrived by."""
    if ticket.last_requester_channel in CHANNELS:
        return ticket.last_requester_channel
    return SOURCE_CHANNEL.get(ticket.source, EMAIL)


def is_the_requester(session: Session, ticket: Ticket, address: str) -> bool:
    """Whether this address is the ticket's requester (their address, or another address of the same person)."""
    address = (address or "").strip().lower()
    if not address:
        return False
    if address == (ticket.requester_email or "").strip().lower():
        return True
    sender, requester = person_for_address(session, address), person_for_address(session, ticket.requester_email)
    return sender is not None and requester is not None and sender.id == requester.id


def note_requester_wrote(session: Session, ticket: Ticket, address: str, channel: str) -> bool:
    """Remembers that the requester wrote in this channel. Nothing happens for anyone else (a follower). Returns whether
    the channel changed. Does not commit."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel}")
    if not is_the_requester(session, ticket, address):
        return False
    changed = requester_channel(ticket) != channel
    if ticket.last_requester_channel != channel:
        ticket.last_requester_channel = channel
        session.add(ticket)
    return changed


@dataclass(frozen=True)
class ReplyRoute:
    email: bool
    teams: bool


def reply_route(session: Session, ticket: Ticket, message: Message) -> ReplyRoute:
    """Where a public message to the requester goes now. Teams only when the channel is Teams and the bot can reach them;
    email in every other case, so nothing is lost."""
    from app.teams import can_message_requester  # here: teams.py imports this module for the fallback

    if requester_channel(ticket) != TEAMS_CHANNEL or not can_message_requester(session, ticket):
        return ReplyRoute(email=True, teams=False)
    if message.author_user_id is None and _notices_to_teams_today(session, ticket) >= _notice_cap(session):
        return ReplyRoute(email=True, teams=False)  # the same cap as automatic email: past it, the email's own cap holds it back
    person = person_for_address(session, ticket.requester_email)
    return ReplyRoute(email=bool(message.cc) or bool(person and person.teams_replies_also_email), teams=True)


def _notice_cap(session: Session) -> int:
    from app.mail import automatic_cap  # here: mail.py imports messages.py, which imports this module

    return automatic_cap(session)


def _notices_to_teams_today(session: Session, ticket: Ticket) -> int:
    """Automatic notices (no author) already queued for this requester's chat on this ticket in the last 24 hours: what
    the per-ticket cap on automatic messages (app/mail.py) counts for email, so a loop of notices cannot flood the chat."""
    return session.exec(
        select(func.count()).select_from(TeamsPost).join(Message, Message.id == TeamsPost.message_id).where(
            TeamsPost.kind == "requester_message", TeamsPost.ticket_id == ticket.id, Message.author_user_id.is_(None),
            TeamsPost.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
        )
    ).one()


def queue_reply(session: Session, message: Message, ticket: Ticket) -> ReplyRoute:
    """Queues the public message for the requester by the route above. Does not commit."""
    from app.teams import queue_requester_message

    route = reply_route(session, ticket, message)
    if route.teams:
        queue_requester_message(session, message, ticket)
    if route.email:
        queue_message(session, message, ticket)
    return route


def email_instead(session: Session, message_id: Optional[int]) -> Optional[OutboundEmail]:
    """The Teams copy of a message to the requester was given up on: queue its email, unless one is queued already (the
    person chose "Teams and email", or followers were copied). Mail being off queues nothing, as for any message. Does not
    commit."""
    message = session.get(Message, message_id) if message_id else None
    ticket = session.get(Ticket, message.ticket_id) if message is not None else None
    if message is None or ticket is None:
        return None
    from app.messages import requester_visible_messages  # here: messages.py imports this module

    if message.id not in {m.id for m in requester_visible_messages(session, ticket)}:
        return None  # an internal note is never emailed (mail.build_message_mail would refuse it as well)
    if session.exec(select(OutboundEmail.id).where(OutboundEmail.message_id == message.id).limit(1)).first() is not None:
        return None
    return queue_message(session, message, ticket)

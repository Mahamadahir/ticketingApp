"""Teams channel posts (phase 2D): a card in the support channel for each new ticket.

Announcing is two steps so a Teams outage can never lose a ticket or slow the request that made it.
create_ticket() calls queue_new_ticket(), which writes a TeamsPost row in the ticket's own
transaction. The worker later calls deliver_pending(), which sends each unsent row to the Teams
Workflows webhook and marks it sent. Delivery is at least once: if the process dies between the
POST and the commit, the card is sent again.

The webhook URL is a credential (anyone holding it can post to the channel), so it is read from the
environment only (settings.teams_new_ticket_webhook_url) and never stored, logged or put in an
error message. Only the worker needs it. The processes that create tickets need just the switch
(settings.teams_new_ticket_enabled): off, nothing is queued, so a deployment without Teams does not
collect a backlog. On with no URL, posts wait in the table until the worker has one.

The sender is passed in (post_card is the real one) so tests never touch the network.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from sqlalchemy import or_
from sqlmodel import Session, select

from app.config import settings
from app.models import Message, Notification, TeamsPost, Ticket, TicketSource, User
from app.notifications import PORTAL_KINDS, can_be_told_about, notification_text

log = logging.getLogger("app.teams")

NEW_TICKET = "new_ticket"
REQUESTER_MESSAGE = "requester_message"  # a public message on a ticket raised in Teams, passed to the requester's chat with the bot (8D)
NOTIFICATION = "notification"  # a person's unread notification: a direct message from the bot, else a post that @mentions them
DIRECT = "direct"
CHANNEL = "channel"
# A failed post waits 2, 4, 8, 16, 32, 60, 60 minutes before its next try, so a Teams outage of a few
# hours is ridden out; after that it is left alone with its reason.
MAX_ATTEMPTS = 8
MAX_BACKOFF_MINUTES = 60
BATCH = 20
WEBHOOK_MISSING_RETRY_MINUTES = 5  # a channel post in a process that has no webhook URL is looked at again after this
TIMEOUT_SECONDS = 10
# A 4xx other than these means the request itself is refused (a revoked or wrong webhook), which
# retrying cannot fix.
RETRYABLE_CLIENT_ERRORS = (408, 429)
APP_REMOVED_STATUSES = (403, 404)  # a message into a chat, refused because the app was removed

SOURCE_LABELS = {
    TicketSource.EMAIL: "Email",
    TicketSource.PORTAL: "Portal",
    TicketSource.TEAMS: "Teams",
    TicketSource.AGENT: "Raised by an agent",
}


def queue_new_ticket(session: Session, ticket: Ticket) -> Optional[TeamsPost]:
    """Queues the announcement of a new ticket, if announcing is switched on. Does not commit."""
    if not settings.teams_new_ticket_enabled:
        return None
    post = TeamsPost(kind=NEW_TICKET, ticket_id=ticket.id)
    session.add(post)
    return post


def can_message_requester(session: Session, ticket: Ticket) -> bool:
    """Whether the bot can reach the ticket's requester in their chat right now: the bot is on and they have a person
    record with a conversation. The one question app/reply_channel.py asks before it chooses Teams for a reply."""
    if not settings.teams_bot_configured:
        return False
    from app.identity import person_for_address  # imported here: identity needs the models only, but keep this module light
    from app.teams_conversations import reference_for

    person = person_for_address(session, ticket.requester_email)
    return person is not None and reference_for(session, person) is not None


def queue_requester_message(session: Session, message: Message, ticket: Ticket) -> Optional[TeamsPost]:
    """Queues a message for the requester (an agent's "Email user", an automatic notice) to go to them in their chat with
    the bot. Only when `can_message_requester` (a person with none is not left a failed row); app/reply_channel.py decides
    whether the reply goes to Teams at all. Which messages may go is decided again when it is sent
    (`requester_message_post`). One post per message, enforced by the database. Does not commit."""
    if not can_message_requester(session, ticket):
        return None
    session.flush()  # the message needs its id
    post = TeamsPost(kind=REQUESTER_MESSAGE, ticket_id=ticket.id, message_id=message.id)
    session.add(post)
    return post


MARKDOWN_CHARACTERS = set("\\`*_{}[]()<>#+-.!|~")
MAX_CARD_TEXT = 200


def plain(text: str) -> str:
    """Text from a requester made safe for a card: Teams renders markdown in a TextBlock, so a
    subject like [Reset your password](https://evil.example) would arrive as a clickable link in
    the support channel. Every markdown character is escaped so it shows as typed, and the length
    is capped."""
    text = " ".join(text.split())[:MAX_CARD_TEXT]
    return "".join("\\" + c if c in MARKDOWN_CHARACTERS else c for c in text)


def ticket_link(ticket: Ticket) -> Optional[str]:
    """Where the "Open ticket" button of a card goes, or None while the web app's address is not known."""
    if not settings.ticket_link_base:
        return None
    return f"{settings.ticket_link_base.rstrip('/')}/tickets/{ticket.number}"


def portal_link(ticket: Ticket) -> Optional[str]:
    """Where a card for someone who is not an agent (an approver, a requester) sends them: the request's page in the portal."""
    if not settings.ticket_link_base:
        return None
    return f"{settings.ticket_link_base.rstrip('/')}/portal/{ticket.number}"


# Who a card is for. Agents are sent to the console, everyone else to the portal (8F).
AUDIENCE_AGENT = "agent"
AUDIENCE_REQUESTER = "requester"


def link_for(ticket: Ticket, audience: str) -> Optional[str]:
    """The one place that picks the link a card carries for its audience."""
    return ticket_link(ticket) if audience == AUDIENCE_AGENT else portal_link(ticket)


def message(card: dict) -> dict:
    """The Teams Workflows payload: a message carrying one Adaptive Card."""
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


def new_ticket_card(ticket: Ticket) -> dict:
    """The Teams Workflows payload: a message carrying one Adaptive Card. The text is the ticket's
    reference (INC-1042), subject, how it arrived and who it is for."""
    body = [
        {"type": "TextBlock", "text": f"New ticket {ticket.reference}", "weight": "Bolder", "size": "Medium"},
        {"type": "TextBlock", "text": plain(ticket.subject), "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "From", "value": plain(ticket.requester_email)},
                {"title": "Via", "value": SOURCE_LABELS.get(ticket.source, ticket.source.value)},
            ],
        },
    ]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    link = ticket_link(ticket)
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}]
    return message(card)


def post_card(payload: dict) -> None:
    """Sends one payload to the webhook. Raises on any failure; the exception text can contain the
    URL, so callers record describe_failure() and never str() of it."""
    response = httpx.post(settings.teams_new_ticket_webhook_url, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()


def is_permanent(error: Exception) -> bool:
    """Whether trying again cannot help: Teams refused the request itself (a 4xx other than a timeout
    or a rate limit)."""
    return (
        isinstance(error, httpx.HTTPStatusError)
        and 400 <= error.response.status_code < 500
        and error.response.status_code not in RETRYABLE_CLIENT_ERRORS
    )


def describe_failure(error: Exception) -> str:
    """Why a send failed, without the webhook URL (httpx puts it in its messages)."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"Teams answered {error.response.status_code}"
    return type(error).__name__


class Unsendable(Exception):
    """Raised by a card builder when its post can never be sent (what it is about is gone). The
    message is the reason kept on the row."""


def new_ticket_post(session: Session, post: TeamsPost, bot_refused: bool = False) -> dict:
    ticket = session.get(Ticket, post.ticket_id) if post.ticket_id else None
    if ticket is None:
        raise Unsendable("Ticket not found")
    return new_ticket_card(ticket)


def queue_notification(session: Session, notification: Notification) -> TeamsPost:
    """Queues the Teams copy of a notification (app.notifications.hand_to_teams calls this for each one
    that is due). Written in the worker's transaction. One post per notification, enforced by the
    database."""
    post = TeamsPost(kind=NOTIFICATION, notification_id=notification.id, ticket_id=notification.ticket_id)
    session.add(post)
    return post


KIND_LABELS = {"assigned": "Assigned to you", "mentioned": "You were mentioned", "replied": "Ticket updated"}


def mention_name(user: User) -> str:
    """A name safe to put inside <at>...</at>: no markup or markdown, so it cannot close the tag or
    bold the line, and it matches the mention entity exactly."""
    name = re.sub(r"[^\w .'-]|_", "", user.display_name or "")  # underscores would be read as italics
    name = " ".join(name.split())[:80]
    return name or user.email.partition("@")[0]


def notification_card(notification: Notification, person: User, ticket: Optional[Ticket]) -> dict:
    """The Teams payload for a notification: a card that @mentions the person (by email, which is what
    Teams knows them by) with the notification's text and a link to the ticket. The text can hold a
    requester's words (a ticket subject), so it is made plain like the new-ticket card's."""
    name = mention_name(person)
    at = f"<at>{name}</at>"
    body = [
        {"type": "TextBlock", "text": f"{at} {plain(notification_text(notification, ticket))}", "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "What", "value": KIND_LABELS.get(notification.kind, plain(notification.kind))},
                *([{"title": "By", "value": plain(notification.actor_email)}] if notification.actor_email else []),
            ],
        },
    ]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "msteams": {
            "entities": [
                {"type": "mention", "text": at, "mentioned": {"id": person.email, "name": name}},
            ]
        },
    }
    link = ticket_link(ticket) if ticket is not None else None
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}]
    return message(card)


@dataclass(frozen=True)
class BotDelivery:
    """What a builder returns when the post goes to a person as a direct message from the bot, rather than to the
    channel webhook: where the conversation is, and the Adaptive Card itself (not the Workflows payload) or, for a
    message written by a person, plain text."""

    service_url: str
    conversation_id: str
    card: Optional[dict] = None
    text: Optional[str] = None  # a plain text message instead of a card (an agent's reply to a requester, 8D)


def notifications_on() -> bool:
    """Whether Teams copies of notifications are sent at all: to the channel (the switch) or as direct messages
    (the bot). The worker queues copies only while this is true; with it false, due copies are dropped."""
    return settings.teams_notifications_enabled or settings.teams_bot_configured


def route_for(session: Session, person: User, direct_only: bool = False, bot_refused: bool = False):
    """The one place that decides how a person's Teams copy travels, worked out when it is sent (so a person who
    installs the app after the copy was queued still gets it in the chat): a direct message from the bot when the bot
    is on and the person has a conversation with it, else the channel @mention when that is switched on, else None
    (the copy is dropped). `direct_only` is for a copy that must never go to a shared channel (a request waiting for
    someone's vote: it names the request and carries the buttons): the channel is then not an option. `bot_refused` is
    for a copy whose direct message Teams has just refused with 403 or 404 (the app was removed): the bot is then not an
    option for this copy, so it goes to the channel if that is on, else nowhere. Returns (DIRECT, conversation row),
    (CHANNEL, None) or (None, None)."""
    if settings.teams_bot_configured and not bot_refused:
        from app.teams_conversations import reference_for  # imported here: it needs bot_connector, which needs this module

        conversation = reference_for(session, person)
        if conversation is not None:
            return DIRECT, conversation
    if settings.teams_notifications_enabled and not direct_only:
        return CHANNEL, None
    return None, None


def has_chat_with_bot(session: Session, person: User) -> bool:
    """Whether the person would be messaged by the bot right now (the bot is on and they have a conversation with it).
    What the approval reminders ask: they go to Teams only as a direct message, not to the channel."""
    return route_for(session, person)[0] == DIRECT


def can_deliver(session: Session, person: User, direct_only: bool = False) -> bool:
    """Whether a Teams copy for this person has anywhere to go right now (`direct_only`: in their chat with the bot). Asked when the copy is handed over, so a
    person with no conversation (and no channel fallback) is dropped there, counted with the other drops, rather
    than leaving a failed row in the outbox for every notification."""
    return route_for(session, person, direct_only)[0] is not None


def notification_post(session: Session, post: TeamsPost, bot_refused: bool = False):
    if not notifications_on():
        # Switched off after this was queued: the switch is checked again at send time, not only when queuing.
        raise Unsendable("Sending notifications to Teams is switched off")
    notification = session.get(Notification, post.notification_id) if post.notification_id else None
    if notification is None:
        raise Unsendable("Notification not found")
    person = session.get(User, notification.user_id)
    if person is None:
        raise Unsendable("Person not found")
    if not person.is_active:
        # Inactive and anonymised people (an anonymised account is made inactive) are never messaged.
        raise Unsendable("The person is no longer active")
    ticket = session.get(Ticket, notification.ticket_id) if notification.ticket_id else None
    if ticket is not None and not can_be_told_about(session, person, notification, ticket):
        # Checked again at send time: the ticket may have moved team since the copy was queued, or the person
        # may no longer be asked to vote on it.
        raise Unsendable("The person can no longer open the ticket")
    route, conversation = route_for(session, person, direct_only=notification.kind in PORTAL_KINDS, bot_refused=bot_refused)
    if route == DIRECT:
        from app.bot_cards import notification_card as direct_card  # the bot's cards live in one module (8C extends it)

        return BotDelivery(conversation.service_url, conversation.conversation_id, direct_card(session, notification, ticket))
    if route == CHANNEL:
        return notification_card(notification, person, ticket)
    raise Unsendable("No Teams conversation with the bot, and notifications to the channel are switched off")


def requester_message_post(session: Session, post: TeamsPost, bot_refused: bool = False) -> "BotDelivery":
    """The text of a message for the requester, under the ticket's reference so they can tell which request it is about.
    The same rules as the email (app/mail.py build_message_mail): the ticket is triaged and the message is one the
    requester may see (`requester_visible_messages`, so an internal note can never go); and the person is a real, active
    one with a chat. Only their own chat: never the channel."""
    from app.action_links import put_links_in
    from app.identity import person_for_address
    from app.messages import is_triaged, mail_subject, requester_visible_messages
    from app.person_data import is_placeholder_address
    from app.teams_conversations import reference_for

    if not settings.teams_bot_configured:
        raise Unsendable("The Teams bot is switched off")
    if bot_refused:
        raise Unsendable("Teams refused the message (the app was removed)")  # only their own chat will do; the email goes instead
    message = session.get(Message, post.message_id) if post.message_id else None
    ticket = session.get(Ticket, post.ticket_id) if post.ticket_id else None
    if message is None or ticket is None or message.ticket_id != ticket.id:
        raise Unsendable("Message not found")
    if not is_triaged(ticket):
        raise Unsendable("The ticket has not been triaged yet")
    if message.id not in {m.id for m in requester_visible_messages(session, ticket)}:
        raise Unsendable("The requester may not see this message")
    if is_placeholder_address(ticket.requester_email):
        raise Unsendable("The person was anonymised")
    person = person_for_address(session, ticket.requester_email)
    if person is None or not person.is_active:
        raise Unsendable("The person is not active")
    conversation = reference_for(session, person)
    if conversation is None:
        raise Unsendable("No conversation with the bot")
    body = put_links_in(session, ticket, message.body)
    link = link_for(ticket, AUDIENCE_REQUESTER)
    footer = f"\n\nOpen in the portal: {link}" if link else ""
    return BotDelivery(conversation.service_url, conversation.conversation_id, text=f"{mail_subject(session, ticket)}\n\n{body}{footer}")


# One builder per kind of post: it turns the row into the payload to send, or raises Unsendable.
# A new kind of post is one builder and one entry here; sending, backoff and giving up are shared.
CARD_BUILDERS: dict[str, Callable[..., "dict | BotDelivery"]] = {  # (session, post, bot_refused=False)
    NEW_TICKET: new_ticket_post,
    NOTIFICATION: notification_post,
    REQUESTER_MESSAGE: requester_message_post,
}


def backoff_minutes(attempts: int) -> int:
    """How long a post that has failed `attempts` times waits: 2, 4, 8, 16, 32, then 60 minutes."""
    return min(2**attempts, MAX_BACKOFF_MINUTES)


def record_failure(post: TeamsPost, error: Exception, now: datetime) -> None:
    """Notes a failed send on the row: one more attempt, the reason (never a URL), and when to try next, or
    nothing more to try when the request itself was refused. Every kind of post, webhook or bot, fails this way."""
    post.attempts += 1
    post.last_error = describe_failure(error)
    if is_permanent(error):
        post.attempts = MAX_ATTEMPTS
    else:
        post.next_attempt_at = now + timedelta(minutes=backoff_minutes(post.attempts))


def give_up(post: TeamsPost, reason: str) -> None:
    post.attempts = MAX_ATTEMPTS
    post.last_error = reason


def send_bot(delivery: BotDelivery) -> None:
    """Sends one direct message through the Bot Connector (app/bot_connector.py). Raises on any failure; the outbox
    records it like a webhook failure (never the token or the address)."""
    from app.bot_connector import ConversationRef, send_card, send_text  # imported here: bot_connector imports this module

    ref = ConversationRef(delivery.service_url, delivery.conversation_id)
    if delivery.text is not None:
        send_text(ref, delivery.text)
    else:
        send_card(ref, delivery.card)


def deliver_pending(
    session: Session,
    send: Optional[Callable[[dict], None]] = None,
    limit: int = BATCH,
    now: Optional[datetime] = None,
    bot_send: Optional[Callable[[BotDelivery], None]] = None,
) -> tuple[int, int]:
    """Sends the oldest unsent posts that are due, up to `limit`: a webhook payload through `send`, a direct message
    from the bot through `bot_send`. A post that fails waits longer each time (see MAX_ATTEMPTS) and is given up on
    at once if Teams refuses the request itself. The row keeps the reason. One failure never stops the rest. A post
    that needs the webhook waits, untried, in a process that has no URL. Returns (sent, failed). Does not commit;
    the worker commits after the job."""
    send = send or post_card
    bot_send = bot_send or send_bot
    now = now or datetime.now(timezone.utc)
    due = (
        TeamsPost.sent_at.is_(None),
        TeamsPost.attempts < MAX_ATTEMPTS,
        or_(TeamsPost.next_attempt_at.is_(None), TeamsPost.next_attempt_at <= now),
    )
    webhook = settings.teams_new_ticket_configured
    if not webhook:
        waiting = session.exec(select(TeamsPost.id).where(*due, TeamsPost.kind == NEW_TICKET).limit(1)).first()
        if waiting is None and not settings.teams_bot_configured:
            waiting = session.exec(select(TeamsPost.id).where(*due).limit(1)).first()
        if waiting is not None:
            # Announcing is switched on but this process cannot send: say so rather than let posts pile up.
            log.warning("Teams posts are waiting but TEAMS_NEW_TICKET_WEBHOOK_URL is not set in this process (it is the one webhook for every kind of post)")
        if not settings.teams_bot_configured:
            return 0, 0
        # Only the bot can send here. New-ticket cards always need the webhook, so they are left out rather than
        # filling the batch; a notification that would go to the channel is deferred below.
        due = (*due, TeamsPost.kind != NEW_TICKET)
    posts = session.exec(
        select(TeamsPost).where(*due).order_by(TeamsPost.id).limit(limit).with_for_update(skip_locked=True)
    ).all()
    sent = failed = 0
    for post in posts:
        builder = CARD_BUILDERS.get(post.kind)
        if builder is None:
            give_up(post, "Unknown kind")
            session.add(post)
            failed += 1
            continue
        try:
            payload = builder(session, post)
        except Unsendable as reason:
            give_up(post, str(reason))
            gave_up(session, post)
            session.add(post)
            failed += 1
            continue
        direct = isinstance(payload, BotDelivery)
        if not direct and not webhook:
            # Not an attempt (nothing was tried), but not looked at again at once either: it would fill every batch
            # ahead of the direct messages behind it.
            post.next_attempt_at = now + timedelta(minutes=WEBHOOK_MISSING_RETRY_MINUTES)
            session.add(post)
            log.warning("Teams post %s goes to the channel but TEAMS_NEW_TICKET_WEBHOOK_URL is not set in this process", post.id)
            continue
        try:
            error = _transmit(payload, send, bot_send)
            if error is not None and direct and is_app_removed(error):
                # The person removed the app (an uninstall Teams never told us about, or a refusal for another reason): route
                # this copy again in the one place (route_for) without the bot: the channel @mention if that is on,
                # otherwise the builder gives up. The stored chat is kept; the next copy tries the bot again.
                payload = builder(session, post, bot_refused=True)
                if not isinstance(payload, BotDelivery) and not webhook:
                    raise Unsendable("The app was removed, and the channel needs TEAMS_NEW_TICKET_WEBHOOK_URL")
                error = _transmit(payload, send, bot_send)
        except Unsendable as reason:
            give_up(post, f"The app was removed from the person's Teams: {reason}")
            gave_up(session, post)
            session.add(post)
            failed += 1
            continue
        if error is not None:
            record_failure(post, error, now)
            if post.attempts >= MAX_ATTEMPTS:
                gave_up(session, post)
            log.warning("Teams post %s failed (attempt %s): %s", post.id, post.attempts, post.last_error)
            failed += 1
        else:
            post.sent_at = datetime.now(timezone.utc)
            post.last_error = None
            sent += 1
        session.add(post)
    return sent, failed


def _transmit(payload: "dict | BotDelivery", send: Callable[[dict], None], bot_send: Callable[["BotDelivery"], None]) -> Optional[Exception]:
    """Sends one payload; the exception if it failed, else None."""
    try:
        if isinstance(payload, BotDelivery):
            bot_send(payload)
        else:
            send(payload)
    except Exception as error:
        return error
    return None


def is_app_removed(error: Exception) -> bool:
    """Teams answered 403 or 404 to a message into a chat: the person removed the app (a user, or an admin's policy; one
    installed by an admin's setup policy cannot be removed by the user)."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in APP_REMOVED_STATUSES


def gave_up(session: Session, post: TeamsPost) -> None:
    """A post will not be tried again. A message for the requester then goes by email, so a reply is never lost
    (app/reply_channel.py); every other kind is just left with its reason."""
    if post.kind == REQUESTER_MESSAGE:
        from app.reply_channel import email_instead

        email_instead(session, post.message_id)

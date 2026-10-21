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
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from sqlalchemy import or_
from sqlmodel import Session, select

from app.config import settings
from app.models import Notification, TeamsPost, Ticket, TicketSource, User
from app.visibility import Access, access_context, ticket_access

log = logging.getLogger("app.teams")

NEW_TICKET = "new_ticket"
NOTIFICATION = "notification"  # an agent's unread notification, sent as a post that @mentions them
# A failed post waits 2, 4, 8, 16, 32, 60, 60 minutes before its next try, so a Teams outage of a few
# hours is ridden out; after that it is left alone with its reason.
MAX_ATTEMPTS = 8
MAX_BACKOFF_MINUTES = 60
BATCH = 20
TIMEOUT_SECONDS = 10
# A 4xx other than these means the request itself is refused (a revoked or wrong webhook), which
# retrying cannot fix.
RETRYABLE_CLIENT_ERRORS = (408, 429)

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


MARKDOWN_CHARACTERS = set("\\`*_{}[]()<>#+-.!|~")
MAX_CARD_TEXT = 200


def plain(text: str) -> str:
    """Text from a requester made safe for a card: Teams renders markdown in a TextBlock, so a
    subject like [Reset your password](https://evil.example) would arrive as a clickable link in
    the support channel. Every markdown character is escaped so it shows as typed, and the length
    is capped."""
    text = " ".join(text.split())[:MAX_CARD_TEXT]
    return "".join("\\" + c if c in MARKDOWN_CHARACTERS else c for c in text)


def message(card: dict) -> dict:
    """The Teams Workflows payload: a message carrying one Adaptive Card."""
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


def new_ticket_card(ticket: Ticket) -> dict:
    """The Teams Workflows payload: a message carrying one Adaptive Card. The text is the ticket's
    number (#1042, which never changes), subject, how it arrived and who it is for."""
    body = [
        {"type": "TextBlock", "text": f"New ticket {ticket.tag}", "weight": "Bolder", "size": "Medium"},
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
    if settings.ticket_link_base:
        link = f"{settings.ticket_link_base.rstrip('/')}/tickets/{ticket.number}"
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


def new_ticket_post(session: Session, post: TeamsPost) -> dict:
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
        {"type": "TextBlock", "text": f"{at} {plain(notification.text)}", "wrap": True},
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
    if ticket is not None and settings.ticket_link_base:
        link = f"{settings.ticket_link_base.rstrip('/')}/tickets/{ticket.number}"
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}]
    return message(card)


def notification_post(session: Session, post: TeamsPost) -> dict:
    if not settings.teams_notifications_enabled:
        # Switched off after this was queued: the switch is checked again at send time, not only when queuing.
        raise Unsendable("Sending notifications to Teams is switched off")
    notification = session.get(Notification, post.notification_id) if post.notification_id else None
    if notification is None:
        raise Unsendable("Notification not found")
    person = session.get(User, notification.user_id)
    if person is None:
        raise Unsendable("Person not found")
    ticket = session.get(Ticket, notification.ticket_id) if notification.ticket_id else None
    if ticket is not None and ticket_access(person, ticket, access_context(session, person)) < Access.READ:
        # Checked again at send time: the ticket may have moved team since the copy was queued.
        raise Unsendable("The person can no longer open the ticket")
    return notification_card(notification, person, ticket)


# One builder per kind of post: it turns the row into the payload to send, or raises Unsendable.
# A new kind of post is one builder and one entry here; sending, backoff and giving up are shared.
CARD_BUILDERS: dict[str, Callable[[Session, TeamsPost], dict]] = {
    NEW_TICKET: new_ticket_post,
    NOTIFICATION: notification_post,
}


def give_up(post: TeamsPost, reason: str) -> None:
    post.attempts = MAX_ATTEMPTS
    post.last_error = reason


def deliver_pending(
    session: Session,
    send: Optional[Callable[[dict], None]] = None,
    limit: int = BATCH,
    now: Optional[datetime] = None,
) -> tuple[int, int]:
    """Sends the oldest unsent posts that are due, up to `limit`. A post that fails waits longer each
    time (see MAX_ATTEMPTS) and is given up on at once if Teams refuses the request itself. The row
    keeps the reason. One failure never stops the rest. Returns (sent, failed). Does not commit;
    the worker commits after the job."""
    send = send or post_card
    now = now or datetime.now(timezone.utc)
    due = (
        TeamsPost.sent_at.is_(None),
        TeamsPost.attempts < MAX_ATTEMPTS,
        or_(TeamsPost.next_attempt_at.is_(None), TeamsPost.next_attempt_at <= now),
    )
    if not settings.teams_new_ticket_configured:
        waiting = session.exec(select(TeamsPost.id).where(*due).limit(1)).first()
        if waiting is not None:
            # Announcing is switched on but this process cannot send: say so rather than let posts pile up.
            log.warning("Teams posts are waiting but TEAMS_NEW_TICKET_WEBHOOK_URL is not set in this process (it is the one webhook for every kind of post)")
        return 0, 0
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
            session.add(post)
            failed += 1
            continue
        try:
            send(payload)
        except Exception as error:
            post.attempts += 1
            post.last_error = describe_failure(error)
            if is_permanent(error):
                post.attempts = MAX_ATTEMPTS
            else:
                post.next_attempt_at = now + timedelta(minutes=min(2**post.attempts, MAX_BACKOFF_MINUTES))
            log.warning("Teams post %s failed (attempt %s): %s", post.id, post.attempts, post.last_error)
            failed += 1
        else:
            post.sent_at = datetime.now(timezone.utc)
            post.last_error = None
            sent += 1
        session.add(post)
    return sent, failed

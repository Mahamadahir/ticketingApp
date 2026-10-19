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

from datetime import datetime, timezone
from typing import Callable, Optional

import httpx
from sqlmodel import Session, select

from app.config import settings
from app.models import TeamsPost, Ticket, TicketSource

NEW_TICKET = "new_ticket"
MAX_ATTEMPTS = 5
BATCH = 20
TIMEOUT_SECONDS = 10

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


def new_ticket_card(ticket: Ticket) -> dict:
    """The Teams Workflows payload: a message carrying one Adaptive Card. The text is the ticket's
    number (#1042, which never changes), subject, how it arrived and who it is for."""
    body = [
        {"type": "TextBlock", "text": f"New ticket {ticket.tag}", "weight": "Bolder", "size": "Medium"},
        {"type": "TextBlock", "text": ticket.subject, "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "From", "value": ticket.requester_email},
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
    if settings.public_base_url:
        link = f"{settings.public_base_url.rstrip('/')}/tickets/{ticket.number}"
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}]
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


def post_card(payload: dict) -> None:
    """Sends one payload to the webhook. Raises on any failure; the exception text can contain the
    URL, so callers record describe_failure() and never str() of it."""
    response = httpx.post(settings.teams_new_ticket_webhook_url, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()


def describe_failure(error: Exception) -> str:
    """Why a send failed, without the webhook URL (httpx puts it in its messages)."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"Teams answered {error.response.status_code}"
    return type(error).__name__


def deliver_pending(
    session: Session, send: Callable[[dict], None] = post_card, limit: int = BATCH
) -> tuple[int, int]:
    """Sends the oldest unsent posts, up to `limit`. A post that fails is counted and left for the
    next run until it has failed MAX_ATTEMPTS times, then it is left alone (the row keeps the
    reason). One failure never stops the rest. Returns (sent, failed). Does not commit."""
    if not settings.teams_new_ticket_configured:
        return 0, 0
    posts = session.exec(
        select(TeamsPost)
        .where(TeamsPost.sent_at.is_(None), TeamsPost.attempts < MAX_ATTEMPTS)
        .order_by(TeamsPost.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    sent = failed = 0
    for post in posts:
        ticket = session.get(Ticket, post.ticket_id) if post.ticket_id else None
        if ticket is None:
            post.attempts = MAX_ATTEMPTS
            post.last_error = "Ticket not found"
            session.add(post)
            failed += 1
            continue
        try:
            send(new_ticket_card(ticket))
        except Exception as error:
            post.attempts += 1
            post.last_error = describe_failure(error)
            failed += 1
        else:
            post.sent_at = datetime.now(timezone.utc)
            post.last_error = None
            sent += 1
        session.add(post)
    return sent, failed

"""The Adaptive Cards the Teams bot sends to a person in their own chat (phase 8B), built in this one module.

A card here is the bare card (what `app.bot_connector.send_card` takes), not the Workflows payload the channel posts
wrap it in. Every word that came from a requester (a ticket subject) goes through `app.teams.plain`, so it can never
become a link or markup. The person is already in the chat, so there is no @mention.

One card per notification kind: `CARD_FOR_KIND` maps a kind to its builder and `notification_card` falls back to the
plain "what happened, who did it, Open ticket" card. Phase 8C gives the "waiting for your approval" notification its
Approve and Decline buttons by registering a builder for that kind here (and a handler for the button press in
app/teams_bot.py); nothing else about delivery changes, because routing, the unread delay, the person's settings, the
access check and the backoff all happen before a card is built (app/teams.py).
"""

from typing import Callable, Optional

from app.models import Notification, Ticket
from app.notifications import notification_text
from app.teams import KIND_LABELS, plain, ticket_link

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.4"


def card_shell(body: list, actions: Optional[list] = None) -> dict:
    """An Adaptive Card around these body elements and buttons. The shared frame for every card the bot sends."""
    card = {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "body": body}
    if actions:
        card["actions"] = actions
    return card


def open_ticket_action(ticket: Optional[Ticket]) -> list:
    """The "Open ticket" button, or nothing while the web app's address is not known or there is no ticket."""
    link = ticket_link(ticket) if ticket is not None else None
    return [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}] if link else []


def what_happened_card(notification: Notification, ticket: Optional[Ticket]) -> dict:
    """What happened and who did it, with an Open ticket button."""
    facts = [{"title": "What", "value": KIND_LABELS.get(notification.kind, plain(notification.kind))}]
    if notification.actor_email:
        facts.append({"title": "By", "value": plain(notification.actor_email)})
    body = [
        {"type": "TextBlock", "text": plain(notification_text(notification, ticket)), "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    return card_shell(body, open_ticket_action(ticket))


# Kinds that have a card of their own. 8C adds the approval request here.
CARD_FOR_KIND: dict[str, Callable[[Notification, Optional[Ticket]], dict]] = {}


def notification_card(notification: Notification, ticket: Optional[Ticket]) -> dict:
    return CARD_FOR_KIND.get(notification.kind, what_happened_card)(notification, ticket)

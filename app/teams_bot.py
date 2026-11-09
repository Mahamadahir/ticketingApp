"""The Teams bot (phase 8): receives activities from Teams and dispatches them by type.

`receive(session, authorization, activity)` is the one door. It checks the token (app/teams_bot_auth.py), checks
the activity is from our own tenant, then calls the handler registered for the activity's type. The route
(app/routers/teams_bot.py) only reads the body and answers; everything after that is here, so the later phases add
a handler and nothing else:

    @handler("invoke")          # 8C: the Approve / Reject buttons on a card
    def approval_buttons(session, activity): ...
    @handler("message")         # 8D: intake from a direct message (this module's handler for it replies politely)

A handler gets the database session and the activity (already authenticated, from our tenant) and returns the
body to answer with, or None. It commits its own work before it calls Teams (a reply is a slow call that must not
hold a transaction open). Types with no handler are accepted and ignored: Teams sends many we do not use, and
anything but a 2xx makes it retry. Sending out is app/bot_connector.py; who is reachable is
app/teams_conversations.py.

What 8A handles: `installationUpdate` (the app was added to a person's personal scope, or removed: store or forget
their conversation) and `message` (store the conversation, answer that nothing is read yet). Only the personal
scope is served; teams and group chats are ignored.
"""

import logging
from typing import Any, Callable, Optional

from sqlmodel import Session

from app import bot_connector, teams_bot_auth
from app.config import settings
from app.teams_bot_auth import TokenRejected
from app.teams_conversations import remove_reference, save_reference

log = logging.getLogger("app.teams_bot")

Handler = Callable[[Session, dict], Optional[dict]]
HANDLERS: dict[str, Handler] = {}

PERSONAL = "personal"
MESSAGE_NOT_READ_YET = "I send you ticket updates here. I can't read replies yet, so please use the ticketing site for now."


def handler(activity_type: str) -> Callable[[Handler], Handler]:
    """Registers the function that handles an activity type (one per type)."""

    def register(function: Handler) -> Handler:
        if activity_type in HANDLERS:
            raise RuntimeError(f"Two handlers for Teams activity type {activity_type}")
        HANDLERS[activity_type] = function
        return function

    return register


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _sub(activity: dict, *path: str) -> Any:
    value: Any = activity
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def activity_tenant(activity: dict) -> str:
    return _text(_sub(activity, "channelData", "tenant", "id")).strip().lower()


def receive(session: Session, authorization: Optional[str], activity: Any) -> Optional[dict]:
    """Authenticates and dispatches one activity. Raises TokenRejected (401, or 403 for another tenant or channel);
    anything a handler returns is the body to answer with."""
    if not isinstance(activity, dict):
        raise TokenRejected("the body is not an activity")
    teams_bot_auth.check_request(authorization, _text(activity.get("serviceUrl")), _text(activity.get("channelId")))
    # A genuine token only proves the request came from Teams; whose message it is must also be ours. Another
    # organisation can install a bot with the same id only if it is multi-tenant, and ours is not, but this
    # is the check that makes sure of it.
    if not settings.teams_bot_tenant_id or activity_tenant(activity) != settings.teams_bot_tenant_id.strip().lower():
        raise TokenRejected("activity from another tenant", status=403)
    function = HANDLERS.get(_text(activity.get("type")))
    if function is None:
        return None
    return function(session, activity)


def _person(activity: dict) -> tuple[str, Optional[str]]:
    """(Entra object id, Teams user id) of the person the activity is from or about."""
    return _text(_sub(activity, "from", "aadObjectId")), _text(_sub(activity, "from", "id")) or None


def _is_personal(activity: dict) -> bool:
    return _sub(activity, "conversation", "conversationType") == PERSONAL


def _store(session: Session, activity: dict) -> bool:
    oid, teams_user_id = _person(activity)
    row, _ = save_reference(session, oid, _text(activity.get("serviceUrl")), _text(_sub(activity, "conversation", "id")), teams_user_id)
    return row is not None


@handler("installationUpdate")
def installation_update(session: Session, activity: dict) -> Optional[dict]:
    if not _is_personal(activity):
        return None
    action = _text(activity.get("action"))
    if action in ("add", "add-upgrade"):
        _store(session, activity)
    elif action in ("remove", "remove-upgrade"):
        remove_reference(session, _person(activity)[0])
    session.commit()
    return None


@handler("message")
def message(session: Session, activity: dict) -> Optional[dict]:
    if not _is_personal(activity):
        return None
    stored = _store(session, activity)
    session.commit()
    if stored:
        try:
            bot_connector.reply_to(activity, text=MESSAGE_NOT_READ_YET)
        except Exception as error:  # httpx, the token credential or a bad address: none may fail the request
            # Teams must still get its 200: a failed reply is not a reason for it to send the message again.
            log.warning("Teams bot: reply failed: %s", type(error).__name__)
    return None

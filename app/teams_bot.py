"""The Teams bot (phase 8): receives activities from Teams and dispatches them by type.

`receive(session, authorization, activity)` is the one door. It checks the token (app/teams_bot_auth.py), checks
the activity is from our own tenant, then calls the handler registered for the activity's type. The route
(app/routers/teams_bot.py) only reads the body and answers; everything after that is here, so the later phases add
a handler and nothing else:

    @handler("invoke")          # 8C: the Approve / Reject buttons on a card
    def approval_buttons(session, activity): ...
    @handler("message")         # 8D: intake from a direct message (app/teams_intake.py)

A handler gets the database session and the activity (already authenticated, from our tenant) and returns the
body to answer with, or None. It commits its own work before it calls Teams (a reply is a slow call that must not
hold a transaction open). Types with no handler are accepted and ignored: Teams sends many we do not use, and
anything but a 2xx makes it retry. A handler that calls Teams back (a reply) does it in `Handled.after`, which runs once the answer has been sent. Sending out is app/bot_connector.py; who is reachable is
app/teams_conversations.py.

What it handles: `installationUpdate` (the app was added to a person's personal scope, or removed: store or forget
their conversation), `message` (store the conversation, then intake: app/teams_intake.py) and `invoke` (a card button).
Only the personal scope is served; teams and group chats are ignored.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlmodel import Session

from app import bot_connector, teams_bot_auth
from app.config import settings
from app.teams_bot_auth import TokenRejected
from app.teams_conversations import remove_reference, save_reference

log = logging.getLogger("app.teams_bot")



@dataclass
class Handled:
    """What a handler hands back: `body` is the JSON to answer with, `after` is work to do once the answer is sent
    (a reply to the person, say). Teams waits only a few seconds for its answer and sends the activity again when it
    is late, so anything that calls Teams back goes in `after`."""

    body: Optional[dict] = None
    after: Optional[Callable[[], None]] = None


Handler = Callable[[Session, dict], Optional[Handled]]
HANDLERS: dict[str, Handler] = {}

PERSONAL = "personal"


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


def receive(session: Session, authorization: Optional[str], activity: Any) -> Optional[Handled]:
    """Authenticates and dispatches one activity. Raises TokenRejected (401, or 403 for another tenant or channel).
    Returns what the handler returned (None when there is nothing to answer or do afterwards)."""
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
def installation_update(session: Session, activity: dict) -> Optional[Handled]:
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
def message(session: Session, activity: dict) -> Optional[Handled]:
    """A direct message to the bot (8D, app/teams_intake.py): the conversation is stored, then the message raises a ticket,
    is added to one, or asks which."""
    if not _is_personal(activity):
        return None
    stored = _store(session, activity)
    session.commit()
    if not stored:
        return None
    from app.teams_intake import answer  # imported here: it needs the cards and the ticket code, which need this module

    return answer(session, activity)


@handler("invoke")
def invoke(session: Session, activity: dict) -> Optional[Handled]:
    """A card button was pressed. The choice of ticket for a message (8D, app/teams_intake.py) and the approval buttons
    (8C, app/teams_approvals.py) are answered in the response itself: nothing slow is done."""
    from app.bot_cards import VERB_NEW, VERB_PICK  # imported here: the cards need the notifications

    if _text(_sub(activity, "value", "action", "verb")) in (VERB_PICK, VERB_NEW):
        from app.teams_intake import answer_card

        body = answer_card(session, activity)
    else:
        from app.teams_approvals import answer

        body = answer(session, activity)
    return Handled(body=body) if body is not None else None


def _reply(activity: dict, text: str) -> None:
    try:
        bot_connector.reply_to(activity, text=text)
    except Exception as error:  # httpx, the token credential or a bad address: nobody is waiting on this any more
        log.warning("Teams bot: reply failed: %s", type(error).__name__)

"""Approving or declining from the bot's card (phase 8C): the answer to an `adaptiveCard/action` invoke.

The card (app/bot_cards.py) has two `Action.Execute` buttons. Teams sends each press to the bot endpoint as an invoke
activity, which app/teams_bot.py has already authenticated (bearer token, our tenant). What this module does with it:

  * Who votes is the Entra object id Teams puts in `from.aadObjectId`, matched to a person by
    `identity.person_for_object_id`. The card's data names an approval and a vote, nothing more: it is only a
    request to answer *that* vote, and it is refused unless the vote is that person's. A forwarded or edited card
    therefore does nothing for anyone else.
  * The vote goes through `approvals.record_decision` (via "Teams"), so every rule holds as it does for the portal and
    the emailed link: an eligible, active voter whose vote is unanswered; withdrawn, settled and superseded rounds
    refuse; self-approval and the inactive rules are unchanged. This module adds no rule of its own.
  * The answer is the new card (the request replaced by who decided and the outcome) or, when the press did nothing, a
    short message or the card showing why (already answered, no longer waiting). A second press, or a press on a stale
    card, changes nothing and says so. Nothing here calls Teams, so nothing is left to do after the answer: the vote is
    committed before the answer is built, and Teams' few seconds are plenty for one transaction.
"""

import logging
from typing import Any, Optional

from sqlmodel import Session

from app.approvals import APPROVED, DECLINED, ApprovalError, record_decision
from app.bot_cards import VERB_APPROVE, VERB_DECLINE, decided_card
from app.identity import person_for_object_id
from app.models import ApprovalVote

log = logging.getLogger("app.teams_bot")

ACTION_NAME = "adaptiveCard/action"
CARD = "application/vnd.microsoft.card.adaptive"
MESSAGE = "application/vnd.microsoft.activity.message"
VIA = "Teams"

NOT_YOU = "This request is not waiting for an answer from you."
NOT_FOUND = "This request could not be found. Open the ticketing site to see what is waiting for you."
UNKNOWN_PERSON = "I could not match your Teams account to an account on the ticketing site, so I cannot record your answer."


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def card_response(card: dict) -> dict:
    """The invoke response that makes Teams show this card in place of the one that was pressed."""
    return {"statusCode": 200, "type": CARD, "value": card}


def message_response(text: str) -> dict:
    """The invoke response that shows a short message and leaves the card as it is."""
    return {"statusCode": 200, "type": MESSAGE, "value": text}


def answer(session: Session, activity: dict) -> Optional[dict]:
    """The body to answer an invoke activity with, or None for an invoke that is not a card action."""
    if _text(activity.get("name")) != ACTION_NAME:
        return None
    value = activity.get("value") if isinstance(activity.get("value"), dict) else {}
    action = value.get("action") if isinstance(value.get("action"), dict) else {}
    verb = _text(action.get("verb"))
    if verb not in (VERB_APPROVE, VERB_DECLINE):
        return message_response(NOT_FOUND)
    data = action.get("data") if isinstance(action.get("data"), dict) else {}

    sender = activity.get("from") if isinstance(activity.get("from"), dict) else {}
    person = person_for_object_id(session, _text(sender.get("aadObjectId")))
    if person is None:
        return message_response(UNKNOWN_PERSON)

    vote = session.get(ApprovalVote, _int(data.get("vote"))) if _int(data.get("vote")) is not None else None
    if vote is None or vote.approval_id != _int(data.get("approval")):
        return message_response(NOT_FOUND)
    if vote.voter_id != person.id:
        # Forged or forwarded card data: the vote is somebody else's. Say nothing about the request.
        log.warning("Teams bot: card action on vote %s refused: not the voter", vote.id)
        return message_response(NOT_YOU)

    decision = APPROVED if verb == VERB_APPROVE else DECLINED
    try:
        record_decision(session, vote, decision, _text(data.get("comment")), via=VIA)
        session.commit()
    except ApprovalError as error:
        # Already answered, settled, withdrawn, superseded, no longer an approver, inactive or the requester
        # themselves: nothing changed. The card is replaced by how the request stands, with the reason.
        session.rollback()
        session.expire_all()
        return card_response(decided_card(session, session.get(ApprovalVote, vote.id), note=str(error)))
    return card_response(decided_card(session, session.get(ApprovalVote, vote.id)))

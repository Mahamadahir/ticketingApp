"""The Microsoft Graph webhook (phase 3A). Internet-facing: it is served by the public app.

Graph gives a webhook three seconds to answer, and retries and eventually drops the subscription of
one that is slow or failing. So this route does the least it can: it checks the clientState, stores
one row per notification in `GraphNotification`, and answers 202. Fetching the mail, matching it to
a ticket and everything else happens in the worker (3B), from those rows.
"""

import hmac
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.models import GraphNotification
from app.rate_limit import WEBHOOK, rate_limited

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 1_000_000  # Graph batches notifications; this is far above a real batch
MAX_NOTIFICATIONS = 1000
MAX_FIELD = 500  # a stored value is clipped, never trusted to be small


def _text(value: Any) -> Optional[str]:
    return value[:MAX_FIELD] if isinstance(value, str) and value else None


def _matches_client_state(notification: Any) -> bool:
    client_state = notification.get("clientState") if isinstance(notification, dict) else None
    # Constant-time compare, and anything not a string never matches. With no state configured nothing
    # can be checked, so nothing is accepted (an empty clientState would otherwise match an empty setting).
    return bool(settings.graph_webhook_client_state) and isinstance(client_state, str) and hmac.compare_digest(
        client_state.encode(), settings.graph_webhook_client_state.encode()
    )


def _stored(notification: dict) -> GraphNotification:
    resource_data = notification.get("resourceData")
    return GraphNotification(
        subscription_id=_text(notification.get("subscriptionId")),
        change_type=_text(notification.get("changeType")),
        lifecycle_event=_text(notification.get("lifecycleEvent")),
        resource=_text(notification.get("resource")),
        resource_id=_text(resource_data.get("id")) if isinstance(resource_data, dict) else None,
    )


@router.post("/graph/notifications", status_code=202, dependencies=[Depends(rate_limited(WEBHOOK))])
async def graph_notifications(request: Request, session: Session = Depends(get_session)):
    """Receives Graph change notifications (and subscription lifecycle notifications, which Graph
    sends to the same address). Graph validates a new subscription by POSTing here with a
    validationToken query param that must be echoed back as plain text with a 200.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return Response(content=validation_token, media_type="text/plain")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large")
    # Read in pieces and stop at the cap, so a request with no (or a false) length cannot make the
    # server hold an unbounded body in memory.
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Body too large")
    try:
        payload = json.loads(bytes(body))
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Unexpected payload")
    notifications = payload.get("value", [])
    if not isinstance(notifications, list):
        raise HTTPException(status_code=400, detail="Unexpected payload")

    # A notification with the wrong clientState is not from our subscription: dropped without a word,
    # and the answer is the same, so a caller learns nothing about which part failed.
    for notification in notifications[:MAX_NOTIFICATIONS]:
        if _matches_client_state(notification):
            session.add(_stored(notification))
    session.commit()

    return {"status": "accepted"}

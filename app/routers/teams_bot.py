"""The Teams bot's endpoint (phase 8A). Internet-facing: served by the public app only.

Teams (through the Bot Connector) POSTs every activity here. The route reads a bounded body, hands it to
app.teams_bot.receive, which checks the token and the tenant, and answers. Any failed check is a bare 401 (403 for
another tenant or channel) with no body, so the response tells a caller nothing; the reason goes to the log. With
TEAMS_BOT_ENABLED off the route does not exist as far as callers can tell (404).
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.config import settings
from app.database import get_session
from app.rate_limit import TEAMS_BOT, rate_limited
from app.teams_bot import receive
from app.teams_bot_auth import TokenRejected

log = logging.getLogger("app.teams_bot")

router = APIRouter(prefix="/api/teams", tags=["teams-bot"])

MAX_BODY_BYTES = 256_000  # an activity is a few kilobytes


@router.post("/messages", dependencies=[Depends(rate_limited(TEAMS_BOT))])
async def messages(request: Request, background: BackgroundTasks, authorization: Optional[str] = Header(default=None), session: Session = Depends(get_session)):
    if not settings.teams_bot_enabled:
        raise HTTPException(status_code=404)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return Response(status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=413)
    try:
        activity = json.loads(bytes(body))
    except ValueError:
        return Response(status_code=401)  # nothing to authenticate
    try:
        # The checks fetch signing keys and the handlers use the database and call Teams: all blocking.
        handled = await run_in_threadpool(receive, session, authorization, activity)
    except TokenRejected as rejected:
        log.warning("Teams bot: request refused (%s)", rejected.reason)
        return Response(status_code=rejected.status)
    if handled is not None and handled.after is not None:
        background.add_task(handled.after)  # runs after the answer is sent
    if handled is not None and handled.body is not None:
        return JSONResponse(handled.body)
    return Response(status_code=200)

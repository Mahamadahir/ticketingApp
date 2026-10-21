"""The emailed feedback and reopen links (phase 4F). No sign-in: the token in the link says what it may do.

GET shows a confirmation page and never changes anything, so a mail scanner or link preview that fetches
the link cannot reopen a ticket or use up a rating. The POST from that page acts, and uses the link up in
the same transaction (app/action_links.py). Served by the public app (APP_ROLE=public) and rate limited per
caller address, like the approval link, since the token is a credential in an address.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app import action_links
from app.action_links import FEEDBACK, REOPEN, LinkError
from app.database import get_session
from app.models import ActionToken, Ticket
from app.rate_limit import ACTION_LINKS, rate_limited
from app.templating import templates

router = APIRouter(tags=["action-links"], dependencies=[Depends(rate_limited(ACTION_LINKS))])

# The token is in the address, so keep it out of caches and Referer headers.
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _page(request: Request, kind: str, token: str, row: Optional[ActionToken], ticket: Optional[Ticket],
          state: str, message: str = "", status_code: int = 200) -> HTMLResponse:
    """state: ready (the form), done (acted just now), used, expired, closed (cannot reopen), invalid."""
    return templates.TemplateResponse(
        request,
        "action_link_page.html",
        {
            "kind": kind, "token": token, "ticket": ticket, "state": state, "message": message,
            "max_comment_length": action_links.MAX_COMMENT_LENGTH, "user": None,
        },
        status_code=status_code,
        headers=_HEADERS,
    )


def _load(session: Session, kind: str, token: str) -> tuple[Optional[ActionToken], Optional[Ticket]]:
    row = action_links.find(session, kind, token)
    return row, (session.get(Ticket, row.ticket_id) if row is not None else None)


def _state(kind: str, row: ActionToken, ticket: Ticket) -> str:
    if row.used_at is not None:
        return "used"
    if action_links.is_expired(row):
        return "expired"
    if kind == REOPEN and not action_links.can_reopen(ticket):
        return "closed" if ticket.status.value == "closed" else "not_resolved"
    return "ready"


def _show(request: Request, session: Session, kind: str, token: str, message: str = "") -> HTMLResponse:
    row, ticket = _load(session, kind, token)
    if row is None or ticket is None:
        return _page(request, kind, token, None, None, "invalid", status_code=404)
    state = _state(kind, row, ticket)
    return _page(request, kind, token, row, ticket, state, message, status_code=410 if state == "expired" else 200)


@router.get("/feedback/{token}", response_class=HTMLResponse)
def show_feedback(request: Request, token: str, session: Session = Depends(get_session)):
    return _show(request, session, FEEDBACK, token)


@router.post("/feedback/{token}", response_class=HTMLResponse)
def give_feedback(
    request: Request,
    token: str,
    rating: int = Form(...),
    comment: str = Form("", max_length=action_links.MAX_COMMENT_LENGTH + 500),
    session: Session = Depends(get_session),
):
    row, ticket = _load(session, FEEDBACK, token)
    if row is None or ticket is None:
        return _page(request, FEEDBACK, token, None, None, "invalid", status_code=404)
    try:
        action_links.submit_feedback(session, row, rating, comment)
    except LinkError as error:
        session.rollback()
        return _show(request, session, FEEDBACK, token, message=str(error))
    session.commit()
    return _page(request, FEEDBACK, token, row, ticket, "done", "Thank you, your feedback has been recorded.")


@router.get("/reopen/{token}", response_class=HTMLResponse)
def show_reopen(request: Request, token: str, session: Session = Depends(get_session)):
    return _show(request, session, REOPEN, token)


@router.post("/reopen/{token}", response_class=HTMLResponse)
def reopen(request: Request, token: str, session: Session = Depends(get_session)):
    row, ticket = _load(session, REOPEN, token)
    if row is None or ticket is None:
        return _page(request, REOPEN, token, None, None, "invalid", status_code=404)
    state = _state(REOPEN, row, ticket)
    if state != "ready":
        return _show(request, session, REOPEN, token)
    try:
        action_links.reopen_from_link(session, row, ticket)
    except LinkError as error:
        session.rollback()
        return _show(request, session, REOPEN, token, message=str(error))
    session.commit()
    return _page(request, REOPEN, token, row, ticket, "done")

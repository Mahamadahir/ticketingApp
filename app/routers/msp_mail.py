"""Mail from the MSP that matched no ticket (issue #149): an agent links each item to a ticket, or dismisses it. Agents
(and super admins) only; linking needs the same access as working the ticket (app/visibility.py). The matching and the
recording are app/msp.py's."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth import require_roles
from app.database import get_session
from app.models import MspUnmatchedMail, User
from app.msp import MspError, dismiss_unmatched, link_unmatched, unmatched_query
from app.notifications import AGENT_ROLES
from app.pagination import paginate
from app.templating import templates
from app.ticket_service import find_ticket
from app.visibility import Access, access_context, require_access

router = APIRouter(prefix="/msp-mail", tags=["msp-mail"])


def _item(session: Session, item_id: int) -> MspUnmatchedMail:
    item = session.get(MspUnmatchedMail, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Mail not found")
    return item


@router.get("", response_class=HTMLResponse)
def unmatched_list(
    request: Request,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    entries = paginate(session, unmatched_query(), page, request.query_params)
    return templates.TemplateResponse(request, "msp_unmatched.html", {"page": entries, "items": entries.items, "user": user})


@router.post("/{item_id}/link")
def link_item(
    item_id: int,
    ticket: str = Form("", max_length=40),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    item = _item(session, item_id)
    target = find_ticket(session, ticket)
    if target is None:
        raise HTTPException(status_code=400, detail="There is no ticket with that number")
    require_access(user, target, access_context(session, user), Access.WORK)
    try:
        link_unmatched(session, item, target, user)
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    return RedirectResponse(f"/tickets/{target.number}", status_code=303)


@router.post("/{item_id}/dismiss")
def dismiss_item(
    item_id: int,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    item = _item(session, item_id)
    try:
        dismiss_unmatched(session, item, user)
    except MspError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    return RedirectResponse("/msp-mail", status_code=303)

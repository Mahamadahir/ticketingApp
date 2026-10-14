"""The emailed approve/decline link (phase 1H). No sign-in: the signed link says who is answering.

GET shows the request and asks; it never decides. Deciding is the POST from that page, so a
mail scanner or link preview that fetches the link cannot approve or decline for the person.
This is an internet-facing route: it belongs with the public routers when APP_ROLE splits them.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.approval_links import read_token
from app.approvals import (
    MAX_COMMENT_LENGTH,
    PENDING,
    ApprovalError,
    approvals_of,
    decide,
    describe_rule,
)
from app.audit import log_action
from app.database import get_session
from app.messages import requester_visible_messages
from app.models import RequestType, Ticket, TicketApproval, TicketStatus, User
from app.settings_store import APPROVAL_LINK_MINUTES, get_int_setting

router = APIRouter(prefix="/approvals", tags=["approvals"])
templates = Jinja2Templates(directory="app/templates")

# The token is in the address, so keep it out of caches and Referer headers.
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _approval_from(session: Session, token: str) -> Optional[TicketApproval]:
    minutes = get_int_setting(session, APPROVAL_LINK_MINUTES) or 20160
    approval_id = read_token(token, minutes)
    return session.get(TicketApproval, approval_id) if approval_id is not None else None


def _page(request: Request, session: Session, approval: TicketApproval, token: str, message: str = "") -> HTMLResponse:
    ticket = session.get(Ticket, approval.ticket_id)
    approver = session.get(User, approval.approver_id)
    request_type = session.get(RequestType, ticket.request_type_id) if ticket.request_type_id else None
    said = [m for m in requester_visible_messages(session, ticket) if m.direction.value == "inbound"]
    return templates.TemplateResponse(
        request,
        "approval_page.html",
        {
            "token": token,
            "ticket": ticket,
            "approval": approval,
            "approver": approver,
            "request_type": request_type,
            "description": said[0].body if said else "",
            "can_answer": approval.decision == PENDING and ticket.status == TicketStatus.AWAITING_APPROVAL,
            "rule": describe_rule(ticket, len(approvals_of(session, ticket))),
            "max_comment_length": MAX_COMMENT_LENGTH,
            "message": message,
            "user": None,
        },
        headers=_HEADERS,
    )


def _invalid(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "approval_page.html", {"invalid": True, "user": None}, status_code=404, headers=_HEADERS
    )


@router.get("/{token}", response_class=HTMLResponse)
def show(request: Request, token: str, session: Session = Depends(get_session)):
    approval = _approval_from(session, token)
    if approval is None:
        return _invalid(request)
    return _page(request, session, approval, token)


@router.post("/{token}", response_class=HTMLResponse)
def answer(
    request: Request,
    token: str,
    decision: str = Form(...),
    comment: str = Form("", max_length=MAX_COMMENT_LENGTH + 500),
    session: Session = Depends(get_session),
):
    approval = _approval_from(session, token)
    if approval is None:
        return _invalid(request)
    try:
        decide(session, approval, decision, comment)
    except ApprovalError as error:
        session.rollback()
        session.refresh(approval)
        return _page(request, session, approval, token, message=str(error))
    approver = session.get(User, approval.approver_id)
    log_action(session, approver.email, "ticket", approval.ticket_id, f"approval_{decision}", (comment or "")[:200])
    session.commit()
    session.refresh(approval)
    return _page(request, session, approval, token, message="Thank you, your answer has been recorded.")
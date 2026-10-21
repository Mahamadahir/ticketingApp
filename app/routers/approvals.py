"""The emailed approve/decline link (phase 1H). No sign-in: the signed link says who is answering.

GET shows the request and asks; it never decides. Deciding is the POST from that page, so a
mail scanner or link preview that fetches the link cannot approve or decline for the person.
This is an internet-facing route, served by the public app (APP_ROLE=public), so it is rate limited
per caller address: the token is a credential in an address, and guessing it should be impractical.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from app.templating import templates
from sqlmodel import Session

from app.approval_links import read_token
from app.approvals import (
    MAX_COMMENT_LENGTH,
    PENDING,
    WITHDRAWN,
    ApprovalError,
    describe_rule,
    record_decision,
)
from app.database import get_session
from app.messages import requester_visible_messages
from app.rate_limit import APPROVALS, rate_limited
from app.models import ApprovalVote, RequestType, Ticket, TicketApproval, TicketStatus, User
from app.settings_store import APPROVAL_LINK_MINUTES, get_int_setting

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(rate_limited(APPROVALS))])

# The token is in the address, so keep it out of caches and Referer headers.
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _vote_from(session: Session, token: str) -> Optional[ApprovalVote]:
    minutes = get_int_setting(session, APPROVAL_LINK_MINUTES) or 20160
    vote_id = read_token(token, minutes)
    return session.get(ApprovalVote, vote_id) if vote_id is not None else None


def _page(request: Request, session: Session, vote: ApprovalVote, token: str, message: str = "") -> HTMLResponse:
    approval = session.get(TicketApproval, vote.approval_id)
    ticket = session.get(Ticket, approval.ticket_id)
    approver = session.get(User, vote.voter_id)
    request_type = session.get(RequestType, ticket.request_type_id) if ticket.request_type_id else None
    said = [m for m in requester_visible_messages(session, ticket) if m.direction.value == "inbound"]
    return templates.TemplateResponse(
        request,
        "approval_page.html",
        {
            "token": token,
            "ticket": ticket,
            "approval": vote,
            "approver": approver,
            "request_type": request_type,
            "description": said[0].body if said else "",
            "withdrawn": approval.status == WITHDRAWN,
            "can_answer": vote.decision == PENDING and approval.status == PENDING and ticket.status == TicketStatus.AWAITING_APPROVAL,
            "rule": describe_rule(approval),
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
    vote = _vote_from(session, token)
    if vote is None:
        return _invalid(request)
    return _page(request, session, vote, token)


@router.post("/{token}", response_class=HTMLResponse)
def answer(
    request: Request,
    token: str,
    decision: str = Form(...),
    comment: str = Form("", max_length=MAX_COMMENT_LENGTH + 500),
    session: Session = Depends(get_session),
):
    vote = _vote_from(session, token)
    if vote is None:
        return _invalid(request)
    try:
        record_decision(session, vote, decision, comment)
    except ApprovalError as error:
        session.rollback()
        session.refresh(vote)
        return _page(request, session, vote, token, message=str(error))
    session.commit()
    session.refresh(vote)
    return _page(request, session, vote, token, message="Thank you, your answer has been recorded.")
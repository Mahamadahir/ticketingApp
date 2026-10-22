"""Resolve several tickets with one note (phase 4I). The rules are in app/bulk_resolve.py."""

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.auth import require_roles
from app.bulk_resolve import BulkError, NOT_WORKABLE, linked_numbers, preview_bulk, resolve_bulk
from app.database import get_session
from app.forms import MAX_MESSAGE_LENGTH
from app.models import User
from app.notifications import AGENT_ROLES
from app.resolution import ResolveError, check_can_resolve
from app.templating import templates
from app.ticket_service import get_by_number
from app.visibility import Access, access_context, ticket_access

router = APIRouter(prefix="/bulk-resolve", tags=["bulk-resolve"])


@router.get("", response_class=HTMLResponse)
def choose(
    request: Request,
    number: list[int] = Query(default=[]),
    parent: Optional[int] = None,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """The tickets picked on the list, or a parent with the tickets linked under it, and the note."""
    numbers = list(number)
    if parent is not None:
        numbers = linked_numbers(session, parent) + numbers
    levels = access_context(session, user)
    rows = []
    for n in dict.fromkeys(numbers):
        ticket = get_by_number(session, n)
        if ticket is None or ticket_access(user, ticket, levels) != Access.WORK:
            rows.append({"number": n, "ticket": None, "problem": NOT_WORKABLE})
            continue
        try:
            check_can_resolve(session, ticket)
            problem = None
        except ResolveError as error:
            problem = str(error)
        rows.append({"number": n, "ticket": ticket, "problem": problem})
    return templates.TemplateResponse(request, "bulk_resolve.html", {"rows": rows, "user": user})


@router.post("/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    number: list[int] = Form(default=[]),
    resolution_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """One preview for the whole batch: every ticket, who is emailed for it, and what will be skipped."""
    try:
        plan = preview_bulk(session, user, access_context(session, user), number, resolution_notes)
    except BulkError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return templates.TemplateResponse(
        request, "bulk_resolve_preview.html",
        {"plan": plan, "notes": resolution_notes.strip(), "user": user},
    )


@router.post("", response_class=HTMLResponse)
def confirm(
    request: Request,
    number: list[int] = Form(default=[]),
    resolution_notes: str = Form("", max_length=MAX_MESSAGE_LENGTH + 1000),
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Resolves what can be resolved and reports each ticket: resolved, or skipped with the reason."""
    try:
        plan = resolve_bulk(session, user, access_context(session, user), number, resolution_notes)
    except BulkError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    session.commit()
    return templates.TemplateResponse(request, "bulk_resolve_result.html", {"plan": plan, "user": user})

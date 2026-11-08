"""Admin > Data protection (issue #175): the subject access export. Opened by the `data_protection` permission
(super admins always; an agent a super admin names). What goes in the export is decided in app/subject_access.py."""

import re
from typing import Iterator

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, select

from app import anonymisation
from app.auth import require_permission
from app.database import get_session
from app.models import PendingBlobDelete, User
from app.permissions import DATA_PROTECTION
from app.settings_store import SUBJECT_ACCESS_MAX_MB, count_setting
from app.storage import Storage, get_storage
from app.subject_access import build_export
from app.templating import templates

router = APIRouter(prefix="/admin/data-protection", tags=["data-protection"])
require_data_protection = require_permission(DATA_PROTECTION)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHUNK = 1024 * 1024


def _page(request: Request, user: User, session: Session, *, address: str = "", error: str = "", status: int = 200):
    return templates.TemplateResponse(
        request, "admin_data_protection.html",
        {
            "user": user, "max_mb": count_setting(session, SUBJECT_ACCESS_MAX_MB), "address": address, "error": error,
            "waiting_deletes": session.exec(select(PendingBlobDelete).order_by(PendingBlobDelete.created_at).limit(20)).all(),
        },
        status_code=status,
    )


@router.get("", response_class=HTMLResponse)
def show(request: Request, user: User = Depends(require_data_protection), session: Session = Depends(get_session)):
    return _page(request, user, session)


def _stream(handle) -> Iterator[bytes]:
    """The temporary file in chunks; closing it deletes it, whether the download finished or not."""
    try:
        while chunk := handle.read(CHUNK):
            yield chunk
    finally:
        handle.close()


@router.post("/subject-access")
def subject_access(
    request: Request,
    address: str = Form(""),
    user: User = Depends(require_data_protection),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    address = address.strip().lower()
    if not EMAIL_RE.match(address) or len(address) > 254:
        return _page(request, user, session, address=address, error="Enter an email address.", status=400)
    export = build_export(session, storage, address, user)
    session.commit()  # the audit entry: who exported whose data
    name = "subject-access-" + re.sub(r"[^a-z0-9]+", "-", address).strip("-") + ".zip"
    return StreamingResponse(
        _stream(export.file),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(export.size),
            "Cache-Control": "no-store",
        },
    )


# ---- Anonymise a requester (issue #176) ---------------------------------------------------------------------------


def _anonymise_page(request: Request, user: User, *, address="", close_open=False, shown=None, result=None, error="", status=200):
    return templates.TemplateResponse(
        request, "admin_anonymise.html",
        {"user": user, "address": address, "close_open": close_open, "shown": shown, "result": result, "error": error},
        status_code=status,
    )


@router.post("/anonymise/preview", response_class=HTMLResponse)
def anonymise_preview(
    request: Request,
    address: str = Form(""),
    close_open: bool = Form(False),
    user: User = Depends(require_data_protection),
    session: Session = Depends(get_session),
):
    """Shows what anonymising would change, counted by doing it and rolling back. Nothing is saved."""
    address = address.strip().lower()
    if not EMAIL_RE.match(address) or len(address) > 254:
        return _page(request, user, session, error="Enter an email address.", status=400)
    shown = anonymisation.preview(session, address, user, close_open=close_open)
    return _anonymise_page(request, user, address=address, close_open=close_open, shown=shown)


@router.post("/anonymise", response_class=HTMLResponse)
def anonymise_confirm(
    request: Request,
    address: str = Form(""),
    confirm: str = Form(""),
    fingerprint: str = Form(""),
    close_open: bool = Form(False),
    user: User = Depends(require_data_protection),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    address = address.strip().lower()
    if not EMAIL_RE.match(address) or len(address) > 254:
        return _page(request, user, session, error="Enter an email address.", status=400)
    if confirm.strip().lower() != address:
        shown = anonymisation.preview(session, address, user, close_open=close_open)
        return _anonymise_page(
            request, user, address=address, close_open=close_open, shown=shown,
            error="The address typed does not match. Nothing was changed.", status=400,
        )
    try:
        outcome = anonymisation.run(session, address, user, close_open=close_open, expected=fingerprint or None)
    except anonymisation.Refused as error:
        shown = anonymisation.preview(session, address, user, close_open=close_open)
        return _anonymise_page(request, user, address=address, close_open=close_open, shown=shown, error=str(error), status=409)
    # The database change is committed; only now are the stored files deleted. A failure stays queued for the
    # `blob-cleanup` job (and is reported here), so the files are never forgotten and never deleted early.
    deleted, failed = anonymisation.purge_pending_blobs(session, storage, outcome.blob_keys)
    session.commit()
    return _anonymise_page(request, user, result={"outcome": outcome, "deleted": deleted, "failed": failed})

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from app.attachments import is_inline_type, serving_headers
from app.auth import get_current_user
from app.database import get_session
from app.models import Attachment, Ticket, User, UserRole
from app.storage import Storage, get_storage
from app.visibility import NoTicketAccess, access_context, may_view_attachment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.get("/{attachment_id}")
def download(
    attachment_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    storage: Storage = Depends(get_storage),
):
    """Checked on every request, so a link that leaks is useless once the person loses access
    to the ticket. In Azure this redirects to a signed address on the storage origin that works
    for about a minute and a half, with the type and disposition forced by the link; locally the
    bytes are streamed with serving_headers (which also add nosniff and a sandboxing policy that
    the storage origin cannot be made to send: it is a different origin from the app, so a file
    cannot reach the session either way). Files are held in memory, which the size limit bounds."""
    attachment = session.get(Attachment, attachment_id)
    ticket = session.get(Ticket, attachment.ticket_id) if attachment is not None else None
    if attachment is None or ticket is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not may_view_attachment(session, user, attachment, ticket, access_context(session, user)):
        # A requester gets the same answer as for a missing file, as the portal does for
        # tickets; an agent sees the usual "no access" page.
        if user.role in (UserRole.AGENT, UserRole.SUPER_ADMIN):
            raise NoTicketAccess()
        raise HTTPException(status_code=404, detail="Attachment not found")

    url = storage.download_url(
        attachment.blob_key,
        filename=attachment.filename,
        content_type=attachment.content_type,
        inline=is_inline_type(attachment.content_type),
    )
    if url is not None:
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
    try:
        content = storage.read(attachment.blob_key)
    except FileNotFoundError:
        logger.error("Attachment %s has no stored file at %s", attachment.id, attachment.blob_key)
        raise HTTPException(status_code=404, detail="The file is no longer available") from None
    return Response(content=content, headers=serving_headers(attachment))

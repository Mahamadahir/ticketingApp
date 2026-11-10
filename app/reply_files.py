"""Files an agent attaches to a reply or a note on the ticket page (#201).

There is one set of rules for a stored file (app/attachments.py `check_file`, `store_attachment`) and one for how many files
one message may carry (the inbound mail setting `inbound_max_attachments`, app/settings_store.py); this module only applies
them to what an agent chose, and refuses early, with a reason to show:

- an internal note stores its files at once, with the note (`attach_to_note` in app/routers/tickets.py), agents-only like the
  note itself (app/visibility.may_view_attachment);
- an email to the requester has a preview first. At the preview the files are checked and also held (`hold`: stored under
  their content hash, one `ReplyHeldFile` row each, one draft per agent and ticket). Confirming the preview reads them back
  (`take`), attaches them to the new message (app/messages.email_requester) and removes the rows (`release`).
  Abandoned drafts are removed when the agent keeps editing (`discard`), when they preview again, and when they expire
  (`expire`, the recurring job `reply-files`, setting `reply_held_file_hours`). A stored file nothing else uses goes through the
  blob delete queue (`PendingBlobDelete`), the queue anonymisation and the Teams held files use.
- the email carries the files only up to what Graph takes in one send (`email_budget_bytes`); a message over it is refused at
  the preview, so nothing is silently left off.

Nothing here commits; the caller does.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.attachments import AttachmentRejected, NewFile, check_file, clean_filename, keys_in_use, store_blob
from app.config import settings
from app.models import PendingBlobDelete, ReplyHeldFile, Ticket, User
from app.msp import GRAPH_INLINE_LIMIT_MB  # what Graph takes in one send with the files inline
from app.settings_store import (
    INBOUND_MAX_ATTACHMENTS, REPLY_ATTACH_MAX_MB, REPLY_HELD_FILE_HOURS, REQUESTER_UPLOAD_TOTAL_MB, count_setting,
)
from app.storage import Storage, get_storage

log = logging.getLogger("app.reply_files")


class FilesRefused(ValueError):
    """The files cannot be attached; the message says why and is safe to show."""


def read_uploads(session: Session, uploads: list, *, email: bool = False, requester: bool = False) -> list[NewFile]:
    """The files of a form (starlette UploadFile objects), checked by the rules of every stored file. A file input left empty
    sends one nameless part, which is not a file. With `email` the total must also fit one email, and is refused from the
    sizes the form announced before any file is read into memory. With `requester` (a requester's reply or new request on the
    portal) the total must also fit `requester_upload_total_mb`, checked the same two ways. Raises FilesRefused naming the file and why."""
    uploads = [u for u in uploads if getattr(u, "filename", None)]
    most = count_setting(session, INBOUND_MAX_ATTACHMENTS)
    if len(uploads) > most:
        raise FilesRefused(f"Attach at most {most} files to one message.")
    if email:
        check_email_size(session, [u.size or 0 for u in uploads])
    if requester:
        check_requester_total(session, [u.size or 0 for u in uploads])
    files = []
    for upload in uploads:
        data = upload.file.read(settings.attachment_max_bytes + 1)  # one byte over is enough to refuse it, and bounds the memory
        try:
            check_file(upload.filename, data)
        except AttachmentRejected as error:
            raise FilesRefused(f"{upload.filename}: {error}")
        files.append(NewFile(upload.filename, data))
    if email:
        check_email_size(session, [len(item.data) for item in files])
    if requester:
        check_requester_total(session, [len(item.data) for item in files])
    return files


def read_requester_uploads(session: Session, uploads: list) -> list[NewFile]:
    """The one way a requester's files are read on the portal (a reply, or a new request): every rule of `read_uploads` and
    the total-size limit."""
    return read_uploads(session, uploads, requester=True)


def check_requester_total(session: Session, sizes: list[int]) -> None:
    """Refuses a set of files that together are more than a requester may add at once (setting `requester_upload_total_mb`)."""
    limit = count_setting(session, REQUESTER_UPLOAD_TOTAL_MB)
    if sum(sizes) > limit * 1024 * 1024:
        raise FilesRefused(
            f"These files add up to {_megabytes(sum(sizes))}, more than the {limit} MB you can add at once. "
            "Add fewer or smaller files."
        )


def email_budget_bytes(session: Session) -> int:
    return min(count_setting(session, REPLY_ATTACH_MAX_MB), GRAPH_INLINE_LIMIT_MB) * 1024 * 1024


def check_email_size(session: Session, sizes: list[int]) -> None:
    """Refuses files that together are more than one email can carry (the reply may go by email whatever the channel)."""
    budget = email_budget_bytes(session)
    if sum(sizes) > budget:
        raise FilesRefused(
            f"These files add up to {_megabytes(sum(sizes))}, more than the {budget // (1024 * 1024)} MB one email can carry. "
            "Attach fewer or smaller files."
        )


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def hold(session: Session, storage: Optional[Storage], ticket: Ticket, user: User, files: list[NewFile]) -> Optional[str]:
    """Stores the files and records them as this agent's draft on the ticket, replacing any earlier draft of theirs there.
    Returns the draft's token, or None when there are no files."""
    discard(session, ticket, user)
    if not files:
        return None
    storage = storage or get_storage()
    token = secrets.token_urlsafe(16)
    for item in files:
        _, key = store_blob(session, storage, item.data)
        session.add(ReplyHeldFile(
            ticket_id=ticket.id, user_id=user.id, token=token, filename=clean_filename(item.filename), size=len(item.data), blob_key=key,
        ))
    session.flush()
    return token


def held(session: Session, ticket: Ticket, user: User, token: Optional[str], *, lock: bool = False) -> list[ReplyHeldFile]:
    """This agent's draft with that token on this ticket, in the order the files were chosen. Nobody else's draft is found.
    With `lock` the rows are locked until the transaction ends, so a second confirm of the same preview (a double click, a
    second tab) waits, then finds them gone and is refused instead of sending the message twice."""
    if not token:
        return []
    query = select(ReplyHeldFile).where(
        ReplyHeldFile.ticket_id == ticket.id, ReplyHeldFile.user_id == user.id, ReplyHeldFile.token == token
    ).order_by(ReplyHeldFile.id)
    return list(session.exec(query.with_for_update() if lock else query).all())


def take(storage: Optional[Storage], rows: list[ReplyHeldFile]) -> list[NewFile]:
    """The held files read back from storage, to be attached. Raises FilesRefused when one is gone (the preview is then
    shown again)."""
    storage = storage or get_storage()
    files = []
    for row in rows:
        try:
            files.append(NewFile(row.filename, storage.read(row.blob_key)))
        except Exception as error:  # a storage outage, or the file was removed
            log.warning("Reply files: a held file could not be read (%s)", type(error).__name__)
            raise FilesRefused(f"{row.filename} could not be read back. Attach the files again and preview the message again.")
    return files


def release(session: Session, rows: list[ReplyHeldFile]) -> int:
    """Removes held rows. Each stored file no attachment and no other held row uses is queued for deletion (the files just
    attached are in use, so they stay). Returns how many were queued."""
    keys = {r.blob_key for r in rows}
    for row in rows:
        session.delete(row)
    session.flush()
    queued = 0
    for key in keys - keys_in_use(session, keys):
        if session.get(PendingBlobDelete, key) is None:
            session.add(PendingBlobDelete(blob_key=key))
            queued += 1
    return queued


def discard(session: Session, ticket: Ticket, user: User, token: Optional[str] = None) -> int:
    """Drops this agent's draft on the ticket (one with that token, or any when none is named). Returns the files dropped."""
    query = select(ReplyHeldFile).where(ReplyHeldFile.ticket_id == ticket.id, ReplyHeldFile.user_id == user.id)
    if token:
        query = query.where(ReplyHeldFile.token == token)
    rows = list(session.exec(query).all())
    release(session, rows)
    return len(rows)


def release_for_tickets(session: Session, ticket_ids: list[int]) -> int:
    """Drops the drafts on these tickets (anonymisation of their requester). Returns how many held rows went."""
    if not ticket_ids:
        return 0
    rows = list(session.exec(select(ReplyHeldFile).where(ReplyHeldFile.ticket_id.in_(ticket_ids))).all())
    release(session, rows)
    return len(rows)


def expire(session: Session, now: Optional[datetime] = None) -> int:
    """The recurring job `reply-files`: drops drafts older than `reply_held_file_hours` that were never confirmed, and queues
    the stored files nothing uses. Returns how many held rows went."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=count_setting(session, REPLY_HELD_FILE_HOURS))
    rows = list(session.exec(select(ReplyHeldFile).where(ReplyHeldFile.created_at < cutoff).with_for_update(skip_locked=True)).all())
    release(session, rows)
    return len(rows)

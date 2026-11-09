"""Attachments: the one place a file is checked and stored (docs/plan.md, "Storage decisions").

Everything that adds a file to a ticket (an agent's upload, and later mail ingestion) calls
store_attachment, so the size limit, the type checks, the detection of what the file really is,
the skipping of tiny inline pictures and the deduplication are decided here and nowhere else.
Who may download a file is decided only in app/visibility.py (may_view_attachment); how it is
served is decided by serving_headers below, from the detected type alone.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import settings
from app.models import Attachment, Message, ReplyHeldFile, TeamsHeldFile, Ticket, User
from app.storage import Storage, content_disposition
from app.visibility import AccessContext, may_view_attachment

# The only types the browser may show in the page. Never SVG or HTML: both can carry script.
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Refused outright: files whose only use is to be run. Compared with the final extension.
BLOCKED_EXTENSIONS = frozenset(
    {"exe", "dll", "com", "scr", "msi", "bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "hta", "jar", "lnk"}
)

MAX_FILENAME_LENGTH = 200


class AttachmentRejected(ValueError):
    """The file may not be stored; the message says why and is safe to show."""


@dataclass
class NewFile:
    """A file an agent chose to attach to a message of their own (app/reply_files.py): the name it came with and its bytes."""

    filename: str
    data: bytes


def detect_content_type(data: bytes) -> str:
    """What the bytes are, from their first bytes. The sender's claimed type and the filename
    are ignored: a file called photo.png that starts like HTML is not an image. Anything not
    recognised is application/octet-stream, which is always downloaded."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "application/zip"  # also .docx, .xlsx and .pptx, which download like any other
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"  # legacy Office
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    return "application/octet-stream"


def clean_filename(name: str) -> str:
    """Just the file's own name: no directories, no control characters, a sensible length."""
    name = re.split(r"[\\/]", name or "")[-1]
    name = "".join(c for c in name if c.isprintable()).strip().strip(".")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        name = (stem[: MAX_FILENAME_LENGTH - len(extension) - 1] + dot + extension) if dot and len(extension) < 20 else name[:MAX_FILENAME_LENGTH]
    return name or "attachment"


def is_inline_type(content_type: str) -> bool:
    return content_type in INLINE_TYPES


def serving_headers(attachment: Attachment) -> dict[str, str]:
    """Headers for a response that streams the file itself. Shown in the page only if the
    detected type is a safe picture; everything else is forced to download. nosniff stops a
    browser guessing a more dangerous type from the content."""
    show = is_inline_type(attachment.content_type)
    return {
        "Content-Type": attachment.content_type,
        "Content-Disposition": content_disposition(attachment.filename, inline=show),
        "X-Content-Type-Options": "nosniff",
        # Nothing the file contains may run or load anything, even if it is shown.
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
    }


def blob_key(content_hash: str) -> str:
    return f"{content_hash[:2]}/{content_hash}"


def lock_blob(session: Session, key: str) -> None:
    """Holds a transaction-long lock on one stored file's key. An upload takes it before asking whether the blob exists
    and keeps it until its row is committed; the anonymisation cleanup (app/anonymisation.py) takes the same lock before
    it checks that no row uses the blob and deletes it. So a file uploaded again while it is being deleted either has its
    row seen by the cleanup, or finds the blob gone and writes it again."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def keys_in_use(session: Session, keys: set[str]) -> set[str]:
    """Which of these stored files a row still points at: an attachment, or a file held for a Teams question or for an
    agent's reply preview. The one list a delete of a stored file asks (anonymisation's cleanup, the release of held files),
    so a new kind of holder is added here and nowhere else."""
    keys = set(keys)
    if not keys:
        return set()
    used = set(session.exec(select(Attachment.blob_key).where(Attachment.blob_key.in_(keys))).all())
    used |= set(session.exec(select(TeamsHeldFile.blob_key).where(TeamsHeldFile.blob_key.in_(keys))).all())
    used |= set(session.exec(select(ReplyHeldFile.blob_key).where(ReplyHeldFile.blob_key.in_(keys))).all())
    return used


def try_lock_blob(session: Session, key: str) -> bool:
    """The same lock without waiting: False when an upload holds it (the cleanup then leaves the file for its next run)."""
    return bool(session.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}).scalar())


def check_file(filename: str, data: bytes) -> str:
    """The rules every stored file meets, in one place: not empty, within the size limit, not a file whose only use is to be
    run. Returns the cleaned file name; raises AttachmentRejected, with a reason that is safe to show. store_attachment calls
    it, and so does the preview of an agent's reply, so a file is refused for the same reasons at both."""
    if not data:
        raise AttachmentRejected("The file is empty.")
    if len(data) > settings.attachment_max_bytes:
        limit = settings.attachment_max_bytes // (1024 * 1024)
        raise AttachmentRejected(f"The file is larger than the {limit} MB limit.")
    name = clean_filename(filename)
    extension = name.rpartition(".")[2].lower() if "." in name else ""
    if extension in BLOCKED_EXTENSIONS:
        raise AttachmentRejected(f"Files ending .{extension} cannot be attached.")
    return name


def store_blob(session: Session, storage: Storage, data: bytes, content_type: Optional[str] = None) -> tuple[str, str]:
    """Stores the bytes under their content hash unless they are already there, holding the lock that the cleanup of a
    stored file also takes (`lock_blob`). Returns (content_hash, key). The one place bytes are written: an attachment, a file
    held for the Teams question and a file held for an agent's preview all go through it."""
    content_hash = hashlib.sha256(data).hexdigest()
    key = blob_key(content_hash)
    lock_blob(session, key)
    if not storage.exists(key):
        storage.put(key, data, content_type or detect_content_type(data))
    return content_hash, key


def store_attachment(
    session: Session,
    storage: Storage,
    *,
    ticket: Ticket,
    filename: str,
    data: bytes,
    message: Optional[Message] = None,
    uploaded_by: Optional[User] = None,
    inline: bool = False,
) -> Optional[Attachment]:
    """Checks the file and stores it on the ticket. Returns the Attachment, or None when an
    inline picture was too small to keep (a signature logo). Raises AttachmentRejected for a
    file that may not be stored. Adds the row to the session; the caller commits.

    The bytes are stored under their hash, so the same file added twice is one blob (one that
    ends up with no row, because the request failed afterwards, is harmless and costs a few
    bytes). An inline picture already on the same message is not added again: its row is returned."""
    name = check_file(filename, data)
    if message is not None and message.ticket_id != ticket.id:
        raise AttachmentRejected("That message belongs to another ticket.")

    content_type = detect_content_type(data)
    if inline and is_inline_type(content_type) and len(data) < settings.attachment_inline_min_bytes:
        return None

    content_hash = hashlib.sha256(data).hexdigest()
    if inline:
        # The same picture on the same message (or on the ticket itself) once; on another message it
        # is its own row, so each message can show it to whoever may see that message.
        existing = session.exec(
            select(Attachment).where(
                Attachment.ticket_id == ticket.id,
                Attachment.content_hash == content_hash,
                Attachment.is_inline,
                Attachment.message_id == (message.id if message is not None else None),
            )
        ).first()
        if existing is not None:
            return existing

    content_hash, key = store_blob(session, storage, data, content_type)

    attachment = Attachment(
        ticket_id=ticket.id,
        message_id=message.id if message is not None else None,
        filename=name,
        size=len(data),
        content_type=content_type,
        content_hash=content_hash,
        blob_key=key,
        is_inline=inline,
        uploaded_by_id=uploaded_by.id if uploaded_by is not None else None,
    )
    session.add(attachment)
    session.flush()
    return attachment


def attachments_of(session: Session, ticket: Ticket) -> list[Attachment]:
    return list(
        session.exec(select(Attachment).where(Attachment.ticket_id == ticket.id).order_by(Attachment.id)).all()
    )


def files_by_message(session: Session, user: User, levels: AccessContext, messages: list[Message]) -> dict[int, list[Attachment]]:
    """The files that came with each of these messages, by message id, for the agent ticket page: only the ones this
    person may download (app/visibility.may_view_attachment), so a link never ends on a refusal."""
    ids = [m.id for m in messages]
    files: dict[int, list[Attachment]] = {}
    if not ids:
        return files
    tickets: dict[int, Ticket] = {}
    for attachment in session.exec(select(Attachment).where(Attachment.message_id.in_(ids)).order_by(Attachment.id)).all():
        ticket = tickets.get(attachment.ticket_id) or session.get(Ticket, attachment.ticket_id)
        tickets[attachment.ticket_id] = ticket
        if ticket is not None and may_view_attachment(session, user, attachment, ticket, levels):
            files.setdefault(attachment.message_id, []).append(attachment)
    return files

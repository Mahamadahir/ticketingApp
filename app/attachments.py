"""Attachments: the one place a file is checked and stored (docs/plan.md, "Storage decisions").

Everything that adds a file to a ticket (an agent's upload, and later mail ingestion) calls
store_attachment, so the size limit, the type checks, the detection of what the file really is,
the skipping of tiny inline pictures and the deduplication are decided here and nowhere else.
Who may download a file is decided only in app/visibility.py (may_view_attachment); how it is
served is decided by serving_headers below, from the detected type alone.
"""

import hashlib
import re
from typing import Optional

from sqlmodel import Session, select

from app.config import settings
from app.models import Attachment, Message, Ticket, User
from app.storage import Storage, content_disposition

# The only types the browser may show in the page. Never SVG or HTML: both can carry script.
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Refused outright: files whose only use is to be run. Compared with the final extension.
BLOCKED_EXTENSIONS = frozenset(
    {"exe", "dll", "com", "scr", "msi", "bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "hta", "jar", "lnk"}
)

MAX_FILENAME_LENGTH = 200


class AttachmentRejected(ValueError):
    """The file may not be stored; the message says why and is safe to show."""


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

    The bytes are stored under their hash, so the same file added twice is one blob. An
    inline picture already on the ticket is not added again: its existing row is returned."""
    if not data:
        raise AttachmentRejected("The file is empty.")
    if len(data) > settings.attachment_max_bytes:
        limit = settings.attachment_max_bytes // (1024 * 1024)
        raise AttachmentRejected(f"The file is larger than the {limit} MB limit.")
    if message is not None and message.ticket_id != ticket.id:
        raise AttachmentRejected("That message belongs to another ticket.")

    name = clean_filename(filename)
    extension = name.rpartition(".")[2].lower() if "." in name else ""
    if extension in BLOCKED_EXTENSIONS:
        raise AttachmentRejected(f"Files ending .{extension} cannot be attached.")

    content_type = detect_content_type(data)
    if inline and is_inline_type(content_type) and len(data) < settings.attachment_inline_min_bytes:
        return None

    content_hash = hashlib.sha256(data).hexdigest()
    if inline:
        existing = session.exec(
            select(Attachment).where(
                Attachment.ticket_id == ticket.id, Attachment.content_hash == content_hash, Attachment.is_inline
            )
        ).first()
        if existing is not None:
            return existing

    key = blob_key(content_hash)
    if not storage.exists(key):
        storage.put(key, data, content_type)

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

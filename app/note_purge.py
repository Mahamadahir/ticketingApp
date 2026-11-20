"""The deleted-note purge (#255): the recurring job `note-purge` that removes deleted notes for good.

An agent deleting a note only sets `Message.deleted_at` (and `deleted_by_id`); the note, its files and everything else stay in
place and hidden, so Undo can bring it back. This job removes a note once it was deleted at least "Keep deleted notes for"
days ago (`note_keep_deleted_days`, default 1), worked out from `deleted_at` and the setting, never from when the job last ran,
so a late run, a skipped day and a second run after a crash all end in the same place (the same rule as the notification purge,
#239). Removing a note means:

- its attachment rows are deleted, and each stored file no row uses any more is queued in `pending_blob_delete` (identical
  files share one blob, so a file another attachment, held Teams file or held reply file still uses is kept) and then deleted
  from storage right away; a storage failure leaves the entry for the `blob-cleanup` job to retry, and never fails this one;
- the notifications that point at the note (an @mention that was read, or withdrawn when it was deleted) lose that pointer, so
  the rows stay for the notification purge to delete on its own schedule;
- the message row is deleted.

Done in batches, with the rows locked with SKIP LOCKED so two replicas never take the same note.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.anonymisation import purge_pending_blobs
from app.attachments import keys_in_use
from app.models import Attachment, Message, Notification, PendingBlobDelete
from app.settings_store import NOTE_KEEP_DELETED_DAYS, count_setting
from app.storage import Storage

log = logging.getLogger("app.worker")

BATCH = 200
MAX_BATCHES = 100  # at most 20,000 a run; a bigger backlog is finished by the next run


def keep_days(session: Session) -> int:
    return count_setting(session, NOTE_KEEP_DELETED_DAYS)


def purge_deleted_notes(session: Session, now: Optional[datetime] = None, storage: Optional[Storage] = None) -> int:
    """Removes every deleted note that is due, with its files. Returns how many notes. The stored files are deleted after
    the rows are committed (see above), so this commits."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days(session))
    total = 0
    keys: set[str] = set()
    for _ in range(MAX_BATCHES):
        ids = list(
            session.exec(
                select(Message.id)
                .where(Message.deleted_at.is_not(None), Message.deleted_at <= cutoff)
                .order_by(Message.id).limit(BATCH).with_for_update(skip_locked=True)
            ).all()
        )
        if not ids:
            break
        files = session.exec(select(Attachment).where(Attachment.message_id.in_(ids))).all()
        batch_keys = {row.blob_key for row in files}
        for row in files:
            session.delete(row)
        session.flush()
        for key in batch_keys - keys_in_use(session, batch_keys):
            keys.add(key)
            if session.get(PendingBlobDelete, key) is None:
                session.add(PendingBlobDelete(blob_key=key))
        session.exec(update(Notification).where(Notification.message_id.in_(ids)).values(message_id=None))
        session.exec(delete(Message).where(Message.id.in_(ids)))
        session.flush()
        total += len(ids)
    session.commit()
    if keys:
        try:
            if storage is None:
                from app.storage import get_storage

                storage = get_storage()
            purge_pending_blobs(session, storage, keys)
            session.commit()
        except Exception as error:  # storage down or not set up: the blob-cleanup job retries the queued entries
            session.rollback()
            log.warning("note-purge could not delete stored files now (%s); blob-cleanup will retry", type(error).__name__)
    if total:
        log.info("note-purge deleted=%d", total)
    return total

"""Files held while the Teams bot asks which request a message is for (phase 8F, #193).

A message that needs the choice card (app/teams_intake.py) may come with files. The address Teams gives for a file is
short-lived and there is no ticket to put it on yet, so the files are downloaded once, stored under their content hash (the
key an attachment would have) and recorded as `TeamsHeldFile` rows against the pending choice (the `TeamsIntake` row whose
outcome is `asked`). Then:

- the person picks a request (or "a new request"): `take_files` reads them back as `IncomingAttachment`s, so
  `intake.store_files` checks and attaches them exactly as it does for any file; the stored bytes are already there, so
  nothing is uploaded again. `release` removes the held rows.
- the person never answers: the recurring job `teams-pending` (`expire_pending`) ends choices older than the setting
  `teams_pending_choice_hours`, removes the held rows, and queues each stored file nothing else uses on the blob delete queue
  (`PendingBlobDelete`, the queue anonymisation uses; the `blob-cleanup` job deletes them from storage).
- the requester is anonymised: `app/anonymisation.py` calls `release_for_tickets` for the tickets in question.

Nothing here commits; the caller does.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.attachments import keys_in_use, store_blob
from app.intake import IncomingAttachment
from app.models import PendingBlobDelete, TeamsHeldFile, TeamsIntake
from app.settings_store import TEAMS_PENDING_CHOICE_HOURS, count_setting
from app.storage import Storage, get_storage

log = logging.getLogger("app.teams_bot")

ASKED = "asked"  # the outcome of a TeamsIntake row whose question is waiting (also set by app/teams_intake.py)
EXPIRED = "expired"


def hold_files(session: Session, storage: Optional[Storage], row: TeamsIntake, files: list[IncomingAttachment]) -> int:
    """Stores the files that were fetched and records every file (a skipped one with the reason) against the pending choice.
    Returns how many are held. The row must have an id."""
    storage = storage or (get_storage() if any(item.data is not None for item in files) else None)
    held = 0
    for item in files:
        if item.data is None:
            session.add(TeamsHeldFile(intake_id=row.id, filename=item.filename, skipped=item.skipped or "could not be downloaded"))
            continue
        _, key = store_blob(session, storage, item.data)
        session.add(TeamsHeldFile(intake_id=row.id, filename=item.filename, size=len(item.data), blob_key=key))
        held += 1
    return held


def held_rows(session: Session, intake_id: int) -> list[TeamsHeldFile]:
    return list(session.exec(select(TeamsHeldFile).where(TeamsHeldFile.intake_id == intake_id).order_by(TeamsHeldFile.id)).all())


def take_files(session: Session, storage: Optional[Storage], intake_id: int) -> list[IncomingAttachment]:
    """The held files as the files of a message, read back from storage (a file that cannot be read becomes a skipped one,
    named on the ticket). They are not removed: call `release` once they have been attached."""
    rows = held_rows(session, intake_id)
    storage = storage or (get_storage() if any(r.blob_key for r in rows) else None)
    files = []
    for held in rows:
        if held.blob_key is None:
            files.append(IncomingAttachment(held.filename, skipped=held.skipped or "could not be downloaded"))
            continue
        try:
            files.append(IncomingAttachment(held.filename, data=storage.read(held.blob_key)))
        except Exception as error:  # a storage outage, or the file is gone
            log.warning("Teams intake: a held file could not be read (%s)", type(error).__name__)
            files.append(IncomingAttachment(held.filename, skipped="could not be read back"))
    return files


def release(session: Session, rows: list[TeamsHeldFile]) -> int:
    """Removes held rows. Each stored file that no attachment and no other held row uses is queued for deletion (the files
    that were just attached are in use, so they stay). Returns how many files were queued."""
    keys = {r.blob_key for r in rows if r.blob_key}
    for row in rows:
        session.delete(row)
    session.flush()
    if not keys:
        return 0
    queued = 0
    for key in keys - keys_in_use(session, keys):
        if session.get(PendingBlobDelete, key) is None:
            session.add(PendingBlobDelete(blob_key=key))
            queued += 1
    return queued


def release_for_tickets(session: Session, ticket_ids: list[int]) -> int:
    """Drops the held files of the pending choices that name these tickets (anonymisation of their requester). Returns how
    many held rows went."""
    if not ticket_ids:
        return 0
    rows = list(
        session.exec(
            select(TeamsHeldFile).join(TeamsIntake, TeamsIntake.id == TeamsHeldFile.intake_id).where(TeamsIntake.ticket_id.in_(ticket_ids))
        ).all()
    )
    release(session, rows)
    return len(rows)


def expire_pending(session: Session, now: Optional[datetime] = None) -> int:
    """The recurring job: ends every question that has waited longer than `teams_pending_choice_hours` (its row stays, with
    outcome `expired`, so Teams sending the message again is still recognised) and releases its held files. Returns how
    many questions expired."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=count_setting(session, TEAMS_PENDING_CHOICE_HOURS))
    old = session.exec(select(TeamsIntake).where(TeamsIntake.outcome == ASKED, TeamsIntake.created_at < cutoff).with_for_update(skip_locked=True)).all()
    for row in old:
        release(session, held_rows(session, row.id))
        row.outcome = EXPIRED
        session.add(row)
    return len(old)

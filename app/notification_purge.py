"""The notification purge (#239): the recurring job `notification-purge` that deletes notifications for good.

Two rules, both worked out from the rows (`dismissed_at`, `created_at`) and the two settings, never from when the job last ran,
so a late run, a skipped day and a second run after a crash all end in the same place:

- dismissed more than "Keep dismissed notifications for" days ago (default 1): the Undo window is over;
- created more than "Delete notifications after" days ago (default 180), read, unread or dismissed.

A notification's Teams post (`TeamsPost.notification_id`, a foreign key) is deleted with it, whether it was sent or not: it has
no use once the notification is gone, and without that the delete would fail. Done in batches so a large backlog does not
make one huge statement or hold many locks; the rows are locked with SKIP LOCKED so the worker that hands notifications to
Teams is never waited for (a locked row is simply taken at the next run). The runner commits when this returns.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, or_
from sqlmodel import Session, select

from app.models import Notification, TeamsPost
from app.settings_store import NOTIFICATION_DELETE_AFTER_DAYS, NOTIFICATION_KEEP_DISMISSED_DAYS, count_setting

log = logging.getLogger("app.worker")

BATCH = 500
MAX_BATCHES = 200  # at most 100,000 a run; a bigger backlog is finished by the next run


def retention_days(session: Session) -> tuple[int, int]:
    """(keep dismissed, delete after) in days. A stored pair the settings page would refuse (the age limit shorter than the
    dismissed window) is read with the age limit raised to the window, so it can never delete more than the person was told."""
    keep = count_setting(session, NOTIFICATION_KEEP_DISMISSED_DAYS)
    return keep, max(keep, count_setting(session, NOTIFICATION_DELETE_AFTER_DAYS))


def due_for_purge(session: Session, now: datetime):
    """The SQL condition for the notifications that are due to be deleted at `now`."""
    keep, delete_after = retention_days(session)
    return or_(
        Notification.dismissed_at <= now - timedelta(days=keep),
        Notification.created_at <= now - timedelta(days=delete_after),
    )


def purge_notifications(session: Session, now: Optional[datetime] = None) -> int:
    """Deletes every notification that is due, with its Teams post. Returns how many. Does not commit."""
    now = now or datetime.now(timezone.utc)
    due = due_for_purge(session, now)
    total = 0
    for _ in range(MAX_BATCHES):
        ids = list(
            session.exec(
                select(Notification.id).where(due).order_by(Notification.id).limit(BATCH).with_for_update(skip_locked=True)
            ).all()
        )
        if not ids:
            break
        session.exec(delete(TeamsPost).where(TeamsPost.notification_id.in_(ids)))
        session.exec(delete(Notification).where(Notification.id.in_(ids)))
        session.flush()
        total += len(ids)
    if total:
        log.info("notification-purge deleted=%d", total)
    return total

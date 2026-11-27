"""Retrying the files of an email that was taken without them (#348).

`app/inbound.py` takes a mail without its files after two failed reads of them, and writes an `AttachmentRetry` row. The
`inbound-attachments` job (`attachment_retries_job`) reads the files again, on the notification's backoff (1, 2, 4 ... 60 minutes), until
MAX_ATTEMPTS tries have failed in all, counted from the first failure. On success the files are stored on the same message through
`intake.store_files` (the same rules as a mail that arrived whole: size, the inline minimum, the max-files setting, dedupe) and an agent-only
event says so. After the last failure an event says the files could not be read, so the agent knows to ask for them. Nobody is emailed.
A row whose ticket was anonymised meanwhile is dropped quietly: its files are not kept for a person who asked to be forgotten.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.config import settings
from app.graph_client import graph_client
from app.inbound import (
    ADDED_TEXT, BATCH, AttachmentsUnreadable, EVENT_ATTACHMENTS_ADDED, EVENT_ATTACHMENTS_FAILED, FAILED_TEXT, MailSource, backoff_until, load_attachments,
)
from app.intake import store_files
from app.mail import describe_failure
from app.models import AttachmentRetry, Message, Ticket
from app.person_data import is_placeholder_address
from app.run_summary import RunSummary
from app.storage import Storage
from app.teams import MAX_ATTEMPTS
from app.ticket_service import record_event

log = logging.getLogger("app.worker")

NONE_FOUND_TEXT = "No attachments were found on the email after all"


def attachment_retries_job(session: Session) -> RunSummary:
    """The registered job: `retry_attachments`, reported as a summary (retried, added, gave_up). Does nothing unless inbound mail is on."""
    if not (settings.inbound_enabled and settings.graph_configured and settings.graph_shared_mailbox):
        return RunSummary.of(retried=0, added=0, gave_up=0)
    return retry_attachments(session)


def retry_attachments(
    session: Session, client: Optional[MailSource] = None, storage: Optional[Storage] = None, now: Optional[datetime] = None
) -> RunSummary:
    """One run: every retry that is due, oldest first, committing after each. `retried` is the reads attempted, `added` the mails whose
    files were stored, `gave_up` those that failed for the last time."""
    client = client or graph_client
    now = now or datetime.now(timezone.utc)
    summary = RunSummary.of(retried=0, added=0, gave_up=0)
    due = session.exec(
        select(AttachmentRetry.id)
        .where(AttachmentRetry.next_attempt_at <= now, AttachmentRetry.attempts < MAX_ATTEMPTS)
        .order_by(AttachmentRetry.id)
        .limit(BATCH)
    ).all()
    for retry_id in due:
        retry = session.exec(select(AttachmentRetry).where(AttachmentRetry.id == retry_id).with_for_update(skip_locked=True)).first()
        if retry is None:
            continue  # another worker has it, or it was just finished
        ticket = session.get(Ticket, retry.ticket_id)
        message = session.get(Message, retry.message_id)
        if ticket is None or message is None or is_placeholder_address(ticket.requester_email):
            session.delete(retry)
            session.commit()
            continue
        summary.counts["retried"] += 1
        try:
            with session.begin_nested():
                files = load_attachments(session, client, retry.graph_message_id)
                store_files(session, storage, ticket, message, files)
                record_event(session, ticket, EVENT_ATTACHMENTS_ADDED, None, detail=ADDED_TEXT if files else NONE_FOUND_TEXT)
        except Exception as error:
            reason = error.reason if isinstance(error, AttachmentsUnreadable) else describe_failure(error)
            log.warning("Attachments of mail %s still not readable (try %s): %s", retry.id, retry.attempts + 1, reason)
            retry.attempts += 1
            retry.last_error = reason
            if retry.attempts >= MAX_ATTEMPTS:
                record_event(session, ticket, EVENT_ATTACHMENTS_FAILED, None, detail=FAILED_TEXT)
                session.delete(retry)
                summary.counts["gave_up"] += 1
                if ticket.id not in summary.ticket_ids:
                    summary.ticket_ids.append(ticket.id)
            else:
                retry.next_attempt_at = backoff_until(retry.attempts, now)
                session.add(retry)
        else:
            session.delete(retry)
            summary.counts["added"] += 1
            if ticket.id not in summary.ticket_ids:
                summary.ticket_ids.append(ticket.id)
        session.commit()
    return summary

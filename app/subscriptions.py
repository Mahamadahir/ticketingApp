"""Keeping mail flowing from the shared mailbox (phase 3C).

Two jobs in the worker, both safe to run late, twice, or from two processes at once:

1. The subscription (`keep_subscription_alive`). Microsoft Graph tells the public app about new mail
   through a change-notification subscription on the mailbox's inbox. A mail subscription lives at most
   about 7 days, so the worker creates it when it is missing, renews it before it runs low, and
   recreates it when Microsoft lost it, removed it or asks for it (the lifecycle notifications the
   webhook stores in `GraphNotification.lifecycle_event`). One row in `GraphSubscription` per watched
   resource, locked while it is looked at (`FOR UPDATE SKIP LOCKED`, so a second worker skips instead of
   waiting), and Graph itself is asked what exists before anything is created, so a restart, a
   second replica or a lost database row never leaves two live subscriptions.

2. The catch-up (`catch_up`). A webhook can be missed (the public app was down, Microsoft gave up after
   its retries). A delta query on the inbox returns every message that arrived or changed since the
   last run; the `deltaLink` Graph hands back is stored in `MailSyncState` so the next run starts there.
   The first run looks back MAIL_BACKFILL_DAYS. The ids it finds go to the *mail intake*.

The seam with inbound mail (3B): `MailIntake` is any function `(session, message_ids, source) -> count of
newly handed over`. The default, `record_for_processing`, writes a `GraphNotification` row (the same
row the webhook writes) for each id it has not seen before, so inbound processing needs no extra code
to pick up what catch-up found. It dedupes on `resource_id`, which is also what makes a catch-up run
that repeats itself, or finds mail the webhook already delivered, hand nothing over twice. Whoever
processes those rows must skip the ones with a `lifecycle_event`: this module owns them.

Nothing here logs or stores a token or the clientState (only a short fingerprint of it).
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import httpx
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Session, select

from app.config import settings
from app.models import GraphNotification, GraphSubscription, MailSyncState
from app.settings_store import (
    GRAPH_RENEW_MARGIN_MINUTES,
    GRAPH_SUBSCRIPTION_MINUTES,
    MAIL_BACKFILL_DAYS,
    REGISTRY,
    get_int_setting,
)

logger = logging.getLogger(__name__)

SUBSCRIPTION_EVERY_SECONDS = 5 * 60
CATCH_UP_EVERY_SECONDS = 5 * 60
MAX_LIFETIME_MINUTES = 10070  # Graph's limit for a mail subscription is just under 7 days (10080)
RENEW_GUARD = timedelta(minutes=10)  # a lifecycle "please renew" is ignored if we renewed this recently
MAX_PAGES_PER_RUN = 40  # 50 ids a page: a long outage is worked off over several runs, not one huge one
LIFECYCLE_BATCH = 500

REAUTHORIZATION_REQUIRED = "reauthorizationRequired"
SUBSCRIPTION_REMOVED = "subscriptionRemoved"
MISSED = "missed"


class SubscriptionError(Exception):
    """A subscription or catch-up step failed. The text is safe to log and show: a status and a Graph
    error code, never a token, an address or a link."""


class MailIntake(Protocol):
    """Takes Graph message ids the catch-up found and makes sure inbound processing will see them.
    Returns how many were new. Must be idempotent: the same id again hands over nothing. Does not commit."""

    def __call__(self, session: Session, message_ids: list[str], source: str) -> int: ...


def record_for_processing(session: Session, message_ids: list[str], source: str) -> int:
    """The default intake: one `GraphNotification` row per id not seen before, exactly as the webhook
    would have stored it (so 3B's worker job fetches it like any notified message)."""
    unique = list(dict.fromkeys(message_ids))
    if not unique:
        return 0
    known = set(session.exec(select(GraphNotification.resource_id).where(GraphNotification.resource_id.in_(unique))).all())
    new = [message_id for message_id in unique if message_id not in known]
    for message_id in new:
        session.add(
            GraphNotification(
                change_type="created",
                resource=f"users/{settings.graph_shared_mailbox}/messages/{message_id}",
                resource_id=message_id,
            )
        )
    return len(new)


def _utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_graph_time(value: object) -> Optional[datetime]:
    """Graph's "2026-10-27T10:00:00.0000000Z" (seven fractional digits, which Python rejects)."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return _utc(parsed)


def describe_failure(error: Exception) -> str:
    """Why a Graph call failed, without its address (httpx puts it in its messages)."""
    if isinstance(error, httpx.HTTPStatusError):
        code = ""
        try:
            code = str(error.response.json().get("error", {}).get("code") or "")[:80]
        except Exception:
            pass
        return f"Graph answered {error.response.status_code}" + (f" ({code})" if code else "")
    return type(error).__name__


def _status_of(error: Exception) -> Optional[int]:
    return error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None


def _graph(graph=None):
    if graph is not None:
        return graph
    from app.graph_client import graph_client  # resolved now, so tests can swap the client

    return graph_client


def _minutes_setting(session: Session, key: str) -> int:
    value = get_int_setting(session, key)
    return value if value else int(REGISTRY[key].default)


def lifetime_minutes(session: Session) -> int:
    return min(_minutes_setting(session, GRAPH_SUBSCRIPTION_MINUTES), MAX_LIFETIME_MINUTES)


def renew_margin_minutes(session: Session) -> int:
    """Renew when less than this is left. Never more than half a lifetime, or every run would renew."""
    return min(_minutes_setting(session, GRAPH_RENEW_MARGIN_MINUTES), lifetime_minutes(session) // 2)


# ---- The subscription -----------------------------------------------------------------------------


def subscription_resource() -> str:
    return f"users/{settings.graph_shared_mailbox}/mailFolders('Inbox')/messages"


def client_state_fingerprint(client_state: str) -> str:
    return hashlib.sha256(client_state.encode()).hexdigest()[:16]


def _same(a: object, b: object) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip("/").lower() == b.strip("/").lower()


@dataclass
class Outcome:
    action: str  # off, busy, ok, created, renewed
    sync_wanted: bool = False  # a lifecycle notification said mail may have been missed


def _lock_row(session: Session, model, key_column: str, key: str):
    """The row for this resource, created if absent, locked until the transaction ends. None when
    another process holds it: that process is doing the work, so this one skips."""
    session.execute(
        postgresql_insert(model.__table__).values({key_column: key}).on_conflict_do_nothing(index_elements=[key_column])
    )
    return session.exec(
        select(model).where(getattr(model, key_column) == key).with_for_update(skip_locked=True)
    ).first()


def keep_subscription_alive(session: Session, graph=None, now: Optional[datetime] = None) -> Outcome:
    """Makes sure exactly one live subscription exists on the shared mailbox and that it will not run
    out soon. Commits when it creates one (so Graph's id is never lost); otherwise the worker commits.
    On failure records why on the row, then raises SubscriptionError."""
    if not (settings.graph_configured and settings.graph_shared_mailbox and settings.graph_notification_url):
        return Outcome("off")
    graph = _graph(graph)
    now = now or _now()
    row = _lock_row(session, GraphSubscription, "resource", subscription_resource())
    if row is None:
        return Outcome("busy")
    try:
        outcome = _reconcile(session, graph, row, now)
    except Exception as error:
        session.rollback()  # lifecycle notifications taken in this attempt go back to waiting
        message = describe_failure(error)
        # The rollback also undid a first-ever row (it was inserted in this transaction): take it again.
        failed = _lock_row(session, GraphSubscription, "resource", subscription_resource())
        if failed is not None:
            failed.last_error, failed.last_error_at, failed.last_checked_at = message, now, now
            session.add(failed)
        session.commit()
        logger.error("mail subscription: %s", message)
        raise SubscriptionError(message) from None
    row.last_checked_at, row.last_error, row.last_error_at = now, None, None
    session.add(row)
    return outcome


def _take_lifecycle_events(session: Session, row: GraphSubscription, now: datetime) -> tuple[bool, bool, bool]:
    """Marks the waiting lifecycle notifications handled and says what they ask for: (renew, recreate,
    sync). One from a subscription that is not the current one is stale and changes nothing."""
    events = session.exec(
        select(GraphNotification)
        .where(GraphNotification.lifecycle_event.is_not(None), GraphNotification.processed_at.is_(None))  # type: ignore[union-attr]
        .order_by(GraphNotification.id)
        .limit(LIFECYCLE_BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    renew = recreate = sync = False
    for event in events:
        event.processed_at = now
        session.add(event)
        if event.subscription_id and row.subscription_id and event.subscription_id != row.subscription_id:
            continue
        if event.lifecycle_event == REAUTHORIZATION_REQUIRED:
            renew = True
        elif event.lifecycle_event == SUBSCRIPTION_REMOVED:
            recreate = sync = True
        elif event.lifecycle_event == MISSED:
            sync = True
    return renew, recreate, sync


def _reconcile(session: Session, graph, row: GraphSubscription, now: datetime) -> Outcome:
    url = settings.graph_notification_url
    renew_asked, recreate, sync = _take_lifecycle_events(session, row, now)
    fingerprint = client_state_fingerprint(settings.graph_webhook_client_state)

    # Ours are the subscriptions Graph holds for this resource and this address. Anything else
    # (another environment's, another resource) is none of our business and is never touched.
    ours = [
        s for s in graph.list_subscriptions()
        if _same(s.get("resource"), row.resource) and _same(s.get("notificationUrl"), url) and s.get("id")
    ]
    mine = next((s for s in ours if s["id"] == row.subscription_id), None)
    deleted: set[str] = set()

    if mine is not None and (recreate or row.client_state_fingerprint != fingerprint):
        # Removed notice, or the clientState changed (it cannot be changed on a live subscription).
        graph.delete_subscription(mine["id"])
        deleted.add(mine["id"])
        mine = None

    action = "ok"
    if mine is None:
        _create(session, graph, row, now, fingerprint)
        action = "created"
    else:
        row.expires_at = parse_graph_time(mine.get("expirationDateTime")) or _utc(row.expires_at)
        remaining = (row.expires_at - now) if row.expires_at else timedelta(0)
        low = remaining < timedelta(minutes=renew_margin_minutes(session))
        recently = row.last_renewed_at is not None and now - _utc(row.last_renewed_at) < RENEW_GUARD
        if low or (renew_asked and not recently):
            try:
                _renew(session, graph, row, now)
                action = "renewed"
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
                _create(session, graph, row, now, fingerprint)  # gone since we listed it
                action = "created"

    # Duplicates: anything of ours at Graph that is not the one we now keep.
    for stray in ours:
        if stray["id"] != row.subscription_id and stray["id"] not in deleted:
            graph.delete_subscription(stray["id"])
    return Outcome(action, sync_wanted=sync)


def _create(session: Session, graph, row: GraphSubscription, now: datetime, fingerprint: str) -> None:
    url = settings.graph_notification_url
    wanted = now + timedelta(minutes=lifetime_minutes(session))
    created = graph.create_subscription(
        resource=row.resource,
        notification_url=url,
        lifecycle_url=url,
        client_state=settings.graph_webhook_client_state,
        expires_at=wanted,
    )
    row.subscription_id = created["id"]
    row.expires_at = parse_graph_time(created.get("expirationDateTime")) or wanted
    row.client_state_fingerprint = fingerprint
    row.last_renewed_at = now
    session.add(row)
    session.commit()  # Graph now holds it: record its id before anything else can fail


def _renew(session: Session, graph, row: GraphSubscription, now: datetime) -> None:
    wanted = now + timedelta(minutes=lifetime_minutes(session))
    renewed = graph.renew_subscription(row.subscription_id, wanted)
    row.expires_at = parse_graph_time(renewed.get("expirationDateTime")) or wanted
    row.last_renewed_at = now
    session.add(row)


# ---- The catch-up ---------------------------------------------------------------------------------


def mail_folder() -> str:
    return f"users/{settings.graph_shared_mailbox}/mailFolders/inbox"


def catch_up(session: Session, graph=None, intake: Optional[MailIntake] = None, now: Optional[datetime] = None) -> int:
    """One catch-up run: asks Graph for the inbox messages that changed since the stored deltaLink
    (or, the first time, in the last MAIL_BACKFILL_DAYS), hands their ids to the intake, and stores the
    new link. Returns how many ids were new. The stored link moves only together with the ids handed
    over, in one transaction (the worker's commit), so a failure re-reads rather than loses. Does not
    commit. A run that hits MAX_PAGES_PER_RUN stores where it got to and carries on next time."""
    if not (settings.graph_configured and settings.graph_shared_mailbox):
        return 0
    graph = _graph(graph)
    intake = intake or record_for_processing
    now = now or _now()
    state = _lock_row(session, MailSyncState, "folder", mail_folder())
    if state is None:
        return 0  # another run has it
    try:
        handed = _sync_pages(session, graph, intake, state, now)
    except Exception as error:
        session.rollback()
        message = describe_failure(error)
        failed = _lock_row(session, MailSyncState, "folder", mail_folder())
        if failed is not None:
            failed.last_error, failed.last_error_at = message, now
            session.add(failed)
        session.commit()
        logger.error("mail catch-up: %s", message)
        raise SubscriptionError(message) from None
    return handed


def _sync_pages(session: Session, graph, intake: MailIntake, state: MailSyncState, now: datetime) -> int:
    since = now - timedelta(days=_minutes_setting(session, MAIL_BACKFILL_DAYS))
    link = state.resume_link or state.delta_link
    handed = 0
    restarted = False
    for _ in range(MAX_PAGES_PER_RUN):
        try:
            page = graph.inbox_delta_page(link, since if link is None else None)
        except httpx.HTTPStatusError as error:
            # 410: Graph no longer remembers our place. Start again from the backfill window; the
            # intake's dedupe means nothing already collected is collected again.
            if error.response.status_code != 410 or link is None or restarted:
                raise
            state.delta_link = state.resume_link = link = None
            restarted = True
            continue
        ids = [
            item["id"] for item in page.get("value", [])
            if isinstance(item, dict) and item.get("id") and "@removed" not in item
        ]
        handed += intake(session, ids, "catch-up")
        next_link, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
        if next_link:
            state.resume_link = link = next_link
            continue
        if not delta_link:
            raise SubscriptionError("Graph returned neither a next page nor a delta link")
        state.delta_link, state.resume_link = delta_link, None
        state.last_synced_at, state.last_error, state.last_error_at = now, None, None
        state.last_handed_over = handed
        session.add(state)
        return handed
    session.add(state)  # out of pages for this run: the resume link is kept
    return handed


# ---- Health ---------------------------------------------------------------------------------------


def health_warnings(session: Session, now: Optional[datetime] = None) -> list[str]:
    """What /health reports about mail intake. Empty when nothing is wrong or nothing has been set up
    (no subscription row means the worker has never managed one)."""
    now = now or _now()
    warnings = []
    row = session.exec(select(GraphSubscription).where(GraphSubscription.resource == subscription_resource())).first()
    if row is not None:
        expires = _utc(row.expires_at)
        if row.subscription_id is None or expires is None:
            warnings.append("Mail subscription does not exist yet")
        elif expires <= now:
            warnings.append("Mail subscription has expired")
        elif expires - now < timedelta(minutes=renew_margin_minutes(session)):
            warnings.append("Mail subscription expires soon and has not been renewed")
        if row.last_error:
            warnings.append("Mail subscription: the last check failed")
    sync = session.exec(select(MailSyncState).where(MailSyncState.folder == mail_folder())).first()
    if sync is not None and sync.last_error:
        warnings.append("Mail catch-up: the last run failed")
    return warnings

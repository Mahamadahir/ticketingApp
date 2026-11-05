"""UK bank holidays from gov.uk (issue #151). A recurring job (every six hours, so a failed fetch is tried again the same day) (`run_bank_holiday_fetch`, in app/scheduled_jobs.py)
fetches https://www.gov.uk/bank-holidays.json and keeps the dates of the chosen region (the `bank_holiday_region`
setting) in the `bankholidaycache` table. Everything that works out business days reads `cached_holidays` and
never the network. The typed list (`msp_bank_holidays`, for extra closure days such as the Christmas shutdown) stays;
`app.msp._holidays` joins the two.

A failed fetch (network, a bad status, a body that is not the expected shape) keeps the last good list, is logged, and
never empties anything. The job only fails, so it shows red on Admin > Recurring jobs, once the last good fetch is
more than `bank_holiday_fail_days` old (or there has never been one: with no list to fall back on a failure is
visible at once). Nothing here commits.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlmodel import Session

from app.audit import log_action
from app.models import BankHolidayCache
from app.settings_store import BANK_HOLIDAY_FAIL_DAYS, BANK_HOLIDAY_REGION, REGISTRY, get_int_setting, get_setting

log = logging.getLogger("app.bank_holidays")

URL = "https://www.gov.uk/bank-holidays.json"
TIMEOUT_SECONDS = 10.0
REGIONS = ("england-and-wales", "scotland", "northern-ireland")
DEFAULT_REGION = "england-and-wales"
AUDIT_ACTOR = "worker"
AUDIT_ENTITY = "bank_holidays"


class FetchError(Exception):
    """gov.uk could not be read, or did not answer in the shape this expects."""


def region_setting(session: Session) -> str:
    value = get_setting(session, BANK_HOLIDAY_REGION)
    return value if value in REGIONS else DEFAULT_REGION


def parse_feed(payload: object, region: str) -> list[date]:
    """The dates of one region from gov.uk's JSON ({region: {"events": [{"date": "2026-12-25", ...}]}}). An empty or
    malformed answer raises FetchError, so it can never replace a good list with nothing."""
    try:
        events = payload[region]["events"]  # type: ignore[index]
        days = sorted({date.fromisoformat(event["date"]) for event in events})
    except (KeyError, TypeError, ValueError) as error:
        raise FetchError(f"the answer has no usable dates for {region}: {error!r}") from error
    if not days:
        raise FetchError(f"the answer lists no dates for {region}")
    return days


def fetch_feed(region: str) -> list[date]:
    """One request to gov.uk (httpx with a timeout, like the other outbound calls)."""
    try:
        response = httpx.get(URL, timeout=TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise FetchError(f"{type(error).__name__}: {error}") from error
    return parse_feed(payload, region)


def cached_holidays(session: Session, region: Optional[str] = None) -> frozenset[date]:
    """The cached gov.uk dates for the region (the setting's by default); none until the first good fetch. A stored
    row that cannot be read is treated as none rather than stopping the business-day maths."""
    row = session.get(BankHolidayCache, region or region_setting(session))
    if row is None or not row.dates:
        return frozenset()
    try:
        return frozenset(date.fromisoformat(part) for part in row.dates.split(",") if part)
    except ValueError:
        return frozenset()


def refresh_bank_holidays(session: Session, now: Optional[datetime] = None) -> bool:
    """Fetches and stores the region's dates. Returns True when the list was refreshed. On a failure the last good list
    stays; raises FetchError only when that list is older than the setting (or there is none)."""
    now = now or datetime.now(timezone.utc)
    region = region_setting(session)
    row = session.get(BankHolidayCache, region)
    try:
        days = fetch_feed(region)
    except FetchError as error:
        log.warning("Bank holiday fetch failed for %s, keeping the last good list: %s", region, error)
        limit = get_int_setting(session, BANK_HOLIDAY_FAIL_DAYS)
        limit = limit if limit is not None and limit >= 1 else int(REGISTRY[BANK_HOLIDAY_FAIL_DAYS].default)
        fetched_at = row.fetched_at if row is not None else None
        if fetched_at is not None and fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if fetched_at is None or now - fetched_at > timedelta(days=limit):
            since = "ever" if fetched_at is None else f"since {fetched_at:%Y-%m-%d}"
            raise FetchError(f"Bank holidays have not been fetched from gov.uk {since} ({error})") from error
        return False
    text = ",".join(day.isoformat() for day in days)
    if row is None:
        log_action(session, AUDIT_ACTOR, AUDIT_ENTITY, 0, "updated", f"{region}: {len(days)} dates")
        session.add(BankHolidayCache(region=region, dates=text, fetched_at=now))
    else:
        if row.dates != text:
            log_action(session, AUDIT_ACTOR, AUDIT_ENTITY, 0, "updated", f"{region}: {len(days)} dates")
        row.dates, row.fetched_at = text, now
        session.add(row)
    return True


def run_bank_holiday_fetch(session: Session) -> None:
    """The recurring job (daily, registered in app/scheduled_jobs.py)."""
    refresh_bank_holidays(session)

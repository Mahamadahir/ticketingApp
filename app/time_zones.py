"""Per-person time zones. Everything is stored and calculated in UTC; only what a person SEES is converted,
and only here.

Where a person's zone comes from (each is an IANA name, validated with zoneinfo; an unknown name is ignored):
  1. Their account (`User.time_zone`): reported by their browser on every page (`set_browser_zone`, so it
     follows them as they travel) or chosen by hand on their settings page (`set_manual_zone`, which the
     browser then stops overwriting until `clear_manual_zone`).
  2. For a requester who only writes by email: their Outlook mailbox time zone read through Graph
     (`RequesterTimeZone`, filled by `refresh_requester_zones`, run by the worker). Graph returns a Windows
     name ("GMT Standard Time"); `windows_to_iana` maps it with the CLDR table in app/windows_zones.py.
     A requester without an account is stored by lower-cased address, like Ticket.requester_email.
  3. Everyone else: the `default_time_zone` setting.

Display goes through ONE pair of functions, `format_moment` and `format_date`, and the Jinja filters
`local_time` and `local_date` in app/templating.py, which use the viewer's zone. No template formats a time
itself. An email to a requester uses `format_moment` with `zone_for_email`.

The next feature (a Due date, issue #121) turns a date into a deadline with `end_of_day_utc` (a zone name) or
`end_of_day_for_email` / `end_of_day_for_user`: the end of day D in that person's zone, as UTC.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Callable, NamedTuple, Optional
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Session, select

from app.models import RequesterTimeZone, User
from app.settings_store import (
    DEFAULT_TIME_ZONE,
    REGISTRY,
    SCHEDULED_MORNING_HOUR,
    TIME_ZONE_REFRESH_DAYS,
    TIME_ZONE_RETRY_HOURS,
    count_setting,
    get_setting,
)
from app.windows_zones import WINDOWS_TO_IANA

logger = logging.getLogger(__name__)

SOURCE_BROWSER = "browser"
SOURCE_MANUAL = "manual"
SOURCE_MAILBOX = "mailbox"

LOOKUPS_PER_RUN = 50  # a first ticket from many new senders is spread over a few runs, not one long one


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def zone_choices() -> list[str]:
    """Every zone name the settings page offers, sorted."""
    return sorted(_known_zones())


def valid_zone(name: Optional[str]) -> Optional[str]:
    """The name if it is a real IANA time zone, else None. Checked against the installed tz database
    rather than by constructing a ZoneInfo, which also accepts odd paths."""
    name = (name or "").strip()
    return name if name in _known_zones() else None


def windows_to_iana(name: Optional[str]) -> Optional[str]:
    """A Windows (or already IANA) time zone name as an IANA name, or None if it is not one we know.
    Graph also says "Customized Time Zone" for a custom zone: that is not mappable and gives None."""
    name = (name or "").strip()
    if not name:
        return None
    return valid_zone(WINDOWS_TO_IANA.get(name)) or valid_zone(name)


def default_zone(session: Session) -> str:
    """The organisation default; an invalid stored value falls back to the placeholder."""
    return valid_zone(get_setting(session, DEFAULT_TIME_ZONE)) or REGISTRY[DEFAULT_TIME_ZONE].default


# ---- Whose zone ----------------------------------------------------------------------------------------


def _mailbox_zone(session: Session, email: str) -> Optional[str]:
    row = session.get(RequesterTimeZone, email.strip().lower())
    return valid_zone(row.time_zone) if row is not None else None


def zone_for_user(session: Session, user: User) -> str:
    """A signed-in person's zone: their own, else the one read from their mailbox, else the default."""
    return valid_zone(user.time_zone) or _mailbox_zone(session, user.email) or default_zone(session)


def zone_for_email(session: Session, email: str) -> str:
    """The zone for an email address: the account's own, else the mailbox reading, else the default.
    What an email to a requester is written in."""
    address = email.strip().lower()
    account = session.exec(select(User).where(func.lower(User.email) == address)).first()
    if account is not None and valid_zone(account.time_zone):
        return account.time_zone
    return _mailbox_zone(session, address) or default_zone(session)


# ---- Display and conversion ----------------------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _year_if_not_now(local: datetime, zone_name: str, now: Optional[datetime]) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone_name))
    return local.year != current.year


def format_moment(moment: datetime, zone_name: str, *, seconds: bool = False, now: Optional[datetime] = None) -> str:
    """A moment in that zone with the zone shown: "25 Oct 17:00 GMT" (the year is added when it is not this
    year, "25 Oct 2025 17:00 GMT"). A naive datetime is read as UTC, which is how everything is stored."""
    zone_name = valid_zone(zone_name) or "UTC"
    local = _aware(moment).astimezone(ZoneInfo(zone_name))
    day = f"{local.day} {local:%b}" + (f" {local.year}" if _year_if_not_now(local, zone_name, now) else "")
    clock = f"{local:%H:%M:%S}" if seconds else f"{local:%H:%M}"
    return f"{day} {clock} {local:%Z}"


def format_date(moment: datetime, zone_name: str, *, now: Optional[datetime] = None) -> str:
    """Only the date, for where the time does not matter: "25 Oct" (with the year when it is not this year).
    The date is the one in that zone, so near midnight it can differ from the UTC date."""
    zone_name = valid_zone(zone_name) or "UTC"
    local = _aware(moment).astimezone(ZoneInfo(zone_name))
    return f"{local.day} {local:%b}" + (f" {local.year}" if _year_if_not_now(local, zone_name, now) else "")


def end_of_day_utc(day: date, zone_name: str) -> datetime:
    """The moment day D ends in that zone, as UTC: the first instant of D+1 there. A deadline of "the end of
    D" is therefore reached at this instant (compare with `<`). Handles daylight saving, so the day may be 23
    or 25 hours long. An unknown zone name is read as UTC."""
    zone = ZoneInfo(valid_zone(zone_name) or "UTC")
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)


def morning_of(day: date, zone_name: str, hour: int) -> datetime:
    """The moment the morning of day D starts in that zone (the given hour), as UTC. The Scheduled job
    (app/due_today.py) and the check made when Scheduled is set (app/required_by.py) both use it."""
    zone = ZoneInfo(valid_zone(zone_name) or "UTC")
    return datetime.combine(day, time(hour), tzinfo=zone).astimezone(timezone.utc)


def parse_dates(text: str) -> list[date]:
    """Dates written 2026-12-25, separated by commas, semicolons or spaces. Raises ValueError on anything else."""
    return [date.fromisoformat(part) for part in re.split(r"[,;\s]+", text.strip()) if part] if text.strip() else []


def next_business_morning(after: datetime, zone_name: str, hour: int, holidays: frozenset[date] = frozenset()) -> datetime:
    """The first business morning (the given hour, Monday to Friday, not a listed holiday) after `after`, in that
    zone, as UTC. A moment before the hour on a business day is that day's morning; at or after it, the next one."""
    zone = ZoneInfo(valid_zone(zone_name) or "UTC")
    local = _aware(after).astimezone(zone)
    day = local.date() if local.time() < time(hour) else local.date() + timedelta(days=1)
    for _ in range(366):  # a year of holidays cannot all be business days
        if day.weekday() < 5 and day not in holidays:
            break
        day += timedelta(days=1)
    return morning_of(day, zone_name, hour)


def scheduled_morning_hour(session: Session) -> int:
    """The SCHEDULED_MORNING_HOUR setting as an hour, 0 to 23 (a bad stored value falls back to the default)."""
    hour = count_setting(session, SCHEDULED_MORNING_HOUR)
    return hour if 0 <= hour <= 23 else int(REGISTRY[SCHEDULED_MORNING_HOUR].default)


def end_of_day_for_user(session: Session, day: date, user: User) -> datetime:
    return end_of_day_utc(day, zone_for_user(session, user))


def end_of_day_for_email(session: Session, day: date, email: str) -> datetime:
    """"End of day D in this requester's zone, as UTC" for whoever wrote in from that address."""
    return end_of_day_utc(day, zone_for_email(session, email))


# ---- A person's own zone (account) ---------------------------------------------------------------------


def set_browser_zone(user: User, name: Optional[str]) -> bool:
    """Stores the zone the browser reports. Ignored while the person has chosen one by hand, and when the
    name is not a real zone. Returns True when the stored zone changed. The caller adds and commits."""
    zone = valid_zone(name)
    if zone is None or user.time_zone_source == SOURCE_MANUAL:
        return False
    if user.time_zone == zone and user.time_zone_source == SOURCE_BROWSER:
        return False
    user.time_zone = zone
    user.time_zone_source = SOURCE_BROWSER
    return True


def set_manual_zone(user: User, name: Optional[str]) -> bool:
    """A zone chosen by hand: wins over the browser until it is cleared. False (nothing changed) when the
    name is not a real zone."""
    zone = valid_zone(name)
    if zone is None:
        return False
    user.time_zone = zone
    user.time_zone_source = SOURCE_MANUAL
    return True


def clear_manual_zone(user: User) -> None:
    """Gives control back to the browser: the next page load reports and stores its zone again."""
    if user.time_zone_source == SOURCE_MANUAL:
        user.time_zone = None
        user.time_zone_source = None


# ---- A requester's zone from their mailbox -------------------------------------------------------------


def note_requester(session: Session, email: str, now: Optional[datetime] = None) -> None:
    """Remembers that a requester exists so the worker reads their mailbox time zone soon. Called when a
    ticket is created; costs one insert the first time an address is seen and nothing afterwards. Safe to
    call from two places at once."""
    session.exec(
        postgresql_insert(RequesterTimeZone)
        .values(email=email.strip().lower(), next_check_at=now or datetime.now(timezone.utc))
        .on_conflict_do_nothing(index_elements=["email"])
    )


class RefreshResult(NamedTuple):
    looked_at: int  # requesters whose check was due
    failed: int  # of those, how many raised (not "Graph does not know them", which is a normal answer)
    first_error: str  # what the first failure was, for the log


def refresh_requester_zones(
    session: Session,
    read_zone: Optional[Callable[[str], Optional[str]]] = None,
    now: Optional[datetime] = None,
) -> RefreshResult:
    """Reads the mailbox time zone of every requester whose check is due (a new one, or one older than the
    refresh setting) and stores it. One person's failure does not stop the others; it is retried after the
    retry setting, and until then their earlier reading, or the default, is used. No row is locked while
    Graph is asked: there is one worker, and two readings of the same mailbox would store the same value.
    Does not commit."""
    from app.graph_client import graph_client  # resolved now, so tests can swap the function

    read_zone = read_zone or graph_client.mailbox_time_zone
    now = now or datetime.now(timezone.utc)
    refresh = timedelta(days=count_setting(session, TIME_ZONE_REFRESH_DAYS))
    retry = timedelta(hours=count_setting(session, TIME_ZONE_RETRY_HOURS))
    due = session.exec(
        select(RequesterTimeZone)
        .where(RequesterTimeZone.next_check_at <= now)
        .order_by(RequesterTimeZone.next_check_at)
        .limit(LOOKUPS_PER_RUN)
    ).all()
    failed, first_error = 0, ""
    for row in due:
        zone = None
        try:
            zone = windows_to_iana(read_zone(row.email))
        except Exception as error:
            failed += 1
            status = getattr(getattr(error, "response", None), "status_code", None)
            first_error = first_error or f"{type(error).__name__}{f' {status}' if status else ''}"
            logger.warning("time zone: could not read a mailbox time zone (%s)", first_error)
        row.checked_at = now
        if zone is not None:
            row.time_zone, row.source = zone, SOURCE_MAILBOX
            row.next_check_at = now + refresh
        else:
            # Not known to Graph, a zone we cannot map, or a failure: keep any earlier reading and ask again later.
            row.next_check_at = now + retry
        session.add(row)
    return RefreshResult(len(due), failed, first_error)

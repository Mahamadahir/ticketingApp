"""What every report shares (issue #173, Phase 7): the date range in the viewer's zone, the filters, the
visibility limit, the table a report returns, the CSV writer and the audit entry for a download.

A report is a function `(session, user, levels, filters, now) -> Report` registered in `app/reports.py`;
the page, the CSV link, the audit entry and the visibility limit all come from here, so a new report (satisfaction,
MSP, Major Incidents, Problems: #174) is one function and one registry line.

Who may see which tickets is decided only by `app.visibility.visible_ticket_condition`: `ticket_conditions`
applies it to every report, so a team lead's figures cover the teams they can see and a super admin's cover all.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select as sa_select
from sqlmodel import Session

from app.audit import log_action
from app.models import Category, Team, Ticket, TicketSource, TicketType, User, UserRole
from app.settings_store import REPORT_MAX_DAYS, count_setting
from app.time_zones import end_of_day_utc, zone_for_user
from app.visibility import AccessContext, visible_ticket_condition

DEFAULT_RANGE_DAYS = 30
PRIORITIES = ("p1", "p2", "p3", "p4")
AUDIT_ENTITY = "report"
AUDIT_DOWNLOAD = "report_csv_download"
# Spreadsheet programs run a cell that starts with one of these as a formula (CSV injection). Tab and
# carriage return are on the usual lists too: some programs skip them and read the formula after.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
CSV_BOM = "﻿"  # so Excel reads the file as UTF-8


class ReportInputError(Exception):
    """A filter or date that cannot be used; the message is shown to the viewer."""


@dataclass
class Report:
    """One table, for the page and for the CSV. Numbers stay numbers (times in minutes, rates in percent);
    None is an empty cell. `notes` are (label, value) lines kept with the table: what was left out, what the
    figures mean."""

    title: str
    columns: list[str]
    rows: list[list]
    notes: list[tuple[str, str]] = field(default_factory=list)
    description: str = ""


@dataclass
class ReportFilters:
    from_day: date
    to_day: date
    zone: str
    start: datetime  # the first instant of from_day in the viewer's zone, as UTC
    end: datetime  # the first instant after to_day there; the range is [start, end)
    team_id: Optional[int] = None
    ticket_type: Optional[TicketType] = None
    category_id: Optional[int] = None
    priority: Optional[str] = None
    source: Optional[TicketSource] = None
    uses_range: bool = True  # False for a report that is a picture of now (backlog age): no dates in links or audit

    def as_params(self) -> dict[str, str]:
        """The filters as query parameters, so the CSV link and the audit entry carry exactly what the page used."""
        params = {"from": self.from_day.isoformat(), "to": self.to_day.isoformat()} if self.uses_range else {}
        if self.team_id is not None:
            params["team"] = str(self.team_id)
        if self.ticket_type is not None:
            params["type"] = self.ticket_type.value
        if self.category_id is not None:
            params["category"] = str(self.category_id)
        if self.priority:
            params["priority"] = self.priority
        if self.source is not None:
            params["source"] = self.source.value
        return params

    def describe(self) -> str:
        text = ", ".join(f"{key}={value}" for key, value in self.as_params().items())
        return f"{text} ({self.zone})" if text else f"({self.zone})"


def _optional_int(text: str, label: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit()) or len(text) > 9:
        raise ReportInputError(f"{label} is not valid")
    return int(text)


def _day(text: str, label: str) -> Optional[date]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ReportInputError(f"{label} must be a date written 2026-11-30")


def _enum(enum_class, text: str, label: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return enum_class(text)
    except ValueError:
        raise ReportInputError(f"{label} is not valid")


def parse_filters(
    session: Session, user: User, params: Mapping[str, str], now: Optional[datetime] = None, uses_range: bool = True
) -> ReportFilters:
    """The filters from the query string. The range is whole days in the viewer's own zone, default the last 30
    days up to today; it is capped by the setting `report_max_days`. A report that does not use the range
    (`uses_range` False) ignores any dates given. Raises ReportInputError."""
    now = now or datetime.now(timezone.utc)
    zone = zone_for_user(session, user)
    today = now.astimezone(ZoneInfo(zone)).date()
    if not uses_range:
        params = {key: value for key, value in params.items() if key not in ("from", "to")}
    to_day = _day(params.get("to", ""), "To") or today
    from_day = _day(params.get("from", ""), "From") or to_day - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if from_day > to_day:
        raise ReportInputError("The From date is after the To date")
    longest = count_setting(session, REPORT_MAX_DAYS)
    if (to_day - from_day).days + 1 > longest:
        raise ReportInputError(f"The date range can be at most {longest} days")
    priority = (params.get("priority") or "").strip().lower()
    if priority and priority not in PRIORITIES:
        raise ReportInputError("Priority is not valid")
    try:
        start, end = end_of_day_utc(from_day - timedelta(days=1), zone), end_of_day_utc(to_day, zone)
    except OverflowError:  # a date at the very edge of what a date can hold
        raise ReportInputError("The date is out of range")
    return ReportFilters(
        from_day=from_day,
        to_day=to_day,
        zone=zone,
        start=start,
        end=end,
        uses_range=uses_range,
        team_id=_optional_int(params.get("team", ""), "Team"),
        ticket_type=_enum(TicketType, params.get("type", ""), "Type"),
        category_id=_optional_int(params.get("category", ""), "Category"),
        priority=priority or None,
        source=_enum(TicketSource, params.get("source", ""), "Source"),
    )


def ticket_conditions(user: User, levels: AccessContext, filters: ReportFilters) -> list:
    """The SQL conditions on Ticket every report starts from: the tickets this person can see (the one
    rule, app/visibility.py), then the filters. The date range is not here: each report says which
    moment it measures (created, resolved, now)."""
    visible = visible_ticket_condition(user, levels)
    return ([visible] if visible is not None else []) + filter_conditions(filters)


def filter_conditions(filters: ReportFilters) -> list:
    """Only the filters (team, type, category, priority, source) as conditions on Ticket, without the visibility
    limit: for a report that has its own rule for who may read its rows (satisfaction: `feedback_condition`)."""
    conditions = []
    if filters.team_id is not None:
        conditions.append(Ticket.team_id == filters.team_id)
    if filters.ticket_type is not None:
        conditions.append(Ticket.type == filters.ticket_type)
    if filters.category_id is not None:
        # A main category covers its sub-categories.
        conditions.append(
            Ticket.category_id.in_(
                sa_select(Category.id).where(or_(Category.id == filters.category_id, Category.parent_id == filters.category_id))
            )
        )
    if filters.priority:
        conditions.append(Ticket.priority == filters.priority)
    if filters.source is not None:
        conditions.append(Ticket.source == filters.source)
    return conditions


def team_choices(session: Session, user: User, levels: AccessContext) -> list[Team]:
    """The teams offered in the Team filter: all of them for a super admin, otherwise the ones the person can see."""
    query = sa_select(Team).order_by(Team.name)
    if user.role != UserRole.SUPER_ADMIN:
        query = query.where(Team.id.in_(list(levels)))
    return list(session.scalars(query).all())


def category_choices(session: Session) -> list[tuple[int, str]]:
    """(id, label) for the Category filter: main categories and their sub-categories, labelled Parent / Child."""
    categories = list(session.scalars(sa_select(Category).order_by(Category.name)).all())
    names = {c.id: c.name for c in categories}
    return sorted(
        ((c.id, f"{names[c.parent_id]} / {c.name}" if c.parent_id in names else c.name) for c in categories),
        key=lambda pair: pair[1].lower(),
    )


# ---- CSV -------------------------------------------------------------------------------------------------


def csv_cell(value):
    """One cell. Text that a spreadsheet would read as a formula gets a leading apostrophe; numbers are written
    as they are (a negative number stays a number); None is empty."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS):
        return "'" + value
    return value


def csv_text(report: Report, filters: Optional[ReportFilters] = None) -> str:
    """The report as CSV text with a byte-order mark: the header row, the rows, then (after a blank row) the
    notes and the filters used."""
    out = io.StringIO()
    out.write(CSV_BOM)
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([csv_cell(c) for c in report.columns])
    for row in report.rows:
        writer.writerow([csv_cell(c) for c in row])
    extra = list(report.notes)
    if filters is not None:
        extra.append(("Filters", filters.describe()))
    if extra:
        writer.writerow([])
        for label, value in extra:
            writer.writerow([csv_cell(label), csv_cell(value)])
    return out.getvalue()


def audit_download(session: Session, user: User, report_key: str, filters: ReportFilters) -> None:
    """Records who downloaded which report with which filters. Does not commit: the route commits, so the
    entry and the response go together. Entity id 0 (a report is not a row)."""
    log_action(session, user.email, AUDIT_ENTITY, 0, AUDIT_DOWNLOAD, f"{report_key}: {filters.describe()}")


# ---- Small helpers the reports share ------------------------------------------------------------------


def priority_label(priority: Optional[str]) -> str:
    return priority.upper() if priority else "None"


def priority_order(priority: Optional[str]) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def percent(part: int, whole: int) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None

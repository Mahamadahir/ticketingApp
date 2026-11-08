"""The Phase 7A-i reports (issue #173): volume, SLA compliance, response and resolution times, backlog age and
reopen rate. Built on the ticket columns and `ticket_event`, never the audit log. Every figure is worked out in
SQL (grouped, counted, averaged there); Python only labels, orders and adds the totals row.

Each report is a function `(session, user, levels, filters, now) -> Report` (app/report_kit.py) and is listed in
REPORTS, which drives the menu, the page and the CSV link. #174 adds its reports as more entries.

What is measured and left out (stated on each page):
- Volume counts tickets created in the range, backdated ones included.
- SLA compliance, times: tickets created in the range. Backdated tickets (their dates are entered, not measured) and
  tickets without an SLA are left out and counted in the notes.
- Times are elapsed, not working time: the SLA clock is 24/7 (app/sla.py). Resolution time leaves out the time
  spent in Awaiting user, as the clock does; first response is plain elapsed time (the ticket stores only the total
  paused time, not when it was paused).
- Backlog age is a picture of now and ignores the date range.
- Reopen rate: tickets resolved in the range, and how many of them were reopened afterwards.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, literal, or_, select as sa_select
from sqlalchemy.orm import aliased
from sqlmodel import Session

from app.models import Category, FINISHED_STATUSES, Team, Ticket, TicketEvent, TicketStatus, User
from app.report_kit import (
    Report,
    ReportFilters,
    percent,
    priority_label,
    priority_order,
    ticket_conditions,
)
from app.reopening import EVENT_RECLOSED, EVENT_REOPENED
from app.ticket_service import EVENT_STATUS_CHANGED
from app.visibility import AccessContext

NO_TEAM = "(no team)"
NO_CATEGORY = "(not categorised)"


def _type_label(value) -> str:
    return getattr(value, "value", str(value)).replace("_", " ").capitalize()


def _created_in_range(filters: ReportFilters):
    return [Ticket.created_at >= filters.start, Ticket.created_at < filters.end]


def _team_priority_rows(rows) -> list:
    """Sorts rows that start (team name, priority) by team then priority."""
    return sorted(rows, key=lambda row: ((row[0] or "~"), priority_order(row[1])))


# ---- 1. Volume ---------------------------------------------------------------------------------------------


def volume(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    where = [*ticket_conditions(user, levels, filters), *_created_in_range(filters)]
    parent = aliased(Category)
    category_label = case(
        (Category.id.is_(None), NO_CATEGORY),
        (parent.id.is_not(None), parent.name + " / " + Category.name),
        else_=Category.name,
    )
    breakdowns = [
        ("Category", category_label, [(Category, Ticket.category_id == Category.id), (parent, Category.parent_id == parent.id)]),
        ("Team", func.coalesce(Team.name, NO_TEAM), [(Team, Ticket.team_id == Team.id)]),
        ("Type", Ticket.type, []),
        ("Source", Ticket.source, []),
    ]
    rows: list[list] = []
    for name, label, joins in breakdowns:
        query = sa_select(label.label("label"), func.count().label("n")).select_from(Ticket)
        for target, on in joins:
            query = query.outerjoin(target, on)
        query = query.where(*where).group_by(label)
        found = [(_type_label(value) if name in ("Type", "Source") else value, n) for value, n in session.execute(query).all()]
        rows += [[name, value, n] for value, n in sorted(found, key=lambda pair: (-pair[1], str(pair[0])))]
    total = session.execute(sa_select(func.count()).select_from(Ticket).where(*where)).scalar_one()
    return Report(
        title="Volume",
        columns=["Breakdown", "Value", "Tickets"],
        rows=rows,
        notes=[("Tickets created in the range", str(total))],
        description="Tickets created in the date range, counted four ways (each breakdown adds up to the total). Backdated tickets are included.",
    )


# ---- 2. SLA compliance ---------------------------------------------------------------------------------------


def sla_compliance(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    base = [*ticket_conditions(user, levels, filters), *_created_in_range(filters)]
    tracked = [Ticket.is_backdated.is_(False)]
    paused = Ticket.sla_paused_at.is_not(None)
    # First response: met if an agent emailed by the deadline; breached if emailed late, or still not emailed
    # and the deadline has passed (a paused clock is not decided yet).
    fr_has = Ticket.first_response_due_at.is_not(None)
    fr_met = and_(fr_has, Ticket.first_responded_at.is_not(None), Ticket.first_responded_at <= Ticket.first_response_due_at)
    fr_breached = and_(
        fr_has,
        or_(
            and_(Ticket.first_responded_at.is_not(None), Ticket.first_responded_at > Ticket.first_response_due_at),
            and_(Ticket.first_responded_at.is_(None), Ticket.first_response_due_at < now, ~paused),
        ),
    )
    # Resolution: decided when finished (on time or late), or when open and already overdue.
    finished = Ticket.status.in_(FINISHED_STATUSES) & Ticket.resolved_at.is_not(None)
    res_has = Ticket.resolution_due_at.is_not(None)
    res_met = and_(res_has, finished, Ticket.resolved_at <= Ticket.resolution_due_at)
    res_breached = and_(
        res_has,
        or_(
            and_(finished, Ticket.resolved_at > Ticket.resolution_due_at),
            and_(~finished, Ticket.resolution_due_at < now, ~paused),
        ),
    )
    team_name = func.coalesce(Team.name, NO_TEAM)
    query = (
        sa_select(
            team_name, Ticket.priority,
            func.count().filter(fr_met), func.count().filter(fr_breached),
            func.count().filter(fr_has), func.count().filter(res_met),
            func.count().filter(res_breached), func.count().filter(res_has),
        )
        .select_from(Ticket).outerjoin(Team, Ticket.team_id == Team.id)
        .where(*base, *tracked).group_by(team_name, Ticket.priority)
    )
    rows, totals, undecided_fr, undecided_res = [], [0] * 4, 0, 0
    for name, priority, f_met, f_bad, f_all, r_met, r_bad, r_all in session.execute(query).all():
        if not f_all and not r_all:
            continue
        undecided_fr += f_all - f_met - f_bad
        undecided_res += r_all - r_met - r_bad
        for i, value in enumerate((f_met, f_bad, r_met, r_bad)):
            totals[i] += value
        rows.append([name, priority, f_met, f_bad, percent(f_met, f_met + f_bad), r_met, r_bad, percent(r_met, r_met + r_bad)])
    rows = [[n, priority_label(p), *rest] for n, p, *rest in _team_priority_rows(rows)]
    rows.append(["All", "All", totals[0], totals[1], percent(totals[0], totals[0] + totals[1]),
                 totals[2], totals[3], percent(totals[2], totals[2] + totals[3])])
    backdated = session.execute(
        sa_select(func.count()).select_from(Ticket).where(*base, Ticket.is_backdated.is_(True))
    ).scalar_one()
    no_sla = session.execute(
        sa_select(func.count()).select_from(Ticket).where(
            *base, *tracked, Ticket.first_response_due_at.is_(None), Ticket.resolution_due_at.is_(None)
        )
    ).scalar_one()
    return Report(
        title="SLA compliance",
        columns=["Team", "Priority", "First response met", "First response breached", "First response % met",
                 "Resolution met", "Resolution breached", "Resolution % met"],
        rows=rows,
        notes=[
            ("Left out: backdated tickets", str(backdated)),
            ("Left out: tickets without an SLA", str(no_sla)),
            ("First response not yet decided (clock running or paused)", str(undecided_fr)),
            ("Resolution not yet decided (clock running or paused)", str(undecided_res)),
        ],
        description=(
            "Tickets created in the range. Met means done by the deadline; breached means done late, or not done and already "
            "overdue; % met is met out of met plus breached. The Awaiting user pause is already in the deadlines. "
            "A ticket with a Due date is measured against it."
        ),
    )


# ---- 3. Mean and median times --------------------------------------------------------------------------------


def _minutes(seconds) -> Optional[float]:
    return round(float(seconds) / 60, 1) if seconds is not None else None


def times(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    base = [*ticket_conditions(user, levels, filters), *_created_in_range(filters), Ticket.is_backdated.is_(False)]
    first_seconds = func.extract("epoch", Ticket.first_responded_at - Ticket.created_at)
    # The clock stops in Awaiting user, so that wait is taken off a resolution time (as it is off the deadline).
    resolve_seconds = func.greatest(
        func.extract("epoch", Ticket.resolved_at - Ticket.created_at) - Ticket.sla_paused_seconds, 0
    )
    team_name = func.coalesce(Team.name, NO_TEAM)

    def stats(seconds, extra, grouped: bool):
        columns = [func.count(), func.avg(seconds), func.percentile_cont(0.5).within_group(seconds)]
        if grouped:
            query = sa_select(team_name, Ticket.priority, *columns).group_by(team_name, Ticket.priority)
        else:
            query = sa_select(literal("All"), literal("All"), *columns)
        query = query.select_from(Ticket).outerjoin(Team, Ticket.team_id == Team.id).where(*base, *extra)
        return {(name, priority): rest for name, priority, *rest in session.execute(query).all()}

    first_extra = [Ticket.first_responded_at.is_not(None)]
    resolve_extra = [Ticket.status.in_(FINISHED_STATUSES), Ticket.resolved_at.is_not(None)]
    first, resolve = stats(first_seconds, first_extra, True), stats(resolve_seconds, resolve_extra, True)
    first_all, resolve_all = stats(first_seconds, first_extra, False), stats(resolve_seconds, resolve_extra, False)

    def cells(found):
        count, mean, median = found or (0, None, None)
        return [count, _minutes(mean), _minutes(median)]

    keys = sorted(set(first) | set(resolve), key=lambda key: (key[0] or "~", priority_order(key[1])))
    rows = [[name, priority_label(priority), *cells(first.get((name, priority))), *cells(resolve.get((name, priority)))]
            for name, priority in keys]
    rows.append(["All", "All", *cells(first_all.get(("All", "All"))), *cells(resolve_all.get(("All", "All")))])
    backdated = session.execute(
        sa_select(func.count()).select_from(Ticket).where(
            *ticket_conditions(user, levels, filters), *_created_in_range(filters), Ticket.is_backdated.is_(True)
        )
    ).scalar_one()
    return Report(
        title="Response and resolution times",
        columns=["Team", "Priority", "First response: tickets", "First response: mean (minutes)", "First response: median (minutes)",
                 "Resolution: tickets", "Resolution: mean (minutes)", "Resolution: median (minutes)"],
        rows=rows,
        notes=[("Left out: backdated tickets", str(backdated))],
        description=(
            "Elapsed time, not working time: the SLA clock runs 24 hours a day, every day (it stops only in Awaiting user). "
            "Tickets created in the range. Resolution time leaves out time spent in Awaiting user; first response is the plain "
            "time from creation to the first email an agent sent. Only tickets that have had a first response, or are "
            "resolved or closed, are counted."
        ),
    )


# ---- 4. Backlog age ------------------------------------------------------------------------------------------

BACKLOG_BUCKETS = [("Under 1 day", None, 1), ("1 to 3 days", 1, 3), ("3 to 7 days", 3, 7), ("7 to 30 days", 7, 30), ("Over 30 days", 30, None)]


def backlog_age(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    open_now = [*ticket_conditions(user, levels, filters), ~Ticket.status.in_(FINISHED_STATUSES)]
    bucket_filters = []
    for _, low, high in BACKLOG_BUCKETS:
        conditions = []
        if low is not None:
            conditions.append(Ticket.created_at <= now - timedelta(days=low))
        if high is not None:
            conditions.append(Ticket.created_at > now - timedelta(days=high))
        bucket_filters.append(and_(*conditions))
    team_name = func.coalesce(Team.name, NO_TEAM)
    query = (
        sa_select(team_name, Ticket.priority, *[func.count().filter(f) for f in bucket_filters], func.count())
        .select_from(Ticket).outerjoin(Team, Ticket.team_id == Team.id)
        .where(*open_now).group_by(team_name, Ticket.priority)
    )
    found = _team_priority_rows(session.execute(query).all())
    rows = [[name, priority_label(priority), *counts] for name, priority, *counts in found]
    rows.append(["All", "All", *[sum(row[i] for row in rows) for i in range(2, 2 + len(BACKLOG_BUCKETS) + 1)]])
    return Report(
        title="Backlog age",
        columns=["Team", "Priority", *[label for label, _, _ in BACKLOG_BUCKETS], "Open tickets"],
        rows=rows,
        notes=[("As of", now.astimezone(ZoneInfo(filters.zone)).strftime("%Y-%m-%d %H:%M ") + filters.zone)],
        description=(
            "Tickets that are not Resolved or Closed right now, by how long ago they were created. The date range does not "
            "apply: this is a picture of today. Tickets waiting on the user or an approval count as open."
        ),
    )


# ---- 5. Reopen rate ------------------------------------------------------------------------------------------


def reopen_rate(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    # The tickets resolved in the range (once each), and when each was first resolved in it.
    resolved = (
        sa_select(TicketEvent.ticket_id.label("ticket_id"), func.min(TicketEvent.created_at).label("at"))
        .where(
            TicketEvent.event_type == EVENT_STATUS_CHANGED,
            TicketEvent.to_value == TicketStatus.RESOLVED.value,
            TicketEvent.created_at >= filters.start,
            TicketEvent.created_at < filters.end,
        )
        .group_by(TicketEvent.ticket_id)
        .subquery()
    )

    def later_event(event_type: str, to_value: Optional[str] = None):
        later = aliased(TicketEvent)
        conditions = [later.ticket_id == resolved.c.ticket_id, later.event_type == event_type, later.created_at >= resolved.c.at]
        if to_value is not None:
            conditions.append(later.to_value == to_value)
        return exists().where(*conditions)

    team_name = func.coalesce(Team.name, NO_TEAM)
    query = (
        sa_select(
            team_name, func.count(),
            func.count().filter(later_event(EVENT_REOPENED)),
            func.count().filter(later_event(EVENT_RECLOSED)),
            func.count().filter(later_event(EVENT_RECLOSED, "said_thanks")),
        )
        .select_from(resolved)
        .join(Ticket, Ticket.id == resolved.c.ticket_id)
        .outerjoin(Team, Ticket.team_id == Team.id)
        .where(*ticket_conditions(user, levels, filters))
        .group_by(team_name)
    )
    found = sorted(session.execute(query).all(), key=lambda row: row[0])
    rows = [[name, n, reopened, percent(reopened, n), reclosed, thanks] for name, n, reopened, reclosed, thanks in found]
    totals = [sum(row[i] for row in rows) for i in (1, 2, 4, 5)]
    rows.append(["All", totals[0], totals[1], percent(totals[1], totals[0]), totals[2], totals[3]])
    return Report(
        title="Reopen rate",
        columns=["Team", "Tickets resolved", "Reopened afterwards", "Reopen rate %", "Reclosed afterwards", "Reclosed: user said thanks"],
        rows=rows,
        description=(
            "Tickets resolved in the date range (counted once each), and how many were reopened after that, by a reply or by an "
            "agent. Reclosed is a reopened ticket closed again; 'user said thanks' is the reclose reason of that name. "
            "The priority filter applies; the rest are as on the other reports."
        ),
    )


# ---- The list ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportDef:
    key: str
    title: str
    summary: str
    build: Callable[[Session, User, AccessContext, ReportFilters, datetime], Report]
    uses_range: bool = True  # False: a picture of now (backlog age), so the page hides the dates


REPORTS: dict[str, ReportDef] = {
    d.key: d
    for d in (
        ReportDef("volume", "Volume", "Tickets created, by category, team, type and source.", volume),
        ReportDef("sla", "SLA compliance", "Share of first responses and resolutions on time, by team and priority.", sla_compliance),
        ReportDef("times", "Response and resolution times", "Mean and median time to first response and to resolution.", times),
        ReportDef("backlog", "Backlog age", "Open tickets now, by how long they have been open.", backlog_age, uses_range=False),
        ReportDef("reopen", "Reopen rate", "Resolved tickets that were reopened, and recloses where the user said thanks.", reopen_rate),
    )
}

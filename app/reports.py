"""The Phase 7A-i reports (issue #173): volume, SLA compliance, response and resolution times, backlog age and
reopen rate. Built on the ticket columns and `ticket_event`, never the audit log. Every figure is worked out in
SQL (grouped, counted, averaged there); Python only labels, orders and adds the totals row.

Each report is a function `(session, user, levels, filters, now) -> Report` (app/report_kit.py) and is listed in
REPORTS, which drives the menu, the page and the CSV link. #174 added satisfaction, MSP handoffs, Major Incidents and
incidents per Problem as more entries.

What is measured and left out (stated on each page):
- Volume counts tickets created in the range, backdated ones included.
- SLA compliance, times: tickets created in the range. Backdated tickets (their dates are entered, not measured) and
  tickets without an SLA are left out and counted in the notes.
- Times are elapsed, not working time: the SLA clock is 24/7 (app/sla.py). Resolution time leaves out the time
  spent in Awaiting user, as the clock does; first response is plain elapsed time (the ticket stores only the total
  paused time, not when it was paused).
- Backlog age is a picture of now and ignores the date range.
- Reopen rate: tickets resolved in the range, and how many of them were reopened afterwards.
- Satisfaction: feedback given in the range. Who may read a row is `feedback_condition` (app/visibility.py), not ticket
  visibility; the filters apply to the rated ticket. The requester is never shown.
- MSP handoffs: handoffs sent in the range (a `sent_to_msp` event each), and what became of each before the next one.
- Major Incidents: opened in the range. Problems: opened in the range, plus every Problem still open.
"""

from dataclasses import dataclass
import statistics
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, literal, or_, select as sa_select
from sqlalchemy.orm import aliased
from sqlmodel import Session

from app.models import (
    Category, FINISHED_STATUSES, Team, Ticket, TicketEvent, TicketFeedback, TicketStatus, TicketType, User,
)
from app.report_kit import (
    Report,
    ReportFilters,
    filter_conditions,
    percent,
    priority_label,
    priority_order,
    ticket_conditions,
)
from app.reopening import EVENT_RECLOSED, EVENT_REOPENED
from app.ticket_service import EVENT_STATUS_CHANGED
from app.msp import BY_AUTOMATIC, EVENT_MSP_ACKNOWLEDGED, EVENT_MSP_TOLD, EVENT_SENT_TO_MSP
from app.msp_actions import EVENT_TAKEN_BACK
from app.time_zones import format_moment
from app.visibility import AccessContext, feedback_condition, visible_ticket_condition

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
        ("Category", category_label, [Category.id], [(Category, Ticket.category_id == Category.id), (parent, Category.parent_id == parent.id)]),
        ("Team", func.coalesce(Team.name, NO_TEAM), [Team.id], [(Team, Ticket.team_id == Team.id)]),
        ("Type", Ticket.type, [], []),
        ("Source", Ticket.source, [], []),
    ]
    rows: list[list] = []
    for name, label, group_ids, joins in breakdowns:
        query = sa_select(label.label("label"), func.count().label("n")).select_from(Ticket)
        for target, on in joins:
            query = query.outerjoin(target, on)
        query = query.where(*where).group_by(label, *group_ids)  # by id too: two categories can share a label
        found = [(_type_label(value) if name in ("Type", "Source") else value, n) for value, n in session.execute(query).all()]
        rows += [[name, value, n] for value, n in sorted(found, key=lambda pair: (-pair[1], str(pair[0])))]
    total = sum(n for breakdown, _, n in rows if breakdown == "Type")
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
    # A Due date does not move for a pause (app/sla.py), so a paused ticket with one is not undecided.
    resolution_paused = and_(paused, Ticket.due_on.is_(None))
    finished = Ticket.status.in_(FINISHED_STATUSES) & Ticket.resolved_at.is_not(None)
    # Finished with no email to the requester (closed as a duplicate, resolved by phone): judged by when it
    # finished, so one closed before the deadline is not counted late afterwards.
    responded = func.coalesce(Ticket.first_responded_at, case((finished, Ticket.resolved_at)))
    # First response: met if an agent emailed by the deadline; breached if emailed late, or still not emailed
    # and the deadline has passed (a paused clock is not decided yet).
    fr_has = Ticket.first_response_due_at.is_not(None)
    fr_met = and_(fr_has, responded.is_not(None), responded <= Ticket.first_response_due_at)
    fr_breached = and_(
        fr_has,
        or_(
            and_(responded.is_not(None), responded > Ticket.first_response_due_at),
            and_(responded.is_(None), Ticket.first_response_due_at < now, ~paused),
        ),
    )
    # Resolution: decided when finished (on time or late), or when open and already overdue.
    res_has = Ticket.resolution_due_at.is_not(None)
    res_met = and_(res_has, finished, Ticket.resolved_at <= Ticket.resolution_due_at)
    res_breached = and_(
        res_has,
        or_(
            and_(finished, Ticket.resolved_at > Ticket.resolution_due_at),
            and_(~finished, Ticket.resolution_due_at < now, ~resolution_paused),
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
            "overdue; % met is met out of met plus breached. The Awaiting user pause is already in the deadlines (a Due date "
            "does not move for it, so a ticket past its Due date is breached even while paused). A ticket finished without "
            "an email to the requester is judged by when it finished."
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

    Part = aliased(Ticket)  # a split original is left out: its parts are counted, not the family twice
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
        .where(*ticket_conditions(user, levels, filters), ~exists().where(Part.split_from_id == Ticket.id))
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
            "Tickets resolved in the date range (counted once each; a ticket that was split is left out and its parts counted), and how many were reopened after that, by a reply or by an "
            "agent. Reclosed is a reopened ticket closed again; 'user said thanks' is the reclose reason of that name. "
            "The priority filter applies; the rest are as on the other reports."
        ),
    )


# ---- 6. Satisfaction -----------------------------------------------------------------------------------------

MAX_COMMENTS = 200
NO_AGENT = "(automatic resolve)"
STAR_COLUMNS = [f"{n} star{'s' if n > 1 else ''}" for n in range(1, 6)]


def satisfaction(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    # Feedback given in the range. Who may read a row is feedback_condition and only that (an agent: the tickets they
    # resolved; an admin: all, automatic resolves included); the ticket's visibility adds nothing, as on the feedback
    # pages. The filters narrow what is rated. The requester is never selected.
    where = [
        feedback_condition(user), *filter_conditions(filters),
        TicketFeedback.created_at >= filters.start, TicketFeedback.created_at < filters.end,
    ]
    parent = aliased(Category)
    category_label = case(
        (Category.id.is_(None), NO_CATEGORY),
        (parent.id.is_not(None), parent.name + " / " + Category.name),
        else_=Category.name,
    )
    stars = [func.count().filter(TicketFeedback.rating == n) for n in range(1, 6)]
    breakdowns = [
        ("Agent", func.coalesce(User.display_name, NO_AGENT), [User.id], [(User, TicketFeedback.resolved_by_id == User.id)]),
        ("Team", func.coalesce(Team.name, NO_TEAM), [Team.id], [(Team, Ticket.team_id == Team.id)]),
        ("Category", category_label, [Category.id], [(Category, Ticket.category_id == Category.id), (parent, Category.parent_id == parent.id)]),
    ]

    def figures(n, average, *counts) -> list:
        return [n, round(float(average), 2) if average is not None else None, *counts]

    def rated(*columns):
        return sa_select(*columns, func.count(), func.avg(TicketFeedback.rating), *stars).select_from(TicketFeedback).join(
            Ticket, Ticket.id == TicketFeedback.ticket_id
        )

    overall = session.execute(rated().where(*where)).one()
    rows: list[list] = [["Overall", "All", *figures(*overall), None]]
    for name, label, group_ids, joins in breakdowns:
        query = rated(label)
        for target, on in joins:
            query = query.outerjoin(target, on)
        query = query.where(*where).group_by(label, *group_ids)  # by id too: two categories can share a label
        found = sorted(session.execute(query).all(), key=lambda row: (-row[1], str(row[0])))
        rows += [[name, value, *figures(n, average, *counts), None] for value, n, average, *counts in found]
    # The comments the rule allows, newest first. A comment row is one rating (its own star column is 1).
    commented = (
        sa_select(Ticket, TicketFeedback.rating, TicketFeedback.comment)
        .select_from(TicketFeedback).join(Ticket, Ticket.id == TicketFeedback.ticket_id)
        .where(*where, TicketFeedback.comment != "")
        .order_by(TicketFeedback.created_at.desc(), TicketFeedback.id.desc())
        .limit(MAX_COMMENTS + 1)
    )
    found = session.execute(commented).all()
    for ticket, rating, comment in found[:MAX_COMMENTS]:
        rows.append(["Comment", ticket.reference, 1, float(rating), *[1 if n == rating else 0 for n in range(1, 6)], comment])
    notes = [("Ratings in the range", str(overall[0]))]
    if len(found) > MAX_COMMENTS:
        notes.append(("Comments", f"only the newest {MAX_COMMENTS} are listed"))
    return Report(
        title="Satisfaction",
        columns=["Breakdown", "Value", "Ratings", "Average rating", *STAR_COLUMNS, "Comment"],
        rows=rows,
        notes=notes,
        description=(
            "Ratings the requesters gave in the date range, from 1 to 5, as the average and the spread, overall and by agent "
            "(who resolved the ticket), team and category. The filters apply to the rated ticket. You see only the feedback "
            "you may already read: an agent sees the tickets they resolved themselves; a super admin sees everything, "
            "including tickets resolved automatically. The requester is never shown."
        ),
    )


# ---- 7. MSP handoffs -----------------------------------------------------------------------------------------


def msp_handoffs(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    sent = aliased(TicketEvent)

    def next_send():
        later = aliased(TicketEvent)
        return (
            sa_select(func.min(later.created_at))
            .where(later.ticket_id == sent.ticket_id, later.event_type == EVENT_SENT_TO_MSP, later.created_at > sent.created_at)
            .correlate(sent).scalar_subquery()
        )

    def first_after(event_type: str, *conditions):
        """When the first such event came for this handoff: at or after it was sent, before the next one."""
        event = aliased(TicketEvent)
        return (
            sa_select(func.min(event.created_at))
            .where(
                event.ticket_id == sent.ticket_id, event.event_type == event_type, event.created_at >= sent.created_at,
                or_(next_send().is_(None), event.created_at < next_send()),
                *[condition(event) for condition in conditions],
            )
            .correlate(sent).scalar_subquery()
        )

    team_name = func.coalesce(Team.name, NO_TEAM)
    handoffs = (
        sa_select(
            sent.id.label("id"), team_name.label("team"), (sent.to_value == BY_AUTOMATIC).label("automatic"),
            sent.created_at.label("sent_at"),
            first_after(EVENT_MSP_ACKNOWLEDGED).label("acked_at"),
            first_after(EVENT_TAKEN_BACK).label("taken_back_at"),
            # Released: the MSP was told it is no longer needed because IT resolved or closed it.
            first_after(EVENT_MSP_TOLD, lambda e: e.to_value.in_(("resolved", "closed"))).label("released_at"),
        )
        .select_from(sent).join(Ticket, Ticket.id == sent.ticket_id).outerjoin(Team, Ticket.team_id == Team.id)
        .where(
            sent.event_type == EVENT_SENT_TO_MSP, sent.created_at >= filters.start, sent.created_at < filters.end,
            *ticket_conditions(user, levels, filters),
        )
        .subquery()
    )
    ack_seconds = func.extract("epoch", handoffs.c.acked_at - handoffs.c.sent_at)
    columns = [
        func.count(), func.count().filter(handoffs.c.automatic), func.count().filter(~handoffs.c.automatic),
        func.count().filter(handoffs.c.acked_at.is_not(None)), func.percentile_cont(0.5).within_group(ack_seconds),
        func.count().filter(handoffs.c.taken_back_at.is_not(None)), func.count().filter(handoffs.c.released_at.is_not(None)),
    ]

    def cells(n, automatic, manual, acked, median, taken_back, released) -> list:
        return [n, automatic, manual, acked, percent(acked, n), _minutes(median), taken_back, released]

    found = session.execute(sa_select(handoffs.c.team, *columns).group_by(handoffs.c.team)).all()
    rows = [[name, *cells(*rest)] for name, *rest in sorted(found, key=lambda row: row[0])]
    total = session.execute(sa_select(*columns)).one()
    rows.append(["All", *cells(*total)])
    return Report(
        title="MSP handoffs",
        columns=["Team", "Sent to the MSP", "Automatic (18:00 handoff)", "Manual (Send to MSP)", "Acknowledged by the MSP",
                 "Acknowledged %", "Median time to acknowledgement (minutes)", "Taken back", "Released"],
        rows=rows,
        description=(
            "Handoffs sent in the date range, counted once per send (a ticket sent twice counts twice). Automatic is the "
            "scheduled handoff; manual is an agent using Send to MSP. Acknowledged, taken back and released count what happened "
            "to that handoff before the ticket was sent again: the MSP's acknowledgement email, IT taking the ticket back, "
            "and IT telling the MSP it is no longer needed because it was resolved or closed. Times are elapsed. The type, "
            "category, priority and source filters apply to the ticket."
        ),
    )


# ---- 8 and 9. Major Incidents and Problems --------------------------------------------------------------------


def _linked_counts(user: User, levels: AccessContext):
    """Per parent: how many tickets are linked to it that this viewer may see, and how many of those are Incidents.
    (Visibility as for a ticket's own linked list, app/linking.py: a viewer counts only what they could open.)"""
    visible = visible_ticket_condition(user, levels)
    return (
        sa_select(
            Ticket.linked_parent_id.label("parent_id"), func.count().label("n"),
            func.count().filter(Ticket.type == TicketType.INCIDENT).label("incidents"),
        )
        .where(Ticket.linked_parent_id.is_not(None), *([visible] if visible is not None else []))
        .group_by(Ticket.linked_parent_id)
        .subquery()
    )


def _parents(session: Session, user: User, levels: AccessContext, filters: ReportFilters, ticket_type: TicketType, extra):
    linked = _linked_counts(user, levels)
    query = (
        sa_select(Ticket, func.coalesce(Team.name, NO_TEAM), func.coalesce(linked.c.n, 0), func.coalesce(linked.c.incidents, 0))
        .select_from(Ticket).outerjoin(Team, Ticket.team_id == Team.id).outerjoin(linked, linked.c.parent_id == Ticket.id)
        .where(Ticket.type == ticket_type, *ticket_conditions(user, levels, filters), extra)
        .order_by(Ticket.created_at, Ticket.id)
    )
    return session.execute(query).all()


def major_incidents(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    found = _parents(
        session, user, levels, filters, TicketType.MAJOR_INCIDENT,
        and_(Ticket.created_at >= filters.start, Ticket.created_at < filters.end),
    )
    rows, minutes = [], []
    for ticket, team, linked, _ in found:
        done = ticket.status in FINISHED_STATUSES and ticket.resolved_at is not None
        duration = _minutes((ticket.resolved_at - ticket.created_at).total_seconds()) if done else None
        if duration is not None:
            minutes.append(duration)
        rows.append([
            ticket.reference, ticket.subject, team, _type_label(ticket.status),
            format_moment(ticket.created_at, filters.zone), format_moment(ticket.resolved_at, filters.zone) if done else None,
            duration, linked,
        ])
    return Report(
        title="Major Incidents",
        columns=["Reference", "Subject", "Team", "Status", "Opened", "Resolved", "Duration (minutes)", "Linked tickets"],
        rows=rows,
        notes=[
            ("Major Incidents opened in the range", str(len(rows))),
            ("Resolved or closed", str(len(minutes))),
            ("Mean duration (minutes)", str(round(statistics.fmean(minutes), 1)) if minutes else "none yet"),
            ("Median duration (minutes)", str(round(statistics.median(minutes), 1)) if minutes else "none yet"),
            ("Linked tickets in all", str(sum(row[-1] for row in rows))),
        ],
        description=(
            "Major Incidents created in the date range. Duration is elapsed time from opened to resolved (an open one has "
            "none yet). Linked tickets are those linked under it now that you may see. The filters apply to the Major Incident itself."
        ),
    )


def problems(session: Session, user: User, levels: AccessContext, filters: ReportFilters, now: datetime) -> Report:
    # Opened in the range, plus every Problem still open whenever it was opened: an old open Problem is the point.
    found = _parents(
        session, user, levels, filters, TicketType.PROBLEM,
        or_(and_(Ticket.created_at >= filters.start, Ticket.created_at < filters.end), ~Ticket.status.in_(FINISHED_STATUSES)),
    )
    # Open Problems first, then the most linked (the query already ordered them oldest first, and a sort is stable).
    found = sorted(found, key=lambda row: (row[0].status in FINISHED_STATUSES, -row[2]))
    rows = [
        [ticket.reference, ticket.subject, team, _type_label(ticket.status), format_moment(ticket.created_at, filters.zone), incidents, linked]
        for ticket, team, linked, incidents in found
    ]
    return Report(
        title="Incidents per Problem",
        columns=["Reference", "Subject", "Team", "Status", "Opened", "Linked incidents", "Linked tickets"],
        rows=rows,
        notes=[
            ("Problems listed", str(len(rows))),
            ("Open", str(sum(1 for ticket, *_ in found if ticket.status not in FINISHED_STATUSES))),
            ("Linked incidents in all", str(sum(row[5] for row in rows))),
        ],
        description=(
            "Problems created in the date range, and every Problem that is still open. Open ones come first, the most linked "
            "first. Linked incidents are the Incidents linked under the Problem now (that you may see); Linked tickets also "
            "counts other types. The filters apply to the Problem itself."
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
        ReportDef("satisfaction", "Satisfaction", "Ratings from requesters: average and spread by agent, team and category.", satisfaction),
        ReportDef("msp", "MSP handoffs", "Tickets sent to the MSP, automatic and manual, acknowledged, taken back, released.", msp_handoffs),
        ReportDef("major-incidents", "Major Incidents", "Major Incidents with how long they ran and how many tickets were linked.", major_incidents),
        ReportDef("problems", "Incidents per Problem", "Each Problem with the incidents linked to it, open Problems first.", problems),
    )
}

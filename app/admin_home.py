"""The admin home (9H, #212): what needs a look first, then one card per admin group with a couple of real counts and
quick links. Everything is limited to the admin pages the viewer may open (`navigation.allowed_admin_pages`): a line
or a count belongs to a page, and is left out when the viewer cannot open that page."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, func, select

from app import outage
from app.categories import CATEGORY_REVIEW_CONDITIONS
from app.config import settings
from app.models import (
    AuditLog,
    Category,
    OutboundEmail,
    PendingBlobDelete,
    RequestType,
    ScheduledJob,
    TeamsPost,
    Ticket,
    User,
)
from app.navigation import ADMIN_GROUPS, NavItem
from app.scheduled_jobs import SCHEDULED_JOBS, JobHealth, job_health, timing
from app.settings_store import JOB_LATE_MINUTES
from app.subscriptions import health_warnings
from app.teams import MAX_ATTEMPTS
from app.time_zones import format_moment

# A stored file whose delete has failed this many times is worth a look (the blob-cleanup job keeps retrying it).
BLOB_DELETE_FAILURES_TO_SHOW = 3


@dataclass(frozen=True)
class Attention:
    tone: str  # chip tone: red, amber, blue
    tag: str  # the chip's word
    text: str
    href: Optional[str] = None
    action: str = ""


@dataclass(frozen=True)
class Stat:
    value: str
    label: str


@dataclass(frozen=True)
class AreaCard:
    title: str
    stats: tuple = ()  # Stat
    links: tuple = ()  # NavItem


def _count(session: Session, model, *conditions) -> int:
    return session.exec(select(func.count()).select_from(model).where(*conditions)).one()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _jobs(session: Session, now: datetime) -> JobHealth:
    rows = session.exec(select(ScheduledJob)).all()
    return job_health(rows, now, timing(session, JOB_LATE_MINUTES), {job.name for job in SCHEDULED_JOBS})


def needs_attention(session: Session, allowed: frozenset[str], now: datetime, zone: str) -> list[Attention]:
    """The lines that apply right now, most serious first. Empty when nothing needs a look."""
    lines: list[Attention] = []
    if "outage" in allowed:
        state = outage.status(session)
        if state is not None:
            since = f" since {format_moment(state.since, zone, now=now)}" if state.since else ""
            by = f", switched on by {state.by}" if state.by else ""
            lines.append(Attention("red", "On", f"Outage mode is on{since}{by}.", "/admin/outage", "Open outage mode"))
    if "scheduled-jobs" in allowed:
        health = _jobs(session, now)
        jobs_page = "/admin/scheduled-jobs"
        if health.failing:
            names = ", ".join(health.failing)
            lines.append(Attention("red", "Failing", f"Recurring jobs failing: {names}.", jobs_page, "Open scheduled jobs"))
        if health.stuck:
            names = ", ".join(health.stuck)
            lines.append(Attention("red", "Stuck", f"Recurring jobs that did not finish: {names}.", jobs_page, "Open scheduled jobs"))
        if health.overdue:
            names = ", ".join(health.overdue)
            lines.append(Attention("amber", "Overdue", f"Recurring jobs overdue (is the worker running?): {names}.", jobs_page, "Open scheduled jobs"))
    if "outbound-mail" in allowed:
        unsent = OutboundEmail.sent_at.is_(None)
        given_up = _count(session, OutboundEmail, unsent, OutboundEmail.attempts >= MAX_ATTEMPTS)
        retrying = _count(session, OutboundEmail, unsent, OutboundEmail.attempts > 0, OutboundEmail.attempts < MAX_ATTEMPTS)
        outbox = "/admin/outbound-mail?waiting=true"
        if given_up:
            lines.append(Attention(
                "red", "Given up", f"{_plural(given_up, 'outbound email was', 'outbound emails were')} given up after {MAX_ATTEMPTS} tries.",
                outbox, "Open outbound mail",
            ))
        if retrying:
            lines.append(Attention(
                "amber", "Waiting", f"{_plural(retrying, 'outbound email is', 'outbound emails are')} waiting to retry after an error.",
                outbox, "Open outbound mail",
            ))
        # Teams posts have no page of their own; the line says what happened and the worker log has the reason.
        teams_given_up = _count(session, TeamsPost, TeamsPost.sent_at.is_(None), TeamsPost.attempts >= MAX_ATTEMPTS)
        if teams_given_up:
            lines.append(Attention(
                "red", "Given up", f"{_plural(teams_given_up, 'Teams post was', 'Teams posts were')} given up after {MAX_ATTEMPTS} tries.",
            ))
    if "mail-intake" in allowed:
        for warning in health_warnings(session, now):
            lines.append(Attention("amber", "Mail", f"{warning}.", "/admin/mail-intake", "Open mail intake"))
    if "data-protection" in allowed:
        failing = _count(session, PendingBlobDelete, PendingBlobDelete.attempts >= BLOB_DELETE_FAILURES_TO_SHOW)
        if failing:
            lines.append(Attention(
                "amber", "Failing",
                f"{_plural(failing, 'stored file', 'stored files')} could not be deleted after {BLOB_DELETE_FAILURES_TO_SHOW} or more tries.",
                "/admin/data-protection", "Open data protection",
            ))
    if "category-review" in allowed:
        review = _count(session, Ticket, *CATEGORY_REVIEW_CONDITIONS)
        if review:
            lines.append(Attention(
                "blue", "Review", _plural(review, 'resolved ticket triaged as "Other" needs', 'resolved tickets triaged as "Other" need') + " a proper category.",
                "/admin/category-review", "Review",
            ))
    return lines


def _start_of_today(now: datetime, zone: str) -> datetime:
    local = now.astimezone(ZoneInfo(zone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _stats(session: Session, page: str, now: datetime, zone: str) -> list[Stat]:
    """The counts a card shows for one admin page."""
    if page == "users":
        return [
            Stat(str(_count(session, User, User.is_active.is_(True))), "active users"),
            Stat(str(_count(session, User, User.is_active.is_(False))), "inactive"),
        ]
    if page == "categories":
        return [Stat(str(_count(session, Category, Category.is_active.is_(True))), "categories")]
    if page == "request-types":
        return [Stat(str(_count(session, RequestType, RequestType.is_active.is_(True))), "request types")]
    if page == "mail-intake":
        return [Stat("On" if settings.graph_configured and settings.graph_shared_mailbox else "Off", "mail intake")]
    if page == "outbound-mail":
        return [Stat(str(_count(session, OutboundEmail, OutboundEmail.sent_at.is_(None))), "emails waiting")]
    if page == "scheduled-jobs":
        health = _jobs(session, now)
        return [Stat(str(health.total - health.unhealthy), "jobs healthy"), Stat(str(health.unhealthy), "not healthy")]
    if page == "data-protection":
        return [Stat(str(_count(session, PendingBlobDelete)), "files waiting to be deleted")]
    if page == "audit-log":
        return [Stat(str(_count(session, AuditLog, AuditLog.created_at >= _start_of_today(now, zone))), "audit entries today")]
    return []


def area_cards(session: Session, allowed: frozenset[str], now: datetime, zone: str) -> list[AreaCard]:
    """One card per admin group the viewer has a page in: the counts of those pages, and a link to each."""
    cards = []
    for title, pages in ADMIN_GROUPS:
        mine = [page for page in pages if page.key in allowed]
        if not mine:
            continue
        stats = tuple(stat for page in mine for stat in _stats(session, page.key, now, zone))
        links = tuple(NavItem(page.key, page.label, page.href, "") for page in mine)
        cards.append(AreaCard(title, stats, links))
    return cards


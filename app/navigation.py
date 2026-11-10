"""The app shell's navigation (9A): the one definition of what each person sees in the sidebar (agents) or the top bar
(the portal), with the counts beside it. Who may see an item is decided here, from the role and the permissions in
app/permissions.py and app/visibility.py; a template only draws what it is given and never checks access itself. The
routes still enforce access on their own, so this is a convenience, not the gate."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs

from sqlalchemy import func
from sqlmodel import Session, select

from app.approvals import count_pending_for
from app.models import FINISHED_STATUSES, Ticket, User, UserRole
from app.msp import unmatched_query
from app.notifications import count_unread
from app.visibility import access_context, has_permission, visible_ticket_condition

AGENT_ROLES = (UserRole.SUPER_ADMIN, UserRole.AGENT)

# Icon paths (24x24, stroke), drawn by _icon.html.
ICONS = {
    "queue": "M3 6h18M3 12h18M3 18h12",
    "mine": "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21c1.5-4 4.5-6 8-6s6.5 2 8 6",
    "triage": "M4 4h16v6H4zM4 14h10v6H4z",
    "approvals": "M9 12l2 2 4-4M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z",
    "msp": "M4 7h16M4 12h16M4 17h10M18 15l3 2-3 2",
    "reports": "M5 20V10M12 20V4M19 20v-7",
    "canned": "M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3z",
    "admin": "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-1.8-.3 1.6 1.6 0 00-1 1.5V21a2 2 0 01-4 0v-.1a1.6 1.6 0 00-1-1.5 1.6 1.6 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.6 1.6 0 00.3-1.8 1.6 1.6 0 00-1.5-1H3a2 2 0 010-4h.1a1.6 1.6 0 001.5-1 1.6 1.6 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.6 1.6 0 001.8.3H9a1.6 1.6 0 001-1.5V3a2 2 0 014 0v.1a1.6 1.6 0 001 1.5 1.6 1.6 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 00-.3 1.8V9a1.6 1.6 0 001.5 1H21a2 2 0 010 4h-.1a1.6 1.6 0 00-1.5 1z",
    "requests": "M4 5h16v11H8l-4 4z",
    "feedback": "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z",
    "settings": "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M16 4v4M10 10v4M18 16v4",
    "plus": "M12 5v14M5 12h14",
    "appearance": "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
}

# Where the theme choice lives on My settings (the fieldset has this id).
APPEARANCE_HREF = "/settings/notifications#appearance"


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str
    count: Optional[int] = None
    tone: str = "gray"  # the chip tone of the count
    active: bool = False


@dataclass(frozen=True)
class Shell:
    """What base.html draws around a page for this person."""

    kind: str  # "console" (agents), "portal" (everyone on a portal page, and requesters everywhere) or "bare" (not signed in)
    items: tuple = ()  # the main navigation
    person_menu: tuple = ()  # the person menu (9G), in order: My requests, My feedback, Settings, Appearance (agents); Settings, Appearance (the portal)
    unread: int = 0
    can_search: bool = False


def _admin_href(session: Session, user: User) -> Optional[str]:
    """Where "Admin" leads: the first admin page this person may open (a super admin all of them; an agent holding
    an extra permission only the pages it opens)."""
    if user.role == UserRole.SUPER_ADMIN:
        return "/admin/users"
    if has_permission(session, user, "manage_request_templates"):
        return "/admin/request-types"
    if has_permission(session, user, "data_protection"):
        return "/admin/data-protection"
    return None


def _count(session: Session, query) -> int:
    return session.exec(select(func.count()).select_from(query.subquery())).one()


def _agent_items(session: Session, user: User) -> list[tuple]:
    """(key, label, href, icon, count, tone) for the agent console, in order."""
    visible = visible_ticket_condition(user, access_context(session, user))
    open_tickets = select(Ticket.id).where(Ticket.status.notin_(FINISHED_STATUSES))
    if visible is not None:
        open_tickets = open_tickets.where(visible)
    mine = open_tickets.where(Ticket.assigned_agent_id == user.id)
    untriaged = open_tickets.where(Ticket.triaged_at.is_(None))
    items = [
        ("queue", "Queue", "/tickets", "queue", _count(session, open_tickets), "blue"),
        ("mine", "My tickets", "/tickets?mine=true", "mine", _count(session, mine), "gray"),
        ("untriaged", "Untriaged", "/tickets?untriaged=true", "triage", _count(session, untriaged), "amber"),
        ("approvals", "Approvals", "/portal/approvals", "approvals", count_pending_for(session, user), "amber"),
        ("msp", "MSP mail", "/msp-mail", "msp", _count(session, unmatched_query()), "gray"),
    ]
    if has_permission(session, user, "view_reports"):
        items.append(("reports", "Reports", "/reports", "reports", None, "gray"))
    if user.role == UserRole.SUPER_ADMIN:
        items.append(("canned", "Canned responses", "/admin/canned", "canned", None, "gray"))
    admin = _admin_href(session, user)
    if admin:
        items.append(("admin", "Admin", admin, "admin", None, "gray"))
    return items


def _active_key(items: list[tuple], path: str, query: dict) -> Optional[str]:
    """The one item that is current: the most specific whose address contains this page's. /tickets?mine=true is
    My tickets and /tickets alone is the Queue; a ticket page (/tickets/1042) belongs to the Queue; Admin covers every
    /admin page except the ones with an item of their own (Canned responses)."""
    best, best_score = None, -1
    for key, _label, href, *_rest in items:
        base, _, wanted = href.partition("?")
        wanted_query = parse_qs(wanted)
        if base == "/portal":
            hit = path == base or re.fullmatch(r"/portal/\d+.*", path) is not None
        elif key == "admin":
            hit = path == "/admin" or path.startswith("/admin/")
        else:
            hit = path == base or path.startswith(base + "/")
        if not hit:
            continue
        if wanted_query:
            if any(query.get(name) != values for name, values in wanted_query.items()):
                continue
        elif base == "/tickets" and any(name in query for name in ("mine", "untriaged")):
            continue
        score = len(base) * 10 + (5 if wanted_query else 0) + (0 if key == "admin" else 1)
        if score > best_score:
            best, best_score = key, score
    return best


def build_shell(session: Session, user: Optional[User], path: str, query: dict) -> Shell:
    """The navigation for this person on this page. `query` is the page's query string as parse_qs gives it."""
    if user is None:
        return Shell(kind="bare")
    unread = count_unread(session, user)
    on_portal = path == "/portal" or path.startswith("/portal/")
    if user.role in AGENT_ROLES and not on_portal:
        rows = _agent_items(session, user)
        current = _active_key(rows, path, query)
        items = tuple(NavItem(k, label, href, ICONS[icon], count, tone, k == current) for k, label, href, icon, count, tone in rows)
        own = [
            ("requests", "My requests", "/portal", "requests"),
            ("feedback", "My feedback", "/my-feedback", "feedback"),
            ("settings", "Settings", "/settings/notifications", "settings"),
            ("appearance", "Appearance", APPEARANCE_HREF, "appearance"),
        ]
        menu = tuple(NavItem(k, label, href, ICONS[icon], active=path == href) for k, label, href, icon in own)
        return Shell(kind="console", items=items, person_menu=menu, unread=unread, can_search=True)
    # The portal: requesters everywhere, and anyone on a portal page. Agents get a way back to the console.
    pending = count_pending_for(session, user)
    rows = [
        ("requests", "My requests", "/portal", "requests", None, "gray"),
        ("new", "New request", "/portal/new", "plus", None, "gray"),
        ("approvals", "Approvals", "/portal/approvals", "approvals", pending, "amber"),
    ]
    current = _active_key(rows, path, query)
    items = [NavItem(k, label, href, ICONS[icon], count, tone, k == current) for k, label, href, icon, count, tone in rows]
    if user.role in AGENT_ROLES:
        items.append(NavItem("console", "Agent console", "/tickets", ICONS["queue"]))
    # Settings sits in the person's menu (the initials), not among the links; My requests is already in the top bar.
    menu = (
        NavItem("settings", "Settings", "/settings/notifications", ICONS["settings"], active=path.startswith("/settings")),
        NavItem("appearance", "Appearance", APPEARANCE_HREF, ICONS["appearance"]),
    )
    return Shell(kind="portal", items=tuple(items), person_menu=menu, unread=unread)

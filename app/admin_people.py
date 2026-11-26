"""Admin > Users: a page per person (#322), built to grow.

The person page is drawn from two registries, so a new feature adds one entry and one template, never an edit to the
page itself:

- `PERSON_SECTIONS`: the cards under "At a glance" (Role, Teams, ..., Account status). Each `Section` has a key (its
  anchor is `#sec-<key>`), a title, a one-line lede, an optional `shown` check, a template under `_person/` and a
  `build` function that returns the view model that template reads as `v`. The jump links come from the same list.
- `PERSON_FACTS`: the read-only facts of the "At a glance" card. Each `Fact` builds a `FactValue` (text, an optional
  link, an optional tone) or None when it does not apply to this person. `PERSON_SHORTCUTS` are the two "see more"
  links beside them.

Both read a `PersonContext`, which loads each shared thing (teams, grants, links) once and only when asked, so a
fact and a section that need the same rows share one query. Facts are one query each, never a loop per row.

Nothing here decides who may do what: the routes keep their rules, and the page is for super admins only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from typing import Callable, Optional
from urllib.parse import urlencode

from sqlmodel import Session, func, select

from app import linked_identities as links
from app.activity import LOGIN_SUCCESS
from app.identity import requester_emails
from app.models import (
    FINISHED_STATUSES,
    ActivityLog,
    MembershipLevel,
    Team,
    TeamMembership,
    Ticket,
    User,
    UserPermission,
    UserRole,
)
from app.permissions import ASSIGNABLE_ROLES, PERMISSIONS
from app.sessions import live_session_counts
from app.status_display import NOT_FINISHED

ROLE_TONE = {"super_admin": "t-blue", "agent": "t-green", "end_user": "t-gray"}
SIGN_IN_METHODS = {
    "sso": "Microsoft",
    "local": "password",
    "break_glass": "break-glass",
    "emergency": "emergency password",
    "dev": "dev login",
}

# What the page says after a change (`?done=<key>` on the redirect). Anything else shows nothing.
DONE_MESSAGES = {
    "role": "Role saved.",
    "teams": "Teams saved.",
    "perms": "Extra permissions saved.",
    "signed_out": "Signed out everywhere.",
    "oid_cleared": "Microsoft link cleared.",
    "unlocked": "Account unlocked.",
    "unlinked": "Unlinked.",
    "activated": "Account activated.",
    "deactivated": "Account deactivated.",
}


def role_label(role: UserRole) -> str:
    return role.value.replace("_", " ").capitalize()


def person_url(user_id: int, section: str = "", done: str = "") -> str:
    """The person's page, optionally on a section and with the message a change leaves behind."""
    url = f"/admin/users/{user_id}"
    if done in DONE_MESSAGES:
        url += "?" + urlencode({"done": done})
    return url + (f"#sec-{section}" if section else "")


# ---- What a section or a fact reads --------------------------------------------------------------------------------


class PersonContext:
    """One person, as the page sees them. Each property loads once, on first use."""

    def __init__(self, session: Session, person: User, now: Optional[datetime] = None):
        self.session = session
        self.person = person
        self.now = now or datetime.now(timezone.utc)

    @cached_property
    def all_teams(self) -> list[Team]:
        return list(self.session.exec(select(Team).order_by(Team.name)).all())

    @cached_property
    def memberships(self) -> list[tuple[TeamMembership, Team]]:
        """The person's teams with their level, by team name, in one query."""
        rows = self.session.exec(
            select(TeamMembership, Team)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(TeamMembership.user_id == self.person.id)
            .order_by(Team.name)
        ).all()
        return [(membership, team) for membership, team in rows]

    @cached_property
    def granted(self) -> set[str]:
        return set(
            self.session.exec(select(UserPermission.permission).where(UserPermission.user_id == self.person.id)).all()
        )

    @cached_property
    def live_sessions(self) -> int:
        return live_session_counts(self.session, [self.person.id]).get(self.person.id, 0)

    @cached_property
    def merged_into_link(self):
        """The link that merged this account into another, or None."""
        return links.link_of(self.session, self.person)

    @cached_property
    def merged_into(self) -> Optional[User]:
        link = self.merged_into_link
        return self.session.get(User, link.main_user_id) if link is not None else None

    @cached_property
    def link_rows(self) -> list["links.LinkRow"]:
        """The active links this person is the main account of (their other addresses and merged accounts)."""
        return [
            row for row in links.rows_for(self.session, links.ACTIVE, user=self.person) if row.link.main_user_id == self.person.id
        ]

    @cached_property
    def addresses(self) -> set[str]:
        return requester_emails(self.person, self.session)

    @property
    def is_locked(self) -> bool:
        until = self.person.locked_until
        if until is None:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > self.now

    @property
    def is_agent(self) -> bool:
        return self.person.role in ASSIGNABLE_ROLES or self.person.role == UserRole.SUPER_ADMIN


# ---- At a glance ---------------------------------------------------------------------------------------------------


@dataclass
class FactValue:
    text: str
    href: Optional[str] = None  # where the fact is explained or can be changed, when there is somewhere to go
    tone: Optional[str] = None  # a chip tone (t-green, t-amber, t-red ...) for a state worth noticing
    hint: str = ""  # a smaller second line
    moment: Optional[datetime] = None  # shown as a time in the viewer's zone, in place of `text`


@dataclass
class Fact:
    key: str
    label: str
    build: Callable[[PersonContext], Optional[FactValue]]


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _fact_open_tickets(ctx: PersonContext) -> Optional[FactValue]:
    if not ctx.is_agent:
        return None
    count = ctx.session.exec(
        select(func.count()).select_from(Ticket).where(
            Ticket.assigned_agent_id == ctx.person.id, Ticket.status.not_in(FINISHED_STATUSES)
        )
    ).one()
    href = "/tickets?" + urlencode({"assignee": ctx.person.id, "status": NOT_FINISHED}) if count else None
    tone = "t-amber" if count and not ctx.person.is_active else None
    return FactValue(_plural(count, "open ticket"), href, tone, "assigned to them" + (" (inactive account)" if tone else ""))


def _fact_raised(ctx: PersonContext) -> Optional[FactValue]:
    count = ctx.session.exec(
        select(func.count()).select_from(Ticket).where(func.lower(func.trim(Ticket.requester_email)).in_(ctx.addresses))
    ).one()
    href = "/tickets?" + urlencode({"requester": ctx.person.id}) if count else None
    return FactValue(_plural(count, "ticket"), href, None, "raised, under any of their addresses")


def _fact_last_sign_in(ctx: PersonContext) -> Optional[FactValue]:
    row = ctx.session.exec(
        select(ActivityLog.created_at, ActivityLog.detail)
        .where(ActivityLog.actor_user_id == ctx.person.id, ActivityLog.event_type == LOGIN_SUCCESS)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(1)
    ).first()
    href = "/admin/activity-log?" + urlencode({"actor": ctx.person.email, "event_type": LOGIN_SUCCESS})
    if row is None:
        return FactValue("Never signed in", href)
    created_at, detail = row
    how = SIGN_IN_METHODS.get(detail or "", detail or "an unknown way")
    return FactValue("", href, None, "by " + how, moment=created_at)


def _fact_sessions(ctx: PersonContext) -> Optional[FactValue]:
    return FactValue(_plural(ctx.live_sessions, "signed-in session"), "#sec-signin")


def _fact_teams(ctx: PersonContext) -> Optional[FactValue]:
    if ctx.person.role == UserRole.END_USER:
        return None
    if not ctx.memberships:
        return FactValue("No teams", "#sec-teams", "t-amber" if ctx.person.is_active else None, "sees no team's tickets")
    return FactValue(
        ", ".join(f"{team.name} ({membership.level.value})" for membership, team in ctx.memberships), "#sec-teams"
    )


def _fact_permissions(ctx: PersonContext) -> Optional[FactValue]:
    if ctx.person.role == UserRole.SUPER_ADMIN:
        return FactValue("All (super admin)", "#sec-perms")
    if ctx.person.role not in ASSIGNABLE_ROLES:
        return None
    if not ctx.granted:
        return FactValue("None", "#sec-perms")
    labels = [PERMISSIONS[key].label for key in PERMISSIONS if key in ctx.granted]
    return FactValue(", ".join(labels), "#sec-perms")


def _fact_locked(ctx: PersonContext) -> Optional[FactValue]:
    person = ctx.person
    if not person.password_hash:
        return None  # only a local account has a password to get wrong
    failed = _plural(person.failed_login_count, "failed attempt")
    if ctx.is_locked:
        return FactValue("Locked", "#sec-signin", "t-red", failed)
    return FactValue("Not locked", "#sec-signin", None, failed)


def _fact_linked(ctx: PersonContext) -> Optional[FactValue]:
    if ctx.merged_into is not None:
        return FactValue(f"Merged into {ctx.merged_into.email}", person_url(ctx.merged_into.id), "t-amber")
    return FactValue(_plural(len(ctx.link_rows), "linked identity", "linked identities"), "#sec-linked")


PERSON_FACTS: list[Fact] = [
    Fact("open_tickets", "Open tickets", _fact_open_tickets),
    Fact("raised", "Tickets raised", _fact_raised),
    Fact("last_sign_in", "Last sign-in", _fact_last_sign_in),
    Fact("sessions", "Signed in now", _fact_sessions),
    Fact("teams", "Teams", _fact_teams),
    Fact("permissions", "Extra permissions", _fact_permissions),
    Fact("locked", "Account lock", _fact_locked),
    Fact("linked", "Linked identities", _fact_linked),
]


def _shortcuts(ctx: PersonContext) -> list[tuple[str, str]]:
    person = ctx.person
    return [
        ("See their activity", "/tickets/activity?" + urlencode({"person": person.email.lower()})),
        ("Audit log for this person", "/admin/audit-log?" + urlencode({"subject_user_id": person.id})),
    ]


PERSON_SHORTCUTS: list[Callable[[PersonContext], list[tuple[str, str]]]] = [_shortcuts]


def glance(ctx: PersonContext) -> dict:
    """The At a glance card: the facts that apply to this person, and the shortcut links."""
    facts = []
    for fact in PERSON_FACTS:
        value = fact.build(ctx)
        if value is not None:
            facts.append((fact, value))
    return {"facts": facts, "shortcuts": [link for provider in PERSON_SHORTCUTS for link in provider(ctx)]}


# ---- The sections --------------------------------------------------------------------------------------------------


@dataclass
class Section:
    key: str
    title: str
    lede: str
    template: str  # under app/templates/, e.g. "_person/role.html"; it reads the view model as `v`
    build: Callable[[PersonContext], dict]
    shown: Callable[[PersonContext], bool] = lambda ctx: True
    danger: bool = False  # red-bordered, and last


def _role(ctx: PersonContext) -> dict:
    return {"roles": list(UserRole), "current": ctx.person.role, "tone": ROLE_TONE[ctx.person.role.value]}


def _teams(ctx: PersonContext) -> dict:
    return {
        "rows": ctx.memberships,
        "teams": ctx.all_teams,
        "levels": list(MembershipLevel),
        "is_end_user": ctx.person.role == UserRole.END_USER,
    }


def _perms(ctx: PersonContext) -> dict:
    role = ctx.person.role
    kind = "all" if role == UserRole.SUPER_ADMIN else ("form" if role in ASSIGNABLE_ROLES else "none")
    return {"kind": kind, "permissions": PERMISSIONS, "granted": ctx.granted}


def _signin(ctx: PersonContext) -> dict:
    person = ctx.person
    return {
        "local": bool(person.password_hash),
        "locked": ctx.is_locked,
        "locked_until": person.locked_until,
        "show_unlock": bool(person.password_hash) and (ctx.is_locked or bool(person.failed_login_count)),
        "username": person.email.split("@")[0],
        "sessions": ctx.live_sessions,
    }


def _linked(ctx: PersonContext) -> dict:
    return {"merged_into": ctx.merged_into, "rows": ctx.link_rows}


def _status(ctx: PersonContext) -> dict:
    return {"merged_into": ctx.merged_into, "lede": "Currently active." if ctx.person.is_active else "Currently inactive."}


PERSON_SECTIONS: list[Section] = [
    Section("role", "Role", "What this person can do in the app.", "_person/role.html", _role),
    Section("teams", "Teams", "Which teams' tickets they see and work.", "_person/teams.html", _teams),
    Section("perms", "Extra permissions", "Agents only. A super admin holds every permission.", "_person/perms.html", _perms),
    Section("signin", "Sign-in", "How this person signs in, and what you can reset.", "_person/signin.html", _signin),
    Section(
        "linked", "Linked identities", "Other addresses and accounts that are the same person.", "_person/linked.html", _linked
    ),
    Section("status", "Account status", "", "_person/status.html", _status, danger=True),
]


@dataclass
class DrawnSection:
    section: Section
    view: dict
    lede: str


def sections_for(ctx: PersonContext) -> list[DrawnSection]:
    """The sections to draw for this person, in registry order, each with its view model. The jump links use the same."""
    drawn = []
    for section in PERSON_SECTIONS:
        if section.shown(ctx):
            view = section.build(ctx)
            drawn.append(DrawnSection(section, view, view.get("lede") or section.lede))
    return drawn


# ---- Pages ---------------------------------------------------------------------------------------------------------


def person_chips(ctx: PersonContext) -> list[tuple[str, str]]:
    """(tone, text) for the header: role, status and the account kinds, as the list shows them."""
    person = ctx.person
    chips = [(ROLE_TONE[person.role.value], role_label(person.role)), ("t-green" if person.is_active else "t-gray", "Active" if person.is_active else "Inactive")]
    if person.is_break_glass:
        chips.append(("t-amber", "Break-glass"))
    if person.password_hash and not person.is_break_glass:
        chips.append(("t-blue", "Local account"))
    if ctx.merged_into is not None:
        chips.append(("t-amber", "Merged"))
    if ctx.is_locked:
        chips.append(("t-red", "Locked"))
    return chips


def person_page_context(
    session: Session, viewer: User, person: User, *, done: str = "", refusal: Optional[tuple[str, str]] = None
) -> dict:
    """Everything `admin_user_person.html` needs. `refusal` is (section key, message): a refused change, shown in its card."""
    ctx = PersonContext(session, person)
    return {
        "user": viewer,
        "person": person,
        "chips": person_chips(ctx),
        "glance": glance(ctx),
        "sections": sections_for(ctx),
        "done": DONE_MESSAGES.get(done, ""),
        "refusal": refusal,
    }


def refusal_page(request, session: Session, viewer: User, person: User, section: str, message: str):
    """A refused change on the person page: the page again, status 400, the message in the card it came from."""
    from app.templating import templates

    return templates.TemplateResponse(
        request,
        "admin_user_person.html",
        person_page_context(session, viewer, person, refusal=(section, message)),
        status_code=400,
    )


# ---- The list ------------------------------------------------------------------------------------------------------


@dataclass
class ListRow:
    user: User
    teams: list[tuple[MembershipLevel, str]] = field(default_factory=list)
    merged_into_id: Optional[int] = None
    merged_into_email: str = ""
    locked: bool = False
    stranded: bool = False  # inactive, and still holds open tickets: they need a new assignee


def list_rows(session: Session, users: list[User], now: Optional[datetime] = None) -> list[ListRow]:
    """The rows of the people table, with what they need loaded in one query each for the whole page."""
    now = now or datetime.now(timezone.utc)
    ids = [u.id for u in users]
    teams: dict[int, list[tuple[MembershipLevel, str]]] = {}
    merged: dict[int, int] = {}
    holding: set[int] = set()
    if ids:
        for membership, team in session.exec(
            select(TeamMembership, Team).join(Team, Team.id == TeamMembership.team_id).where(TeamMembership.user_id.in_(ids)).order_by(Team.name)
        ).all():
            teams.setdefault(membership.user_id, []).append((membership.level, team.name))
        merged = {
            secondary: main
            for secondary, main in session.exec(
                select(links.LinkedIdentity.secondary_user_id, links.LinkedIdentity.main_user_id).where(
                    links.LinkedIdentity.status == links.ACTIVE, links.LinkedIdentity.secondary_user_id.in_(ids)
                )
            ).all()
        }
        holding = set(
            session.exec(
                select(Ticket.assigned_agent_id)
                .where(Ticket.assigned_agent_id.in_(ids), Ticket.status.not_in(FINISHED_STATUSES))
                .distinct()
            ).all()
        )
    mains = {u.id: u.email for u in session.exec(select(User).where(User.id.in_(set(merged.values())))).all()} if merged else {}

    def locked(u: User) -> bool:
        until = u.locked_until
        if until is None:
            return False
        return (until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until) > now

    return [
        ListRow(
            user=u,
            teams=teams.get(u.id, []),
            merged_into_id=merged.get(u.id),
            merged_into_email=mains.get(merged.get(u.id), ""),
            locked=locked(u),
            stranded=(not u.is_active) and u.id in holding,
        )
        for u in users
    ]

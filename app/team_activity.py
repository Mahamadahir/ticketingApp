"""The team activity feed: everything that happened on the tickets an agent can open,
newest first. Ticket events (status, assignment, triage, escalation, links, splits ...)
and messages (emails, requester replies, internal notes) in one list.

Which tickets: the same ones the agent can see anywhere else, so the feed uses
visibility.visible_ticket_condition and never decides access itself. A ticket moved to
another team drops out of the feed of everyone outside that team, history included.

This is separate from the audit log (admin: who changed settings, users, categories) and
the sign-in activity log (admin: who signed in). Those are system-wide; this one is the
work on tickets, for the people doing it.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, String, case, cast, func, literal, null, or_, union_all
from sqlmodel import Session, select

from app.models import Message, MessageDirection, Ticket, TicketEvent, TicketReaction, User
from app.reactions import display_of
from app.notification_settings import AGENT_ROLES
from app.settings_store import ACTIVITY_GROUP_MINUTES, count_setting
from app.status_display import status_label, status_tone
from app.time_zones import format_long_date, valid_zone
from app.visibility import ASSIGNEE_ROLES

EXCERPT = 160

# Event types only an agent can cause (triage, assignment, escalation, links, splits, the MSP steps, dates, priority and
# so on). A step of one of these types is agent work whatever the person's role is now (#270): a demoted agent's past
# changes stay agent cards. Every other event type (created, reopened, status and approval steps, follower steps, and any
# type added later) is decided by the person's role, so a new type that a requester can cause is never shown as an
# agent's by mistake. Add a type here only when a requester or approver can never cause it.
AGENT_ONLY_EVENTS = (
    "triaged", "assigned", "escalated", "team_changed", "type_changed", "priority_changed", "category_changed",
    "category_reviewed", "attachment_renamed", "note_visibility_changed", "backdated", "due_changed", "required_by_changed",
    "linked", "unlinked", "child_linked", "child_unlinked", "split", "split_from", "reclosed",
    "sent_to_msp", "msp_hold", "msp_hold_released", "msp_forwarded", "msp_forward_dismissed", "msp_acknowledged",
    "msp_told_no_longer_needed", "msp_not_told", "with_msp_through", "taken_back_from_msp", "linked_taken_back_from_msp",
)


class Kind(str, Enum):
    EVENTS = "events"  # what changed on a ticket
    CONVERSATION = "conversation"  # emails to the requester and the requester's replies
    NOTES = "notes"  # internal notes


def feed_query(
    visible,
    team_id: Optional[int] = None,
    kind: Optional[Kind] = None,
    person: Optional[str] = None,
    before: Optional[datetime] = None,
    since: Optional[datetime] = None,
):
    """A select of rows (kind, at, row_id, ticket_id, what, actor, from_value, to_value, detail, requester, author_id)
    over the tickets matching `visible` (None means all), newest first. `person` keeps the steps one address took,
    `before` the steps strictly older than that moment and `since` those at or after it (the filters of the page
    apply to steps, never to whole cards)."""
    events = (
        select(
            literal("event").label("kind"),
            TicketEvent.created_at.label("at"),
            TicketEvent.id.label("row_id"),
            TicketEvent.ticket_id.label("ticket_id"),
            TicketEvent.event_type.label("what"),
            TicketEvent.actor_email.label("actor"),
            TicketEvent.from_value.label("from_value"),
            TicketEvent.to_value.label("to_value"),
            TicketEvent.detail.label("detail"),
            Ticket.requester_email.label("requester"),
            cast(null(), Integer).label("author_id"),
            # Agent work is decided from what the step was, not only from the person's role now (#270): a note, a
            # change or an email can only have been made by an agent, so a demoted agent's past work stays agent work.
            # Only the types in AGENT_ONLY_EVENTS are decided that way; every other step goes by the person's role.
            case(
                (User.id.is_(None), False),
                (TicketEvent.event_type.in_(AGENT_ONLY_EVENTS), True),
                else_=User.role.in_(AGENT_ROLES),
            ).label("agent"),
        )
        .join(Ticket, Ticket.id == TicketEvent.ticket_id)
        .join(User, func.lower(User.email) == func.lower(TicketEvent.actor_email), isouter=True)
    )
    messages = (
        select(
            literal("message").label("kind"),
            Message.created_at.label("at"),
            Message.id.label("row_id"),
            Message.ticket_id.label("ticket_id"),
            case(
                (Message.msp_forward.is_not(None), "msp"),
                (Message.visible_note.is_(True), "visible_note"),  # an agent's note the requester can see (#254)
                else_=cast(Message.direction, String),
            ).label("what"),
            func.coalesce(User.email, Message.sender).label("actor"),
            cast(null(), String).label("from_value"),
            cast(null(), String).label("to_value"),
            case(
                (func.length(Message.body) > EXCERPT, func.substr(Message.body, 1, EXCERPT) + "..."),
                else_=Message.body,
            ).label("detail"),
            Ticket.requester_email.label("requester"),
            Message.author_user_id.label("author_id"),
            Message.author_user_id.is_not(None).label("agent"),
        )
        .join(Ticket, Ticket.id == Message.ticket_id)
        .join(User, User.id == Message.author_user_id, isouter=True)
        .where(Message.deleted_at.is_(None))  # a deleted note vanishes from here too (#255)
    )
    # Reactions to the bot's messages (#298) are requester steps: the emoji as received in from_value, its sentiment in
    # to_value and the name Teams gave in detail. Only the requester's and followers' reactions (`by_party`) are steps; anyone
    # else's (an agent's thumbs-up on a notification card) shows on the ticket's thread only, so it is never read as the
    # requester acting. Taking one back is a step of its own, at the time it was taken back.
    def reaction_steps(what: str, moment, *where):
        return (
            select(
                literal("reaction").label("kind"),
                moment.label("at"),
                TicketReaction.id.label("row_id"),
                TicketReaction.ticket_id.label("ticket_id"),
                literal(what).label("what"),
                func.coalesce(User.email, TicketReaction.reactor_address, TicketReaction.reactor_name).label("actor"),
                func.coalesce(TicketReaction.emoji, TicketReaction.raw_type).label("from_value"),
                TicketReaction.sentiment.label("to_value"),
                TicketReaction.reactor_name.label("detail"),
                Ticket.requester_email.label("requester"),
                cast(null(), Integer).label("author_id"),
                literal(False).label("agent"),
            )
            .join(Ticket, Ticket.id == TicketReaction.ticket_id)
            .join(User, User.id == TicketReaction.user_id, isouter=True)
            .where(TicketReaction.by_party.is_(True), *where)
        )

    reactions = [
        reaction_steps("reaction", TicketReaction.reacted_at),
        reaction_steps("reaction_removed", TicketReaction.removed_at, TicketReaction.removed_at.is_not(None)),
    ]
    if kind == Kind.EVENTS:
        branches = [events]
    elif kind == Kind.CONVERSATION:
        branches = [messages.where(Message.direction != MessageDirection.INTERNAL, Message.visible_note.is_(False)), *reactions]
    elif kind == Kind.NOTES:
        branches = [messages.where(or_(Message.direction == MessageDirection.INTERNAL, Message.visible_note.is_(True)))]
    else:
        branches = [events, messages, *reactions]

    scoped = []
    for branch in branches:
        if visible is not None:
            branch = branch.where(visible)
        if team_id is not None:
            branch = branch.where(Ticket.team_id == team_id)
        scoped.append(branch)

    combined = union_all(*scoped).subquery()
    query = select(*combined.c)
    if person:
        query = query.where(func.lower(combined.c.actor) == person.lower())
    if before is not None:
        query = query.where(combined.c.at < before)
    if since is not None:
        query = query.where(combined.c.at >= since)
    return query.order_by(combined.c.at.desc(), combined.c.kind, combined.c.row_id.desc())


KIND_LABELS = {
    Kind.EVENTS: "Changes",
    Kind.CONVERSATION: "Emails and replies",
    Kind.NOTES: "Internal notes",
}

_MESSAGE_WHAT = {
    "msp": "message from the MSP",
    MessageDirection.OUTBOUND.value: "emailed the requester",
    MessageDirection.INBOUND.value: "message from the requester",
    MessageDirection.INTERNAL.value: "added an internal note",
    "visible_note": "added a note the requester can see",
}


def reaction_words(row) -> str:
    """A reaction as one phrase: "Reacted 👍 like", "Took back 👍 like" (the reaction is shown as received)."""
    shown = display_of(row.from_value or "")
    return f"Took back {shown}" if row.what == "reaction_removed" else f"Reacted {shown}"


def describe(row) -> str:
    """One line for a feed row."""
    if row.kind == "reaction":
        return reaction_words(row) + (f" in Teams ({row.detail})" if row.detail else "")
    if row.kind == "message":
        # The database may hand back the enum's name (OUTBOUND) or its value (outbound).
        text = _MESSAGE_WHAT.get(row.what.lower(), row.what)
        return f"{text}: {row.detail}" if row.detail else text
    text = row.what.replace("_", " ")
    if row.from_value or row.to_value:
        text += f": {row.from_value or 'none'} -> {row.to_value or 'none'}"
    if row.detail:
        text += f" ({row.detail})"
    return text


# ---- Sessions: steps folded into cards ----------------------------------------------------------------------
#
# The feed above is one row per step. The page and the side drawer show cards instead. Everything that decides
# what a card is lives in group_steps below and nowhere else; the templates only draw the cards they are given.
#
# A *session* is the steps by agents on one ticket, each no more than the gap (the setting, 30 minutes by default)
# after the one before. When more than one agent acts on the ticket inside that window it is still ONE card, with a
# line per person: the card is the ticket's stretch of work, the lines say who did what. Requester replies and
# system steps (automatic jobs, the MSP, an automatic email) are never part of an agent's session: each is a card of
# its own, and sits in the timeline by its own time.

ROLE_AGENT = "agent"
ROLE_REQUESTER = "requester"
ROLE_SYSTEM = "system"

# "Show earlier" loads this many steps at a time (then reaches further back only so that no card is cut in two).
LOAD_STEPS = 150
# The side drawer shows about this many of the latest cards.
DRAWER_CARDS = 15


@dataclass
class Step:
    """One thing that happened on a ticket, with who did it (`person`, a lower-case address, None for the system)."""

    ticket_id: int
    at: datetime
    role: str = ROLE_AGENT
    person: Optional[str] = None
    group: str = "events"  # which kind filter it belongs to: events, conversation or notes
    phrase: str = ""  # the short wording used in a card's summary line: "Triaged", "Added a note"
    text: str = ""  # the full line shown when a card is expanded (describe(): wording plus the excerpt)
    excerpt: str = ""  # the start of an email or note, shown beside a card's single step
    kind: str = "event"
    row_id: int = 0
    clock: str = ""  # HH:MM in the viewer's zone, filled in when a timeline is built
    # What the card draws as a sentence (see _activity_card.html): the event type or, for a message, its direction.
    what: str = ""
    from_chip: Optional[tuple[str, str]] = None  # (label, tone) of a status change's old status
    to_chip: Optional[tuple[str, str]] = None  # ... and of the new one
    assignee: str = ""  # who an assignment is to, by name
    assignee_you: bool = False  # ... and whether that is the viewer
    first_line: str = ""  # the first line of an email, shown under its sentence


@dataclass
class Line:
    """What one person did in a card."""

    person: Optional[str]
    steps: list[Step]
    name: str = ""  # what the card says: "You" for the viewer
    real_name: str = ""  # their name, for the initials

    @property
    def summary(self) -> str:
        """The step wordings in order, a run of the same one written once with a count: "Added a note x2"."""
        phrases: list[list] = []
        for step in self.steps:
            if phrases and phrases[-1][0] == step.phrase:
                phrases[-1][1] += 1
            else:
                phrases.append([step.phrase, 1])
        return " · ".join(p if n == 1 else f"{p} ×{n}" for p, n in phrases)


@dataclass
class Card:
    ticket_id: int
    role: str
    steps: list[Step]  # oldest first
    lines: list[Line] = field(default_factory=list)
    ticket: Optional[Ticket] = None
    team_name: str = ""
    requester_name: str = ""
    span: str = ""  # "10:01–10:20", or one time

    @property
    def start(self) -> datetime:
        return self.steps[0].at

    @property
    def end(self) -> datetime:
        return self.steps[-1].at

    @property
    def single(self) -> bool:
        return len(self.steps) == 1

    def name_of(self, step: Step) -> str:
        return next((line.name for line in self.lines if line.person == step.person), step.person or "System")

    @property
    def people(self) -> list[str]:
        return [line.name or line.person or "System" for line in self.lines]

    @property
    def title(self) -> str:
        """Who the card is about: one name, "A and B" or "A, B and C"; the requester; or the system."""
        if self.role == ROLE_REQUESTER:
            return self.lines[0].name or self.requester_name or "The requester"
        if self.role == ROLE_SYSTEM:
            return "System"
        names = self.people
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


@dataclass
class Day:
    label: str
    cards: list[Card]


@dataclass
class Timeline:
    days: list[Day]
    earlier: Optional[datetime]  # the "before" of the Show earlier link; None when there is nothing older

    @property
    def cards(self) -> list[Card]:
        return [card for day in self.days for card in day.cards]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def group_steps(steps: Sequence[Step], gap: timedelta) -> list[Card]:
    """Fold steps into cards, newest card (by its latest step) first. The one place sessions are decided: see above."""
    ordered = sorted(steps, key=lambda s: (_aware(s.at), s.kind, s.row_id))
    cards: list[Card] = []
    runs: dict[int, list[Step]] = {}
    for step in ordered:
        if step.role != ROLE_AGENT:
            cards.append(Card(step.ticket_id, step.role, [step], [Line(step.person, [step])]))
        else:
            runs.setdefault(step.ticket_id, []).append(step)
    for ticket_id, run in runs.items():
        current = [run[0]]
        for step in run[1:]:
            if _aware(step.at) - _aware(current[-1].at) <= gap:
                current.append(step)
            else:
                cards.append(_agent_card(ticket_id, current))
                current = [step]
        cards.append(_agent_card(ticket_id, current))
    cards.sort(key=lambda c: (_aware(c.end), c.steps[-1].row_id, c.ticket_id), reverse=True)
    return cards


def _agent_card(ticket_id: int, steps: list[Step]) -> Card:
    lines: dict[Optional[str], Line] = {}
    for step in steps:  # a person's line sits where their first step is
        lines.setdefault(step.person, Line(step.person, [])).steps.append(step)
    return Card(ticket_id, ROLE_AGENT, steps, list(lines.values()))


_PRIORITY = re.compile(r"\bP[1-4]\b")


def phrase_of(row, names: Mapping[str, str]) -> str:
    """The short wording of a step for a card's summary line."""
    if row.kind == "reaction":
        return reaction_words(row)
    if row.kind == "message":
        what = row.what.lower()
        return {
            "msp": "Message from the MSP",
            MessageDirection.OUTBOUND.value: "Emailed the requester",
            MessageDirection.INBOUND.value: "Requester replied",
            MessageDirection.INTERNAL.value: "Added a note",
            "visible_note": "Added a note the requester can see",
        }.get(what, what.capitalize())
    if row.what == "triaged":
        priority = _PRIORITY.search(row.detail or "")
        extra = ", ".join(x for x in (priority.group(0) if priority else None, row.to_value) if x)
        return f"Triaged ({extra})" if extra else "Triaged"
    if row.what == "status_changed":
        if row.to_value == "resolved":
            return "Resolved"
        if row.to_value == "closed":
            return "Closed"
        return f"Status: {(row.to_value or 'none').replace('_', ' ')}"
    if row.what == "assigned":
        return f"Assigned to {names.get((row.to_value or '').lower(), row.to_value)}" if row.to_value else "Unassigned"
    if row.what == "team_changed":
        return f"Re-assigned to {row.to_value}" if row.to_value else "Re-assigned"
    if row.what == "escalated":
        return f"Escalated to {row.to_value}" if row.to_value else "Escalated"
    if row.what == "reopened":
        return "Reopened"
    if row.what == "note_visibility_changed":  # #255
        return "Made a note visible to the requester" if row.to_value == "visible" else "Hid a note from the requester"
    return row.what.replace("_", " ").capitalize()


def role_of(row) -> str:
    """Who took a step. Requester replies and system steps are never an agent's."""
    actor = (row.actor or "").lower()
    if row.kind == "reaction":
        return ROLE_REQUESTER  # shown as a requester step, whoever reacted
    if row.kind == "message":
        if row.what.lower() == MessageDirection.INBOUND.value:
            return ROLE_REQUESTER
        return ROLE_AGENT if row.author_id is not None else ROLE_SYSTEM  # no author: an automatic email, the MSP
    if not actor:
        return ROLE_SYSTEM
    # An agent's event is theirs even on a ticket they raised for themselves; anyone else (the requester, an approver)
    # stands alone.
    return ROLE_AGENT if row.agent else ROLE_REQUESTER


def status_chip(value: Optional[str]) -> tuple[str, str]:
    """(label, chip tone) of a status: the one mapping in app/status_display.py."""
    return status_label(value), status_tone(value)


def step_of(row, names: Mapping[str, str], viewer: Optional[str] = None) -> Step:
    if row.kind == "reaction":
        group = "conversation"
    elif row.kind == "message":
        group = "notes" if row.what.lower() in (MessageDirection.INTERNAL.value, "visible_note") else "conversation"
    else:
        group = "events"
    role = role_of(row)
    # A system step has no person, whatever address an automatic email was sent from.
    person = None if role == ROLE_SYSTEM else (row.actor or "").lower() or None
    event = row.kind == "event"
    to_address = (row.to_value or "").lower()
    status = event and row.what == "status_changed"
    assigned = event and row.what == "assigned"
    return Step(
        ticket_id=row.ticket_id, at=row.at, role=role, person=person, group=group,
        phrase=phrase_of(row, names), text=describe(row), excerpt=(row.detail or "") if row.kind == "message" else "",
        kind=row.kind, row_id=row.row_id,
        what=row.what if event else row.what.lower(),
        from_chip=status_chip(row.from_value) if status and row.from_value else None,
        to_chip=status_chip(row.to_value) if status else None,
        assignee=names.get(to_address, row.to_value or "") if assigned else "",
        assignee_you=assigned and bool(viewer) and to_address == viewer,
        first_line=((row.detail or "").strip().splitlines() or [""])[0] if not event else "",
    )


def _clock(moment: datetime, zone: str) -> str:
    return _aware(moment).astimezone(ZoneInfo(zone)).strftime("%H:%M")


def day_heading(moment: datetime, zone: str, now: datetime) -> str:
    """Today, Yesterday, then the date written out, all by the day it is in the viewer's zone."""
    tz = ZoneInfo(zone)
    day = _aware(moment).astimezone(tz).date()
    today = _aware(now).astimezone(tz).date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return format_long_date(day, zone, now=now)


def gap_of(session: Session) -> timedelta:
    return timedelta(minutes=count_setting(session, ACTIVITY_GROUP_MINUTES))


def _fetch(session: Session, visible, filters: dict, before=None, since=None, limit: Optional[int] = None):
    query = feed_query(visible, before=before, since=since, **filters)
    return session.exec(query.limit(limit) if limit else query).all()


def load_timeline(
    session: Session,
    visible,
    zone: str,
    *,
    team_id: Optional[int] = None,
    kind: Optional[Kind] = None,
    person: Optional[str] = None,
    before: Optional[datetime] = None,
    steps: Optional[int] = None,
    min_cards: int = 0,
    now: Optional[datetime] = None,
    viewer: Optional[str] = None,
) -> Timeline:
    """The cards for one stretch of time, under day headings, for the viewer's zone.

    The "Show earlier" rule, so that a card is never cut across two loads: a load takes the newest `steps` steps older
    than `before`, and then reaches further back until no session card begins within the gap of its far end (an
    earlier step could still belong to it). The next load starts from that far end (`earlier`): this load holds the
    steps at or after it, the next one those strictly before it. A busy stretch therefore loads a few more steps than
    asked for, never fewer than a whole card. `min_cards` makes the load larger until it holds that many cards (the
    drawer). `viewer` is the viewer's address: their steps are written as "You", and an assignment to them "(You)"."""
    steps = steps or LOAD_STEPS
    zone = valid_zone(zone) or "UTC"
    now = now or datetime.now(timezone.utc)
    gap = gap_of(session)
    filters = {"team_id": team_id, "kind": kind, "person": person}
    while True:
        newest = _fetch(session, visible, filters, before=before, limit=steps)
        since = _aware(newest[-1].at) if len(newest) >= steps else None
        rows = list(_fetch(session, visible, filters, before=before, since=since))
        while True:
            cards = group_steps([step_of(r, {}) for r in rows], gap)
            if since is None:
                break
            edge = [c.start for c in cards if c.role == ROLE_AGENT and _aware(c.start) < since + gap]
            if not edge:
                break
            reached = min(_aware(e) for e in edge) - gap
            rows += _fetch(session, visible, filters, before=since, since=reached)  # only the stretch not yet read
            since = reached
        more = since is not None and bool(_fetch(session, visible, filters, before=since, limit=1))
        if len(cards) >= min_cards or not more or steps >= LOAD_STEPS * 8:
            break
        steps *= 2
    people, tickets, teams = _lookups(session, rows)
    viewer = (viewer or "").lower() or None
    cards = group_steps([step_of(r, people, viewer) for r in rows], gap)  # again, now that the names are known
    days: list[Day] = []
    for card in cards:
        card.ticket = tickets.get(card.ticket_id)
        has_team = card.ticket is not None and card.ticket.team_id is not None
        card.team_name = teams.get(card.ticket.team_id, "Unassigned") if has_team else "Unassigned"
        if card.ticket is not None:
            card.requester_name = people.get(card.ticket.requester_email.lower(), card.ticket.requester_email)
        for line in card.lines:
            line.real_name = people.get(line.person or "", line.person or "System")
            line.name = "You" if viewer and line.person == viewer else line.real_name
        for step in card.steps:
            step.clock = _clock(step.at, zone)
        first, last = card.steps[0].clock, card.steps[-1].clock
        card.span = first if first == last else f"{first}–{last}"
        label = day_heading(card.end, zone, now)
        if not days or days[-1].label != label:
            days.append(Day(label, []))
        days[-1].cards.append(card)
    return Timeline(days, since if more else None)


def _lookups(session: Session, rows) -> tuple[dict[str, str], dict[int, Ticket], dict[int, str]]:
    from app.models import Team

    ticket_ids = {r.ticket_id for r in rows}
    tickets = {t.id: t for t in session.exec(select(Ticket).where(Ticket.id.in_(ticket_ids))).all()} if ticket_ids else {}
    addresses = {(r.actor or "").lower() for r in rows} | {t.requester_email.lower() for t in tickets.values()}
    addresses |= {(r.to_value or "").lower() for r in rows if r.what == "assigned"}
    addresses.discard("")
    people = (
        {u.email.lower(): u.display_name for u in session.exec(select(User).where(func.lower(User.email).in_(addresses))).all()}
        if addresses else {}
    )
    teams = {t.id: t.name for t in session.exec(select(Team)).all()}
    return people, tickets, teams


def person_choices(session: Session, chosen: Optional[str] = None) -> list[User]:
    """The Person filter's choices (#262): every current agent, that is every active user who can hold tickets
    (ASSIGNEE_ROLES, break-glass accounts left out as for assignment), by display name. A query on the user table only,
    never a read of the ticket history. `chosen` is the address in the URL: that person is listed too, whatever their role
    now, so a demoted agent whose past steps are agent cards can still be picked and stays selected."""
    listed = User.role.in_(ASSIGNEE_ROLES) & User.is_active & User.is_break_glass.is_(False)
    if chosen:
        listed = or_(listed, func.lower(User.email) == chosen.lower())
    return list(session.exec(select(User).where(listed).order_by(User.display_name)).all())


def count_steps_today(session: Session, visible, person: str, zone: str, now: Optional[datetime] = None) -> int:
    """How many steps this address took today (the day in the viewer's zone), on the tickets the viewer can see."""
    tz = ZoneInfo(valid_zone(zone) or "UTC")
    now = _aware(now or datetime.now(timezone.utc))
    midnight = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    feed = feed_query(visible, person=person, since=midnight).subquery()
    return session.exec(select(func.count()).select_from(feed)).one()

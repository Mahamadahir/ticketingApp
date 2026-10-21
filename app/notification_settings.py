"""Notification settings (phase 2B): which channels each person wants for each kind of event.

The event types and the channels they can use are listed here, with the default for each. A
person's choices are stored only where they differ from the default (NotificationPreference), so
changing a default here reaches everyone who never changed it.

Only the in-app channel delivers today. Teams arrives with the worker (2C) and email with phase 3;
the choices are recorded now so those channels read them from the start (is_enabled). Whatever
sends a notification asks is_enabled for its event type and channel, never a setting of its own.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.audit import log_action
from app.models import NotificationPreference, User, UserRole

IN_APP = "in_app"
TEAMS = "teams"
EMAIL = "email"

RAISED_FOR_YOU = "raised_for_you"
FOLLOWER_UPDATE = "follower_update"

# The first half of the (two-number) advisory lock that serialises one person's saves; the second
# is their user id. A different space from the worker's single-number lock.
SAVE_LOCK_SPACE = 726_002

# Where each channel stands, shown beside it so nobody expects a message that cannot arrive yet.
CHANNELS: dict[str, tuple[str, Optional[str]]] = {
    IN_APP: ("In the app (the bell)", None),
    TEAMS: ("Teams", "not connected yet"),
    EMAIL: ("Email", "not connected yet"),
}

AGENT_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)
EVERYONE = (UserRole.END_USER, UserRole.AGENT, UserRole.SUPER_ADMIN)


@dataclass(frozen=True)
class EventType:
    kind: str
    label: str
    description: str
    roles: tuple[UserRole, ...]  # who has this setting
    channels: tuple[str, ...]  # the channels it can use
    default_on: tuple[str, ...]  # the channels that are on until the person changes them


EVENT_TYPES: tuple[EventType, ...] = (
    EventType(
        "assigned", "A ticket is assigned to you",
        "Triage, assigning or re-assigning a ticket to you, by someone else.",
        AGENT_ROLES, (IN_APP, TEAMS), (IN_APP, TEAMS),
    ),
    EventType(
        "mentioned", "You are mentioned in an internal note",
        "Someone writes @ and the part of your email address before the @.",
        AGENT_ROLES, (IN_APP, TEAMS), (IN_APP, TEAMS),
    ),
    EventType(
        "replied", "A ticket assigned to you is updated by the requester",
        "The requester, or someone copied on the ticket, replies on a ticket assigned to you, whatever its "
        "status (also with the MSP or a supplier, where a reply moves nothing).",
        AGENT_ROLES, (IN_APP, TEAMS), (IN_APP, TEAMS),
    ),
    EventType(
        RAISED_FOR_YOU, "An agent raises a ticket for you",
        "An email telling you a ticket was raised on your behalf. Also off for everyone if an admin "
        "switches it off in Settings.",
        EVERYONE, (EMAIL,), (EMAIL,),
    ),
    EventType(
        FOLLOWER_UPDATE, "Someone else updates a ticket you are on",
        "An email passing on what the requester or a follower wrote on a ticket you raised or follow, when you "
        "were not already sent it.",
        EVERYONE, (EMAIL,), (EMAIL,),
    ),
)
_BY_KIND = {event.kind: event for event in EVENT_TYPES}


def offers(kind: str, channel: str) -> bool:
    """Whether this kind of event can use this channel at all. An unlisted kind offers none, so it is
    never sent anywhere a person has not been able to choose (is_enabled alone says yes to an unlisted
    channel, which suits the bell but would put an unchosen @mention in a shared channel)."""
    event = _BY_KIND.get(kind)
    return event is not None and channel in event.channels


def events_for(user: User) -> list[EventType]:
    return [event for event in EVENT_TYPES if user.role in event.roles]


def is_enabled(session: Session, user: User, kind: str, channel: str) -> bool:
    """Whether this person wants this event on this channel. A kind or channel that is not
    listed here is on, so a new kind of notification is never silently dropped."""
    event = _BY_KIND.get(kind)
    if event is None or channel not in event.channels:
        return True
    row = session.exec(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id,
            NotificationPreference.kind == kind,
            NotificationPreference.channel == channel,
        )
    ).first()
    return row.enabled if row is not None else channel in event.default_on


def choices_for(session: Session, user: User) -> list[tuple[EventType, list[tuple[str, bool]]]]:
    """For the settings page: each event the person has, with each of its channels on or off."""
    rows = {
        (row.kind, row.channel): row.enabled
        for row in session.exec(select(NotificationPreference).where(NotificationPreference.user_id == user.id)).all()
    }
    return [
        (event, [(channel, rows.get((event.kind, channel), channel in event.default_on)) for channel in event.channels])
        for event in events_for(user)
    ]


def save_choices(session: Session, user: User, ticked: set[str]) -> int:
    """Stores the page's checkboxes, each named "kind:channel". Everything the person has that is
    not ticked is off. Returns how many settings changed. Does not commit."""
    # One save at a time per person: a double click or two open tabs would otherwise both read "no
    # row yet" and both insert it. The second waits here, then sees the first's rows. An advisory
    # lock held to the end of the transaction, not a lock on the user's row, so nothing else that
    # writes that person (signing in, the lockout counter) is ever held up by a save.
    session.exec(text("select pg_advisory_xact_lock(:space, :id)").bindparams(space=SAVE_LOCK_SPACE, id=user.id))
    existing = {
        (row.kind, row.channel): row
        for row in session.exec(select(NotificationPreference).where(NotificationPreference.user_id == user.id)).all()
    }
    changed = []
    for event in events_for(user):
        for channel in event.channels:
            wanted = f"{event.kind}:{channel}" in ticked
            row = existing.get((event.kind, channel))
            current = row.enabled if row is not None else channel in event.default_on
            if wanted != current:
                changed.append(f"{event.kind}/{channel} {'on' if wanted else 'off'}")
            if wanted == (channel in event.default_on):
                if row is not None:
                    session.delete(row)
            elif row is not None:
                row.enabled = wanted
                session.add(row)
            else:
                session.add(NotificationPreference(user_id=user.id, kind=event.kind, channel=channel, enabled=wanted))
    if changed:
        log_action(session, user.email, "notification_settings", user.id, "update", ", ".join(changed))
    return len(changed)

MAX_TEAMS_DELAY_MINUTES = 24 * 60


class InvalidDelay(ValueError):
    """The delay typed on the settings page is not a whole number of minutes in range."""


def parse_teams_delay(raw: Optional[str]) -> Optional[int]:
    """The page's "unread for how many minutes" box: blank means the default (None), otherwise a
    whole number of minutes from 1 to a day."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_TEAMS_DELAY_MINUTES:
        raise InvalidDelay(f"Enter a whole number of minutes from 1 to {MAX_TEAMS_DELAY_MINUTES}, or leave it blank")
    return int(raw)


def save_teams_delay(session: Session, user: User, delay: Optional[int]) -> bool:
    """Stores how long this person's unread notifications wait before also going to Teams (None: the
    default). Returns whether it changed. Does not commit."""
    if delay == user.teams_delay_minutes:
        return False
    log_action(
        session, user.email, "notification_settings", user.id, "update",
        f"teams delay {user.teams_delay_minutes or 'default'} -> {delay or 'default'}",
    )
    user.teams_delay_minutes = delay
    session.add(user)
    return True

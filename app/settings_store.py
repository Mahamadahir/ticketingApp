"""Admin-editable settings. Values are stored as text in the setting table; this
module declares which keys exist, what they mean and their defaults, so a value
that was never set still has a sensible answer and the admin page knows what to
show. Values marked TBC in docs/plan.md live here, never in code."""

from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session

from app.models import Setting


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: str  # "team": a team id, empty for none; "minutes": a whole number; "percent": 1 to 99; "flag": on or off; "count": a whole number, 1 to 1000
    description: str
    default: str = ""


DEFAULT_TRIAGE_TEAM = "default_triage_team_id"
TELL_REQUESTER_RAISED_FOR = "tell_requester_when_raised_for_them"
APPROVAL_LINK_MINUTES = "approval_link_minutes"
TEAMS_UNREAD_MINUTES = "teams_unread_minutes"
AUTOMATIC_EMAIL_CAP = "automatic_email_cap_per_ticket"
SLA_AT_RISK_PERCENT = "sla_at_risk_percent"
GRAPH_SUBSCRIPTION_MINUTES = "graph_subscription_minutes"
GRAPH_RENEW_MARGIN_MINUTES = "graph_renew_margin_minutes"
MAIL_BACKFILL_DAYS = "mail_backfill_days"

# PLACEHOLDER SLA targets, in minutes on a 24/7 clock, until the real ones are agreed
# (TBC in docs/plan.md). They are settings, changed under Admin > Settings; a blank
# value means that target is not tracked.
_SLA_PLACEHOLDERS = {
    "p1": (15, 240),
    "p2": (60, 480),
    "p3": (240, 4320),
    "p4": (480, 10080),
}


def sla_key(priority: str, which: str) -> str:
    """The setting key for a target, e.g. sla_p1_first_response_minutes."""
    return f"sla_{priority}_{which}_minutes"


def _sla_definitions() -> dict[str, SettingDef]:
    definitions = {}
    for priority, (first, resolution) in _SLA_PLACEHOLDERS.items():
        for which, label, default in (
            ("first_response", "first response", first),
            ("resolution", "resolution", resolution),
        ):
            key = sla_key(priority, which)
            definitions[key] = SettingDef(
                key=key,
                label=f"{priority.upper()} {label} target (minutes)",
                kind="minutes",
                description=(
                    f"How long a {priority.upper()} ticket may wait for its {label}. The clock runs "
                    "24/7 and stops while the ticket is Awaiting user. Placeholder value until the "
                    "real target is agreed; blank means not tracked."
                ),
                default=str(default),
            )
    return definitions


REGISTRY: dict[str, SettingDef] = {
    DEFAULT_TRIAGE_TEAM: SettingDef(
        key=DEFAULT_TRIAGE_TEAM,
        label="Default triage team",
        kind="team",
        description=(
            "New tickets arrive in this team's queue until someone triages them. "
            "Also where an 'Other' category sends a ticket if the category has no team of its own."
        ),
    ),
    **_sla_definitions(),
    TELL_REQUESTER_RAISED_FOR: SettingDef(
        key=TELL_REQUESTER_RAISED_FOR,
        label="Tell the requester when an agent raises a ticket for them",
        kind="flag",
        description=(
            "On: the requester is emailed that a ticket was raised for them. The email itself "
            "starts with mail intake and sending (phase 3); this decides whether it is sent. "
            "Each person will also be able to turn it off in their own notification settings."
        ),
        default="on",
    ),
    TEAMS_UNREAD_MINUTES: SettingDef(
        key=TEAMS_UNREAD_MINUTES,
        label="Unread notifications are also sent to Teams after (minutes)",
        kind="minutes",
        description=(
            "A notification still unread in the app after this long is also sent to Teams, for people "
            "who have Teams on for that event. Each person can set their own delay in their notification "
            "settings; this is the default for everyone who has not. The Teams delivery itself starts "
            "once Teams is connected."
        ),
        default="5",
    ),
    APPROVAL_LINK_MINUTES: SettingDef(
        key=APPROVAL_LINK_MINUTES,
        label="How long an emailed approve/decline link works (minutes)",
        kind="minutes",
        description=(
            "Approvers get a link that opens a page where they approve or decline. After this long "
            "the link stops working and they use the portal instead. 20160 is 14 days."
        ),
        default="20160",
    ),
    AUTOMATIC_EMAIL_CAP: SettingDef(
        key=AUTOMATIC_EMAIL_CAP,
        label="Automatic emails to one ticket's requester, at most per day",
        kind="count",
        description=(
            "Stops a mail loop: automatic emails (approval decisions, 'a ticket was raised for you') "
            "to the same ticket are held back once this many have gone in the last 24 hours. Agent "
            "replies are never counted. Placeholder value until the real limit is agreed."
        ),
        default="10",
    ),
    GRAPH_SUBSCRIPTION_MINUTES: SettingDef(
        key=GRAPH_SUBSCRIPTION_MINUTES,
        label="Mail subscription lifetime when created or renewed (minutes)",
        kind="minutes",
        description=(
            "How far ahead the worker asks Microsoft to extend the mailbox subscription each time. Graph "
            "allows at most 10070 minutes for mail (a larger number is cut to that). Placeholder until "
            "proved on staging."
        ),
        default="10000",
    ),
    GRAPH_RENEW_MARGIN_MINUTES: SettingDef(
        key=GRAPH_RENEW_MARGIN_MINUTES,
        label="Renew the mail subscription when this little time is left (minutes)",
        kind="minutes",
        description=(
            "The worker renews the subscription once less than this is left, and /health warns below it. "
            "1440 is 24 hours: a worker outage of a day still leaves the subscription alive."
        ),
        default="1440",
    ),
    MAIL_BACKFILL_DAYS: SettingDef(
        key=MAIL_BACKFILL_DAYS,
        label="Mail catch-up: how many days back the first sync looks",
        kind="count",
        description=(
            "The first catch-up sync (or one restarted after Microsoft discarded its place) collects mail "
            "received in this many days. Later runs carry on from where the last one stopped, however long "
            "ago that was. Mail already collected is never collected twice."
        ),
        default="3",
    ),
    SLA_AT_RISK_PERCENT: SettingDef(
        key=SLA_AT_RISK_PERCENT,
        label="SLA 'at risk' threshold (percent)",
        kind="percent",
        description="A ticket shows amber when this share of its target time or less is left.",
        default="25",
    ),
}


def get_setting(session: Session, key: str) -> str:
    definition = REGISTRY[key]
    row = session.get(Setting, key)
    return row.value if row is not None else definition.default


def get_flag(session: Session, key: str) -> bool:
    """An on/off setting."""
    return get_setting(session, key).strip().lower() != "off"


def get_int_setting(session: Session, key: str) -> Optional[int]:
    value = get_setting(session, key).strip()
    # ASCII digits only: str.isdigit() also accepts characters like a superscript
    # two, which int() then refuses.
    return int(value) if value.isascii() and value.isdigit() else None


def set_setting(session: Session, key: str, value: str) -> None:
    """Does not commit."""
    if key not in REGISTRY:
        raise KeyError(key)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        session.add(row)

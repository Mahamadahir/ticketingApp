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
    kind: str  # "team": a team id, empty for none; "minutes": a whole number; "percent": 1 to 99; "vote_percent": 1 to 100; "flag": on or off; "count": a whole number, 1 to 1000
    description: str
    default: str = ""


DEFAULT_TRIAGE_TEAM = "default_triage_team_id"
TELL_REQUESTER_RAISED_FOR = "tell_requester_when_raised_for_them"
APPROVAL_LINK_MINUTES = "approval_link_minutes"
TEAMS_UNREAD_MINUTES = "teams_unread_minutes"
AUTOMATIC_EMAIL_CAP = "automatic_email_cap_per_ticket"
INBOUND_MAX_PER_SENDER_HOUR = "inbound_max_per_sender_hour"
INBOUND_MAX_ATTACHMENTS = "inbound_max_attachments"
INBOUND_MAX_CC = "inbound_max_cc"
SLA_AT_RISK_PERCENT = "sla_at_risk_percent"
GRAPH_SUBSCRIPTION_MINUTES = "graph_subscription_minutes"
GRAPH_RENEW_MARGIN_MINUTES = "graph_renew_margin_minutes"
MAIL_BACKFILL_DAYS = "mail_backfill_days"
JOB_LEASE_MINUTES = "scheduled_job_lease_minutes"
JOB_RETRY_MINUTES = "scheduled_job_retry_minutes"
JOB_LATE_MINUTES = "scheduled_job_late_minutes"
PRESENCE_RETRY_MINUTES = "presence_retry_minutes"
APPROVAL_DEFAULT_PASS_PERCENT = "approval_default_pass_percent"
APPROVAL_DEFAULT_FAIL_PERCENT = "approval_default_fail_percent"
APPROVAL_REMINDERS_ENABLED = "approval_reminders_enabled"
APPROVAL_REMIND_AFTER_DAYS = "approval_remind_after_days"
APPROVAL_REMIND_EVERY_DAYS = "approval_remind_every_days"
APPROVAL_MAX_REMINDERS = "approval_max_reminders"
APPROVAL_ESCALATE_AFTER_DAYS = "approval_escalate_after_days"

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
    INBOUND_MAX_PER_SENDER_HOUR: SettingDef(
        key=INBOUND_MAX_PER_SENDER_HOUR,
        label="Inbound mail: most tickets and replies from one sender in an hour",
        kind="count",
        description=(
            "Stops a mail loop: once one sender has made this many tickets and replies in the last hour, "
            "their further mail is set aside (shown under Mail intake) and makes nothing. Placeholder "
            "value until the real limit is agreed; set it high rather than off."
        ),
        default="30",
    ),
    INBOUND_MAX_ATTACHMENTS: SettingDef(
        key=INBOUND_MAX_ATTACHMENTS,
        label="Inbound mail: most attachments kept from one email",
        kind="count",
        description="Files beyond this many on one email are not kept; the ticket notes which were skipped. Placeholder.",
        default="20",
    ),
    INBOUND_MAX_CC: SettingDef(
        key=INBOUND_MAX_CC,
        label="Inbound mail: most Cc addresses made followers from one email",
        kind="count",
        description=(
            "When an email opens a ticket, the first this many addresses on its Cc line become followers "
            "(the follower list has its own overall cap as well). Placeholder."
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
        label="Mail catch-up: how many days back a restarted sync looks",
        kind="count",
        description=(
            "Used only when Microsoft has discarded the catch-up's place and it must start again: it then "
            "collects mail received in this many days. The very first run never looks back (mail already in "
            "the inbox is not imported); later runs carry on from where the last one stopped, however long "
            "ago that was. Mail already collected is never collected twice."
        ),
        default="3",
    ),
    JOB_LEASE_MINUTES: SettingDef(
        key=JOB_LEASE_MINUTES,
        label="Recurring jobs: how long a run may hold its job (minutes)",
        kind="minutes",
        description=(
            "A recurring job (auto-close, chase, reminders) is held by the worker that runs it. If that "
            "worker dies mid-run, the job is free to run again once this long has passed. Set it longer "
            "than the slowest job takes. Placeholder until the real jobs exist."
        ),
        default="10",
    ),
    JOB_RETRY_MINUTES: SettingDef(
        key=JOB_RETRY_MINUTES,
        label="Recurring jobs: wait before retrying a failed run (minutes)",
        kind="minutes",
        description="After a failure the job is tried again after this long, or at its next normal time if that is sooner.",
        default="5",
    ),
    JOB_LATE_MINUTES: SettingDef(
        key=JOB_LATE_MINUTES,
        label="Recurring jobs: how overdue counts as late (minutes)",
        kind="minutes",
        description=(
            "A job that starts this much after it was due (the worker was down, or a run overran) still "
            "runs once, and the audit log records that it was late."
        ),
        default="3",
    ),
    PRESENCE_RETRY_MINUTES: SettingDef(
        key=PRESENCE_RETRY_MINUTES,
        label="Out-of-office lookups: wait before retrying a person who could not be read (minutes)",
        kind="minutes",
        description=(
            "When Graph does not know a person or gives no reading for them, the presence job leaves them "
            "alone for this long before asking again. Placeholder (1440 is a day)."
        ),
        default="1440",
    ),
    APPROVAL_DEFAULT_PASS_PERCENT: SettingDef(
        key=APPROVAL_DEFAULT_PASS_PERCENT,
        label="New request types: share of the group that must approve (percent)",
        kind="vote_percent",
        description=(
            "Pre-filled on the New request type form; each type keeps its own number. Placeholder (a simple "
            "majority). The pass and fail percentages of a type must add up to more than 100."
        ),
        default="51",
    ),
    APPROVAL_DEFAULT_FAIL_PERCENT: SettingDef(
        key=APPROVAL_DEFAULT_FAIL_PERCENT,
        label="New request types: share of the group that must decline (percent)",
        kind="vote_percent",
        description="Pre-filled on the New request type form; each type keeps its own number. Placeholder.",
        default="50",
    ),
    APPROVAL_REMINDERS_ENABLED: SettingDef(
        key=APPROVAL_REMINDERS_ENABLED,
        label="Approval reminders: send them",
        kind="flag",
        description=(
            "On: an approver who has not answered a request is emailed a reminder (and, if the request goes "
            "unanswered for long enough, the super admins are told). Off until staging, like the other "
            "automations; switch it on once mail is proved there."
        ),
        default="off",
    ),
    APPROVAL_REMIND_AFTER_DAYS: SettingDef(
        key=APPROVAL_REMIND_AFTER_DAYS,
        label="Approval reminders: first reminder after (days)",
        kind="count",
        description="An approver who has not answered is first reminded this many days after the request was sent to them. Placeholder.",
        default="2",
    ),
    APPROVAL_REMIND_EVERY_DAYS: SettingDef(
        key=APPROVAL_REMIND_EVERY_DAYS,
        label="Approval reminders: repeat every (days)",
        kind="count",
        description="After the first reminder, an approver who still has not answered is reminded again this many days after the last one. Placeholder.",
        default="2",
    ),
    APPROVAL_MAX_REMINDERS: SettingDef(
        key=APPROVAL_MAX_REMINDERS,
        label="Approval reminders: most reminders to one approver",
        kind="count",
        description="Each approver is reminded at most this many times about one request, then left alone. Placeholder.",
        default="3",
    ),
    APPROVAL_ESCALATE_AFTER_DAYS: SettingDef(
        key=APPROVAL_ESCALATE_AFTER_DAYS,
        label="Approval reminders: tell the super admins after (days)",
        kind="count",
        description=(
            "A request still waiting this many days after it was sent is brought to the super admins' attention "
            "in the bell, once. They can then waive it or chase the approvers. Placeholder."
        ),
        default="7",
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


def count_setting(session: Session, key: str) -> int:
    """A count setting as a number, falling back to its default if the stored value is not one."""
    value = get_int_setting(session, key)
    return value if value is not None else int(REGISTRY[key].default)


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

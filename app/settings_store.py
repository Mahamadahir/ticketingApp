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
    kind: str  # "team": a team id, empty for none; "minutes": a whole number; "percent": 1 to 99; "vote_percent": 1 to 100; "flag": on or off; "count": a whole number, 1 to 1000; "hour": a whole number, 0 to 23; "clock": a time of day written 09:00 or 17:30; "time_zone": an IANA time zone name, blank for the default; "email": one email address, blank for none; "region": one of england-and-wales, scotland, northern-ireland; "dates": a list of dates (2026-12-25, separated by commas or spaces), blank for none; "off_minutes": a whole number of minutes, 0 is off; "regex": a regular expression with the named groups the setting says
    description: str
    default: str = ""


DEFAULT_TRIAGE_TEAM = "default_triage_team_id"
TELL_REQUESTER_RAISED_FOR = "tell_requester_when_raised_for_them"
APPROVAL_LINK_MINUTES = "approval_link_minutes"
FEEDBACK_LINK_MINUTES = "feedback_link_minutes"
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
SESSION_IDLE_MINUTES = "session_idle_minutes"
SESSION_MAX_DAYS = "session_max_days"
SESSION_KEEP_DAYS = "session_keep_days"
APPROVAL_DEFAULT_PASS_PERCENT = "approval_default_pass_percent"
APPROVAL_REMINDERS_ENABLED = "approval_reminders_enabled"
APPROVAL_REMIND_AFTER_DAYS = "approval_remind_after_days"
APPROVAL_REMIND_EVERY_DAYS = "approval_remind_every_days"
APPROVAL_MAX_REMINDERS = "approval_max_reminders"
APPROVAL_ESCALATE_AFTER_DAYS = "approval_escalate_after_days"
AUTO_CLOSE_ENABLED = "auto_close_enabled"
AUTO_CLOSE_AFTER_DAYS = "auto_close_after_days"
CHASE_ENABLED = "chase_enabled"
CHASE_AFTER_DAYS = "chase_after_days"
CHASE_RESOLVE_AFTER_DAYS = "chase_resolve_after_days"
BULK_RESOLVE_MAX = "bulk_resolve_max_tickets"
MAX_SUB_TICKETS = "max_sub_tickets"  # shared by manual splitting and automatic sub-requests
REQUIRED_BY_LEAD_DAYS = "required_by_lead_days"  # the queue lead time when a request type has none of its own
DUE_DAYS_BEFORE_REQUIRED_BY = "due_days_before_required_by"
SCHEDULED_MORNING_HOUR = "scheduled_morning_hour"
DEFAULT_TIME_ZONE = "default_time_zone"
TIME_ZONE_REFRESH_DAYS = "time_zone_refresh_days"
TIME_ZONE_RETRY_HOURS = "time_zone_retry_hours"
MSP_SUBJECT_PATTERN_DEFAULT = (
    r"(?i)^\s*(?:(?:re|fw|fwd)\s*:\s*)*Ticket\s?#(?P<number>\d+)\s*/\s*(?P<title>.+?)\s+--\s+has been\s+(?P<action>[a-z][a-z ]*?)\s*$"
)
MSP_ADDRESS = "msp_address"
MSP_HANDOFF_HOUR = "msp_handoff_hour"
MSP_BUSINESS_MORNING_HOUR = "msp_business_morning_hour"
MSP_BANK_HOLIDAYS = "msp_bank_holidays"
BANK_HOLIDAY_REGION = "bank_holiday_region"
BANK_HOLIDAY_FAIL_DAYS = "bank_holiday_fail_days"
MSP_HANDOFF_WARN_MINUTES = "msp_handoff_warn_minutes"
MSP_BUSINESS_START = "msp_business_start"
MSP_BUSINESS_END = "msp_business_end"
MSP_ATTACH_MAX_MB = "msp_attach_max_mb"
MSP_SUBJECT_PATTERN = "msp_subject_pattern"
MSP_ACK_MATCH_DAYS = "msp_ack_match_days"
MSP_ACK_ALERT_MINUTES = "msp_ack_alert_minutes"

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
                    "24/7 and stops while the ticket is Awaiting user (a Due date never moves because of the wait). Placeholder value until the "
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
    FEEDBACK_LINK_MINUTES: SettingDef(
        key=FEEDBACK_LINK_MINUTES,
        label="How long the emailed feedback link works (minutes)",
        kind="minutes",
        description=(
            "The resolution email carries a link where the requester rates how the ticket was handled. "
            "It works once, and stops working after this long. 43200 is 30 days. Placeholder value."
        ),
        default="43200",
    ),
    AUTOMATIC_EMAIL_CAP: SettingDef(
        key=AUTOMATIC_EMAIL_CAP,
        label="Automatic emails to one ticket's requester, at most per day",
        kind="count",
        description=(
            "Stops a mail loop: automatic emails (approval decisions, 'a ticket was raised for you', the receipt, the chase 'is this still an issue?') "
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
    SESSION_IDLE_MINUTES: SettingDef(
        key=SESSION_IDLE_MINUTES,
        label="Sign-in: idle timeout (minutes)",
        kind="minutes",
        description=(
            "A signed-in browser that makes no request for this long is signed out. Placeholder 480 "
            "(8 hours), TBC. Blank uses the placeholder."
        ),
        default="480",
    ),
    SESSION_MAX_DAYS: SettingDef(
        key=SESSION_MAX_DAYS,
        label="Sign-in: longest a session can last (days)",
        kind="count",
        description="Even an active session ends this long after sign-in, and the person signs in again. Placeholder 14, TBC. More than 365 counts as 365.",
        default="14",
    ),
    SESSION_KEEP_DAYS: SettingDef(
        key=SESSION_KEEP_DAYS,
        label="Sign-in: days to keep ended sessions (days)",
        kind="count",
        description="A daily job deletes the record of a session this long after it expired or was signed out. Placeholder 30, TBC.",
        default="30",
    ),
    PRESENCE_RETRY_MINUTES: SettingDef(
        key=PRESENCE_RETRY_MINUTES,
        label="Out-of-office lookups: wait before retrying a person who could not be read (minutes)",
        kind="minutes",
        description=(
            "When Graph does not know a person or gives no reading for them, the presence job leaves them "
            "alone for this long before asking again. Default 60 (an hour; decided 3 Nov 2026)."
        ),
        default="60",
    ),
    APPROVAL_DEFAULT_PASS_PERCENT: SettingDef(
        key=APPROVAL_DEFAULT_PASS_PERCENT,
        label="New request types: share of the group that must approve (percent)",
        kind="vote_percent",
        description=(
            "Pre-filled on the New request type form; each type keeps its own number. Placeholder (a simple "
            "majority)."
        ),
        default="51",
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
    AUTO_CLOSE_ENABLED: SettingDef(
        key=AUTO_CLOSE_ENABLED,
        label="Auto-close: close old resolved tickets",
        kind="flag",
        description=(
            "On: a ticket that has stayed Resolved for the number of days below is closed automatically. "
            "Off until staging, like the other automations."
        ),
        default="off",
    ),
    AUTO_CLOSE_AFTER_DAYS: SettingDef(
        key=AUTO_CLOSE_AFTER_DAYS,
        label="Auto-close: close a resolved ticket after (days)",
        kind="count",
        description=(
            "A ticket Resolved for this many days is closed. A reopen or a reply before then keeps it open. "
            "The emailed reopen link works for the same time (it expires when the ticket auto-closes). Placeholder."
        ),
        default="7",
    ),
    CHASE_ENABLED: SettingDef(
        key=CHASE_ENABLED,
        label="Chase: chase tickets waiting on the requester",
        kind="flag",
        description=(
            "On: a ticket in Awaiting user with no reply is chased with an email, and resolved automatically "
            "if there is still no reply. Never applies to tickets With supplier or With MSP. Off until staging."
        ),
        default="off",
    ),
    CHASE_AFTER_DAYS: SettingDef(
        key=CHASE_AFTER_DAYS,
        label="Chase: send the chase email after (days)",
        kind="count",
        description="A ticket in Awaiting user this long with no reply is emailed \"Is this still an issue?\". Placeholder.",
        default="3",
    ),
    CHASE_RESOLVE_AFTER_DAYS: SettingDef(
        key=CHASE_RESOLVE_AFTER_DAYS,
        label="Chase: resolve automatically after the chase email (days)",
        kind="count",
        description=(
            "A chased ticket still waiting this many days after the chase email was sent is resolved automatically "
            "(the resolution email says to reply to reopen it). Placeholder."
        ),
        default="3",
    ),
    REQUIRED_BY_LEAD_DAYS: SettingDef(
        key=REQUIRED_BY_LEAD_DAYS,
        label="Queue lead time (calendar days), when the request type has none",
        kind="count",
        description=(
            "A request with a Due date is ranked in the agents' queue as if it were due this many calendar "
            "days before that date, so work that takes time rises early. Only changes the sorting. A request type "
            "can set its own under Admin > Request types. Placeholder."
        ),
        default="1",
    ),
    DUE_DAYS_BEFORE_REQUIRED_BY: SettingDef(
        key=DUE_DAYS_BEFORE_REQUIRED_BY,
        label="Due date: days before Required by, when the request type has none",
        kind="count",
        description=(
            "A request with a Required by date is due this many calendar days before it (a meeting room needed on "
            "the 23rd is due by the end of the 22nd), in the requester's time zone. A request type can set its "
            "own under Admin > Request types. An agent can move the Due date with a reason. Placeholder."
        ),
        default="1",
    ),
    SCHEDULED_MORNING_HOUR: SettingDef(
        key=SCHEDULED_MORNING_HOUR,
        label="Scheduled tickets: the hour that counts as the morning of the Due date (0 to 23)",
        kind="hour",
        description=(
            "From this hour on the Due date, in the requester's time zone, a Scheduled ticket goes back to Open and "
            "its assignee is told it is due today. Placeholder."
        ),
        default="7",
    ),
    DEFAULT_TIME_ZONE: SettingDef(
        key=DEFAULT_TIME_ZONE,
        label="Default time zone",
        kind="time_zone",
        description=(
            "An IANA name such as Europe/London. Shown to anyone whose own time zone is not known: someone who has "
            "not signed in yet, an external sender, a mailbox lookup that failed. Everything is stored in UTC; this "
            "only changes how times are shown. Blank means Europe/London. Placeholder."
        ),
        default="Europe/London",
    ),
    TIME_ZONE_REFRESH_DAYS: SettingDef(
        key=TIME_ZONE_REFRESH_DAYS,
        label="Requesters' time zones: read their Outlook mailbox again after (days)",
        kind="count",
        description=(
            "A requester who only uses email has their time zone read from their Outlook mailbox when their first "
            "ticket arrives, and again after this long, so a move is picked up. Placeholder."
        ),
        default="30",
    ),
    TIME_ZONE_RETRY_HOURS: SettingDef(
        key=TIME_ZONE_RETRY_HOURS,
        label="Requesters' time zones: try again after a failed lookup after (hours)",
        kind="count",
        description=(
            "Graph did not know the address (an external sender) or the lookup failed. Until it works the default "
            "time zone applies. Placeholder."
        ),
        default="24",
    ),
    MSP_ADDRESS: SettingDef(
        key=MSP_ADDRESS,
        label="MSP: the address tickets are sent to",
        kind="email",
        description=(
            "The support partner's address. Send to MSP, an escalation to the MSP and the automatic handoff all email it. "
            "Blank (the default) means no MSP is set: Send to MSP is refused and the automatic handoff does nothing."
        ),
        default="",
    ),
    MSP_HANDOFF_HOUR: SettingDef(
        key=MSP_HANDOFF_HOUR,
        label="MSP: the hour of the automatic handoff (0 to 23)",
        kind="hour",
        description=(
            "Every day from this hour, in the default time zone, open tickets of the teams that are included in the automatic "
            "handoff and due to breach before the next business morning go to the MSP, unless held. Placeholder: 18 is 18:00."
        ),
        default="18",
    ),
    MSP_BUSINESS_MORNING_HOUR: SettingDef(
        key=MSP_BUSINESS_MORNING_HOUR,
        label="MSP: the hour business starts again, Monday to Friday (0 to 23)",
        kind="hour",
        description=(
            "The automatic handoff takes tickets due to breach before the next morning at this hour (Monday to Friday, "
            "bank holidays below excluded, in the default time zone). It only runs between the handoff hour and then. Placeholder: 8 is 08:00."
        ),
        default="8",
    ),
    MSP_BANK_HOLIDAYS: SettingDef(
        key=MSP_BANK_HOLIDAYS,
        label="MSP: extra closure days, when there is no business morning",
        kind="dates",
        description=(
            "Dates (such as 2026-12-25 and 2026-12-28, separated by commas or spaces) that count as a weekend for the "
            "automatic MSP handoff, on top of the gov.uk bank holidays fetched for the region below (use this for extra "
            "closure days such as the Christmas shutdown). Blank means none."
        ),
        default="",
    ),
    BANK_HOLIDAY_REGION: SettingDef(
        key=BANK_HOLIDAY_REGION,
        label="Bank holidays: gov.uk region",
        kind="region",
        description=(
            "Which gov.uk bank holidays count as non-business days: england-and-wales, scotland or northern-ireland. "
            "A daily job fetches them (the business-day maths never calls gov.uk). The typed list above still adds "
            "extra closure days."
        ),
        default="england-and-wales",
    ),
    BANK_HOLIDAY_FAIL_DAYS: SettingDef(
        key=BANK_HOLIDAY_FAIL_DAYS,
        label="Bank holidays: fail the fetch job after this many days without a good fetch",
        kind="count",
        description=(
            "A failed fetch keeps the last good list. Once the last good fetch is older than this many days the job "
            "shows as failed on Admin > Recurring jobs. Placeholder."
        ),
        default="3",
    ),
    MSP_HANDOFF_WARN_MINUTES: SettingDef(
        key=MSP_HANDOFF_WARN_MINUTES,
        label="MSP: warn the assignee this many minutes before the automatic handoff",
        kind="off_minutes",
        description=(
            "On a handoff day, this long before the handoff hour each assignee of a ticket that would be handed off "
            "tonight gets a bell entry saying so (Hold keeps it). 0 turns the warning off. Placeholder."
        ),
        default="30",
    ),
    MSP_BUSINESS_START: SettingDef(
        key=MSP_BUSINESS_START,
        label="MSP: business hours start (HH:MM)",
        kind="clock",
        description=(
            "Monday to Friday (bank holidays above excluded), in the default time zone. When the MSP replies and leaves the "
            "user off To and Cc, an agent previews and forwards it during business hours; outside them it is forwarded to the "
            "user automatically, with no preview (an accepted risk). Placeholder: 09:00."
        ),
        default="09:00",
    ),
    MSP_BUSINESS_END: SettingDef(
        key=MSP_BUSINESS_END,
        label="MSP: business hours end (HH:MM)",
        kind="clock",
        description="See the start of business hours. Placeholder: 17:30. An end before the start makes every moment out of hours.",
        default="17:30",
    ),
    MSP_ATTACH_MAX_MB: SettingDef(
        key=MSP_ATTACH_MAX_MB,
        label="MSP: most attachments sent with a ticket (MB in all)",
        kind="count",
        description=(
            "Files on the ticket are attached to the email to the MSP up to this total; the rest are listed by name as not "
            "attached. Graph takes a request of about 4 MB and files are sent base64-encoded (a third larger), so a larger value is treated as 2. Placeholder."
        ),
        default="2",
    ),
    MSP_SUBJECT_PATTERN: SettingDef(
        key=MSP_SUBJECT_PATTERN,
        label="MSP: the pattern of the subject of the MSP's mail",
        kind="regex",
        description=(
            "A regular expression (Python syntax) for the subject of the MSP's own mail, such as "
            "\"Ticket#7000237/Printer offline -- has been received\". The named group number is the MSP's ticket number and "
            "title is the rest of the subject; the optional group action is the word after \"has been\" (\"received\" marks the "
            "acknowledgement). The default accepts Ticket#123 and Ticket #123. Changing it is only needed if the MSP changes its wording."
        ),
        default=MSP_SUBJECT_PATTERN_DEFAULT,
    ),
    MSP_ACK_MATCH_DAYS: SettingDef(
        key=MSP_ACK_MATCH_DAYS,
        label="MSP: how many days back an acknowledgement is matched by its title",
        kind="count",
        description=(
            "An acknowledgement without our [INC-n] reference is matched to the one ticket whose email to the MSP, sent in this "
            "many days, has exactly the same subject as its title. Placeholder."
        ),
        default="7",
    ),
    MSP_ACK_ALERT_MINUTES: SettingDef(
        key=MSP_ACK_ALERT_MINUTES,
        label="MSP: tell the assignee when it has not confirmed a ticket after (minutes)",
        kind="off_minutes",
        description=(
            "When a ticket sent to the MSP has no acknowledgement this many minutes after the email to the MSP was sent, the "
            "assignee (or the team's members when nobody is assigned) gets one bell entry. 0 turns it off. Placeholder."
        ),
        default="30",
    ),
    MAX_SUB_TICKETS: SettingDef(
        key=MAX_SUB_TICKETS,
        label="Sub-tickets: most on one ticket",
        kind="count",
        description=(
            "The most sub-tickets one ticket can have in all: the parts an agent splits it into and the sub-requests "
            "made automatically from its request type's templates both count. Placeholder."
        ),
        default="10",
    ),
    BULK_RESOLVE_MAX: SettingDef(
        key=BULK_RESOLVE_MAX,
        label="Bulk resolve: most tickets in one go",
        kind="count",
        description="The most tickets an agent can resolve together with one note. Placeholder.",
        default="50",
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

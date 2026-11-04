from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, CheckConstraint, Column, Index, UniqueConstraint, event, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Field, Relationship, SQLModel


class TicketStatus(str, Enum):
    NEW = "new"  # untriaged
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_USER = "awaiting_user"
    AWAITING_APPROVAL = "awaiting_approval"  # a service request waiting for its approvers (app/approvals.py)
    WITH_SUPPLIER = "with_supplier"  # waiting on an outside supplier or vendor (not the MSP)
    WITH_MSP = "with_msp"
    SCHEDULED = "scheduled"  # work planned for a later date (usually the Required by date); set by hand
    RESOLVED = "resolved"
    CLOSED = "closed"


# Statuses in which the ticket counts as finished.
FINISHED_STATUSES = (TicketStatus.RESOLVED, TicketStatus.CLOSED)


class TicketType(str, Enum):
    INCIDENT = "incident"
    SERVICE_REQUEST = "service_request"
    MAJOR_INCIDENT = "major_incident"
    PROBLEM = "problem"


# Display prefix per ticket type, e.g. INC-42. It follows the ticket's current type and is
# never used to look a ticket up: the number alone identifies it.
TICKET_TYPE_PREFIXES = {
    TicketType.INCIDENT: "INC",
    TicketType.SERVICE_REQUEST: "SR",
    TicketType.MAJOR_INCIDENT: "MI",
    TicketType.PROBLEM: "PRB",
}


class TicketSource(str, Enum):
    EMAIL = "email"
    PORTAL = "portal"
    TEAMS = "teams"
    AGENT = "agent"


class MessageDirection(str, Enum):
    INBOUND = "inbound"  # from the requester (or a follower)
    OUTBOUND = "outbound"  # an email to the requester
    INTERNAL = "internal"  # an agent's note; never leaves the agent screens


class MessageVisibility(str, Enum):
    PUBLIC = "public"  # the requester (and portal) can see it
    AGENTS_ONLY = "agents_only"  # notes, and later MSP mail


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    AGENT = "agent"
    END_USER = "end_user"


class MembershipLevel(str, Enum):
    MEMBER = "member"  # sees and works the team's tickets
    VIEWER = "viewer"  # read-only


class Team(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    # A sub-team has a parent. Members of the parent get the same level on the
    # sub-team's tickets (app/visibility.py). One level deep only.
    parent_team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    # Whether the automatic MSP handoff (app/msp.py) takes this team's tickets. Off unless an admin switches it on;
    # an agent can still send any ticket by hand.
    msp_handoff_included: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TicketEscalator(SQLModel, table=True):
    """An agent who escalated this ticket to another team. They keep working access to
    this one ticket (app/visibility.py); nothing else about being an escalator grants any."""

    ticket_id: int = Field(foreign_key="ticket.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", primary_key=True)


class RelatedTicket(SQLModel, table=True):
    """A link between a ticket and an earlier one it follows on from: a requester replied after the
    earlier ticket was Closed, so a new ticket was opened (app/reopening.py). A plain link shown on both
    ticket pages, not a parent and child (that is linked_parent_id). ticket_id is the newer ticket and
    related_ticket_id the earlier; one row per pair."""

    __table_args__ = (UniqueConstraint("ticket_id", "related_ticket_id", name="uq_relatedticket_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    related_ticket_id: int = Field(foreign_key="ticket.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TeamMembership(SQLModel, table=True):
    """Which teams a user belongs to, and how. One row per user and team."""

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    team_id: int = Field(foreign_key="team.id", primary_key=True)
    level: MembershipLevel = Field(default=MembershipLevel.MEMBER)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    role: UserRole = Field(default=UserRole.END_USER)
    # Set only for local accounts (no Entra ID identity, e.g. a contractor).
    # SSO accounts are provisioned just-in-time on first login and never
    # have a password_hash.
    password_hash: Optional[str] = Field(default=None)
    failed_login_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    locked_until: Optional[datetime] = Field(default=None)
    # How long an unread notification waits before it is also sent to Teams, for this person.
    # None means the default (the "teams_unread_minutes" setting).
    teams_delay_minutes: Optional[int] = Field(default=None)
    # Out-of-office detection (phase 2E, app/presence.py). The Entra object id is looked up once from
    # the email; the flag is what the worker last read from Graph, and when. Read it through
    # app.presence.is_out_of_office, which ignores a stale reading.
    # Unique, so two users can never share one directory person (NULLs are allowed any number of times).
    entra_object_id: Optional[str] = Field(default=None, unique=True, index=True)
    out_of_office: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    out_of_office_checked_at: Optional[datetime] = Field(default=None)
    # When the presence job last failed to read this person (no directory match, or Graph returned no
    # reading); cleared by the next good reading. The job skips them until the retry setting has passed.
    last_presence_lookup_at: Optional[datetime] = Field(default=None)
    # Phase 4: only stops presence polling. Hiding from pickers, refusing sign-in and the nightly Entra
    # check that sets this for leavers are phase 6.
    is_active: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    # The person's own time zone (an IANA name), what every page shows them times in (app/time_zones.py).
    # The source is "browser" (reported by the page, follows them as they travel) or "manual" (chosen on
    # their settings page; the browser no longer overwrites it until they clear it). None: not known yet.
    time_zone: Optional[str] = Field(default=None)
    time_zone_source: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RequesterTimeZone(SQLModel, table=True):
    """The time zone of someone who writes in by email, read from their Outlook mailbox through Graph
    (mailboxSettings.timeZone, app/time_zones.py). Keyed by lower-cased address, the way a ticket stores its
    requester (Ticket.requester_email), so it works for people with no account. An account's own time zone
    (User.time_zone) wins over this. time_zone stays None when the lookup found nothing (an external
    sender, a failed lookup), and the organisation default applies. next_check_at says when the worker looks
    again: soon after a first ticket or a failure, after the refresh interval after a good reading."""

    email: str = Field(primary_key=True)
    time_zone: Optional[str] = Field(default=None)
    source: Optional[str] = Field(default=None)  # "mailbox" once a reading has been stored
    checked_at: Optional[datetime] = Field(default=None)  # the last lookup, good or not
    next_check_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class Ticket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # The ticket's identity: one number from a single counter shared by every type,
    # assigned on insert (see _prepare_new_ticket). It never changes, even when triage
    # changes the type. The reference (INC-1042) is the number with the type's prefix
    # for display; lookups use the number only, so a link keeps working after a retype.
    type: TicketType = Field(default=TicketType.INCIDENT)
    number: Optional[int] = Field(default=None, nullable=False, unique=True, index=True)

    @property
    def reference(self) -> Optional[str]:
        if self.number is None:
            return None
        return f"{TICKET_TYPE_PREFIXES[self.type]}-{self.number}"

    @property
    def tag(self) -> Optional[str]:
        """#1042: the stable form, for events and the audit log. The reference's prefix
        follows the type, so text stored with it would go stale after a retype."""
        return f"#{self.number}" if self.number is not None else None

    subject: str
    requester_email: str
    status: TicketStatus = Field(default=TicketStatus.NEW)
    source: TicketSource = Field(default=TicketSource.EMAIL)
    # Who created the ticket when that is a person other than the requester: the agent who
    # raised it for them (source "agent"), or the requester themselves in the portal.
    created_by_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    # The kind of service request this is, when it was raised from the request catalogue
    # (app/request_types.py). Null for everything else, including a service request an agent
    # retyped at triage.
    request_type_id: Optional[int] = Field(default=None, foreign_key="requesttype.id", index=True)
    graph_conversation_id: Optional[str] = Field(default=None, index=True)

    # When the problem was reported and when it was fixed, as entered. They
    # equal created_at and the status change time until backdating (30 days
    # max) lets an agent enter earlier dates.
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = Field(default=None)
    # Set by the resolve action (app/resolution.py): what was done, who resolved it (null for an
    # automatic resolve) and how many times it has been reopened. closed_at is when it was
    # closed (set by set_status whenever the status becomes Closed).
    resolution_notes: Optional[str] = Field(default=None)
    resolved_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    closed_at: Optional[datetime] = Field(default=None)
    reopen_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # Chasing a quiet Awaiting user ticket (app/auto_close_chase.py): when the chase email was queued and
    # how many have been (0 or 1 today). Cleared whenever the ticket leaves Awaiting user (a reply, the
    # reopen link, an agent's change), which is what cancels a chase.
    chased_at: Optional[datetime] = Field(default=None)
    chase_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # Set (and never cleared) once an agent enters earlier dates than the real ones,
    # so the ticket is logged after the fact. Such tickets count towards volume but are
    # left out of SLA figures; created_at still holds when it was really entered.
    is_backdated: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    # A service request's Required by date (app/required_by.py): when given, the end of that day
    # replaces the SLA resolution target (app/sla.py). A calendar date, read as UTC.
    required_by: Optional[date] = Field(default=None)
    # The Due date (app/required_by.py): the SLA resolution target is the end of this day in the requester's
    # time zone. Defaults to Required by minus the request type's days; `due_set_by_hand` is set when an agent
    # moved it, so a later change of Required by clamps it instead of recalculating it. Scheduled needs one.
    due_on: Optional[date] = Field(default=None)
    due_set_by_hand: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    # When an agent last changed a form answer after the first filling in (app/request_questions.py). Nothing else
    # reacts to it yet: it is the hook for the automatic sub-requests of #118 part 2 (answers changed after they
    # were made means a note for agents, never an automatic change).
    answers_changed_at: Optional[datetime] = Field(default=None)
    # The sub-requests an agent confirmed at triage (or the rules produced for an automatic triage) while the request
    # waits for approval: a list of {"template_id", "subject"}, made once approval passes and cleared then, or when the
    # approval fails (app/subrequests.py).
    pending_subrequests: Optional[list] = Field(default=None, sa_column=Column(JSON, nullable=True))

    # Set on a sub-ticket made by splitting another ticket (the original). One level only:
    # a sub-ticket is never split again. Separate from linked_parent_id, which is for
    # grouping tickets under a Major Incident or Problem.
    split_from_id: Optional[int] = Field(default=None, foreign_key="ticket.id", index=True)

    team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    assigned_agent_id: Optional[int] = Field(default=None, foreign_key="user.id")

    # Triage is its own state, separate from assignment. A ticket is untriaged
    # until someone saves the triage panel, which sets the category (and so the
    # team), the type, who triaged it and when. Nothing else leaves New.
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    triaged_at: Optional[datetime] = Field(default=None)
    triaged_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    # Set when it was triaged into "Other": after it is resolved a super admin
    # reviews it and gives it a proper category.
    needs_category_review: bool = Field(default=False)

    # Set when triaged (and editable after). Impact x urgency gives the priority
    # (app/sla.py). Stored as text: the values are the Level and Priority enums there.
    impact: Optional[str] = Field(default=None)
    urgency: Optional[str] = Field(default=None)
    priority: Optional[str] = Field(default=None, index=True)

    # The SLA clock (app/sla.py). The due times are created_at + target + the time
    # spent paused, and are recomputed when the priority changes or a pause ends.
    # While paused (Awaiting user) sla_paused_at is set and the clock is not running.
    first_response_due_at: Optional[datetime] = Field(default=None)
    resolution_due_at: Optional[datetime] = Field(default=None)
    first_responded_at: Optional[datetime] = Field(default=None)
    sla_paused_at: Optional[datetime] = Field(default=None)
    sla_paused_seconds: int = Field(default=0, sa_column_kwargs={"server_default": "0"})

    # Set when this ticket is linked to a Major Incident or Problem (app/linking.py).
    # One level only: a parent is never itself linked to anything.
    linked_parent_id: Optional[int] = Field(default=None, foreign_key="ticket.id", index=True)

    is_escalated: bool = Field(default=False)
    escalation_reason: Optional[str] = Field(default=None)
    escalated_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    escalated_at: Optional[datetime] = Field(default=None)

    # The MSP handoff (app/msp.py). msp_hold keeps the ticket out of the automatic handoff until the agent releases
    # it; msp_sent_at is when it last went to the MSP (by hand or by the 18:00 job).
    msp_hold: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    msp_sent_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    messages: list["Message"] = Relationship(back_populates="ticket")


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id")
    sender: str
    body: str
    direction: MessageDirection
    # Anything shown to the requester or sent as mail must come through
    # app/messages.requester_visible_messages, which returns PUBLIC only.
    visibility: MessageVisibility = Field(default=MessageVisibility.PUBLIC)
    # Who was copied on an outgoing email, as a comma-separated list, recorded at
    # send time so the timeline shows exactly who received it.
    cc: Optional[str] = Field(default=None)
    # The Graph id of the mail this message arrived as (app/inbound.py); blank for notes, portal
    # comments and outgoing mail, whose id sendMail does not return. A reply can be threaded onto it.
    graph_message_id: Optional[str] = Field(default=None, index=True)
    author_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    ticket: Ticket = Relationship(back_populates="messages")


class Attachment(SQLModel, table=True):
    """A file on a ticket (app/attachments.py). The bytes live in storage (app/storage.py) under
    blob_key, which is derived from the content hash, so identical files share one blob. Only
    content_type, detected from the bytes, ever decides how the file is served; the sender's
    claimed type and the filename are never trusted."""

    __table_args__ = (Index("ix_attachment_ticket_hash", "ticket_id", "content_hash"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    # The message it arrived with or was attached to; None for a file added to the ticket itself.
    message_id: Optional[int] = Field(default=None, foreign_key="message.id", index=True)
    filename: str
    size: int
    content_type: str
    content_hash: str  # sha-256, hex
    blob_key: str
    # Picture embedded in the body (a signature logo) rather than a file someone attached.
    is_inline: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    uploaded_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TicketFollower(SQLModel, table=True):
    """Someone who is copied on a ticket's public emails.

    Requesters and agents add followers by email; a follower with a portal
    account sees the ticket under Following (read-only). An internal follower is
    an agent (today: whoever escalated the ticket) who is copied while they follow, including
    on MSP mail. It grants no access to the ticket: that follows its team."""

    __table_args__ = (UniqueConstraint("ticket_id", "email", name="uq_ticketfollower_ticket_email"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    email: str  # lower case
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    is_internal: bool = Field(default=False)
    added_by_email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Category(SQLModel, table=True):
    """What a ticket is about. A main category (no parent) belongs to a team and
    decides where triaged tickets go; a sub-category inherits its parent's team.
    "Other" is the seeded main category an agent picks when nothing fits."""

    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_category_parent_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    parent_id: Optional[int] = Field(default=None, foreign_key="category.id", index=True)
    team_id: Optional[int] = Field(default=None, foreign_key="team.id")  # main categories only
    # Where escalating a ticket in this category sends it (app/escalation.py). A team,
    # or None for the MSP. A sub-category follows its parent's unless it overrides.
    escalation_team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    escalation_inherits: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    is_other: bool = Field(default=False)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Setting(SQLModel, table=True):
    """An admin-editable setting. Which keys exist, and their defaults, are
    declared in app/settings_store.py."""

    key: str = Field(primary_key=True)
    value: str


class CannedResponse(SQLModel, table=True):
    """A ready-made reply an agent can drop into the email box. Managed by admins in
    app/canned.py. Switching one off hides it from agents; it is never deleted, so the
    admin list keeps the wording."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(unique=True, index=True)
    body: str
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EmailTemplate(SQLModel, table=True):
    """An admin's wording for one automatic email. A row exists only for a template an admin has
    changed; no row means the default in app/email_templates.py (which also holds the list of keys
    and their placeholders). Resetting deletes the row."""

    key: str = Field(primary_key=True)
    subject: str = Field(default="")  # empty for a template whose subject is the ticket's own
    body: str
    updated_by: str  # the admin's email
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NeverFollowAddress(SQLModel, table=True):
    """An address that must never become a follower (the shared mailbox and its aliases, a
    distribution list). Managed by admins in app/never_follow.py; the address is stored
    normalised (lower case) and matched exactly."""

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    reason: str = Field(default="")
    added_by: str  # the admin's email
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ThanksRule(SQLModel, table=True):
    """An admin-managed rule for a thank-you reply (app/thanks_rules.py): a reply on a Resolved ticket whose
    new text matches an enabled rule is recorded and does not reopen the ticket. match_type is equals,
    starts_with, contains or ends_with; pattern is stored normalised (lower case, no punctuation or emoji)
    and may use * as a wildcard; max_length is the longest normalised reply it applies to."""

    id: Optional[int] = Field(default=None, primary_key=True)
    match_type: str
    pattern: str
    max_length: int
    enabled: bool = Field(default=True)
    created_by: str  # the admin's email, or "system" for a seeded rule
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ThanksCandidate(SQLModel, table=True):
    """A reply an agent reclosed as "user said thanks", waiting for an admin to make it a rule or dismiss
    it (app/thanks_rules.py). One row per distinct normalised text; occurrences counts how often it came
    up while pending. status is pending, dismissed or rule."""

    id: Optional[int] = Field(default=None, primary_key=True)
    normalised_text: str = Field(unique=True, index=True)
    sample_text: str  # the reply as written, cut short
    first_ticket_id: Optional[int] = Field(default=None, foreign_key="ticket.id")
    occurrences: int = Field(default=1)
    status: str = Field(default="pending", index=True)
    decided_by: Optional[str] = Field(default=None)
    decided_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalGroup(SQLModel, table=True):
    """A named set of people who vote on requests (app/approval_groups.py). A request type points at
    one. Members need an account (they sign in once) and need not be agents: approvers are often
    managers. Groups are never deleted, so an approval keeps the group it was asked of."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalGroupMember(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_approvalgroupmember"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="approvalgroup.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)


class UserPermission(SQLModel, table=True):
    """An extra permission a super admin has granted (app/permissions.py holds the registry of keys).
    Super admins need no row: they hold every permission. Whether a row counts also depends on the
    person's role (see app.visibility.has_permission)."""
    __table_args__ = (UniqueConstraint("user_id", "permission", name="uq_userpermission"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    permission: str = Field(index=True)
    granted_by: Optional[str] = Field(default=None)  # the super admin's email
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RequestType(SQLModel, table=True):
    """A thing people can ask IT for ("New starter", "Software install"), each with the
    group that approves it. Managed by admins in app/request_types.py. Switching one off hides
    it from the request form; it is never deleted, so tickets keep their request type."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = Field(default="")
    is_active: bool = Field(default=True)
    approval_group_id: Optional[int] = Field(default=None, foreign_key="approvalgroup.id", index=True)
    # Share of the voters that must approve for the request to pass, 1 to 100 (app/approvals.py);
    # it fails as soon as that can no longer be reached.
    pass_percent: int = Field(default=51)
    # Whether the request form offers a Required by date for this kind of request (app/required_by.py).
    offers_required_by: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    # Days before Required by that a ticket of this type is due, and days before Due that the agents' queue
    # ranks it (app/required_by.py, app/sla.py). Blank: the global setting is used.
    due_days_before: Optional[int] = Field(default=None)
    queue_lead_days: Optional[int] = Field(default=None)
    # Optional defaults (app/request_types.py). All four set: a portal request of this type triages itself
    # when raised (app/request_types.py `self_triage`); some set: they only prefill the triage form.
    default_team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    default_category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    default_impact: Optional[str] = Field(default=None)  # a sla.Level value
    default_urgency: Optional[str] = Field(default=None)
    # Off by default (#118 part 2): a sub-request of THIS kind made automatically from another type's templates is
    # covered by its parent's approval. On: it goes through this type's own approval group like any request.
    subrequests_need_own_approval: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RequestSubTemplate(SQLModel, table=True):
    """A sub-request template on a request type (#118 part 2, app/subrequests.py): when a request of the type is
    triaged (once approved, if it needs approval) each template whose conditions hold becomes a sub-ticket.
    `condition_groups` is a list of groups, each a list of {"question_id", "operator", "value"}: every condition of a
    group must hold (AND) and the template applies when any group does (OR); no groups means always. The sub-request
    gets `team_id` and `category_id` (the category belongs to the team), the optional kind of request, impact and
    urgency (the parent's when blank), the subject from `subject_pattern` ({Question label} is replaced by the answer),
    the named agent or the parent's assignee when they are available, and a Due date `due_days_before` days before the
    parent's Required by (blank: the Due date setting)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    request_type_id: int = Field(foreign_key="requesttype.id", index=True)
    position: int = Field(default=0)
    is_active: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    sub_request_type_id: Optional[int] = Field(default=None, foreign_key="requesttype.id")
    team_id: int = Field(foreign_key="team.id")
    category_id: int = Field(foreign_key="category.id")
    impact: Optional[str] = Field(default=None)
    urgency: Optional[str] = Field(default=None)
    subject_pattern: str
    assignee_id: Optional[int] = Field(default=None, foreign_key="user.id")
    assign_to_parent_assignee: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    due_days_before: Optional[int] = Field(default=None)
    condition_groups: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, server_default="[]"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RequestTypeQuestion(SQLModel, table=True):
    """A form question on a request type (#118 part 1, app/request_questions.py): short text, a pick list
    (`options`), yes/no or a date, optionally required. `position` orders them. Once answered a question is switched
    off (`is_active` false), never deleted, so tickets keep their answers; its id is stable, which is what
    conditions on the answers (part 2) refer to."""

    id: Optional[int] = Field(default=None, primary_key=True)
    request_type_id: int = Field(foreign_key="requesttype.id", index=True)
    label: str
    kind: str  # text, choice, yes_no, date
    options: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, server_default="[]"))
    is_required: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    is_active: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    position: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TicketAnswer(SQLModel, table=True):
    """One answer to a form question on a ticket. `value` is canonical text whatever the kind (the typed text, the
    chosen option, "yes" or "no", an ISO date), so a condition (equals, not equals, is one of, is answered) is a
    comparison of strings and a row's existence is "answered". A blank answer has no row. The question's label and
    kind are copied as they were when answered (`question_label`, `question_kind`), so the ticket keeps reading
    the same after an admin renames or retires the question; `question_id` is the stable reference."""

    __table_args__ = (UniqueConstraint("ticket_id", "question_id", name="uq_ticketanswer_ticket_question"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    question_id: int = Field(foreign_key="requesttypequestion.id", index=True)
    question_label: str
    question_kind: str
    value: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TicketApproval(SQLModel, table=True):
    """The approval asked of a ticket (app/approvals.py): at most one pending at a time. The group,
    the pass percentage and the number of voters are copied when it starts, so editing the request
    type or the group later cannot change a request already waiting."""

    __table_args__ = (
        Index("uq_ticketapproval_one_pending", "ticket_id", unique=True, postgresql_where=text("status = 'pending'")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    group_id: int = Field(foreign_key="approvalgroup.id", index=True)
    pass_percent: int
    voters: int  # fixed when the approval starts
    status: str = Field(default="pending")  # pending, approved, declined, waived, withdrawn
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    settled_at: Optional[datetime] = Field(default=None)
    # Set once when the super admins were told this approval has waited too long (app/approval_reminders.py).
    escalated_at: Optional[datetime] = Field(default=None)


class ApprovalVote(SQLModel, table=True):
    """One voter's say on an approval, created for every voter when it starts. pending until they
    decide; not_needed once the outcome was settled without them."""

    __table_args__ = (UniqueConstraint("approval_id", "voter_id", name="uq_approvalvote"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    approval_id: int = Field(foreign_key="ticketapproval.id", index=True)
    voter_id: int = Field(foreign_key="user.id", index=True)
    decision: str = Field(default="pending")  # pending, approved, declined, not_needed
    comment: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: Optional[datetime] = Field(default=None)
    # Reminders queued to this voter and when the last one was (app/approval_reminders.py): what makes the
    # reminder job safe to run late or twice.
    reminder_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_reminded_at: Optional[datetime] = Field(default=None)


class ActionToken(SQLModel, table=True):
    """An emailed one-time link (phase 4F): the feedback or reopen link in the resolution email. Only the
    hash of the token is stored, so the database cannot be used to build a working link; the link itself is
    made when the email is sent and never kept (app/action_links.py)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    kind: str  # feedback, reopen
    token_hash: str = Field(unique=True)
    # Who resolved the ticket when the link was made: a rating is about that agent, even if the ticket is
    # reopened and resolved by someone else before the link is used.
    resolved_by_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    used_at: Optional[datetime] = Field(default=None)


class TicketFeedback(SQLModel, table=True):
    """The requester's rating of how a ticket was handled, given once through the emailed link. Never
    changed afterwards."""

    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="ck_ticketfeedback_rating"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    token_id: int = Field(foreign_key="actiontoken.id", unique=True)
    rating: int
    comment: str = Field(default="")
    resolved_by_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Notification(SQLModel, table=True):
    """Something an agent should know about (app/notifications.py): shown in the bell until read.
    One row per recipient."""

    # The bell counts unread rows for every open tab every 30 seconds; this keeps that cheap
    # however much read history there is.
    __table_args__ = (
        Index("ix_notification_user_unread", "user_id", postgresql_where=text("read_at IS NULL")),
        # The worker looks only for rows wanting a Teams copy that has not been dealt with.
        Index(
            "ix_notification_teams_due", "created_at",
            postgresql_where=text("teams_wanted AND teams_handled_at IS NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    kind: str  # assigned, mentioned
    ticket_id: Optional[int] = Field(default=None, foreign_key="ticket.id", index=True)
    actor_email: Optional[str] = Field(default=None)  # who caused it; None for the system
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    read_at: Optional[datetime] = Field(default=None)
    # Whether it shows in the bell. False when the person turned the bell off for this event but
    # still wants Teams: such a row is written already read, so the bell never counts it.
    bell: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    # The person wanted a Teams copy when it was written. teams_handled_at is set once the worker has
    # dealt with it: handed to Teams, or dropped (they turned Teams off since, or can no longer open
    # the ticket). A copy is sent only if it is still wanted at that moment.
    teams_wanted: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    teams_handled_at: Optional[datetime] = Field(default=None)


class NotificationPreference(SQLModel, table=True):
    """One person's choice to differ from the default for one event on one channel
    (app/notification_settings.py). A row exists only while it differs from the default."""

    __table_args__ = (UniqueConstraint("user_id", "kind", "channel"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    kind: str  # an event type: assigned, mentioned, raised_for_you
    channel: str  # in_app, teams, email
    enabled: bool


class TeamsPost(SQLModel, table=True):
    """A message waiting to go to a Teams channel (app/teams.py). Written in the same transaction as
    what it announces, so a ticket is never announced and then rolled back, or created and
    forgotten. The worker sends it and marks it sent."""

    # The sender looks for unsent rows only; this keeps that cheap however many were sent.
    __table_args__ = (Index("ix_teamspost_unsent", "id", postgresql_where=text("sent_at IS NULL")),)

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # new_ticket, notification
    ticket_id: Optional[int] = Field(default=None, foreign_key="ticket.id", index=True)
    # For a "notification" post: the notification it carries. Unique, so one notification can never
    # be posted twice, whatever happens to the job that queues it.
    notification_id: Optional[int] = Field(default=None, foreign_key="notification.id", unique=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: Optional[datetime] = Field(default=None)
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    next_attempt_at: Optional[datetime] = Field(default=None)  # after a failure: not before this
    last_error: Optional[str] = Field(default=None)  # why the last attempt failed; never the URL


class OutboundEmail(SQLModel, table=True):
    """An email waiting to go out from the shared mailbox (app/outbox.py queues it, app/mail.py
    builds and sends it). Written in the same transaction as what it is about, so a reply is never
    recorded and then forgotten, or sent and then rolled back. The row keeps what was sent
    (subject, copied addresses, text) and, for a failure, why."""

    __table_args__ = (
        Index("ix_outboundemail_unsent", "id", postgresql_where=text("sent_at IS NULL")),
        # One follower update per person per message, so a replay can never tell anyone twice.
        UniqueConstraint("about_message_id", "to_address", name="uq_outboundemail_about_message_to"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # reply, notice, raised_for_you, approval, approval_reminder, approval_withdrawn, approval_overdue, acknowledgement, follower_update, chase
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    # For a reply or notice: the message it carries. Unique, so one message can never be emailed twice.
    message_id: Optional[int] = Field(default=None, foreign_key="message.id", unique=True)
    # For an approval email: the vote it asks for. The link is made when the email is sent.
    vote_id: Optional[int] = Field(default=None, foreign_key="approvalvote.id", index=True)
    # For a follower update: the inbound message being passed on. Not unique by itself (one message is
    # passed to several people); the pair with to_address is.
    about_message_id: Optional[int] = Field(default=None, foreign_key="message.id", index=True)
    to_address: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: Optional[datetime] = Field(default=None)
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    next_attempt_at: Optional[datetime] = Field(default=None)  # after a failure: not before this
    last_error: Optional[str] = Field(default=None)
    # What was built for the send (never an approval link).
    subject: Optional[str] = Field(default=None)
    cc: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)


class ScheduledJob(SQLModel, table=True):
    """One recurring job driven by the database (app/scheduled_jobs.py): when it is next due, when it
    last worked, who holds it right now, and why it last failed. One row per registered job, created by
    the worker's tick. A job that is overdue runs once, not once per missed slot."""

    name: str = Field(primary_key=True)
    next_run_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    last_started_at: Optional[datetime] = Field(default=None)
    last_success_at: Optional[datetime] = Field(default=None)
    # The lease: set when a worker claims the job, cleared when it finishes. A lease that has expired
    # (the worker crashed mid-run) no longer blocks the job.
    lease_owner: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    last_error_at: Optional[datetime] = Field(default=None)
    failures_in_a_row: int = Field(default=0, sa_column_kwargs={"server_default": "0"})


class GraphNotification(SQLModel, table=True):
    """A change notification Microsoft Graph sent to the webhook (phase 3A). The webhook only checks
    the clientState and stores one row per notification, then answers 202 within Graph's three
    seconds. The worker (3B) fetches the message and turns it into a ticket or a reply, and (3C) acts
    on subscription lifecycle events. Nothing here is mail content: Graph sends only ids."""

    # The worker looks for unprocessed rows only; this keeps that cheap however many were handled.
    __table_args__ = (
        Index("ix_graphnotification_unprocessed", "id", postgresql_where=text("processed_at IS NULL")),
        # The catch-up sync (app/subscriptions.py) asks which message ids it has already handed over.
        Index("ix_graphnotification_resource_id", "resource_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    subscription_id: Optional[str] = Field(default=None)
    change_type: Optional[str] = Field(default=None)  # created, updated, deleted
    # Set on a lifecycle notification (reauthorizationRequired, subscriptionRemoved, missed), which
    # arrives at the same address; blank for a change to a message.
    lifecycle_event: Optional[str] = Field(default=None)
    resource: Optional[str] = Field(default=None)  # e.g. users/<mailbox>/messages/<id>
    resource_id: Optional[str] = Field(default=None)  # the message id, from resourceData.id
    processed_at: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    # 3B: tries so far and the earliest next one, so a message Graph cannot give us yet backs off and
    # one that never works is given up on instead of being fetched every run.
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    next_attempt_at: Optional[datetime] = Field(default=None)


class InboundMail(SQLModel, table=True):
    """One row for every mail the intake has handled, whatever came of it (app/inbound.py). The unique
    Graph id and internet message id are what stop the same mail becoming two tickets or two replies, however
    often a notification or the catch-up job hands it over. Holds ids and the outcome, not the mail."""

    id: Optional[int] = Field(default=None, primary_key=True)
    graph_message_id: str = Field(unique=True)
    # The Message-ID header: the same email reaching the mailbox twice gets two Graph ids but one of these.
    internet_message_id: Optional[str] = Field(default=None, unique=True)
    # Graph's thread id. A later mail in the same conversation is matched to this row's ticket.
    conversation_id: Optional[str] = Field(default=None, index=True)
    sender: Optional[str] = Field(default=None, index=True)
    # Who the mail was addressed to, lower-case bare addresses joined by ", " (4B-6). What decides which
    # followers were not copied is worked out from the mail itself; these are the record of it.
    to_addresses: Optional[str] = Field(default=None)
    cc_addresses: Optional[str] = Field(default=None)
    outcome: str  # ticket_created, reply_added or ignored_*
    ticket_id: Optional[int] = Field(default=None, foreign_key="ticket.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="message.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GraphSubscription(SQLModel, table=True):
    """The one Graph change-notification subscription this app keeps on the shared mailbox's inbox
    (phase 3C, app/subscriptions.py). One row per watched resource: the unique `resource` is what stops
    two workers, or a restart, from each creating their own. The row is locked while it is checked,
    created or renewed. Holds no secret: the clientState is kept only as a short fingerprint, so a
    changed GRAPH_WEBHOOK_CLIENT_STATE is noticed and the subscription recreated."""

    id: Optional[int] = Field(default=None, primary_key=True)
    resource: str = Field(unique=True)
    subscription_id: Optional[str] = Field(default=None)  # Graph's id; None until created (or after it was lost)
    expires_at: Optional[datetime] = Field(default=None)
    client_state_fingerprint: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_renewed_at: Optional[datetime] = Field(default=None)  # a create or a renewal
    last_checked_at: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    last_error_at: Optional[datetime] = Field(default=None)


class MailSyncState(SQLModel, table=True):
    """Where the catch-up sync of a mailbox folder has got to (phase 3C): Graph's delta query hands
    back a deltaLink that means "everything changed since here", kept so the next run starts there.
    `resume_link` is the next page of a run that was cut short. Opaque links, not secrets, and never logged."""

    id: Optional[int] = Field(default=None, primary_key=True)
    folder: str = Field(unique=True)  # e.g. users/<mailbox>/mailFolders/inbox
    delta_link: Optional[str] = Field(default=None)
    resume_link: Optional[str] = Field(default=None)
    # Where this folder's mail starts counting: set to "now" by the very first run (which hands nothing
    # over, so existing inbox mail is never imported), or to the start of the backfill window when
    # Graph forgot our place. None means no run has started yet.
    starting_point_at: Optional[datetime] = Field(default=None)
    last_synced_at: Optional[datetime] = Field(default=None)
    last_handed_over: int = Field(default=0, sa_column_kwargs={"server_default": "0"})  # new ids at the last run
    last_error: Optional[str] = Field(default=None)
    last_error_at: Optional[datetime] = Field(default=None)


TICKET_COUNTER = "ticket"


class NumberCounter(SQLModel, table=True):
    """The last number issued, one row per counter. Tickets use the one named "ticket",
    shared by every type."""

    name: str = Field(primary_key=True)
    last_number: int = Field(default=0)


class TicketEvent(SQLModel, table=True):
    """History of a ticket for reporting: what changed, who did it, when.
    Separate from the audit log, which covers the whole system."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    event_type: str
    actor_email: Optional[str] = Field(default=None)  # None for system events
    from_value: Optional[str] = Field(default=None)
    to_value: Optional[str] = Field(default=None)
    detail: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActivityLog(SQLModel, table=True):
    """Security and request activity: sign-ins, failed sign-ins, lockouts, denied
    access and every state-changing request. Separate from AuditLog, which records
    named business actions on a ticket, user or team. Holds no request bodies,
    passwords, tokens or query strings, and the path is the route template."""

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )
    event_type: str = Field(index=True)
    actor_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    # The signed-in user's email, or for a failed sign-in the address that was tried.
    actor_email: Optional[str] = Field(default=None, index=True)
    method: Optional[str] = Field(default=None)
    path: Optional[str] = Field(default=None)  # route template, e.g. /tickets/{number}/note
    status_code: Optional[int] = Field(default=None)
    ip: Optional[str] = Field(default=None)
    user_agent: Optional[str] = Field(default=None)
    detail: Optional[str] = Field(default=None)


class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    actor_email: str
    action: str
    detail: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def allocate_number(connection) -> int:
    """Issues the next ticket number, shared by every type. The counter row is updated
    atomically; on Postgres the row lock is held until the transaction ends, so two
    tickets created at once cannot get the same number."""
    counter = NumberCounter.__table__
    bump = (
        update(counter)
        .where(counter.c.name == TICKET_COUNTER)
        .values(last_number=counter.c.last_number + 1)
        .returning(counter.c.last_number)
    )
    number = connection.execute(bump).scalar()
    if number is None:
        # No row yet. The migration seeds it, but code must not depend on that: create
        # it if absent (a concurrent creator may win the race, which is fine), then bump.
        connection.execute(
            postgresql_insert(counter).values(name=TICKET_COUNTER, last_number=0).on_conflict_do_nothing()
        )
        number = connection.execute(bump).scalar()
    return number


@event.listens_for(Ticket, "before_insert")
def _prepare_new_ticket(mapper, connection, ticket: Ticket) -> None:
    """Gives every new ticket its number, so no code path can create one without, and
    keeps one rule true: a ticket that is not New has been triaged. (Tickets are
    created New, and only triage moves them on.)"""
    if ticket.number is None:
        ticket.number = allocate_number(connection)
    if ticket.status != TicketStatus.NEW and ticket.triaged_at is None:
        ticket.triaged_at = datetime.now(timezone.utc)

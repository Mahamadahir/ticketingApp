from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, event, text, update
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TicketEscalator(SQLModel, table=True):
    """An agent who escalated this ticket to another team. They keep working access to
    this one ticket (app/visibility.py); nothing else about being an escalator grants any."""

    ticket_id: int = Field(foreign_key="ticket.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", primary_key=True)


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    # The approval rule this ticket is held to, copied from its request type when approvals
    # start so that editing the request type later does not change a request already waiting.
    approvals_required: Optional[int] = Field(default=None)
    declines_required: Optional[int] = Field(default=None)
    graph_conversation_id: Optional[str] = Field(default=None, index=True)

    # When the problem was reported and when it was fixed, as entered. They
    # equal created_at and the status change time until backdating (30 days
    # max) lets an agent enter earlier dates.
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = Field(default=None)
    # Set (and never cleared) once an agent enters earlier dates than the real ones,
    # so the ticket is logged after the fact. Such tickets count towards volume but are
    # left out of SLA figures; created_at still holds when it was really entered.
    is_backdated: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})

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
    graph_message_id: Optional[str] = Field(default=None)
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


class RequestType(SQLModel, table=True):
    """A thing people can ask IT for ("New starter", "Software install"), each with the
    people who approve it. Managed by admins in app/request_types.py. Switching one off hides
    it from the request form; it is never deleted, so tickets keep their request type."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = Field(default="")
    is_active: bool = Field(default=True)
    # How many approvers must approve before the work goes ahead, and how many must decline
    # before the request is closed. Both default to one: any single approver decides.
    approvals_required: int = Field(default=1)
    declines_required: int = Field(default=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RequestTypeApprover(SQLModel, table=True):
    """Someone who approves requests of a type. They need an account (they sign in once),
    and need not be an agent: approvers are often managers."""

    __table_args__ = (UniqueConstraint("request_type_id", "user_id", name="uq_requesttypeapprover"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    request_type_id: int = Field(foreign_key="requesttype.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)


class TicketApproval(SQLModel, table=True):
    """One approver's say on one ticket (app/approvals.py). pending until they decide; not_needed
    once the ticket's outcome was settled without them."""

    __table_args__ = (UniqueConstraint("ticket_id", "approver_id", name="uq_ticketapproval"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    approver_id: int = Field(foreign_key="user.id", index=True)
    decision: str = Field(default="pending")  # pending, approved, declined, not_needed
    comment: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: Optional[datetime] = Field(default=None)


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

    __table_args__ = (Index("ix_outboundemail_unsent", "id", postgresql_where=text("sent_at IS NULL")),)

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # reply, notice, raised_for_you, approval
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    # For a reply or notice: the message it carries. Unique, so one message can never be emailed twice.
    message_id: Optional[int] = Field(default=None, foreign_key="message.id", unique=True)
    # For an approval email: the approval it asks about. The link is made when the email is sent.
    approval_id: Optional[int] = Field(default=None, foreign_key="ticketapproval.id", index=True)
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

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import event, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlmodel import Field, Relationship, SQLModel


class TicketStatus(str, Enum):
    NEW = "new"  # untriaged
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_USER = "awaiting_user"
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


# Reference prefix per ticket type, e.g. INC-42.
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
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    AGENT = "agent"
    END_USER = "end_user"


class Team(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    role: UserRole = Field(default=UserRole.END_USER)
    team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    # Set only for local accounts (no Entra ID identity, e.g. a contractor).
    # SSO accounts are provisioned just-in-time on first login and never
    # have a password_hash.
    password_hash: Optional[str] = Field(default=None)
    failed_login_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    locked_until: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Ticket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # Assigned on insert (see app/ticket_numbers.py): a separate counter per
    # type, so INC-1 and SR-1 both exist. The reference is the shareable id.
    type: TicketType = Field(default=TicketType.INCIDENT)
    number: Optional[int] = Field(default=None, nullable=False)
    reference: Optional[str] = Field(default=None, nullable=False, unique=True, index=True)

    subject: str
    requester_email: str
    status: TicketStatus = Field(default=TicketStatus.NEW)
    source: TicketSource = Field(default=TicketSource.EMAIL)
    graph_conversation_id: Optional[str] = Field(default=None, index=True)

    # When the problem was reported and when it was fixed, as entered. They
    # equal created_at and the status change time until backdating (30 days
    # max) lets an agent enter earlier dates.
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = Field(default=None)

    team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    assigned_agent_id: Optional[int] = Field(default=None, foreign_key="user.id")

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
    graph_message_id: Optional[str] = Field(default=None)
    author_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    ticket: Ticket = Relationship(back_populates="messages")


class TicketCounter(SQLModel, table=True):
    """Last number issued for each ticket type."""

    ticket_type: TicketType = Field(primary_key=True)
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


class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    actor_email: str
    action: str
    detail: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@event.listens_for(Ticket, "before_insert")
def _assign_reference(mapper, connection, ticket: Ticket) -> None:
    """Gives every new ticket its number and reference (INC-42), so no code
    path can create one without. The counter row is updated atomically; on
    Postgres the row lock is held until the transaction ends, so two tickets
    created at once cannot get the same number."""
    if ticket.reference:
        return

    counter = TicketCounter.__table__
    bump = (
        update(counter)
        .where(counter.c.ticket_type == ticket.type)
        .values(last_number=counter.c.last_number + 1)
        .returning(counter.c.last_number)
    )
    number = connection.execute(bump).scalar()
    if number is None:
        # No row yet for this type. The migration seeds them, but code must
        # not depend on that: create it if absent (a concurrent creator may
        # win the race, which is fine), then bump it.
        connection.execute(
            postgresql_insert(counter).values(ticket_type=ticket.type, last_number=0).on_conflict_do_nothing()
        )
        number = connection.execute(bump).scalar()

    ticket.number = number
    ticket.reference = f"{TICKET_TYPE_PREFIXES[ticket.type]}-{number}"

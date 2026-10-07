from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


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
    subject: str
    requester_email: str
    status: TicketStatus = Field(default=TicketStatus.OPEN)
    graph_conversation_id: Optional[str] = Field(default=None, index=True)

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


class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    actor_email: str
    action: str
    detail: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

"""Scheduled ticket status

Revision ID: c4e7a2b9d106
Revises: 8b3e5a1c9d27
Create Date: 2026-10-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4e7a2b9d106'
down_revision: Union[str, Sequence[str], None] = '8b3e5a1c9d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # A new enum value cannot be used in the transaction that adds it.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE ticketstatus ADD VALUE IF NOT EXISTS 'SCHEDULED'")


def downgrade() -> None:
    # The SCHEDULED enum value stays: Postgres cannot drop a value from an enum, and an unused
    # value is harmless. Tickets still Scheduled move to In progress so the older code, which does
    # not know the status, can still read them. The pause Scheduled started is ended here, as moving
    # out of Scheduled would: the time waited is added to sla_paused_seconds and to the stored
    # deadlines (a Required by date does not move), so the ticket does not look overdue at once.
    waited = "GREATEST(0, EXTRACT(EPOCH FROM (now() - sla_paused_at))::int)"
    op.execute(
        "UPDATE ticket SET "
        f"first_response_due_at = first_response_due_at + make_interval(secs => {waited}), "
        f"resolution_due_at = CASE WHEN required_by IS NULL THEN resolution_due_at + make_interval(secs => {waited}) "
        "ELSE resolution_due_at END, "
        f"sla_paused_seconds = sla_paused_seconds + {waited}, sla_paused_at = NULL "
        "WHERE status = 'SCHEDULED' AND sla_paused_at IS NOT NULL"
    )
    op.execute("UPDATE ticket SET status = 'IN_PROGRESS' WHERE status = 'SCHEDULED'")

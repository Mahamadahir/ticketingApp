"""sla reopen restart

Revision ID: a355c0de01b2
Revises: c3e9a1d7b546
Create Date: 2026-11-27 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a355c0de01b2'
down_revision: Union[str, Sequence[str], None] = 'c3e9a1d7b546'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('ticket', sa.Column('resolution_restarted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ticket', sa.Column('resolution_restart_paused_seconds', sa.Integer(), server_default='0', nullable=False))
    op.add_column('ticket', sa.Column('first_resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ticket', sa.Column('first_resolution_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ticket', sa.Column('first_resolution_paused_seconds', sa.Integer(), nullable=True))
    # Tickets reopened before this change: keep the first resolution on record so the reports count each once. Their
    # deadline was never restarted, so it is still the one the first resolution was judged against. The restart itself is
    # not backfilled (it needs the priority targets from the settings): they keep the deadline they have.
    op.execute(
        """
        UPDATE ticket SET
            first_resolved_at = first_event.at,
            first_resolution_due_at = ticket.resolution_due_at,
            first_resolution_paused_seconds = ticket.sla_paused_seconds
        FROM (
            SELECT ticket_id, MIN(created_at) AS at FROM ticketevent
            WHERE event_type = 'status_changed' AND to_value IN ('resolved', 'closed')
            GROUP BY ticket_id
        ) AS first_event
        WHERE first_event.ticket_id = ticket.id AND ticket.reopen_count > 0
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ticket', 'first_resolution_paused_seconds')
    op.drop_column('ticket', 'first_resolution_due_at')
    op.drop_column('ticket', 'first_resolved_at')
    op.drop_column('ticket', 'resolution_restart_paused_seconds')
    op.drop_column('ticket', 'resolution_restarted_at')

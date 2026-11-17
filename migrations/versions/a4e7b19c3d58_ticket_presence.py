"""ticket presence: who has a ticket open and what they are doing

Revision ID: a4e7b19c3d58
Revises: c239d1a7e5b3
Create Date: 2026-11-17 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a4e7b19c3d58'
down_revision: Union[str, Sequence[str], None] = 'c239d1a7e5b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ticket_presence',
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('activity', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('action_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('last_seen_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('ticket_id', 'user_id'),
    )
    op.create_index(op.f('ix_ticket_presence_last_seen_at'), 'ticket_presence', ['last_seen_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_ticket_presence_last_seen_at'), table_name='ticket_presence')
    op.drop_table('ticket_presence')

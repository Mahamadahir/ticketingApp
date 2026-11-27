"""attachment retries

An email whose attachments could not be read twice is taken without its files (#348); this table holds the retry of the
files on their own, and `graphnotification.attachment_failures` counts the failed attachment reads of a notification.

Revision ID: e5b81c4a7d29
Revises: d41e7a9b3c05
Create Date: 2026-11-27 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e5b81c4a7d29'
down_revision: Union[str, Sequence[str], None] = 'd41e7a9b3c05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('graphnotification', sa.Column('attachment_failures', sa.Integer(), server_default='0', nullable=False))
    op.create_table(
        'attachment_retry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('graph_message_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['message.id']),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('graph_message_id'),
    )
    op.create_index(op.f('ix_attachment_retry_ticket_id'), 'attachment_retry', ['ticket_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_attachment_retry_ticket_id'), table_name='attachment_retry')
    op.drop_table('attachment_retry')
    op.drop_column('graphnotification', 'attachment_failures')

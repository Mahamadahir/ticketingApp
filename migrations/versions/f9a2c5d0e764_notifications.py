"""in-app notifications

Revision ID: f9a2c5d0e764
Revises: e8f1b4c9d653
Create Date: 2026-10-14 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f9a2c5d0e764'
down_revision: Union[str, Sequence[str], None] = 'e8f1b4c9d653'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=True),
        sa.Column('actor_email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notification_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notification_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_created_at'))
        batch_op.drop_index(batch_op.f('ix_notification_ticket_id'))
        batch_op.drop_index(batch_op.f('ix_notification_user_id'))
    op.drop_table('notification')
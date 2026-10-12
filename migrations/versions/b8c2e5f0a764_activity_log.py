"""activity log

Adds activity_log: sign-ins, failed sign-ins, lockouts, refused access and every
state-changing request, for super admins. See app/activity.py for what is and is
never recorded.

Revision ID: b8c2e5f0a764
Revises: a7b1d4e9f653
Create Date: 2026-10-12 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b8c2e5f0a764'
down_revision: Union[str, Sequence[str], None] = 'a7b1d4e9f653'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'activitylog',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('actor_email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('method', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('path', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('ip', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('user_agent', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('detail', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['actor_user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('activitylog', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activitylog_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_activitylog_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_activitylog_actor_email'), ['actor_email'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('activitylog', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activitylog_actor_email'))
        batch_op.drop_index(batch_op.f('ix_activitylog_event_type'))
        batch_op.drop_index(batch_op.f('ix_activitylog_created_at'))
    op.drop_table('activitylog')
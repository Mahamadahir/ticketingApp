"""notification Teams copies: which rows want one, and each person's delay

Revision ID: b3e6d9a2f418
Revises: b2d5f8a1c306
Create Date: 2026-10-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e6d9a2f418'
down_revision: Union[str, Sequence[str], None] = 'b2d5f8a1c306'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows stay as they were: shown in the bell, and no Teams copy (so turning this on
    # does not send a flood of old notifications).
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bell', sa.Boolean(), server_default='true', nullable=False))
        batch_op.add_column(sa.Column('teams_wanted', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('teams_handled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            'ix_notification_teams_due', ['created_at'], unique=False,
            postgresql_where=sa.text('teams_wanted AND teams_handled_at IS NULL'),
        )
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('teams_delay_minutes', sa.Integer(), nullable=True))
    with op.batch_alter_table('teamspost', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notification_id', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('teamspost_notification_id_key', ['notification_id'])
        batch_op.create_foreign_key('teamspost_notification_id_fkey', 'notification', ['notification_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('teamspost', schema=None) as batch_op:
        batch_op.drop_constraint('teamspost_notification_id_fkey', type_='foreignkey')
        batch_op.drop_constraint('teamspost_notification_id_key', type_='unique')
        batch_op.drop_column('notification_id')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('teams_delay_minutes')
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_teams_due', postgresql_where=sa.text('teams_wanted AND teams_handled_at IS NULL'))
        batch_op.drop_column('teams_handled_at')
        batch_op.drop_column('teams_wanted')
        batch_op.drop_column('bell')
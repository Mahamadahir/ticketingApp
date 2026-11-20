"""event groups, deleted notes and the note a mention came from (#255)

Revision ID: c255a3f8d1e6
Revises: b6d83f21a9c4
Create Date: 2026-11-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c255a3f8d1e6'
down_revision: Union[str, Sequence[str], None] = 'b6d83f21a9c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ticketevent', schema=None) as batch_op:
        batch_op.add_column(sa.Column('group_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column('deleted_by_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_message_deleted_by_id_user', 'user', ['deleted_by_id'], ['id'])
        batch_op.create_index('ix_message_deleted', ['deleted_at'], unique=False, postgresql_where=sa.text('deleted_at IS NOT NULL'))

    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.add_column(sa.Column('message_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_notification_message_id'), ['message_id'], unique=False)
        batch_op.create_foreign_key('fk_notification_message_id_message', 'message', ['message_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_constraint('fk_notification_message_id_message', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_notification_message_id'))
        batch_op.drop_column('message_id')

    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_index('ix_message_deleted', postgresql_where=sa.text('deleted_at IS NOT NULL'))
        batch_op.drop_constraint('fk_message_deleted_by_id_user', type_='foreignkey')
        batch_op.drop_column('deleted_by_id')
        batch_op.drop_column('deleted_at')

    with op.batch_alter_table('ticketevent', schema=None) as batch_op:
        batch_op.drop_column('group_id')

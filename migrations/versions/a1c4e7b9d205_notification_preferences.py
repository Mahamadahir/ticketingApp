"""notification preferences

Revision ID: a1c4e7b9d205
Revises: f9a2c5d0e764
Create Date: 2026-10-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1c4e7b9d205'
down_revision: Union[str, Sequence[str], None] = 'f9a2c5d0e764'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notificationpreference',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('channel', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'kind', 'channel'),
    )
    with op.batch_alter_table('notificationpreference', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notificationpreference_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('notificationpreference', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notificationpreference_user_id'))
    op.drop_table('notificationpreference')

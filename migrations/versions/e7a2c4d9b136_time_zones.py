"""per-person time zones

Adds the person's own time zone and where it came from to user, and the requester_time_zone table
(the Outlook mailbox time zone of people who write in by email, keyed by lower-cased address).

Revision ID: e7a2c4d9b136
Revises: a7d3e1f60b92
Create Date: 2026-11-03 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e7a2c4d9b136'
down_revision: Union[str, Sequence[str], None] = 'a7d3e1f60b92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('time_zone', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('time_zone_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_table(
        'requestertimezone',
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('time_zone', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('email'),
    )
    with op.batch_alter_table('requestertimezone', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_requestertimezone_next_check_at'), ['next_check_at'], unique=False)
    # Everyone who already has a ticket is looked up by the worker too, not only people who write in later.
    op.execute(
        "INSERT INTO requestertimezone (email, next_check_at) "
        "SELECT DISTINCT lower(trim(requester_email)), now() FROM ticket WHERE requester_email IS NOT NULL "
        "ON CONFLICT (email) DO NOTHING"
    )


def downgrade() -> None:
    with op.batch_alter_table('requestertimezone', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_requestertimezone_next_check_at'))
    op.drop_table('requestertimezone')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('time_zone_source')
        batch_op.drop_column('time_zone')

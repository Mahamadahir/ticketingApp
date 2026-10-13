"""sub-tickets: the ticket a ticket was split from

Revision ID: a4b7d0e5f219
Revises: f3a6c9d4e108
Create Date: 2026-10-13 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4b7d0e5f219'
down_revision: Union[str, Sequence[str], None] = 'f3a6c9d4e108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('split_from_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_split_from_id'), ['split_from_id'], unique=False)
        batch_op.create_foreign_key('ticket_split_from_id_fkey', 'ticket', ['split_from_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('ticket_split_from_id_fkey', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ticket_split_from_id'))
        batch_op.drop_column('split_from_id')

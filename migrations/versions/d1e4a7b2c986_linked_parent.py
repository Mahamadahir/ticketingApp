"""link a ticket to a parent Major Incident or Problem

Revision ID: d1e4a7b2c986
Revises: c9d3f6a1b875
Create Date: 2026-10-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e4a7b2c986'
down_revision: Union[str, Sequence[str], None] = 'c9d3f6a1b875'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('linked_parent_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_linked_parent_id'), ['linked_parent_id'], unique=False)
        batch_op.create_foreign_key('ticket_linked_parent_id_fkey', 'ticket', ['linked_parent_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('ticket_linked_parent_id_fkey', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ticket_linked_parent_id'))
        batch_op.drop_column('linked_parent_id')

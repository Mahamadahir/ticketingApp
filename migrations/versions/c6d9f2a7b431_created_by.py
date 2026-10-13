"""who created a ticket

Revision ID: c6d9f2a7b431
Revises: b5c8e1f6a320
Create Date: 2026-10-13 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d9f2a7b431'
down_revision: Union[str, Sequence[str], None] = 'b5c8e1f6a320'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_foreign_key('ticket_created_by_id_fkey', 'user', ['created_by_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('ticket_created_by_id_fkey', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ticket_created_by_id'))
        batch_op.drop_column('created_by_id')
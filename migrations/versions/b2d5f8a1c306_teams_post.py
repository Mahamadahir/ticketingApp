"""teams post outbox

Revision ID: b2d5f8a1c306
Revises: a1c4e7b9d205
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b2d5f8a1c306'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7b9d205'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'teamspost',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('teamspost', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_teamspost_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index('ix_teamspost_unsent', ['id'], unique=False, postgresql_where=sa.text('sent_at IS NULL'))


def downgrade() -> None:
    with op.batch_alter_table('teamspost', schema=None) as batch_op:
        batch_op.drop_index('ix_teamspost_unsent', postgresql_where=sa.text('sent_at IS NULL'))
        batch_op.drop_index(batch_op.f('ix_teamspost_ticket_id'))
    op.drop_table('teamspost')

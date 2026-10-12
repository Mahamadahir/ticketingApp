"""priority and the SLA clock

Adds impact, urgency and priority to ticket, and the SLA fields: the two deadlines,
when the first response happened, when the clock was paused and how long it has been
paused in total. Existing tickets get no priority (they were triaged before it
existed); they show no SLA and sort after tickets that have one.

Revision ID: c9d3f6a1b875
Revises: b8c2e5f0a764
Create Date: 2026-10-13 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c9d3f6a1b875'
down_revision: Union[str, Sequence[str], None] = 'b8c2e5f0a764'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('impact', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('urgency', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('priority', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('first_response_due_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('resolution_due_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('first_responded_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('sla_paused_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column('sla_paused_seconds', sa.Integer(), server_default='0', nullable=False)
        )
        batch_op.create_index(batch_op.f('ix_ticket_priority'), ['priority'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticket_priority'))
        batch_op.drop_column('sla_paused_seconds')
        batch_op.drop_column('sla_paused_at')
        batch_op.drop_column('first_responded_at')
        batch_op.drop_column('resolution_due_at')
        batch_op.drop_column('first_response_due_at')
        batch_op.drop_column('priority')
        batch_op.drop_column('urgency')
        batch_op.drop_column('impact')

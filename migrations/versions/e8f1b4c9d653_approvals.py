"""approvals: the Awaiting approval status, thresholds on request types, approvals on tickets

Revision ID: e8f1b4c9d653
Revises: d7e0a3b8c542
Create Date: 2026-10-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e8f1b4c9d653'
down_revision: Union[str, Sequence[str], None] = 'd7e0a3b8c542'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # A new enum value cannot be used in the transaction that adds it.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE ticketstatus ADD VALUE IF NOT EXISTS 'AWAITING_APPROVAL'")

    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approvals_required', sa.Integer(), server_default='1', nullable=False))
        batch_op.add_column(sa.Column('declines_required', sa.Integer(), server_default='1', nullable=False))

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approvals_required', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('declines_required', sa.Integer(), nullable=True))

    op.create_table(
        'ticketapproval',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('approver_id', sa.Integer(), nullable=False),
        sa.Column('decision', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('comment', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['approver_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticket_id', 'approver_id', name='uq_ticketapproval'),
    )
    with op.batch_alter_table('ticketapproval', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ticketapproval_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ticketapproval_approver_id'), ['approver_id'], unique=False)


def downgrade() -> None:
    # The AWAITING_APPROVAL enum value stays: Postgres cannot drop a value from an enum, and
    # an unused value is harmless.
    with op.batch_alter_table('ticketapproval', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticketapproval_approver_id'))
        batch_op.drop_index(batch_op.f('ix_ticketapproval_ticket_id'))
    op.drop_table('ticketapproval')

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('declines_required')
        batch_op.drop_column('approvals_required')

    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_column('declines_required')
        batch_op.drop_column('approvals_required')
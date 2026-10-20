"""outbound email outbox

Revision ID: d5a8b1c4e730
Revises: c4f7a0b3d529
Create Date: 2026-10-20 13:32:29.150453

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd5a8b1c4e730'
down_revision: Union[str, Sequence[str], None] = 'c4f7a0b3d529'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('outboundemail',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ticket_id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('approval_id', sa.Integer(), nullable=True),
    sa.Column('to_address', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('subject', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('cc', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.ForeignKeyConstraint(['approval_id'], ['ticketapproval.id'], ),
    sa.ForeignKeyConstraint(['message_id'], ['message.id'], ),
    sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id')
    )
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_outboundemail_approval_id'), ['approval_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_outboundemail_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index('ix_outboundemail_unsent', ['id'], unique=False, postgresql_where=sa.text('sent_at IS NULL'))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.drop_index('ix_outboundemail_unsent', postgresql_where=sa.text('sent_at IS NULL'))
        batch_op.drop_index(batch_op.f('ix_outboundemail_ticket_id'))
        batch_op.drop_index(batch_op.f('ix_outboundemail_approval_id'))

    op.drop_table('outboundemail')

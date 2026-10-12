"""ticket followers

Adds ticket_follower (people copied on a ticket's emails, and internal
followers who keep read access) and message.cc (who was copied on an email).

Read access after escalating used to come from ticket.escalated_by_id. It now
comes from an internal follower row, so every existing escalation is backfilled
with one and nobody loses access they had.

Revision ID: f6a0c3d8e542
Revises: e5f9b2c7d431
Create Date: 2026-10-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f6a0c3d8e542'
down_revision: Union[str, Sequence[str], None] = 'e5f9b2c7d431'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ticketfollower',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('is_internal', sa.Boolean(), nullable=False),
        sa.Column('added_by_email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticket_id', 'email', name='uq_ticketfollower_ticket_email'),
    )
    with op.batch_alter_table('ticketfollower', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ticketfollower_ticket_id'), ['ticket_id'], unique=False)

    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cc', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    op.execute(
        'INSERT INTO ticketfollower (ticket_id, email, user_id, is_internal, added_by_email, created_at) '
        'SELECT t.id, LOWER(u.email), u.id, TRUE, u.email, NOW() '
        'FROM ticket t JOIN "user" u ON u.id = t.escalated_by_id '
        'WHERE t.escalated_by_id IS NOT NULL'
    )


def downgrade() -> None:
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_column('cc')
    with op.batch_alter_table('ticketfollower', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticketfollower_ticket_id'))
    op.drop_table('ticketfollower')

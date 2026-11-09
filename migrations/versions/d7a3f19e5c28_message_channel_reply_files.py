"""message channel and files held for a reply

Revision ID: d7a3f19e5c28
Revises: cbf4e82d802a
Create Date: 2026-11-09 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd7a3f19e5c28'
down_revision: Union[str, Sequence[str], None] = 'cbf4e82d802a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    # Best effort for the rows that exist, from what they record; what cannot be told stays null (no chip is drawn).
    # The enum columns are stored as their names, so they are compared lower-cased.
    op.execute("""
        UPDATE message SET channel = CASE
            WHEN lower(direction::text) = 'internal' THEN 'agent'
            WHEN msp_forward IS NOT NULL THEN 'email'
            WHEN lower(direction::text) = 'outbound' AND author_user_id IS NULL THEN 'system'
            WHEN lower(direction::text) = 'outbound' AND EXISTS (
                SELECT 1 FROM teamspost WHERE teamspost.message_id = message.id AND teamspost.kind = 'requester_message'
            ) THEN 'teams'
            WHEN lower(direction::text) = 'outbound' THEN 'email'
            WHEN EXISTS (SELECT 1 FROM teamsintake WHERE teamsintake.message_id = message.id) THEN 'teams'
            WHEN graph_message_id IS NOT NULL THEN 'email'
            ELSE (
                SELECT CASE lower(ticket.source::text) WHEN 'portal' THEN 'portal' ELSE NULL END
                FROM ticket WHERE ticket.id = message.ticket_id
            )
        END
    """)

    op.create_table(
        'reply_held_file',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('filename', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('blob_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('reply_held_file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reply_held_file_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_reply_held_file_token'), ['token'], unique=False)
        batch_op.create_index(batch_op.f('ix_reply_held_file_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reply_held_file', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reply_held_file_user_id'))
        batch_op.drop_index(batch_op.f('ix_reply_held_file_token'))
        batch_op.drop_index(batch_op.f('ix_reply_held_file_ticket_id'))
    op.drop_table('reply_held_file')
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_column('channel')

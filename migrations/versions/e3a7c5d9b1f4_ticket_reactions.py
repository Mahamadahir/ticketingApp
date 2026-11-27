"""ticket reactions (#298): the bot's sent messages, the shared reaction record, and quick ratings from a reaction

Revision ID: e3a7c5d9b1f4
Revises: e7a2c9d4b6f1
Create Date: 2026-11-24 10:02:57.838953

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e3a7c5d9b1f4'
down_revision: Union[str, Sequence[str], None] = 'e7a2c9d4b6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('bot_message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('activity_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('conversation_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ticket_id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['message.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'activity_id', name='uq_bot_message_activity')
    )
    with op.batch_alter_table('bot_message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bot_message_ticket_id'), ['ticket_id'], unique=False)

    op.create_table('ticket_reaction',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ticket_id', sa.Integer(), nullable=False),
    sa.Column('channel', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('message_kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('message_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('message_sent_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('reactor_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('reactor_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('reactor_address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('raw_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('emoji', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('sentiment', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('by_party', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('reacted_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('removed_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('dismissed_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('dismissed_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['dismissed_by_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['message_id'], ['message.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('ticket_reaction', schema=None) as batch_op:
        batch_op.create_index('ix_ticket_reaction_open_negative', ['ticket_id'], unique=False, postgresql_where=sa.text("sentiment = 'negative' AND removed_at IS NULL AND dismissed_at IS NULL AND by_party"))
        batch_op.create_index(batch_op.f('ix_ticket_reaction_ticket_id'), ['ticket_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ticket_reaction_user_id'), ['user_id'], unique=False)
        batch_op.create_index('uq_ticket_reaction_live', ['channel', 'message_ref', 'reactor_ref', 'raw_type'], unique=True, postgresql_where=sa.text('removed_at IS NULL'))

    with op.batch_alter_table('ticketfeedback', schema=None) as batch_op:
        batch_op.add_column(sa.Column('from_reaction', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('reaction_id', sa.Integer(), nullable=True))
        batch_op.alter_column('token_id',
               existing_type=sa.INTEGER(),
               nullable=True)
        batch_op.create_foreign_key('fk_ticketfeedback_reaction_id', 'ticket_reaction', ['reaction_id'], ['id'], ondelete='SET NULL')



def downgrade() -> None:
    """Downgrade schema."""
    # A quick rating that came from a reaction has no link behind it, so it cannot stay once the link is required again.
    op.execute("DELETE FROM ticketfeedback WHERE token_id IS NULL")
    with op.batch_alter_table('ticketfeedback', schema=None) as batch_op:
        batch_op.drop_constraint('fk_ticketfeedback_reaction_id', type_='foreignkey')
        batch_op.alter_column('token_id',
               existing_type=sa.INTEGER(),
               nullable=False)
        batch_op.drop_column('reaction_id')
        batch_op.drop_column('from_reaction')

    with op.batch_alter_table('ticket_reaction', schema=None) as batch_op:
        batch_op.drop_index('uq_ticket_reaction_live', postgresql_where=sa.text('removed_at IS NULL'))
        batch_op.drop_index(batch_op.f('ix_ticket_reaction_user_id'))
        batch_op.drop_index(batch_op.f('ix_ticket_reaction_ticket_id'))
        batch_op.drop_index('ix_ticket_reaction_open_negative', postgresql_where=sa.text("sentiment = 'negative' AND removed_at IS NULL AND dismissed_at IS NULL AND by_party"))

    op.drop_table('ticket_reaction')
    with op.batch_alter_table('bot_message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bot_message_ticket_id'))

    op.drop_table('bot_message')

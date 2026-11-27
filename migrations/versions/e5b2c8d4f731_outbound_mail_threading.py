"""outbound email threading: the Sent Items id, the message replied to, a fallback note (#350)

Revision ID: e5b2c8d4f731
Revises: c3e9a1d7b546
Create Date: 2026-11-27 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e5b2c8d4f731'
down_revision: Union[str, Sequence[str], None] = 'c3e9a1d7b546'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.add_column(sa.Column('graph_message_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('reply_to_graph_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('thread_note', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_index(batch_op.f('ix_outboundemail_graph_message_id'), ['graph_message_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outboundemail_graph_message_id'))
        batch_op.drop_column('thread_note')
        batch_op.drop_column('reply_to_graph_id')
        batch_op.drop_column('graph_message_id')

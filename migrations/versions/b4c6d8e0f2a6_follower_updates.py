"""follower updates

Stores the To and Cc of each inbound mail (inboundmail.to_addresses, cc_addresses) and lets an
outbound email name the inbound message it passes on (outboundemail.about_message_id), unique per
recipient so a replay never tells anyone twice.

Revision ID: b4c6d8e0f2a6
Revises: a3483204f1e2
Create Date: 2026-10-21 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b4c6d8e0f2a6'
down_revision: Union[str, Sequence[str], None] = 'a3483204f1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('inboundmail', sa.Column('to_addresses', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('inboundmail', sa.Column('cc_addresses', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('outboundemail', sa.Column('about_message_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_outboundemail_about_message_id'), 'outboundemail', ['about_message_id'], unique=False)
    op.create_foreign_key('fk_outboundemail_about_message_id', 'outboundemail', 'message', ['about_message_id'], ['id'])
    op.create_unique_constraint('uq_outboundemail_about_message_to', 'outboundemail', ['about_message_id', 'to_address'])


def downgrade() -> None:
    op.drop_constraint('uq_outboundemail_about_message_to', 'outboundemail', type_='unique')
    op.drop_constraint('fk_outboundemail_about_message_id', 'outboundemail', type_='foreignkey')
    op.drop_index(op.f('ix_outboundemail_about_message_id'), table_name='outboundemail')
    op.drop_column('outboundemail', 'about_message_id')
    op.drop_column('inboundmail', 'cc_addresses')
    op.drop_column('inboundmail', 'to_addresses')

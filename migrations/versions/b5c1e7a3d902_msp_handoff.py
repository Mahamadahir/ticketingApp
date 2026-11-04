"""MSP handoff: ticket hold and sent time, team inclusion in the automatic handoff

Revision ID: b5c1e7a3d902
Revises: c4e8a1d7b362
Create Date: 2026-11-04 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b5c1e7a3d902'
down_revision: Union[str, Sequence[str], None] = 'c4e8a1d7b362'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('msp_hold', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('msp_sent_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.add_column(sa.Column('msp_handoff_included', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.drop_column('msp_handoff_included')
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('msp_sent_at')
        batch_op.drop_column('msp_hold')

"""outbound email cap override (Send anyway, #349)

Revision ID: c3e9a1d7b546
Revises: e5b81c4a7d29
Create Date: 2026-11-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c3e9a1d7b546'
down_revision: Union[str, Sequence[str], None] = 'e5b81c4a7d29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cap_override_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('cap_override_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('outboundemail', schema=None) as batch_op:
        batch_op.drop_column('cap_override_at')
        batch_op.drop_column('cap_override_by')

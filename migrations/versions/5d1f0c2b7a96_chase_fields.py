"""chase fields

Revision ID: 5d1f0c2b7a96
Revises: d546e15ede32
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '5d1f0c2b7a96'
down_revision: Union[str, Sequence[str], None] = 'd546e15ede32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chased_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column('chase_count', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('chase_count')
        batch_op.drop_column('chased_at')

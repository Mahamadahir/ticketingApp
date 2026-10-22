"""required by date

Revision ID: 8b3e5a1c9d27
Revises: 5d1f0c2b7a96
Create Date: 2026-10-22 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b3e5a1c9d27'
down_revision: Union[str, Sequence[str], None] = '5d1f0c2b7a96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('required_by', sa.Date(), nullable=True))
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('offers_required_by', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_column('offers_required_by')
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('required_by')

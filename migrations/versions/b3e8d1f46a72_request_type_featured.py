"""request type featured on the portal home page

Revision ID: b3e8d1f46a72
Revises: d7a3f19e5c28
Create Date: 2026-11-10 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e8d1f46a72'
down_revision: Union[str, Sequence[str], None] = 'd7a3f19e5c28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_featured', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_column('is_featured')

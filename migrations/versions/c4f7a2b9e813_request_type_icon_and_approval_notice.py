"""request type icon and approval group notice

Revision ID: c4f7a2b9e813
Revises: b3e8d1f46a72
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f7a2b9e813'
down_revision: Union[str, Sequence[str], None] = 'b3e8d1f46a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('icon', sa.String(), nullable=True))
    with op.batch_alter_table('approvalgroup', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_notice', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('approvalgroup', schema=None) as batch_op:
        batch_op.drop_column('request_notice')
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_column('icon')

"""inbound mail received_at

Revision ID: a7d2e9b41c58
Revises: d1a7e5b93c42
Create Date: 2026-11-20 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a7d2e9b41c58'
down_revision: Union[str, Sequence[str], None] = 'd1a7e5b93c42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('inboundmail', sa.Column('received_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('inboundmail', 'received_at')

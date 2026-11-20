"""indexes for the Team activity window

Revision ID: d4e8a1b7c3f9
Revises: c255a3f8d1e6
Create Date: 2026-11-20 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4e8a1b7c3f9'
down_revision: Union[str, Sequence[str], None] = 'c255a3f8d1e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_message_created_at', 'message', ['created_at'], unique=False)
    op.create_index('ix_ticketevent_created_at', 'ticketevent', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ticketevent_created_at', table_name='ticketevent')
    op.drop_index('ix_message_created_at', table_name='message')

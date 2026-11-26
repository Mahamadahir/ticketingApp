"""request_type_mailbox_action

Revision ID: b5e2c8a1f903
Revises: d7a3f9b2c614
Create Date: 2026-11-26 11:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b5e2c8a1f903'
down_revision: Union[str, Sequence[str], None] = 'd7a3f9b2c614'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('requesttype', sa.Column('mailbox_action', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('requesttype', 'mailbox_action')

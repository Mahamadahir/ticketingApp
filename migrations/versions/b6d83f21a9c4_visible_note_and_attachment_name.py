"""visible notes and the shown name of an attachment (#254)

Revision ID: b6d83f21a9c4
Revises: a4e7b19c3d58
Create Date: 2026-11-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b6d83f21a9c4'
down_revision: Union[str, Sequence[str], None] = 'a4e7b19c3d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('message', sa.Column('visible_note', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('attachment', sa.Column('display_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('attachment', 'display_name')
    op.drop_column('message', 'visible_note')

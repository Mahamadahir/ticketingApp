"""email reactions (#299): the kind of an email's reactions, and the Outlook reactions job's warning

Revision ID: b8d2f6a1c350
Revises: e3a7c5d9b1f4
Create Date: 2026-11-25 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b8d2f6a1c350'
down_revision: Union[str, Sequence[str], None] = 'e3a7c5d9b1f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('outboundemail', sa.Column('reaction_kind', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('mailsyncstate', sa.Column('last_warning', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('mailsyncstate', sa.Column('last_warning_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mailsyncstate', 'last_warning_at')
    op.drop_column('mailsyncstate', 'last_warning')
    op.drop_column('outboundemail', 'reaction_kind')

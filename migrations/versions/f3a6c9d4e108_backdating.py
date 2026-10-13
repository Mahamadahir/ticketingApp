"""backdating flag

Adds ticket.is_backdated: set once an agent enters earlier reported or resolved dates
than the real ones. Such tickets count towards volume but are left out of SLA figures.

Revision ID: f3a6c9d4e108
Revises: e2f5b8c3d097
Create Date: 2026-10-13 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a6c9d4e108'
down_revision: Union[str, Sequence[str], None] = 'e2f5b8c3d097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_backdated', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('is_backdated')

"""ticket.sla_starts_at: when the SLA clock starts, if not at created_at

Nullable; null means created_at, so every existing ticket behaves as before.

Revision ID: a4d8e1b6c297
Revises: b8d2f6a1c350
Create Date: 2026-11-25 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a4d8e1b6c297'
down_revision: Union[str, Sequence[str], None] = 'b8d2f6a1c350'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sla_starts_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('sla_starts_at')

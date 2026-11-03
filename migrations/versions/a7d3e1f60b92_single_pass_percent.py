"""approvals: a single pass percentage

Revision ID: a7d3e1f60b92
Revises: c4e7a2b9d106
Create Date: 2026-11-03 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e1f60b92'
down_revision: Union[str, Sequence[str], None] = 'c4e7a2b9d106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The app is not live, so the fail percentages are simply dropped.
    op.drop_column('requesttype', 'fail_percent')
    op.drop_column('ticketapproval', 'fail_percent')
    op.execute("DELETE FROM setting WHERE key = 'approval_default_fail_percent'")


def downgrade() -> None:
    # Best effort: the column comes back with a default of 50, raised where needed so that pass + fail
    # stays above 100 as the old rule required.
    op.add_column('requesttype', sa.Column('fail_percent', sa.Integer(), server_default='50', nullable=False))
    op.add_column('ticketapproval', sa.Column('fail_percent', sa.Integer(), server_default='50', nullable=False))
    op.execute("UPDATE requesttype SET fail_percent = GREATEST(50, 101 - pass_percent)")
    op.execute("UPDATE ticketapproval SET fail_percent = GREATEST(50, 101 - pass_percent)")
    op.alter_column('ticketapproval', 'fail_percent', server_default=None)

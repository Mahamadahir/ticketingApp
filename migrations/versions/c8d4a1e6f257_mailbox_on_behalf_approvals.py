"""mailbox_on_behalf_approvals

Revision ID: c8d4a1e6f257
Revises: b5e2c8a1f903
Create Date: 2026-11-26 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d4a1e6f257'
down_revision: Union[str, Sequence[str], None] = 'b5e2c8a1f903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('requesttype', sa.Column('any_one_approver', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('ticketapproval', sa.Column('any_one', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('approvalvote', sa.Column('extra_voter', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('approvalvote', 'extra_voter')
    op.drop_column('ticketapproval', 'any_one')
    op.drop_column('requesttype', 'any_one_approver')

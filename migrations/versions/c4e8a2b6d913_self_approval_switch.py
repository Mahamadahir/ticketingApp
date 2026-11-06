"""request type switch: approvers may approve their own requests

Revision ID: c4e8a2b6d913
Revises: b8d3e1f7a642
Create Date: 2026-11-06 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2b6d913"
down_revision: Union[str, None] = "b8d3e1f7a642"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("requesttype", sa.Column("approvers_may_self_approve", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("requesttype", "approvers_may_self_approve")

"""outage mode: emergency passwords, emergency sessions, emails not about a ticket

Revision ID: d5f1b7c3a928
Revises: c4e8a2b6d913
Create Date: 2026-11-06 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f1b7c3a928"
down_revision: Union[str, None] = "c4e8a2b6d913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user", sa.Column("emergency_password_hash", sa.String(), nullable=True))
    op.add_column("user", sa.Column("emergency_password_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user", sa.Column("emergency_password_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_session", sa.Column("emergency", sa.Boolean(), server_default="false", nullable=False))
    op.alter_column("outboundemail", "ticket_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # An email about no ticket cannot be kept once the column is required again.
    op.execute("DELETE FROM outboundemail WHERE ticket_id IS NULL")
    op.alter_column("outboundemail", "ticket_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("user_session", "emergency")
    op.drop_column("user", "emergency_password_used_at")
    op.drop_column("user", "emergency_password_set_at")
    op.drop_column("user", "emergency_password_hash")

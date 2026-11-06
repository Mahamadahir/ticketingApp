"""break-glass accounts: user flag, TOTP secret and last step, session flag

Revision ID: b8d3e1f7a642
Revises: a7c1d4e9f258
Create Date: 2026-11-06 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d3e1f7a642"
down_revision: Union[str, None] = "a7c1d4e9f258"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user", sa.Column("is_break_glass", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("user", sa.Column("totp_secret", sa.String(), nullable=True))
    op.add_column("user", sa.Column("totp_last_step", sa.Integer(), nullable=True))
    op.add_column("user_session", sa.Column("break_glass", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("user_session", "break_glass")
    op.drop_column("user", "totp_last_step")
    op.drop_column("user", "totp_secret")
    op.drop_column("user", "is_break_glass")

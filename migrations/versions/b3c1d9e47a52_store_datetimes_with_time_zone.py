"""store datetimes with time zone

SQLModel 0.0.48 maps datetime fields to timezone-aware columns. The app has
always written aware UTC values, so existing values are read as UTC.

Revision ID: b3c1d9e47a52
Revises: ee7af7a00801
Create Date: 2026-10-09 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c1d9e47a52'
down_revision: Union[str, Sequence[str], None] = 'ee7af7a00801'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, nullable)
COLUMNS = [
    ('auditlog', 'created_at', False),
    ('message', 'created_at', False),
    ('team', 'created_at', False),
    ('ticket', 'created_at', False),
    ('ticket', 'updated_at', False),
    ('ticket', 'escalated_at', True),
    ('user', 'created_at', False),
    ('user', 'locked_until', True),
]


def upgrade() -> None:
    for table, column, nullable in COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(),
                type_=sa.DateTime(timezone=True),
                existing_nullable=nullable,
                postgresql_using=f'"{column}" AT TIME ZONE \'UTC\'',
            )


def downgrade() -> None:
    for table, column, nullable in COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(timezone=True),
                type_=sa.DateTime(),
                existing_nullable=nullable,
                postgresql_using=f'"{column}" AT TIME ZONE \'UTC\'',
            )

"""admin-managed email templates

Adds email_template: one row per automatic email an admin has reworded. No row means the
default held in app/email_templates.py.

Revision ID: a3f6d0b8c471
Revises: e4b7c1a9d052
Create Date: 2026-10-21 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f6d0b8c471'
down_revision: Union[str, Sequence[str], None] = 'e4b7c1a9d052'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'emailtemplate',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('body', sa.String(), nullable=False),
        sa.Column('updated_by', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('emailtemplate')

"""scheduled jobs: Run now (who asked, when, and the due time to resume)

Revision ID: e7a2c9d4b6f1
Revises: d4e8a1b7c3f9
Create Date: 2026-11-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e7a2c9d4b6f1'
down_revision: Union[str, Sequence[str], None] = 'd4e8a1b7c3f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('scheduledjob', sa.Column('run_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('scheduledjob', sa.Column('run_requested_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('scheduledjob', sa.Column('run_resume_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('scheduledjob', 'run_resume_at')
    op.drop_column('scheduledjob', 'run_requested_by')
    op.drop_column('scheduledjob', 'run_requested_at')

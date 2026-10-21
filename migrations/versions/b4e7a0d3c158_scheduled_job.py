"""scheduled_job: recurring jobs driven by the database

Revision ID: b4e7a0d3c158
Revises: a3f6d0b8c471
Create Date: 2026-10-21 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e7a0d3c158'
down_revision: Union[str, Sequence[str], None] = 'a3f6d0b8c471'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scheduledjob',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_owner', sa.String(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failures_in_a_row', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_index(op.f('ix_scheduledjob_next_run_at'), 'scheduledjob', ['next_run_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_scheduledjob_next_run_at'), table_name='scheduledjob')
    op.drop_table('scheduledjob')

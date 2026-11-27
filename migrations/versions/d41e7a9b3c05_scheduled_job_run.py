"""scheduled job run history

One row per run of a scheduled job (#341): when it started and finished, how it ended, what it did (fixed-key counts and the
tickets it changed), the error of a failed run and who asked for a Run now. Kept for the `job_run_keep_days` setting by the
`job-run-purge` job.

Revision ID: d41e7a9b3c05
Revises: c3f8a2d6b941
Create Date: 2026-11-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd41e7a9b3c05'
down_revision: Union[str, Sequence[str], None] = 'c3f8a2d6b941'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scheduled_job_run',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('outcome', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('summary_counts', sa.JSON(), server_default='{}', nullable=False),
        sa.Column('ticket_ids', sa.JSON(), server_default='[]', nullable=False),
        sa.Column('tickets_more', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('requested_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('worker', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_scheduled_job_run_job_started', 'scheduled_job_run', ['job_name', sa.text('started_at DESC')], unique=False)
    op.create_index('ix_scheduled_job_run_started_at', 'scheduled_job_run', ['started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_scheduled_job_run_started_at', table_name='scheduled_job_run')
    op.drop_index('ix_scheduled_job_run_job_started', table_name='scheduled_job_run')
    op.drop_table('scheduled_job_run')

"""due date, per-type lead times, Scheduled no longer pauses the clock

Adds ticket.due_on and ticket.due_set_by_hand, and request type due_days_before and queue_lead_days.
Existing Required by dates keep their deadline: due_on is set to the same day. A ticket that is Scheduled
had its clock paused; Scheduled no longer pauses, so the wait so far is added to its paused time and the
clock runs again. A Scheduled ticket with no date gets today's, so the morning job takes it back to Open.

Revision ID: f3b9d2a6c841
Revises: e7a2c4d9b136
Create Date: 2026-11-03 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b9d2a6c841'
down_revision: Union[str, Sequence[str], None] = 'e7a2c4d9b136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('due_on', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('due_set_by_hand', sa.Boolean(), server_default='false', nullable=False))
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('due_days_before', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('queue_lead_days', sa.Integer(), nullable=True))
    op.execute("UPDATE ticket SET due_on = required_by WHERE required_by IS NOT NULL")
    # Scheduled stops pausing the clock: end the wait in progress the way leaving a paused status does.
    op.execute(
        "UPDATE ticket SET "
        "sla_paused_seconds = sla_paused_seconds + GREATEST(0, EXTRACT(EPOCH FROM (now() - sla_paused_at))::int), "
        "first_response_due_at = first_response_due_at + (now() - sla_paused_at), "
        "resolution_due_at = CASE WHEN required_by IS NULL THEN resolution_due_at + (now() - sla_paused_at) "
        "ELSE resolution_due_at END, "
        "sla_paused_at = NULL "
        "WHERE status = 'SCHEDULED' AND sla_paused_at IS NOT NULL"
    )
    op.execute("UPDATE ticket SET due_on = CURRENT_DATE WHERE status = 'SCHEDULED' AND due_on IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_column('queue_lead_days')
        batch_op.drop_column('due_days_before')
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_column('due_set_by_hand')
        batch_op.drop_column('due_on')

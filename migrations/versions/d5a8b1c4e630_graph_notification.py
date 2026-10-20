"""graph notifications: change notifications stored by the public webhook

Revision ID: d5a8b1c4e630
Revises: a9d3c6e1f275
Create Date: 2026-10-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a8b1c4e630'
down_revision: Union[str, Sequence[str], None] = 'a9d3c6e1f275'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'graphnotification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('subscription_id', sa.String(), nullable=True),
        sa.Column('change_type', sa.String(), nullable=True),
        sa.Column('lifecycle_event', sa.String(), nullable=True),
        sa.Column('resource', sa.String(), nullable=True),
        sa.Column('resource_id', sa.String(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_graphnotification_unprocessed', 'graphnotification', ['id'], unique=False,
        postgresql_where=sa.text('processed_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_graphnotification_unprocessed', table_name='graphnotification', postgresql_where=sa.text('processed_at IS NULL'))
    op.drop_table('graphnotification')

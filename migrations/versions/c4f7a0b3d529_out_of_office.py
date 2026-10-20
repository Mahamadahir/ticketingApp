"""out-of-office detection: each person's Entra id (unique) and last presence reading

Revision ID: c4f7a0b3d529
Revises: b3e6d9a2f418
Create Date: 2026-10-20 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f7a0b3d529'
down_revision: Union[str, Sequence[str], None] = 'b3e6d9a2f418'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('entra_object_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_entra_object_id'), ['entra_object_id'], unique=True)
        batch_op.add_column(sa.Column('out_of_office', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('out_of_office_checked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('out_of_office_checked_at')
        batch_op.drop_column('out_of_office')
        batch_op.drop_index(batch_op.f('ix_user_entra_object_id'))
        batch_op.drop_column('entra_object_id')

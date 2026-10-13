"""canned responses

Revision ID: b5c8e1f6a320
Revises: a4b7d0e5f219
Create Date: 2026-10-13 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b5c8e1f6a320'
down_revision: Union[str, Sequence[str], None] = 'a4b7d0e5f219'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cannedresponse',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('cannedresponse', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cannedresponse_title'), ['title'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('cannedresponse', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cannedresponse_title'))
    op.drop_table('cannedresponse')

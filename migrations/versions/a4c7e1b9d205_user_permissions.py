"""extra permissions a super admin can grant

Adds the userpermission table (user, permission key, who granted it).

Revision ID: a4c7e1b9d205
Revises: f3b9d2a6c841
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a4c7e1b9d205'
down_revision: Union[str, Sequence[str], None] = 'f3b9d2a6c841'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('userpermission',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('permission', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('granted_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'permission', name='uq_userpermission')
    )
    with op.batch_alter_table('userpermission', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_userpermission_permission'), ['permission'], unique=False)
        batch_op.create_index(batch_op.f('ix_userpermission_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('userpermission', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_userpermission_user_id'))
        batch_op.drop_index(batch_op.f('ix_userpermission_permission'))
    op.drop_table('userpermission')

"""service request types and approvers

Revision ID: d7e0a3b8c542
Revises: c2d5e8f1a436
Create Date: 2026-10-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd7e0a3b8c542'
down_revision: Union[str, Sequence[str], None] = 'c2d5e8f1a436'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'requesttype',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_requesttype_name'), ['name'], unique=True)

    op.create_table(
        'requesttypeapprover',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_type_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['request_type_id'], ['requesttype.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_type_id', 'user_id', name='uq_requesttypeapprover'),
    )
    with op.batch_alter_table('requesttypeapprover', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_requesttypeapprover_request_type_id'), ['request_type_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_requesttypeapprover_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_type_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_request_type_id'), ['request_type_id'], unique=False)
        batch_op.create_foreign_key('ticket_request_type_id_fkey', 'requesttype', ['request_type_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('ticket_request_type_id_fkey', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ticket_request_type_id'))
        batch_op.drop_column('request_type_id')

    with op.batch_alter_table('requesttypeapprover', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_requesttypeapprover_user_id'))
        batch_op.drop_index(batch_op.f('ix_requesttypeapprover_request_type_id'))
    op.drop_table('requesttypeapprover')

    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_requesttype_name'))
    op.drop_table('requesttype')
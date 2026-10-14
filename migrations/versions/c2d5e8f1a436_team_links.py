"""team links, sub-teams and escalation by category

Adds teamlink (admin-set access from one team to another's tickets), team.parent_team_id
(sub-teams), category.escalation_team_id / escalation_inherits (where escalating sends a
ticket; no team means the MSP) and ticketescalator (who keeps access to what they escalated).

Revision ID: c2d5e8f1a436
Revises: b5c8e1f6a320
Create Date: 2026-10-13 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2d5e8f1a436'
down_revision: Union[str, Sequence[str], None] = 'b5c8e1f6a320'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The membershiplevel type already exists (team memberships); reuse it.
    level = postgresql.ENUM('MEMBER', 'VIEWER', name='membershiplevel', create_type=False)
    op.create_table(
        'teamlink',
        sa.Column('from_team_id', sa.Integer(), nullable=False),
        sa.Column('to_team_id', sa.Integer(), nullable=False),
        sa.Column('level', level, nullable=False),
        sa.ForeignKeyConstraint(['from_team_id'], ['team.id']),
        sa.ForeignKeyConstraint(['to_team_id'], ['team.id']),
        sa.PrimaryKeyConstraint('from_team_id', 'to_team_id'),
    )
    op.create_table(
        'ticketescalator',
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('ticket_id', 'user_id'),
    )
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_team_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_team_parent_team_id', 'team', ['parent_team_id'], ['id'])
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.add_column(sa.Column('escalation_team_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('escalation_inherits', sa.Boolean(), server_default='true', nullable=False))
        batch_op.create_foreign_key('fk_category_escalation_team_id', 'team', ['escalation_team_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.drop_constraint('fk_category_escalation_team_id', type_='foreignkey')
        batch_op.drop_column('escalation_inherits')
        batch_op.drop_column('escalation_team_id')
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.drop_constraint('fk_team_parent_team_id', type_='foreignkey')
        batch_op.drop_column('parent_team_id')
    op.drop_table('ticketescalator')
    op.drop_table('teamlink')

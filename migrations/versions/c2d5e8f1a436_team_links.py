"""team visibility links and escalation tiers

Adds teamlink (admin-set access from one team to another's tickets) and
team.escalates_to_team_id (the next tier up).

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
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.add_column(sa.Column('escalates_to_team_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_team_escalates_to_team_id', 'team', ['escalates_to_team_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('team', schema=None) as batch_op:
        batch_op.drop_constraint('fk_team_escalates_to_team_id', type_='foreignkey')
        batch_op.drop_column('escalates_to_team_id')
    op.drop_table('teamlink')

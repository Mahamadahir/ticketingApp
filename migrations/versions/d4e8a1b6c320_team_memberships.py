"""team memberships

Replaces user.team_id (one team per user) with team_membership rows carrying a
level, member or viewer. Each existing assignment becomes a member row, so
nobody gains or loses access.

Revision ID: d4e8a1b6c320
Revises: c7a2f4e81b09
Create Date: 2026-10-12 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4e8a1b6c320'
down_revision: Union[str, Sequence[str], None] = 'c7a2f4e81b09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEVELS = ('MEMBER', 'VIEWER')


def _level_enum() -> postgresql.ENUM:
    # create_type=False: the type is created explicitly, once.
    return postgresql.ENUM(*LEVELS, name='membershiplevel', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    _level_enum().create(bind, checkfirst=True)

    op.create_table(
        'teammembership',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('level', _level_enum(), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['team.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('user_id', 'team_id'),
    )

    op.execute(
        "INSERT INTO teammembership (user_id, team_id, level) "
        "SELECT id, team_id, 'MEMBER' FROM \"user\" WHERE team_id IS NOT NULL"
    )

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('team_id')


def downgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('user_team_id_fkey', 'team', ['team_id'], ['id'])

    # A user can only have one team again: keep the lowest-numbered member team.
    op.execute(
        "UPDATE \"user\" SET team_id = sub.team_id FROM ("
        "SELECT user_id, MIN(team_id) AS team_id FROM teammembership "
        "WHERE level = 'MEMBER' GROUP BY user_id) AS sub "
        "WHERE \"user\".id = sub.user_id"
    )

    op.drop_table('teammembership')
    if bind.dialect.name == 'postgresql':
        _level_enum().drop(bind, checkfirst=True)

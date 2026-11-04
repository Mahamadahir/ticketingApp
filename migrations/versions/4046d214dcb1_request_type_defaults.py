"""request type defaults

Adds the optional defaults of a request type: team, category, impact and urgency. With all four set a portal
request of that type triages itself when raised (#133); otherwise they only prefill the triage form.

Revision ID: 4046d214dcb1
Revises: a4c7e1b9d205
Create Date: 2026-11-04 13:46:13.018075

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '4046d214dcb1'
down_revision: Union[str, Sequence[str], None] = 'a4c7e1b9d205'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.add_column(sa.Column('default_team_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('default_category_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('default_impact', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('default_urgency', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_foreign_key('requesttype_default_team_id_fkey', 'team', ['default_team_id'], ['id'])
        batch_op.create_foreign_key('requesttype_default_category_id_fkey', 'category', ['default_category_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('requesttype', schema=None) as batch_op:
        batch_op.drop_constraint('requesttype_default_category_id_fkey', type_='foreignkey')
        batch_op.drop_constraint('requesttype_default_team_id_fkey', type_='foreignkey')
        batch_op.drop_column('default_urgency')
        batch_op.drop_column('default_impact')
        batch_op.drop_column('default_category_id')
        batch_op.drop_column('default_team_id')

"""message visibility and internal notes

Adds message.visibility (public or agents_only) and an internal message
direction for agent notes. Every existing message was visible to the requester,
so existing rows become public.

Revision ID: e5f9b2c7d431
Revises: d4e8a1b6c320
Create Date: 2026-10-12 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e5f9b2c7d431'
down_revision: Union[str, Sequence[str], None] = 'd4e8a1b6c320'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VISIBILITY = ('PUBLIC', 'AGENTS_ONLY')


def _visibility_enum() -> postgresql.ENUM:
    # create_type=False: created explicitly below, once.
    return postgresql.ENUM(*VISIBILITY, name='messagevisibility', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE messagedirection ADD VALUE IF NOT EXISTS 'INTERNAL'")

    _visibility_enum().create(bind, checkfirst=True)
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('visibility', _visibility_enum(), server_default='PUBLIC', nullable=False))
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.alter_column('visibility', existing_type=_visibility_enum(), server_default=None)


def downgrade() -> None:
    bind = op.get_bind()

    # Internal notes have no place in the old schema; remove them rather than
    # leave rows the old enum cannot represent. (Postgres cannot drop the enum
    # value itself, so INTERNAL stays in the type but unused.)
    op.execute("DELETE FROM message WHERE direction = 'INTERNAL'")
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_column('visibility')
    if bind.dialect.name == 'postgresql':
        _visibility_enum().drop(bind, checkfirst=True)

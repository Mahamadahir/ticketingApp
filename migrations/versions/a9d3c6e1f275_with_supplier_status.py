"""With supplier ticket status

Revision ID: a9d3c6e1f275
Revises: c4f7a0b3d529
Create Date: 2026-10-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a9d3c6e1f275'
down_revision: Union[str, Sequence[str], None] = 'd5a8b1c4e730'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # A new enum value cannot be used in the transaction that adds it.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE ticketstatus ADD VALUE IF NOT EXISTS 'WITH_SUPPLIER'")


def downgrade() -> None:
    # The WITH_SUPPLIER enum value stays: Postgres cannot drop a value from an enum, and an
    # unused value is harmless. Tickets still With supplier move to In progress so the older
    # code, which does not know the status, can still read them.
    op.execute("UPDATE ticket SET status = 'IN_PROGRESS' WHERE status = 'WITH_SUPPLIER'")

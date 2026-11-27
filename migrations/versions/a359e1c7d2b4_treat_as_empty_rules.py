"""treat as empty rules

Revision ID: a359e1c7d2b4
Revises: e5b2c8d4f731
Create Date: 2026-11-27 16:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a359e1c7d2b4'
down_revision: Union[str, Sequence[str], None] = 'e5b2c8d4f731'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The starting rules (#359), editable and removable on the admin page. Equals rules; `*` is a wildcard.
SEED = [
    ('equals', 'sent from my iphone'),
    ('equals', 'sent from my ipad'),
    ('equals', 'sent from my*phone'),
    ('equals', 'get outlook for ios'),
    ('equals', 'get outlook for android'),
    ('equals', 'sent from outlook for*'),
    ('equals', 'sent from mail for windows'),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('treatasemptyrule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('match_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('pattern', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    rules = sa.table(
        'treatasemptyrule',
        sa.column('match_type', sa.String), sa.column('pattern', sa.String), sa.column('enabled', sa.Boolean),
        sa.column('created_by', sa.String), sa.column('created_at', sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(rules, [
        {'match_type': kind, 'pattern': pattern, 'enabled': True, 'created_by': 'system', 'created_at': now}
        for kind, pattern in SEED
    ])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('treatasemptyrule')

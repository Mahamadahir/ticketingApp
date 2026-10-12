"""one ticket number shared by every type

Tickets used to be numbered per type (INC-1 and SR-1 both existed) and a type change at
triage re-issued the reference and kept the old one as an alias. Now one counter feeds
every ticket, the number never changes, and the reference (INC-1042) is the number with
a display prefix that follows the type. So the stored reference, the alias table and the
per-type counters go.

Existing tickets are renumbered 1..n in creation order (nothing is deployed, so no link
or email subject in the wild refers to the old numbers). Downgrade rebuilds the stored
reference from the prefix and the number and re-seeds the per-type counters; aliases are
not recoverable.

Revision ID: e2f5b8c3d097
Revises: d1e4a7b2c986
Create Date: 2026-10-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e2f5b8c3d097'
down_revision: Union[str, Sequence[str], None] = 'd1e4a7b2c986'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PREFIXES = {'INCIDENT': 'INC', 'SERVICE_REQUEST': 'SR', 'MAJOR_INCIDENT': 'MI', 'PROBLEM': 'PRB'}


def upgrade() -> None:
    op.create_table(
        'numbercounter',
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )

    # One sequence of numbers across all types, in the order the tickets were created.
    op.execute(
        "UPDATE ticket SET number = ranked.n FROM "
        "(SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS n FROM ticket) AS ranked "
        "WHERE ticket.id = ranked.id"
    )
    op.execute("INSERT INTO numbercounter (name, last_number) SELECT 'ticket', COALESCE(MAX(number), 0) FROM ticket")

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticket_reference'))
        batch_op.drop_column('reference')
        batch_op.create_index(batch_op.f('ix_ticket_number'), ['number'], unique=True)

    with op.batch_alter_table('ticketalias', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticketalias_ticket_id'))
    op.drop_table('ticketalias')
    op.drop_table('ticketcounter')


def downgrade() -> None:
    op.create_table(
        'ticketcounter',
        sa.Column(
            'ticket_type',
            sa.Enum('INCIDENT', 'SERVICE_REQUEST', 'MAJOR_INCIDENT', 'PROBLEM', name='tickettype', create_type=False),
            nullable=False,
        ),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('ticket_type'),
    )
    op.create_table(
        'ticketalias',
        sa.Column('reference', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.PrimaryKeyConstraint('reference'),
    )
    with op.batch_alter_table('ticketalias', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ticketalias_ticket_id'), ['ticket_id'], unique=False)

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticket_number'))
        batch_op.add_column(sa.Column('reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    case = " ".join(f"WHEN '{t}' THEN '{p}'" for t, p in PREFIXES.items())
    op.execute(f"UPDATE ticket SET reference = (CASE type::text {case} END) || '-' || number")
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.alter_column('reference', nullable=False)
        batch_op.create_index(batch_op.f('ix_ticket_reference'), ['reference'], unique=True)

    # Per-type counters: the highest number issued in each type (the shared numbers are
    # unique across types, so references stay unique).
    op.execute(
        "INSERT INTO ticketcounter (ticket_type, last_number) "
        "SELECT type, MAX(number) FROM ticket GROUP BY type"
    )
    op.drop_table('numbercounter')

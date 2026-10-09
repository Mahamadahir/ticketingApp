"""ticket core: types, references, statuses, source, events

Adds ticket type, a per-type reference (INC-1, SR-1, ...), source,
reported_at/resolved_at and the ticket_event history, and extends the status
enum. Existing tickets become incidents numbered in id order.

Revision ID: c7a2f4e81b09
Revises: b3c1d9e47a52
Create Date: 2026-10-09 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c7a2f4e81b09'
down_revision: Union[str, Sequence[str], None] = 'b3c1d9e47a52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_STATUSES = ('NEW', 'AWAITING_USER', 'WITH_MSP')
TYPES = ('INCIDENT', 'SERVICE_REQUEST', 'MAJOR_INCIDENT', 'PROBLEM')
SOURCES = ('EMAIL', 'PORTAL', 'TEAMS', 'AGENT')


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    # create_type=False: the types are created explicitly below, because
    # add_column and create_table would otherwise try to create them twice.
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == 'postgresql'

    if is_postgres:
        # New enum values cannot be used in the transaction that adds them.
        with op.get_context().autocommit_block():
            for value in NEW_STATUSES:
                op.execute(f"ALTER TYPE ticketstatus ADD VALUE IF NOT EXISTS '{value}'")
        _enum('tickettype', TYPES).create(bind, checkfirst=True)
        _enum('ticketsource', SOURCES).create(bind, checkfirst=True)

    op.create_table(
        'ticketcounter',
        sa.Column('ticket_type', _enum('tickettype', TYPES), nullable=False),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('ticket_type'),
    )
    op.create_table(
        'ticketevent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('actor_email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('from_value', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('to_value', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('detail', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('ticketevent', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ticketevent_ticket_id'), ['ticket_id'], unique=False)

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('type', _enum('tickettype', TYPES), server_default='INCIDENT', nullable=False))
        batch_op.add_column(sa.Column('source', _enum('ticketsource', SOURCES), server_default='EMAIL', nullable=False))
        batch_op.add_column(sa.Column('number', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('reported_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))

    # Existing tickets: incidents, numbered in id order.
    op.execute(
        "UPDATE ticket SET number = sub.rn, reference = 'INC-' || CAST(sub.rn AS VARCHAR) "
        "FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn FROM ticket) AS sub "
        "WHERE ticket.id = sub.id"
    )
    op.execute("UPDATE ticket SET reported_at = created_at")
    op.execute("UPDATE ticket SET resolved_at = updated_at WHERE status IN ('RESOLVED', 'CLOSED')")
    op.execute(
        "INSERT INTO ticketcounter (ticket_type, last_number) VALUES "
        "('INCIDENT', (SELECT COUNT(*) FROM ticket)), "
        "('SERVICE_REQUEST', 0), ('MAJOR_INCIDENT', 0), ('PROBLEM', 0)"
    )

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.alter_column('number', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('reference', existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=False)
        batch_op.alter_column('reported_at', existing_type=sa.DateTime(timezone=True), nullable=False)
        batch_op.alter_column('type', existing_type=_enum('tickettype', TYPES), server_default=None)
        batch_op.alter_column('source', existing_type=_enum('ticketsource', SOURCES), server_default=None)
        batch_op.create_index(batch_op.f('ix_ticket_reference'), ['reference'], unique=True)


def downgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticket_reference'))
        batch_op.drop_column('resolved_at')
        batch_op.drop_column('reported_at')
        batch_op.drop_column('reference')
        batch_op.drop_column('number')
        batch_op.drop_column('source')
        batch_op.drop_column('type')

    with op.batch_alter_table('ticketevent', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticketevent_ticket_id'))
    op.drop_table('ticketevent')
    op.drop_table('ticketcounter')

    # Statuses that did not exist before. Postgres cannot drop enum values,
    # so the extra values stay in the type but no row uses them.
    op.execute("UPDATE ticket SET status = 'OPEN' WHERE status IN ('NEW', 'AWAITING_USER', 'WITH_MSP')")

    if bind.dialect.name == 'postgresql':
        _enum('ticketsource', SOURCES).drop(bind, checkfirst=True)
        _enum('tickettype', TYPES).drop(bind, checkfirst=True)

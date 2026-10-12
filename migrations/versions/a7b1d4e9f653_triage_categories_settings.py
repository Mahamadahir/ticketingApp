"""triage, categories, aliases and settings

Adds category, ticket_alias and setting, and the triage fields on ticket
(category, triaged_at, triaged_by, needs_category_review). Seeds the "Other"
category.

Triage used to be inferred from status. It is now explicit, so every ticket that
is not New is backfilled as triaged (at its last update), and tickets still New
stay untriaged.

Revision ID: a7b1d4e9f653
Revises: f6a0c3d8e542
Create Date: 2026-10-12 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a7b1d4e9f653'
down_revision: Union[str, Sequence[str], None] = 'f6a0c3d8e542'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'category',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('team_id', sa.Integer(), nullable=True),
        sa.Column('is_other', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['category.id']),
        sa.ForeignKeyConstraint(['team_id'], ['team.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('parent_id', 'name', name='uq_category_parent_name'),
    )
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_category_parent_id'), ['parent_id'], unique=False)

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

    op.create_table(
        'setting',
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )

    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('triaged_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('triaged_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('needs_category_review', sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.create_foreign_key('ticket_category_id_fkey', 'category', ['category_id'], ['id'])
        batch_op.create_foreign_key('ticket_triaged_by_id_fkey', 'user', ['triaged_by_id'], ['id'])
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.alter_column('needs_category_review', existing_type=sa.Boolean(), server_default=None)

    # Anything past New was, in effect, already triaged.
    op.execute("UPDATE ticket SET triaged_at = updated_at WHERE status <> 'NEW'")
    # The one seeded category. Its team is set under Admin > Categories; until then
    # it falls back to the default triage team setting.
    op.execute(
        "INSERT INTO category (name, parent_id, team_id, is_other, is_active, created_at) "
        "VALUES ('Other', NULL, NULL, TRUE, TRUE, NOW())"
    )


def downgrade() -> None:
    with op.batch_alter_table('ticket', schema=None) as batch_op:
        batch_op.drop_constraint('ticket_triaged_by_id_fkey', type_='foreignkey')
        batch_op.drop_constraint('ticket_category_id_fkey', type_='foreignkey')
        batch_op.drop_column('needs_category_review')
        batch_op.drop_column('triaged_by_id')
        batch_op.drop_column('triaged_at')
        batch_op.drop_column('category_id')
    op.drop_table('setting')
    with op.batch_alter_table('ticketalias', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticketalias_ticket_id'))
    op.drop_table('ticketalias')
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_category_parent_id'))
    op.drop_table('category')

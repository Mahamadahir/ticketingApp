"""approval groups: groups of voters with pass and fail percentages

Replaces requesttypeapprover and the per-approver ticketapproval rows with approvalgroup and
approvalgroupmember, requesttype.approval_group_id / pass_percent / fail_percent, one ticketapproval
per approval asked of a ticket and one approvalvote per voter.

Data: each request type's approvers become a group named after the type. The old "n of m must
approve" numbers become percentages (floor(100 * n / m); a decline count is the smaller of the old number and the number that
made the approvals unreachable); where pass + fail would not be more than 100
the fail percentage is raised to 101 - pass, because a complete vote must always have an outcome.
Old per-approver rows become votes (same ids, so emailed links and queued approval emails still work).

Revision ID: c5d9e2f7a418
Revises: b4c6d8e0f2a6
Create Date: 2026-10-21 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c5d9e2f7a418'
down_revision: Union[str, Sequence[str], None] = 'b4c6d8e0f2a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _percent(needed: int, total: int) -> int:
    """The percentage that is reached by exactly `needed` of `total` votes: floor(100 * needed / total),
    at least 1 (the app's test is votes * 100 >= percent * voters)."""
    if total <= 0:
        return 100
    return max(1, min(100, 100 * max(1, min(needed, total)) // total))


def _percents(pass_needed: int, fail_needed: int, total: int, default: tuple[int, int]) -> tuple[int, int]:
    """The old "n must approve, m must decline" as percentages. The old rule also declined as soon as the
    approvals could no longer be reached (total - n + 1 declines), so the effective decline count is the
    smaller of the two. Where the sum would not be more than 100 the fail percentage is raised."""
    if total <= 0:
        return default
    fail_needed = min(fail_needed, total - min(pass_needed, total) + 1)
    passing, failing = _percent(pass_needed, total), _percent(fail_needed, total)
    if passing + failing <= 100:
        failing = 101 - passing
    return passing, min(failing, 100)


def _needed(percent: int, total: int) -> int:
    """The smallest number of votes that reaches the percentage (the reverse of _percent)."""
    return max(1, min(total, -(-percent * total // 100))) if total > 0 else 1


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        'approvalgroup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_approvalgroup_name'), 'approvalgroup', ['name'], unique=True)
    op.create_table(
        'approvalgroupmember',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['approvalgroup.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_approvalgroupmember'),
    )
    op.create_index(op.f('ix_approvalgroupmember_group_id'), 'approvalgroupmember', ['group_id'], unique=False)
    op.create_index(op.f('ix_approvalgroupmember_user_id'), 'approvalgroupmember', ['user_id'], unique=False)

    op.add_column('requesttype', sa.Column('approval_group_id', sa.Integer(), nullable=True))
    op.add_column('requesttype', sa.Column('pass_percent', sa.Integer(), server_default='51', nullable=False))
    op.add_column('requesttype', sa.Column('fail_percent', sa.Integer(), server_default='50', nullable=False))
    op.create_index(op.f('ix_requesttype_approval_group_id'), 'requesttype', ['approval_group_id'], unique=False)
    op.create_foreign_key('requesttype_approval_group_id_fkey', 'requesttype', 'approvalgroup', ['approval_group_id'], ['id'])

    # Each request type's approvers become a group named after it.
    types = bind.execute(sa.text(
        "SELECT id, name, approvals_required, declines_required, created_at FROM requesttype ORDER BY id"
    )).all()
    for type_id, name, approvals_required, declines_required, created_at in types:
        group_id = bind.execute(
            sa.text("INSERT INTO approvalgroup (name, created_at) VALUES (:name, :at) RETURNING id"),
            {"name": name, "at": created_at},
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO approvalgroupmember (group_id, user_id) "
                "SELECT :group_id, user_id FROM requesttypeapprover WHERE request_type_id = :type_id"
            ),
            {"group_id": group_id, "type_id": type_id},
        )
        members = bind.execute(
            sa.text("SELECT count(*) FROM approvalgroupmember WHERE group_id = :g"), {"g": group_id}
        ).scalar_one()
        passing, failing = _percents(approvals_required, declines_required, members, (51, 50))
        bind.execute(
            sa.text(
                "UPDATE requesttype SET approval_group_id = :g, pass_percent = :p, fail_percent = :f WHERE id = :t"
            ),
            {"g": group_id, "p": passing, "f": failing, "t": type_id},
        )

    # Keep what the old per-approver rows say, then replace the table.
    old = bind.execute(sa.text(
        "SELECT a.id, a.ticket_id, a.approver_id, a.decision, a.comment, a.created_at, a.decided_at, "
        "t.status::text, t.request_type_id, t.approvals_required, t.declines_required "
        "FROM ticketapproval a JOIN ticket t ON t.id = a.ticket_id ORDER BY a.id"
    )).all()
    emails = bind.execute(sa.text("SELECT id, approval_id FROM outboundemail WHERE approval_id IS NOT NULL")).all()
    # How each settled approval ended, from the ticket's own history.
    outcomes = dict(bind.execute(sa.text(
        "SELECT DISTINCT ON (ticket_id) ticket_id, to_value FROM ticketevent "
        "WHERE event_type = 'approval_outcome' ORDER BY ticket_id, id DESC"
    )).all())

    op.drop_index(op.f('ix_outboundemail_approval_id'), table_name='outboundemail')
    op.drop_constraint('outboundemail_approval_id_fkey', 'outboundemail', type_='foreignkey')
    op.drop_column('outboundemail', 'approval_id')
    op.drop_table('ticketapproval')
    op.drop_column('ticket', 'approvals_required')
    op.drop_column('ticket', 'declines_required')
    op.drop_table('requesttypeapprover')
    op.drop_column('requesttype', 'approvals_required')
    op.drop_column('requesttype', 'declines_required')

    op.create_table(
        'ticketapproval',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('pass_percent', sa.Integer(), nullable=False),
        sa.Column('fail_percent', sa.Integer(), nullable=False),
        sa.Column('voters', sa.Integer(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['group_id'], ['approvalgroup.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ticketapproval_ticket_id'), 'ticketapproval', ['ticket_id'], unique=False)
    op.create_index(op.f('ix_ticketapproval_group_id'), 'ticketapproval', ['group_id'], unique=False)
    op.create_index(
        'uq_ticketapproval_one_pending', 'ticketapproval', ['ticket_id'], unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        'approvalvote',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('approval_id', sa.Integer(), nullable=False),
        sa.Column('voter_id', sa.Integer(), nullable=False),
        sa.Column('decision', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('comment', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['approval_id'], ['ticketapproval.id']),
        sa.ForeignKeyConstraint(['voter_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('approval_id', 'voter_id', name='uq_approvalvote'),
    )
    op.create_index(op.f('ix_approvalvote_approval_id'), 'approvalvote', ['approval_id'], unique=False)
    op.create_index(op.f('ix_approvalvote_voter_id'), 'approvalvote', ['voter_id'], unique=False)

    # One approval per ticket that had approver rows, one vote per old row (same ids).
    by_ticket: dict[int, list] = {}
    for row in old:
        by_ticket.setdefault(row[1], []).append(row)
    for ticket_id, rows in by_ticket.items():
        _, _, _, _, _, _, _, status, type_id, approvals_required, declines_required = rows[0]
        group_id = bind.execute(
            sa.text("SELECT approval_group_id FROM requesttype WHERE id = :t"), {"t": type_id}
        ).scalar_one_or_none()
        if group_id is None:
            continue
        voters = len(rows)
        approved = sum(1 for r in rows if r[3] == 'approved')
        declined = sum(1 for r in rows if r[3] == 'declined')
        passing, failing = _percents(approvals_required or 1, declines_required or 1, voters, (51, 50))
        if status == 'AWAITING_APPROVAL':
            outcome = 'pending'
        elif outcomes.get(ticket_id) in ('approved', 'declined', 'waived'):
            outcome = outcomes[ticket_id]
        elif approved >= (approvals_required or 1):
            outcome = 'approved'
        elif declined >= (declines_required or 1) or approved + sum(1 for r in rows if r[3] == 'pending') < (approvals_required or 1):
            outcome = 'declined'
        else:
            outcome = 'waived'
        decided = [r[6] for r in rows if r[6] is not None]
        approval_id = bind.execute(
            sa.text(
                "INSERT INTO ticketapproval (ticket_id, group_id, pass_percent, fail_percent, voters, status, "
                "created_at, settled_at) VALUES (:t, :g, :p, :f, :v, :s, :c, :d) RETURNING id"
            ),
            {
                "t": ticket_id, "g": group_id, "p": passing, "f": failing, "v": voters, "s": outcome,
                "c": min(r[5] for r in rows), "d": None if outcome == 'pending' else (max(decided) if decided else min(r[5] for r in rows)),
            },
        ).scalar_one()
        for vote_id, _, voter_id, decision, comment, created_at, decided_at, *_rest in rows:
            bind.execute(
                sa.text(
                    "INSERT INTO approvalvote (id, approval_id, voter_id, decision, comment, created_at, decided_at) "
                    "VALUES (:id, :a, :u, :d, :c, :cr, :de)"
                ),
                {"id": vote_id, "a": approval_id, "u": voter_id, "d": decision, "c": comment, "cr": created_at, "de": decided_at},
            )
    bind.execute(sa.text(
        "SELECT setval(pg_get_serial_sequence('approvalvote', 'id'), COALESCE((SELECT max(id) FROM approvalvote), 0) + 1, false)"
    ))

    op.add_column('outboundemail', sa.Column('vote_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_outboundemail_vote_id'), 'outboundemail', ['vote_id'], unique=False)
    op.create_foreign_key('outboundemail_vote_id_fkey', 'outboundemail', 'approvalvote', ['vote_id'], ['id'])
    for email_id, vote_id in emails:
        # Only where the vote exists (its ticket's request type had a group).
        bind.execute(
            sa.text("UPDATE outboundemail SET vote_id = :v WHERE id = :e AND EXISTS (SELECT 1 FROM approvalvote WHERE id = :v)"),
            {"v": vote_id, "e": email_id},
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Keep each ticket's latest approval (the old table allowed one row per approver per ticket).
    latest = bind.execute(sa.text(
        "SELECT DISTINCT ON (ticket_id) id, ticket_id, pass_percent, fail_percent, voters "
        "FROM ticketapproval ORDER BY ticket_id, id DESC"
    )).all()
    votes = bind.execute(sa.text(
        "SELECT v.id, v.approval_id, v.voter_id, v.decision, v.comment, v.created_at, v.decided_at FROM approvalvote v ORDER BY v.id"
    )).all()
    emails = bind.execute(sa.text("SELECT id, vote_id FROM outboundemail WHERE vote_id IS NOT NULL")).all()
    types = bind.execute(sa.text("SELECT id, approval_group_id, pass_percent, fail_percent FROM requesttype")).all()
    members = bind.execute(sa.text("SELECT group_id, user_id FROM approvalgroupmember ORDER BY id")).all()

    op.drop_constraint('outboundemail_vote_id_fkey', 'outboundemail', type_='foreignkey')
    op.drop_index(op.f('ix_outboundemail_vote_id'), table_name='outboundemail')
    op.drop_column('outboundemail', 'vote_id')
    op.drop_table('approvalvote')
    op.drop_table('ticketapproval')
    op.drop_constraint('requesttype_approval_group_id_fkey', 'requesttype', type_='foreignkey')
    op.drop_index(op.f('ix_requesttype_approval_group_id'), table_name='requesttype')
    op.drop_column('requesttype', 'approval_group_id')
    op.drop_column('requesttype', 'pass_percent')
    op.drop_column('requesttype', 'fail_percent')

    op.add_column('requesttype', sa.Column('approvals_required', sa.Integer(), server_default='1', nullable=False))
    op.add_column('requesttype', sa.Column('declines_required', sa.Integer(), server_default='1', nullable=False))
    op.add_column('ticket', sa.Column('approvals_required', sa.Integer(), nullable=True))
    op.add_column('ticket', sa.Column('declines_required', sa.Integer(), nullable=True))
    op.create_table(
        'requesttypeapprover',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_type_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['request_type_id'], ['requesttype.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_type_id', 'user_id', name='uq_requesttypeapprover'),
    )
    op.create_index(op.f('ix_requesttypeapprover_request_type_id'), 'requesttypeapprover', ['request_type_id'], unique=False)
    op.create_index(op.f('ix_requesttypeapprover_user_id'), 'requesttypeapprover', ['user_id'], unique=False)
    op.create_table(
        'ticketapproval',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('approver_id', sa.Integer(), nullable=False),
        sa.Column('decision', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('comment', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['ticket_id'], ['ticket.id']),
        sa.ForeignKeyConstraint(['approver_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticket_id', 'approver_id', name='uq_ticketapproval'),
    )
    op.create_index(op.f('ix_ticketapproval_ticket_id'), 'ticketapproval', ['ticket_id'], unique=False)
    op.create_index(op.f('ix_ticketapproval_approver_id'), 'ticketapproval', ['approver_id'], unique=False)

    group_members: dict[int, list[int]] = {}
    for group_id, user_id in members:
        group_members.setdefault(group_id, []).append(user_id)
    for type_id, group_id, pass_percent, fail_percent in types:
        users = group_members.get(group_id, [])
        bind.execute(
            sa.text("UPDATE requesttype SET approvals_required = :a, declines_required = :d WHERE id = :t"),
            {"a": _needed(pass_percent, len(users)), "d": _needed(fail_percent, len(users)), "t": type_id},
        )
        for user_id in users:
            bind.execute(
                sa.text("INSERT INTO requesttypeapprover (request_type_id, user_id) VALUES (:t, :u)"),
                {"t": type_id, "u": user_id},
            )

    kept = {approval_id: (ticket_id, pass_percent, fail_percent, voters) for approval_id, ticket_id, pass_percent, fail_percent, voters in latest}
    for approval_id, (ticket_id, pass_percent, fail_percent, voters) in kept.items():
        bind.execute(
            sa.text("UPDATE ticket SET approvals_required = :a, declines_required = :d WHERE id = :t"),
            {"a": _needed(pass_percent, voters), "d": _needed(fail_percent, voters), "t": ticket_id},
        )
    for vote_id, approval_id, voter_id, decision, comment, created_at, decided_at in votes:
        if approval_id not in kept:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO ticketapproval (id, ticket_id, approver_id, decision, comment, created_at, decided_at) "
                "VALUES (:id, :t, :u, :d, :c, :cr, :de)"
            ),
            {"id": vote_id, "t": kept[approval_id][0], "u": voter_id, "d": decision, "c": comment, "cr": created_at, "de": decided_at},
        )
    bind.execute(sa.text(
        "SELECT setval(pg_get_serial_sequence('ticketapproval', 'id'), COALESCE((SELECT max(id) FROM ticketapproval), 0) + 1, false)"
    ))

    op.add_column('outboundemail', sa.Column('approval_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_outboundemail_approval_id'), 'outboundemail', ['approval_id'], unique=False)
    op.create_foreign_key('outboundemail_approval_id_fkey', 'outboundemail', 'ticketapproval', ['approval_id'], ['id'])
    for email_id, vote_id in emails:
        bind.execute(
            sa.text("UPDATE outboundemail SET approval_id = :v WHERE id = :e AND EXISTS (SELECT 1 FROM ticketapproval WHERE id = :v)"),
            {"v": vote_id, "e": email_id},
        )

    op.drop_table('approvalgroupmember')
    op.drop_index(op.f('ix_approvalgroup_name'), table_name='approvalgroup')
    op.drop_table('approvalgroup')

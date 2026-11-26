"""seed the two mailbox request types

Data only, no schema change (#325; tracker #305). Creates "Forward my mail" (mailbox action `forwarding`) and "Give someone
access to my mailbox" (`full_access`), both switched off and with no team or category: an admin chooses the team and category
under Admin > Request types and switches them on (the editor refuses to switch one on until all four defaults are set).

A type is created only when no request type already has that mailbox action and none has that name, so a type an admin made by
hand is never touched, and running the migration again adds nothing. It never updates or deletes an existing row.

The downgrade removes only the rows this migration could have created, and only while they are still exactly as seeded and
nothing refers to them (no ticket, question or sub-request template); an edited or used type is left alone.

Revision ID: c3f8a2d6b941
Revises: b5e2c8a1f903
Create Date: 2026-11-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a2d6b941'
down_revision: Union[str, Sequence[str], None] = 'b5e2c8a1f903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEEDS = (
    ("Forward my mail", "forwarding", "mail", "Send the mail that comes to you to a colleague for a set time, such as when you are off sick or on leave."),
    ("Give someone access to my mailbox", "full_access", "key", "Let a colleague read your mailbox or send as you for a set time."),
)


def upgrade() -> None:
    connection = op.get_bind()
    for name, action, icon, description in SEEDS:
        connection.execute(
            sa.text(
                "INSERT INTO requesttype (name, description, is_active, pass_percent, offers_required_by, "
                "subrequests_need_own_approval, approvers_may_self_approve, is_featured, icon, mailbox_action, "
                "default_impact, default_urgency, created_at) "
                "SELECT :name, :description, FALSE, 51, FALSE, FALSE, FALSE, FALSE, :icon, :action, 'low', 'low', NOW() "
                "WHERE NOT EXISTS (SELECT 1 FROM requesttype WHERE mailbox_action = :action) "
                "AND NOT EXISTS (SELECT 1 FROM requesttype WHERE lower(name) = lower(:name))"
            ),
            {"name": name, "description": description, "icon": icon, "action": action},
        )


def downgrade() -> None:
    connection = op.get_bind()
    for name, action, icon, description in SEEDS:
        connection.execute(
            sa.text(
                "DELETE FROM requesttype WHERE name = :name AND mailbox_action = :action AND icon = :icon "
                "AND description = :description AND is_active = FALSE AND approval_group_id IS NULL "
                "AND default_team_id IS NULL AND default_category_id IS NULL AND default_impact = 'low' AND default_urgency = 'low' "
                "AND pass_percent = 51 AND NOT offers_required_by AND NOT is_featured AND due_days_before IS NULL "
                "AND queue_lead_days IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM ticket WHERE request_type_id = requesttype.id) "
                "AND NOT EXISTS (SELECT 1 FROM requesttypequestion WHERE request_type_id = requesttype.id) "
                "AND NOT EXISTS (SELECT 1 FROM requestsubtemplate WHERE request_type_id = requesttype.id "
                "OR sub_request_type_id = requesttype.id)"
            ),
            {"name": name, "action": action, "icon": icon, "description": description},
        )

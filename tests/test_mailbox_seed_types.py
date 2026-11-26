"""The two seeded mailbox request types and their editor rules (#325): the data migration (idempotent, never touches an existing
type), an inactive mailbox type that has no team yet, the Low pre-fill, and the admin home's "Needs attention" line."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import select

from app.admin_home import needs_attention
from app.models import RequestType, Ticket, TicketSource, TicketType
from app.request_types import RequestTypeError, TypeDefaults, create_request_type, update_request_type
from tests.conftest import create_category, create_team

PATH = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "c3f8a2d6b941_seed_mailbox_request_types.py"
spec = importlib.util.spec_from_file_location("seed_mailbox_types", PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def run(db_session, direction):
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()
    db_session.expire_all()


def mailbox_types(db_session):
    return db_session.exec(select(RequestType).where(RequestType.mailbox_action.is_not(None)).order_by(RequestType.id)).all()


def test_the_seed_creates_both_types_off_with_low_low_and_no_team(db_session):
    run(db_session, "upgrade")
    forwarding, access = mailbox_types(db_session)
    assert (forwarding.name, forwarding.mailbox_action, forwarding.icon) == ("Forward my mail", "forwarding", "mail")
    assert (access.name, access.mailbox_action, access.icon) == ("Give someone access to my mailbox", "full_access", "key")
    for row in (forwarding, access):
        assert row.is_active is False and row.approval_group_id is None and row.default_team_id is None and row.default_category_id is None
        assert (row.default_impact, row.default_urgency) == ("low", "low") and row.description.strip()


def test_running_the_seed_again_adds_nothing(db_session):
    run(db_session, "upgrade")
    run(db_session, "upgrade")
    assert len(mailbox_types(db_session)) == 2


def test_an_existing_type_with_the_action_blocks_that_seed_and_is_not_touched(db_session):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    mine = create_request_type(
        db_session, "Mail forwarding (ours)", "Our own wording", None, 51, True,
        defaults=TypeDefaults(team.id, category.id, "high", "medium"), mailbox_action="forwarding", icon="phone",
    )
    db_session.commit()
    before = (mine.name, mine.description, mine.is_active, mine.default_impact, mine.default_urgency, mine.icon, mine.default_team_id)
    run(db_session, "upgrade")
    rows = mailbox_types(db_session)
    assert [r.mailbox_action for r in rows] == ["forwarding", "full_access"]  # only the access type was added
    db_session.refresh(mine)
    assert (mine.name, mine.description, mine.is_active, mine.default_impact, mine.default_urgency, mine.icon, mine.default_team_id) == before


def test_a_type_with_the_same_name_but_no_action_blocks_the_seed_and_is_not_changed(db_session):
    other = RequestType(name="forward my MAIL", description="a plain request", is_active=True, pass_percent=51)
    db_session.add(other)
    db_session.commit()
    run(db_session, "upgrade")
    assert [r.mailbox_action for r in mailbox_types(db_session)] == ["full_access"]
    db_session.refresh(other)
    assert other.mailbox_action is None and other.description == "a plain request"


def test_the_downgrade_removes_only_untouched_unused_seeded_rows(db_session):
    run(db_session, "upgrade")
    forwarding, access = mailbox_types(db_session)
    access.description = "An admin reworded this"
    db_session.add(access)
    db_session.commit()
    run(db_session, "downgrade")
    assert [r.id for r in mailbox_types(db_session)] == [access.id]  # the edited one stays


def test_the_downgrade_keeps_a_seeded_type_a_ticket_refers_to(db_session):
    run(db_session, "upgrade")
    forwarding, access = mailbox_types(db_session)
    db_session.add(Ticket(subject="x", requester_email="a@example.com", source=TicketSource.PORTAL, type=TicketType.SERVICE_REQUEST, request_type_id=forwarding.id))
    db_session.commit()
    run(db_session, "downgrade")
    assert [r.id for r in mailbox_types(db_session)] == [forwarding.id]


# ---- The editor ---------------------------------------------------------------------------------------------------


def test_an_inactive_mailbox_type_can_be_saved_with_no_team_or_category_but_cannot_be_switched_on(db_session):
    made = create_request_type(db_session, "Forward", "d", None, 51, False, mailbox_action="forwarding")
    assert made.default_team_id is None and made.is_active is False
    with pytest.raises(RequestTypeError, match="must triage itself"):
        update_request_type(db_session, made, "Forward", "d", None, 51, True)
    db_session.rollback()
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    update_request_type(db_session, made, "Forward", "d", None, 51, True, defaults=TypeDefaults(team.id, category.id, "low", "low"))
    assert made.is_active is True and made.default_team_id == team.id


def test_an_active_mailbox_type_still_needs_a_team_and_category_on_create(db_session):
    with pytest.raises(RequestTypeError, match="must triage itself"):
        create_request_type(db_session, "Forward", "d", None, 51, True, mailbox_action="forwarding")


def test_the_seeded_types_have_no_approval_group_so_only_mine_is_offered(db_session):
    from app.request_types import offers_someone_elses

    run(db_session, "upgrade")
    assert not any(offers_someone_elses(r) for r in mailbox_types(db_session))


def test_a_blank_impact_and_urgency_are_saved_as_low_for_a_mailbox_type_only(db_session):
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    made = create_request_type(db_session, "Forward", "d", None, 51, True, defaults=TypeDefaults(team.id, category.id), mailbox_action="forwarding")
    assert (made.default_impact, made.default_urgency) == ("low", "low")
    kept = create_request_type(
        db_session, "Access", "d", None, 51, True, defaults=TypeDefaults(team.id, category.id, "high", None), mailbox_action="full_access",
    )
    assert (kept.default_impact, kept.default_urgency) == ("high", "low")  # only the blank one
    plain = create_request_type(db_session, "Ordinary", "d", None, 51, False)
    assert (plain.default_impact, plain.default_urgency) == (None, None)


def test_editing_a_mailbox_type_with_blank_levels_fills_low(db_session):
    made = create_request_type(db_session, "Forward", "d", None, 51, False, mailbox_action="forwarding")
    assert made.default_impact == made.default_urgency == "low"  # filled on create already
    made.default_impact = made.default_urgency = None
    db_session.add(made)
    db_session.commit()
    update_request_type(db_session, made, "Forward", "d", None, 51, False)
    assert (made.default_impact, made.default_urgency) == ("low", "low")


def test_the_editor_shows_the_help_line_under_impact_and_urgency(client, db_session):
    from tests.conftest import login_as, set_role
    from app.models import UserRole

    set_role(db_session, login_as(client, "boss@example.com"), UserRole.SUPER_ADMIN)
    page = client.get("/admin/request-types?new=1").text
    assert "Impact x urgency sets the priority and SLA. Set all four defaults (team, category, impact, urgency) to let a request triage itself." in page


# ---- Needs attention ------------------------------------------------------------------------------------------------


def attention(db_session):
    return [line for line in needs_attention(db_session, frozenset({"request-types"}), datetime.now(timezone.utc), "UTC") if "Mailbox request types" in line.text]


def test_the_admin_home_says_so_while_an_inactive_mailbox_type_has_no_team(db_session):
    assert attention(db_session) == []
    run(db_session, "upgrade")
    [line] = attention(db_session)
    assert line.text == "Mailbox request types need a team before they can be offered."
    first = mailbox_types(db_session)[0]
    assert line.href == f"/admin/request-types?type={first.id}"
    # Only for someone who may open the request types page.
    assert needs_attention(db_session, frozenset(), datetime.now(timezone.utc), "UTC") == []


def test_the_line_goes_once_every_such_type_has_a_team_or_is_on(db_session):
    run(db_session, "upgrade")
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Mailbox")
    first, second = mailbox_types(db_session)
    update_request_type(db_session, first, first.name, first.description, None, 51, True, defaults=TypeDefaults(team.id, category.id, "low", "low"))
    assert len(attention(db_session)) == 1  # the other still waits
    update_request_type(db_session, second, second.name, second.description, None, 51, False, defaults=TypeDefaults(team.id, category.id, "low", "low"))
    assert attention(db_session) == []

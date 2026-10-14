"""access_context is loaded once per database session and never read back stale."""

from sqlalchemy import event
from sqlmodel import Session

from app.database import engine
from app.models import AuditLog, MembershipLevel, TeamMembership as TeamMembershipRow, TicketEscalator, User, UserRole
from app.visibility import Access, access_context
from tests.conftest import add_membership, create_team
from tests.test_visibility import agent_in, make_ticket


def agent_user(db_session, agent_id):
    return db_session.get(User, agent_id)


def test_the_same_context_is_reused_within_a_session(client, db_session):
    team = create_team(db_session, "Support")
    agent = agent_user(db_session, agent_in(client, db_session, member_of=[team]))
    with Session(engine) as session:
        user = session.get(User, agent.id)
        assert access_context(session, user) is access_context(session, user)


def test_each_user_gets_their_own_context(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    first = agent_in(client, db_session, member_of=[a])
    second = agent_in(client, db_session, member_of=[b], email="second@example.com")
    with Session(engine) as session:
        one, two = session.get(User, first), session.get(User, second)
        assert set(access_context(session, one)) == {a.id}
        assert set(access_context(session, two)) == {b.id}


def test_a_commit_drops_the_cached_context(client, db_session):
    a, b = create_team(db_session, "A"), create_team(db_session, "B")
    agent_id = agent_in(client, db_session, member_of=[a])
    with Session(engine) as session:
        user = session.get(User, agent_id)
        assert set(access_context(session, user)) == {a.id}
        add_membership(db_session, agent_id, b.id, MembershipLevel.VIEWER)  # another connection commits
        assert set(access_context(session, user)) == {a.id}  # the cache still holds what it loaded
        session.commit()
        assert access_context(session, user)[b.id] == Access.READ


def test_a_flush_in_the_same_session_drops_the_cached_context(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = make_ticket(db_session, team.id)
    with Session(engine) as session:
        user = session.get(User, agent_id)
        assert ticket.id not in access_context(session, user).escalated_ticket_ids
        session.add(TicketEscalator(ticket_id=ticket.id, user_id=agent_id))
        session.flush()  # a change made in the request itself must show up straight away
        assert ticket.id in access_context(session, user).escalated_ticket_ids
        session.rollback()
        assert ticket.id not in access_context(session, user).escalated_ticket_ids


def test_the_context_follows_a_role_change(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = make_ticket(db_session, team.id)
    db_session.add(TicketEscalator(ticket_id=ticket.id, user_id=agent_id))
    db_session.commit()
    with Session(engine) as session:
        user = session.get(User, agent_id)
        assert ticket.id in access_context(session, user).escalated_ticket_ids
        user.role = UserRole.END_USER  # not flushed yet: the key alone must notice
        assert access_context(session, user).escalated_ticket_ids == frozenset()


def test_a_rolled_back_savepoint_does_not_leave_its_row_in_the_cache(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    ticket = make_ticket(db_session, team.id)
    with Session(engine) as session:
        user = session.get(User, agent_id)
        savepoint = session.begin_nested()
        session.add(TicketEscalator(ticket_id=ticket.id, user_id=agent_id))
        session.flush()
        assert ticket.id in access_context(session, user).escalated_ticket_ids
        savepoint.rollback()
        assert ticket.id not in access_context(session, user).escalated_ticket_ids


def test_loading_with_a_pending_change_still_caches(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    other = create_team(db_session, "Other")
    with Session(engine) as session:
        user = session.get(User, agent_id)
        session.add(TeamMembershipRow(user_id=agent_id, team_id=other.id, level=MembershipLevel.VIEWER))
        first = access_context(session, user)  # the load autoflushes the pending row
        assert first[other.id] == Access.READ
        assert access_context(session, user) is first


def test_a_flush_that_touches_nothing_access_related_keeps_the_cache(client, db_session):
    team = create_team(db_session, "Support")
    agent_id = agent_in(client, db_session, member_of=[team])
    with Session(engine) as session:
        user = session.get(User, agent_id)
        first = access_context(session, user)
        session.add(AuditLog(actor_email="x@example.com", entity_type="ticket", entity_id=1, action="noted"))
        session.flush()
        assert access_context(session, user) is first


def test_opening_a_ticket_loads_the_agents_teams_once(client, db_session):
    team = create_team(db_session, "Support")
    agent_in(client, db_session, member_of=[team])
    ticket = make_ticket(db_session, team.id)
    seen = []

    def record(conn, cursor, statement, *rest):
        if "FROM teammembership" in statement and "teammembership.level" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get(f"/tickets/{ticket.number}").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(seen) == 1

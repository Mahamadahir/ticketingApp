"""The per-test clean-up (tests/conftest.py) empties only the tables a test wrote to. Each test here writes,
runs the clean-up the fixture runs before the next test (`empty_dirty_tables`) and looks at what is left, so
the proof does not depend on which xdist worker runs which test."""

from sqlalchemy import create_engine, text

from app.database import engine
from app.models import Team
from tests.conftest import TEST_DATABASE_URL, empty_dirty_tables, login_as


def count(table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def test_rows_written_by_the_orm_are_gone_and_ids_restart(db_session):
    first = Team(name="Isolation A")
    db_session.add(first)
    db_session.commit()
    empty_dirty_tables()
    assert count("team") == 0
    db_session.expunge_all()
    second = Team(name="Isolation B")
    db_session.add(second)
    db_session.commit()
    assert second.id == 1


def test_rows_written_through_the_test_client_are_gone(client):
    login_as(client, "isolation@example.com")
    assert count("user") == 1
    empty_dirty_tables()
    assert count("user") == 0


def test_rows_written_by_raw_sql_are_gone():
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO numbercounter (name, last_number) VALUES ('isolation', 7)")
    assert count("numbercounter") == 1
    empty_dirty_tables()
    assert count("numbercounter") == 0


def test_rows_written_by_a_tests_own_engine_are_gone():
    own = create_engine(TEST_DATABASE_URL)
    try:
        with own.begin() as connection:
            connection.exec_driver_sql("INSERT INTO numbercounter (name, last_number) VALUES ('own', 3)")
    finally:
        own.dispose()
    empty_dirty_tables()
    assert count("numbercounter") == 0


def test_an_update_and_a_cascading_child_are_covered(db_session):
    team = Team(name="Parent")
    db_session.add(team)
    db_session.commit()
    db_session.execute(text("UPDATE team SET name = 'Renamed' WHERE id = :id"), {"id": team.id})
    db_session.commit()
    empty_dirty_tables()
    assert count("team") == 0


def test_a_clean_table_is_not_truncated():
    """Only dirty tables are named in the TRUNCATE: nothing written means nothing to empty."""
    empty_dirty_tables()
    assert empty_dirty_tables() == []


def test_the_opt_out_does_a_full_truncate(monkeypatch):
    names = empty_dirty_tables()
    assert names == []
    monkeypatch.setenv("TEST_FULL_TRUNCATE", "1")
    full = empty_dirty_tables()
    assert "team" in full and "user" in full and "numbercounter" in full
    assert len(full) > 20


def test_no_table_holds_rows_after_the_clean_up():
    """create_all seeds nothing, and the clean-up leaves every table empty, not only the ones it named."""
    empty_dirty_tables()
    with engine.connect() as connection:
        names = [name for (name,) in connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))]
    left = {name: count(name) for name in names if name != "alembic_version"}
    assert {name: rows for name, rows in left.items() if rows} == {}

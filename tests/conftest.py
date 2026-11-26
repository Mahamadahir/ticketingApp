import os
import re

import psycopg2
from sqlalchemy.engine import make_url

# Tests run against Postgres, the database production uses. Locally that is the
# compose db (`docker compose up -d db`), published on 127.0.0.1.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://ticketing:ticketing@127.0.0.1:5432/ticketing_test"
)


def per_worker(url: str) -> str:
    """Under pytest-xdist (`pytest -n auto`) each worker gets its own database, so the workers never
    wipe each other's tables: ticketing_test becomes ticketing_gw0_test, ticketing_gw1_test and so on.
    A plain `pytest` run (no workers) uses the URL as given."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return url
    parsed = make_url(url)
    base = parsed.database.removesuffix("_test")
    return parsed.set(database=f"{base}_{worker}_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = per_worker(TEST_DATABASE_URL)


# Databases this process created (never one that already existed); dropped when the session ends.
CREATED_DATABASES: list[str] = []


def drop_created_databases(url: str, names: list[str]) -> list[str]:
    """Drops the databases in `names`, which this process created. Each name must end in _test, so
    the dev database can never be dropped here. A failure is reported, never raised: clean-up must
    not turn a green run red. Returns the names dropped."""
    dropped = []
    parsed = make_url(url)
    try:
        connection = psycopg2.connect(
            host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
        )
    except psycopg2.Error as error:
        print(f"Could not drop test databases {names}: cannot connect: {error}")
        return dropped
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for name in names:
                if not name.endswith("_test"):
                    continue
                try:
                    cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # ours: close any stray connection
                    dropped.append(name)
                except psycopg2.Error as error:
                    print(f"Could not drop test database {name}: {error}")
    finally:
        connection.close()
    return dropped


def ensure_test_database(url: str) -> None:
    """Creates the test database if it is missing (and remembers it, so the session drops it at the
    end). Refuses any database whose name does not end in _test, because the tests drop every
    table in it."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise RuntimeError("Tests need PostgreSQL; set TEST_DATABASE_URL to a postgresql+psycopg2 URL")
    if not parsed.database or not parsed.database.endswith("_test"):
        raise RuntimeError(f"Refusing to run tests against '{parsed.database}': the name must end in _test")

    try:
        connection = psycopg2.connect(
            host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
        )
    except psycopg2.OperationalError as error:
        raise RuntimeError(
            "Cannot reach the test Postgres. Start it with `docker compose up -d db` "
            f"(or set TEST_DATABASE_URL). Original error: {error}"
        ) from error
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (parsed.database,))
        if cursor.fetchone() is None:
            cursor.execute(f'CREATE DATABASE "{parsed.database}"')
            CREATED_DATABASES.append(parsed.database)
        # Commits stop waiting for the WAL flush: a test database holds nothing worth keeping, and a
        # crash only loses rows the next run wipes anyway. Only here, behind the _test guard above.
        try:
            cursor.execute(f'ALTER DATABASE "{parsed.database}" SET synchronous_commit = off')
        except psycopg2.Error as error:  # not the owner: only a speed-up, never worth failing the run
            print(f"Could not set synchronous_commit on {parsed.database}: {error}")
    connection.close()


ensure_test_database(TEST_DATABASE_URL)

os.environ["ENV"] = "dev"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SUPER_ADMIN_EMAILS"] = "admin@example.com"
os.environ["SESSION_SECRET_KEY"] = "test-secret"
os.environ["SSO_TENANT_ID"] = ""
os.environ["SSO_CLIENT_ID"] = ""
os.environ["SSO_CLIENT_SECRET"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.database import engine
from app.main import app
from datetime import datetime, timezone

from app.models import Category, MembershipLevel, Team, TeamMembership, TicketStatus, User, UserRole


@pytest.fixture(scope="session", autouse=True)
def schema():
    """Builds the tables once per run (dropping first clears whatever an interrupted run left)."""
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield
    SQLModel.metadata.drop_all(engine)


def pytest_sessionfinish(session, exitstatus):
    """Drops the databases this process created, so runs do not pile up `*_test` databases on the
    server (hundreds slow every checkpoint). Each xdist worker drops its own, the controller any
    shared one. Set KEEP_TEST_DB=1 to keep them (to look at a failed run). A database that existed
    before the run, such as CI's own, is never dropped."""
    if os.environ.get("KEEP_TEST_DB") == "1" or not CREATED_DATABASES:
        return
    engine.dispose()
    drop_created_databases(TEST_DATABASE_URL, list(CREATED_DATABASES))
    CREATED_DATABASES.clear()


# Emptying every table before every test costs a fixed amount per test (new files for every table, index
# and sequence, and a lock on all of them), so a test only empties the tables it wrote to. Writes are
# spotted where every one of them passes: the cursor of any SQLAlchemy engine in this process (the app's
# own sessions, TestClient requests, job code, a test's own engine, `exec_driver_sql`, raw SQL). A statement
# that contains a write keyword marks every known table named in it (an extra table emptied at worst); one
# with a write keyword or a sequence function and no known table (a SQL function that writes) marks them
# all. Foreign keys are no gap: a child row can only exist if a write to the child was seen, so the child is
# marked itself, and the marked tables are emptied children first. Writes that bypass SQLAlchemy (a raw psycopg2
# connection to the test database) are not seen: use a scratch database for those, as the migration tests do.
# The marked tables are emptied with DELETE, not TRUNCATE: a TRUNCATE ... CASCADE of `user` or `ticket` takes
# 25 to 36 of the 56 tables with it (all that reference them, empty or not), each with a new file synced to
# disk, which is the cost this avoids. The ids of an emptied table are put back to 1 with setval, as
# RESTART IDENTITY did. TEST_FULL_TRUNCATE=1 restores the old behaviour (TRUNCATE of every table, every
# test) to debug a suspected leak.
_TABLE_NAMES = [table.name for table in SQLModel.metadata.sorted_tables]
_WRITE_WORDS = re.compile(r"\b(INSERT|UPDATE|DELETE|COPY|MERGE|NEXTVAL|SETVAL)\b", re.IGNORECASE)
_TABLE_WORDS = {name: re.compile(rf'(?<![\w])"?{re.escape(name)}"?(?![\w])', re.IGNORECASE) for name in _TABLE_NAMES}
_DIRTY: set[str] = set()
_CLEANING = False  # the clean-up's own DELETE and setval statements are not writes by a test
# Tables whose single integer primary key is a sequence, to put back to 1 after the table is emptied.
_ID_COLUMNS = {
    table.name: next(iter(table.primary_key.columns)).name
    for table in SQLModel.metadata.sorted_tables
    if len(table.primary_key.columns) == 1
    and next(iter(table.primary_key.columns)).type.python_type is int
    and next(iter(table.primary_key.columns)).autoincrement in (True, "auto")
}


@event.listens_for(Engine, "before_cursor_execute")
def _note_writes(connection, cursor, statement, parameters, context, executemany):
    # A TRUNCATE leaves the table empty, so it dirties nothing; the clean-up's own statements are skipped.
    if _CLEANING or statement.lstrip().upper().startswith("TRUNCATE"):
        return
    if _WRITE_WORDS.search(statement):
        named = {name for name, pattern in _TABLE_WORDS.items() if pattern.search(statement)}
        _DIRTY.update(named or _TABLE_NAMES)


def tables_to_empty() -> list[str]:
    """The tables to empty before the next test, in a stable order; clears the record."""
    if os.environ.get("TEST_FULL_TRUNCATE") == "1":
        _DIRTY.clear()
        return list(_TABLE_NAMES)
    names = [name for name in _TABLE_NAMES if name in _DIRTY]
    _DIRTY.clear()
    return names


def empty_dirty_tables() -> list[str]:
    """Empties the tables written to since the last call (all of them, by TRUNCATE, with TEST_FULL_TRUNCATE=1),
    restarting their ids, and returns their names."""
    global _CLEANING
    names = tables_to_empty()
    if not names:
        return names
    _CLEANING = True
    try:
        with engine.begin() as connection:
            if os.environ.get("TEST_FULL_TRUNCATE") == "1":
                listed = ", ".join(f'"{name}"' for name in names)
                connection.exec_driver_sql(f"TRUNCATE {listed} RESTART IDENTITY CASCADE")
            else:
                for name in reversed(names):  # children before the tables they reference
                    connection.exec_driver_sql(f'DELETE FROM "{name}"')
                for name in names:
                    if name in _ID_COLUMNS:
                        connection.exec_driver_sql(
                            f"SELECT setval(pg_get_serial_sequence('\"{name}\"', '{_ID_COLUMNS[name]}'), 1, false)"
                        )
    except Exception:
        _DIRTY.update(names)  # still dirty: the next test must try again
        raise
    finally:
        _CLEANING = False
    return names


@pytest.fixture(autouse=True)
def fresh_database(schema):
    """Each test starts with empty tables: the ones the previous test wrote to are emptied (all of them,
    by TRUNCATE, with TEST_FULL_TRUNCATE=1). The schema itself is shared by the whole run, so a test must never change
    it (no DDL against the test database); use a scratch database for that, as the migration tests do."""
    empty_dirty_tables()
    yield


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """The public routes' counters are in memory and the test client is always the same caller."""
    from app import rate_limit

    rate_limit.APPROVALS.reset()
    rate_limit.ACTION_LINKS.reset()
    rate_limit.WEBHOOK.reset()
    rate_limit.BREAK_GLASS.reset()
    rate_limit.EMERGENCY.reset()
    rate_limit.TEAMS_BOT.reset()
    rate_limit.PEOPLE_SEARCH.reset()
    yield


@pytest.fixture(autouse=True)
def no_layout_for_htmx(monkeypatch):
    """Generic guard: an htmx form swaps the response into <main>, so a page rendered for an htmx
    request must never carry the layout (header, nav, scripts). Fails any test that triggers it."""
    from starlette.templating import Jinja2Templates

    real = Jinja2Templates.TemplateResponse

    def checked(self, request, name, *args, **kwargs):
        response = real(self, request, name, *args, **kwargs)
        boosted = request.headers.get("HX-Boosted") == "true"  # a boosted request swaps the whole body: it needs the full page
        if request.headers.get("HX-Request") == "true" and not boosted and (b"<html" in response.body or b"<nav>" in response.body):
            raise AssertionError(f"{name} rendered the full page layout for an htmx request")
        return response

    monkeypatch.setattr(Jinja2Templates, "TemplateResponse", checked)


@pytest.fixture
def db_session():
    with Session(engine) as session:
        yield session


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def login_as(client: TestClient, email: str, display_name: str = "Test User") -> int:
    """Signs in via dev-login (JIT-provisions the user) and returns their id."""
    response = client.post(
        "/auth/dev-login",
        data={"email": email, "display_name": display_name},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email.lower())).first()
    return user.id


def set_role(
    db_session: Session,
    user_id: int,
    role: UserRole,
    team_id: int | None = None,
    level: MembershipLevel = MembershipLevel.MEMBER,
) -> None:
    """Sets a user's role and, if a team is given, their membership of it."""
    user = db_session.get(User, user_id)
    user.role = role
    db_session.add(user)
    if team_id is not None:
        add_membership(db_session, user_id, team_id, level, commit=False)
    db_session.commit()


def add_membership(
    db_session: Session,
    user_id: int,
    team_id: int,
    level: MembershipLevel = MembershipLevel.MEMBER,
    commit: bool = True,
) -> None:
    db_session.add(TeamMembership(user_id=user_id, team_id=team_id, level=level))
    if commit:
        db_session.commit()


def create_team(db_session: Session, name: str) -> Team:
    team = Team(name=name)
    db_session.add(team)
    db_session.commit()
    db_session.refresh(team)
    return team

def create_category(db_session: Session, team: Team | None, name: str = "Hardware", parent: Category | None = None) -> Category:
    """A main category owned by the team, or a sub-category of `parent`."""
    category = Category(name=name, team_id=None if parent else (team.id if team else None), parent_id=parent.id if parent else None)
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def escalatable(db_session: Session, ticket, target: Team | None = None, name: str = "Hardware") -> Team:
    """Gives a ticket a category that escalates to `target` (a new team if none is given),
    so the Escalate button has somewhere to send it. Also triages it (escalating needs
    that) if it is still New. Returns the target team."""
    if target is None:
        target = create_team(db_session, f"Escalation target {ticket.id}")
    category = Category(name=f"{name} {ticket.id}", team_id=ticket.team_id, escalation_team_id=target.id)
    db_session.add(category)
    db_session.flush()
    ticket.category_id = category.id
    if ticket.triaged_at is None:
        ticket.triaged_at = datetime.now(timezone.utc)
        ticket.status = TicketStatus.OPEN
    db_session.add(ticket)
    db_session.commit()
    return target


@pytest.fixture
def no_hold_while_active(db_session):
    """Switches off the hold on a Teams copy while the person is active in the app (8F, `teams_hold_while_active_minutes`).
    A test that signs an agent in through the client has a live session, which would otherwise hold every copy it makes due;
    the tests of the hold itself (tests/test_teams_hold.py) leave it on."""
    from app.settings_store import TEAMS_HOLD_WHILE_ACTIVE_MINUTES, set_setting

    set_setting(db_session, TEAMS_HOLD_WHILE_ACTIVE_MINUTES, "0")
    db_session.commit()

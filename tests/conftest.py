import os

import psycopg2
from sqlalchemy.engine import make_url

# Tests run against Postgres, the database production uses. Locally that is the
# compose db (`docker compose up -d db`), published on 127.0.0.1.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://ticketing:ticketing@127.0.0.1:5432/ticketing_test"
)


def ensure_test_database(url: str) -> None:
    """Creates the test database if it is missing. Refuses any database whose
    name does not end in _test, because the tests drop every table in it."""
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
from sqlmodel import Session, SQLModel, select

from app.database import engine
from app.main import app
from datetime import datetime, timezone

from app.models import Category, MembershipLevel, Team, TeamMembership, TicketStatus, User, UserRole


@pytest.fixture(autouse=True)
def fresh_database():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield
    SQLModel.metadata.drop_all(engine)


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

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
    if not parsed.database.endswith("_test"):
        raise RuntimeError(f"Refusing to run tests against '{parsed.database}': the name must end in _test")

    connection = psycopg2.connect(
        host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
    )
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
from app.models import Team, User, UserRole


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


def set_role(db_session: Session, user_id: int, role: UserRole, team_id: int | None = None) -> None:
    user = db_session.get(User, user_id)
    user.role = role
    user.team_id = team_id
    db_session.add(user)
    db_session.commit()


def create_team(db_session: Session, name: str) -> Team:
    team = Team(name=name)
    db_session.add(team)
    db_session.commit()
    db_session.refresh(team)
    return team

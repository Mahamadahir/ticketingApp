import os

os.environ["ENV"] = "dev"
# CI sets DATABASE_URL to a real Postgres; locally the tests default to SQLite.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test.db")
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

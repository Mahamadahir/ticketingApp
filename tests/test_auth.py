from sqlmodel import Session, select

from app.auth import hash_password
from app.database import engine
from app.models import User, UserRole
from tests.conftest import login_as


def test_unauthenticated_request_redirects_to_login(client):
    response = client.get("/tickets", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login"


def test_dev_login_creates_end_user_by_default(client):
    login_as(client, "newbie@example.com")

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == "newbie@example.com")).first()
    assert user.role == UserRole.END_USER


def test_dev_login_promotes_super_admin_email(client):
    login_as(client, "admin@example.com")

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == "admin@example.com")).first()
    assert user.role == UserRole.SUPER_ADMIN


def test_logout_clears_session(client):
    login_as(client, "someone@example.com")
    assert client.get("/tickets", follow_redirects=False).status_code != 307

    client.get("/auth/logout", follow_redirects=False)
    response = client.get("/portal", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login"


def test_local_login_with_correct_password(client, db_session):
    user = User(
        email="contractor@example.com",
        display_name="Contractor",
        role=UserRole.AGENT,
        password_hash=hash_password("correct-horse"),
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/auth/local-login",
        data={"email": "contractor@example.com", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_local_login_with_wrong_password_redirects_with_error(client, db_session):
    user = User(
        email="contractor2@example.com",
        display_name="Contractor",
        role=UserRole.AGENT,
        password_hash=hash_password("correct-horse"),
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/auth/local-login",
        data={"email": "contractor2@example.com", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

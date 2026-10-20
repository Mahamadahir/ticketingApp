import threading

from sqlmodel import Session

from app.auth import attempt_local_login, hash_password
from app.database import engine
from app.models import User, UserRole
from app.pagination import MAX_PAGE
from tests.conftest import login_as


def make_local_user(db_session, email="contractor@example.com"):
    user = User(email=email, display_name="C", role=UserRole.AGENT, password_hash=hash_password("correct-horse"))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_attempts_during_a_lock_do_not_extend_it(client, db_session):
    user = make_local_user(db_session)
    for _ in range(5):
        client.post("/auth/local-login", data={"email": user.email, "password": "wrong"})
    db_session.expire_all()
    locked_until = db_session.get(User, user.id).locked_until
    count = db_session.get(User, user.id).failed_login_count

    for _ in range(10):
        client.post("/auth/local-login", data={"email": user.email, "password": "wrong"})

    db_session.expire_all()
    after = db_session.get(User, user.id)
    assert after.locked_until == locked_until
    assert after.failed_login_count == count


def test_parallel_wrong_guesses_are_all_counted(db_session):
    user = make_local_user(db_session)
    attempts = 4

    def guess():
        with Session(engine) as session:
            attempt_local_login(session, user.email, "wrong")

    threads = [threading.Thread(target=guess) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == attempts


def test_a_malformed_stored_hash_is_just_a_failed_login(client, db_session):
    user = make_local_user(db_session)
    user.password_hash = "not-a-bcrypt-hash"
    db_session.add(user)
    db_session.commit()

    response = client.post("/auth/local-login", data={"email": user.email, "password": "x"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?error=")


def test_enormous_page_number_is_clamped_not_a_server_error(client, db_session):
    login_as(client, "admin@example.com")
    assert client.get("/tickets?page=99999999999999999999").status_code == 200
    assert client.get(f"/tickets?page={MAX_PAGE + 1}").status_code == 200


def test_malformed_filter_is_a_400_not_a_server_error(client, db_session):
    login_as(client, "admin@example.com")
    assert client.get("/admin/audit-log?subject_user_id=abc").status_code == 400


def test_webhook_rejects_bad_json_and_ignores_wrong_client_state(client):
    assert client.post("/webhooks/graph/notifications", content=b"not json").status_code == 400
    assert client.post("/webhooks/graph/notifications", json=["a list"]).status_code == 400

    wrong = client.post("/webhooks/graph/notifications", json={"value": [{"clientState": 5}, "junk", {}]})
    assert wrong.status_code == 202
    assert wrong.json() == {"status": "accepted"}


def test_webhook_still_echoes_the_validation_token(client):
    response = client.post("/webhooks/graph/notifications?validationToken=abc123")
    assert response.text == "abc123"

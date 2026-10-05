from datetime import datetime, timedelta, timezone

import pytest

from app.auth import BASE_LOCKOUT, MAX_FAILED_LOGINS, hash_password
from app.models import User, UserRole

EMAIL = "contractor@example.com"
PASSWORD = "correct-horse"
# Covers bcrypt time spent between setting the lock and checking it.
LOCK_TOLERANCE = timedelta(seconds=30)


@pytest.fixture
def local_user(db_session):
    user = User(email=EMAIL, display_name="Contractor", role=UserRole.AGENT, password_hash=hash_password(PASSWORD))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def attempt_login(client, password: str, email: str = EMAIL):
    return client.post("/auth/local-login", data={"email": email, "password": password}, follow_redirects=False)


def is_signed_in(response) -> bool:
    return response.headers["location"] == "/"


def fail_logins(client, count: int) -> None:
    for _ in range(count):
        assert not is_signed_in(attempt_login(client, "wrong"))


def reload(db_session, user: User) -> User:
    db_session.expire_all()
    return db_session.get(User, user.id)


def as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_roughly(actual: timedelta, expected: timedelta) -> bool:
    return abs(actual - expected) < LOCK_TOLERANCE


def test_account_locks_after_max_failed_attempts(client, db_session, local_user):
    fail_logins(client, MAX_FAILED_LOGINS)

    assert not is_signed_in(attempt_login(client, PASSWORD))
    assert reload(db_session, local_user).locked_until is not None


def test_failures_below_the_limit_do_not_lock(client, db_session, local_user):
    fail_logins(client, MAX_FAILED_LOGINS - 1)

    assert is_signed_in(attempt_login(client, PASSWORD))


def test_successful_login_resets_the_counter(client, db_session, local_user):
    fail_logins(client, MAX_FAILED_LOGINS - 1)
    attempt_login(client, PASSWORD)

    user = reload(db_session, local_user)
    assert user.failed_login_count == 0
    assert user.locked_until is None


def test_locked_and_wrong_password_responses_are_identical(client, db_session, local_user):
    wrong_password = attempt_login(client, "wrong")
    fail_logins(client, MAX_FAILED_LOGINS)
    locked = attempt_login(client, PASSWORD)
    unknown_email = attempt_login(client, PASSWORD, email="nobody@example.com")

    assert wrong_password.headers["location"] == locked.headers["location"] == unknown_email.headers["location"]


def test_login_works_again_once_the_lock_expires(client, db_session, local_user):
    fail_logins(client, MAX_FAILED_LOGINS)
    user = reload(db_session, local_user)
    user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.add(user)
    db_session.commit()

    assert is_signed_in(attempt_login(client, PASSWORD))


def test_each_further_failure_doubles_the_lock(client, db_session, local_user):
    fail_logins(client, MAX_FAILED_LOGINS)
    first_lock = as_utc(reload(db_session, local_user).locked_until)

    fail_logins(client, 1)
    second_lock = as_utc(reload(db_session, local_user).locked_until)

    now = datetime.now(timezone.utc)
    assert is_roughly(first_lock - now, BASE_LOCKOUT)
    assert is_roughly(second_lock - now, BASE_LOCKOUT * 2)

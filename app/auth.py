from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import msal
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, update
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import User, UserRole

AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"
MAX_FAILED_LOGINS = 5
BASE_LOCKOUT = timedelta(minutes=15)
# Caps the lock at 15 minutes x 2^10, about ten and a half days.
MAX_LOCKOUT_DOUBLINGS = 10
SSO_SCOPES = ["User.Read"]

# bcrypt only uses the first 72 bytes of a password.
BCRYPT_MAX_BYTES = 72


class NotAuthenticated(Exception):
    pass


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("ascii"))
    except ValueError:
        # Malformed stored hash: do the same bcrypt work as a real check, so
        # timing doesn't single the account out, and treat it as a wrong password.
        bcrypt.checkpw(_password_bytes(password), _DUMMY_HASH.encode("ascii"))
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


@dataclass
class LoginResult:
    user: Optional[User] = None
    # Why it failed, for the activity log only: unknown_account, not_a_local_account,
    # account_locked or wrong_password. The person at the form never sees this.
    failure: Optional[str] = None
    # Set when this very failure caused the account to lock.
    locked_until: Optional[datetime] = None


def authenticate_local(session: Session, email: str, password: str) -> User | None:
    return attempt_local_login(session, email, password).user


def attempt_local_login(session: Session, email: str, password: str) -> LoginResult:
    """Succeeds only for a correct password on an unlocked account. Every rejection
    looks the same to the person at the form, so it can't be used to learn which
    emails have accounts or which are locked; the reason is returned so the
    caller can record it in the activity log.
    """
    user = session.exec(select(User).where(User.email == email.lower())).first()
    if not user or not user.password_hash:
        # Spend the same bcrypt time as a real check so response timing
        # doesn't reveal whether the account exists.
        _verify_password(password, _DUMMY_HASH)
        return LoginResult(failure="unknown_account" if not user else "not_a_local_account")

    now = datetime.now(timezone.utc)
    is_password_correct = _verify_password(password, user.password_hash)

    if _is_locked(user, now):
        # Attempts during a lock change nothing, so hammering the form cannot
        # extend someone else's lock.
        return LoginResult(failure="account_locked")

    if not is_password_correct:
        locked_until = _record_failed_login(session, user, now)
        session.commit()
        return LoginResult(failure="wrong_password", locked_until=locked_until)

    user.failed_login_count = 0
    user.locked_until = None
    session.add(user)
    session.commit()
    return LoginResult(user=user)


def _is_locked(user: User, now: datetime) -> bool:
    if user.locked_until is None:
        return False
    return _as_utc(user.locked_until) > now


def _record_failed_login(session: Session, user: User, now: datetime) -> Optional[datetime]:
    """Counts the failure in the database in one statement, so parallel
    guesses across workers cannot read the same count and each write back
    the same increment."""
    count = session.exec(
        update(User)
        .where(User.id == user.id)
        .values(failed_login_count=User.failed_login_count + 1)
        .returning(User.failed_login_count)
    ).scalar_one()
    if count < MAX_FAILED_LOGINS:
        return None
    doublings = min(count - MAX_FAILED_LOGINS, MAX_LOCKOUT_DOUBLINGS)
    lock_until = now + BASE_LOCKOUT * (2**doublings)
    # Parallel failures finish in any order; the longest lock wins.
    session.exec(
        update(User)
        .where(User.id == user.id)
        .values(locked_until=func.greatest(func.coalesce(User.locked_until, lock_until), lock_until))
    )
    return lock_until


def _as_utc(moment: datetime) -> datetime:
    # Postgres returns timezone-aware datetimes; this only guards against a
    # naive value reaching the comparison.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        settings.sso_client_id,
        authority=AUTHORITY_TEMPLATE.format(tenant_id=settings.sso_tenant_id),
        client_credential=settings.sso_client_secret,
    )


def build_auth_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        SSO_SCOPES, state=state, redirect_uri=settings.sso_redirect_uri
    )


def acquire_token_by_auth_code(code: str) -> dict:
    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=SSO_SCOPES, redirect_uri=settings.sso_redirect_uri
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("error_description", "Sign-in failed"))
    return result


def get_or_create_user(session: Session, email: str, display_name: str) -> User:
    email = email.lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if user:
        return user

    role = UserRole.SUPER_ADMIN if email in settings.super_admin_email_list else UserRole.END_USER
    user = User(email=email, display_name=display_name, role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()

    user = session.get(User, user_id)
    if not user:
        raise NotAuthenticated()
    # Lets error pages (such as "no access") show the signed-in user's navigation.
    request.state.user = user
    return user


def require_roles(*roles: UserRole):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Not permitted")
        return user

    return dependency

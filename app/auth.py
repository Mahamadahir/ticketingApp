import msal
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import User, UserRole

AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"
SSO_SCOPES = ["User.Read"]

_pwd_context = CryptContext(schemes=["bcrypt"])


class NotAuthenticated(Exception):
    pass


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def authenticate_local(session: Session, email: str, password: str) -> User | None:
    user = session.exec(select(User).where(User.email == email.lower())).first()
    if not user or not user.password_hash:
        return None
    if not _pwd_context.verify(password, user.password_hash):
        return None
    return user


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
    return user


def require_roles(*roles: UserRole):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Not permitted")
        return user

    return dependency

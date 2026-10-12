"""Security and request activity: who did what, from where, and what happened.

Two sources feed the ActivityLog table:
- Dedicated events from the sign-in code: login_success, login_failed (with the
  reason), account_locked, logout and sso_failed.
- A middleware for everything else: every state-changing request (POST and the
  like), and any request refused with 403 (access_denied). It is a safety net:
  an action is recorded even if its feature forgot to write an audit entry.

What is never stored: request bodies, passwords, tokens, cookies or query strings
(the Entra callback carries a one-time code in its query string, and later links
carry tokens in their path). The path stored is the route template, such as
/tickets/{ticket_id}/note. A failure to write a row is logged and swallowed: the
log must never be the reason a request fails.
"""

import logging
from typing import Optional

from fastapi import Request
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import engine
from app.models import ActivityLog, User

logger = logging.getLogger(__name__)

STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# These have their own, more informative events (login_success, login_failed, logout).
HAS_DEDICATED_EVENT = {"/auth/local-login", "/auth/dev-login", "/auth/logout"}
MAX_USER_AGENT = 300
MAX_DETAIL = 500
MAX_EMAIL = 254

# Event types, as shown to super admins.
LOGIN_SUCCESS = "login_success"
LOGIN_FAILED = "login_failed"
ACCOUNT_LOCKED = "account_locked"
LOGOUT = "logout"
SSO_FAILED = "sso_failed"
ACCESS_DENIED = "access_denied"
REQUEST = "request"
PROBLEM_EVENTS = (LOGIN_FAILED, ACCOUNT_LOCKED, SSO_FAILED, ACCESS_DENIED)


def client_ip(request: Request) -> Optional[str]:
    """The caller's address. Behind the Container Apps ingress this is the real
    client only because uvicorn trusts the forwarded headers from that subnet alone
    (FORWARDED_ALLOW_IPS), so it cannot be forged by a client."""
    return request.client.host if request.client else None


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    return value[:limit] if value else value


def record_activity(
    session: Session,
    request: Request,
    event_type: str,
    *,
    actor_email: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    detail: Optional[str] = None,
    status_code: Optional[int] = None,
) -> None:
    """Adds a row on the caller's session without committing, so a sign-in event
    lands in the same transaction as the change it describes."""
    route = request.scope.get("route")
    session.add(
        ActivityLog(
            event_type=event_type,
            actor_user_id=actor_user_id,
            actor_email=_clip(actor_email.lower() if actor_email else None, MAX_EMAIL),
            method=request.method,
            path=getattr(route, "path", None),
            status_code=status_code,
            ip=client_ip(request),
            user_agent=_clip(request.headers.get("user-agent"), MAX_USER_AGENT),
            detail=_clip(detail, MAX_DETAIL),
        )
    )


def _write_request_row(
    request_method: str,
    template: str,
    status_code: int,
    event_type: str,
    user_id: Optional[int],
    ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    with Session(engine) as session:
        user = session.get(User, user_id) if user_id is not None else None
        session.add(
            ActivityLog(
                event_type=event_type,
                actor_user_id=user.id if user else None,
                actor_email=user.email.lower() if user else None,
                method=request_method,
                path=template,
                status_code=status_code,
                ip=ip,
                user_agent=_clip(user_agent, MAX_USER_AGENT),
            )
        )
        session.commit()


class ActivityMiddleware(BaseHTTPMiddleware):
    """Records state-changing requests and refused (403) requests. Must sit inside
    the session middleware (added to the app before it) so the signed-in user can
    be read."""

    async def dispatch(self, request: Request, call_next):
        crashed: Optional[Exception] = None
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:  # noqa: BLE001 - recorded as a 500, then re-raised
            crashed, status_code = exc, 500
        try:
            denied = status_code == 403
            if denied or request.method in STATE_CHANGING_METHODS:
                template = getattr(request.scope.get("route"), "path", None)
                # An unmatched path (a scanner, a typo) has no template; recording
                # those would let anyone flood the table, so they are skipped.
                if template is not None and (denied or template not in HAS_DEDICATED_EVENT):
                    session_data = request.scope.get("session") or {}
                    await run_in_threadpool(
                        _write_request_row,
                        request.method,
                        template,
                        status_code,
                        ACCESS_DENIED if denied else REQUEST,
                        session_data.get("user_id"),
                        client_ip(request),
                        request.headers.get("user-agent"),
                    )
        except Exception:  # noqa: BLE001 - the log must never break a request
            logger.exception("Could not write the activity log row")
        if crashed is not None:
            raise crashed
        return response

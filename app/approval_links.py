"""Emailed approve/decline links (phase 1H).

The link in an approver's email opens a page that shows the request and asks; it changes
nothing. Deciding is a separate POST from that page. A link on its own must never decide,
because mail scanners and link previewers fetch every URL in a message before the person
sees it, and a GET that approved would approve for them.

The token names one approval and is signed and time-limited, so it needs no storage and the
email (phase 3) can mint a fresh one at any time. Possession of the link is what proves who
is answering, as with the reopen and feedback links; the portal is the signed-in way to do
the same.
"""

from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_SALT = "approval-link"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret_key, salt=_SALT)


def make_token(approval_id: int) -> str:
    return _serializer().dumps(approval_id)


def read_token(token: str, max_age_minutes: int) -> Optional[int]:
    """The approval id in a token, or None when it is forged, damaged or older than max_age."""
    try:
        value = _serializer().loads(token, max_age=max_age_minutes * 60)
    except BadSignature:  # includes SignatureExpired
        return None
    return value if isinstance(value, int) else None
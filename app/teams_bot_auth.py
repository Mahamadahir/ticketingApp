"""Checking the token on a request that claims to come from the Bot Connector (phase 8A).

Anyone on the internet can POST to the bot's endpoint, so nothing in an activity is believed until its bearer
token has passed every check the Bot Framework documents for the Connector to bot direction:

1. an `Authorization: Bearer` header holding a JWT signed with RS256 by a key in the Bot Framework's published
   key set (OpenID metadata at login.botframework.com, whose address is a constant here and not a setting, so
   there is no way to point the check somewhere else or switch it off);
2. issuer `https://api.botframework.com`, audience our bot application id, not expired (a small clock skew is
   allowed), and a `serviceUrl` claim equal to the `serviceUrl` of the activity, so a token cannot be replayed
   with an activity that names another service address (our reply, and the bot token it carries, goes there);
3. the signing key is endorsed for the `msteams` channel, and the activity is for that channel.

Every failure is a `TokenRejected` with a status and no detail for the caller (the reason is logged without the
token). Keys are cached and refreshed at least daily; an unknown key id causes one refresh, at most once a minute,
so a flood of made-up key ids cannot become a flood of outbound requests.

The key fetch is `fetch_json`, a module function the tests replace with a fake metadata endpoint.
"""

import logging
import threading
import time
from typing import Any, Optional

import httpx
import jwt

from app.config import settings

log = logging.getLogger("app.teams_bot_auth")

METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
CHANNEL = "msteams"
ALGORITHMS = ["RS256"]
CLOCK_SKEW_SECONDS = 300  # the Bot Framework's documented industry standard
KEYS_MAX_AGE_SECONDS = 24 * 3600  # "at least once every 24 hours"
UNKNOWN_KEY_REFRESH_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 10
MAX_TOKEN_CHARACTERS = 8192


class TokenRejected(Exception):
    """The request is refused. `status` is 401 for a token that is wrong and 403 for a token that is genuine but not
    for this channel or tenant. `reason` is for the log only."""

    def __init__(self, reason: str, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def fetch_json(url: str) -> dict:
    response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    return body


class KeyCache:
    """The Bot Framework's signing keys by key id, with when they were last read."""

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._loaded_at: Optional[float] = None
        self._tried_at: Optional[float] = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._keys, self._loaded_at, self._tried_at = {}, None, None

    @staticmethod
    def _fetch() -> dict[str, dict[str, Any]]:
        metadata = fetch_json(METADATA_URL)
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
            raise ValueError("metadata has no https jwks_uri")
        document = fetch_json(jwks_uri)
        keys = {k["kid"]: k for k in document.get("keys", []) if isinstance(k, dict) and isinstance(k.get("kid"), str)}
        if not keys:
            raise ValueError("no keys")
        return keys

    def key_for(self, kid: str, now: Optional[float] = None) -> dict[str, Any]:
        """The signing key with this id. The keys are re-read when they are a day old or the id is unknown, but never
        more than once a minute, whether or not the last read worked (a failed read is not retried on every request).
        The read happens outside the lock, so a slow Microsoft endpoint never holds up requests that can be answered
        from the keys we have; a first request that arrives while the first read is under way is refused, and Teams
        sends it again."""
        now = time.monotonic() if now is None else now
        with self._lock:
            stale = self._loaded_at is None or now - self._loaded_at >= KEYS_MAX_AGE_SECONDS
            may_try = self._tried_at is None or now - self._tried_at >= UNKNOWN_KEY_REFRESH_SECONDS
            refresh = (stale or kid not in self._keys) and may_try
            if refresh:
                self._tried_at = now
        if refresh:
            try:
                keys = self._fetch()
            except Exception as error:  # a failed refresh keeps the keys we have; a token needing another is refused
                log.warning("Teams bot: reading the Bot Framework keys failed: %s", type(error).__name__)
            else:
                with self._lock:
                    self._keys, self._loaded_at = keys, now
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            raise TokenRejected("unknown signing key")
        return key


KEYS = KeyCache()


def same_service_url(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def check_request(authorization: Optional[str], service_url: str, channel_id: str) -> dict:
    """Returns the token's claims when the request is genuine, else raises TokenRejected."""
    if not settings.teams_bot_app_id:
        raise TokenRejected("no bot application id configured")
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_CHARACTERS:
        raise TokenRejected("no bearer token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise TokenRejected("not a JWT")
    kid = header.get("kid")
    if header.get("alg") not in ALGORITHMS or not isinstance(kid, str):
        raise TokenRejected("wrong algorithm or no key id")
    jwk = KEYS.key_for(kid)
    try:
        claims = jwt.decode(
            token,
            jwt.PyJWK(jwk).key,
            algorithms=ALGORITHMS,
            audience=settings.teams_bot_app_id,
            issuer=ISSUER,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (jwt.PyJWTError, ValueError, KeyError) as error:
        raise TokenRejected(f"token invalid: {type(error).__name__}")
    claimed = claims.get("serviceurl") or claims.get("serviceUrl")
    if not isinstance(claimed, str) or not same_service_url(claimed, service_url or ""):
        raise TokenRejected("serviceUrl does not match the token")
    if channel_id != CHANNEL or CHANNEL not in (jwk.get("endorsements") or []):
        raise TokenRejected("channel not endorsed", status=403)
    return claims

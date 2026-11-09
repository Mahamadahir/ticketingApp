"""A small client for the Bot Connector REST API (phase 8A), over httpx.

The Bot Framework SDK is archived and its successors are new, so this does the four things the app needs and
nothing more: get a token for the bot, reply to an activity, create a one-to-one conversation, and send a message
or an Adaptive Card into a conversation.

Signing in. The bot application is single tenant. With TEAMS_BOT_CLIENT_SECRET set the token comes from the
client-credentials flow against our tenant; without it the token comes from the user-assigned managed identity
whose client id is TEAMS_BOT_APP_ID (Azure only; nothing local can prove it). Either way the scope is
`https://api.botframework.com/.default`. A token is kept until shortly before it expires. It is sent only to the
service URL Teams gave us, which must be https.

Failures. A call raises the httpx exception it met (`HTTPStatusError`, a timeout, a connection error), so the
Teams outbox's rules apply to it unchanged: `app.teams.is_permanent` says whether trying again can help,
`describe_failure` is the text to keep (no URL, no token) and `record_failure` schedules the next try with the
outbox's backoff. Inside one call, a 401 gets one retry with a fresh token, and a 408, 429 or 5xx (or a failure to
connect) is retried twice with a short wait that honours Retry-After, so a blip never reaches the outbox. Longer
outages are the outbox's job: the worker's rows wait 2, 4, 8 ... 60 minutes.
"""

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import httpx

from app.config import settings
from app.teams import RETRYABLE_CLIENT_ERRORS, message

log = logging.getLogger("app.bot_connector")

SCOPE = "https://api.botframework.com/.default"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TIMEOUT_SECONDS = 10
# Waits before the second and third attempt of one call. Short on purpose: a request is waiting on this, or the
# worker's job; anything longer goes back to the outbox.
RETRY_WAITS_SECONDS = (1.0, 3.0)
MAX_RETRY_AFTER_SECONDS = 5.0
TOKEN_EARLY_SECONDS = 300  # renew this long before the token runs out
TEXT_FORMAT = "plain"


@dataclass(frozen=True)
class ConversationRef:
    """Where a message goes: the Bot Connector service address and the conversation on it."""

    service_url: str
    conversation_id: str


class BotAuthFailed(Exception):
    """The Bot Connector refused the bot's own credential (a 401 even with a fresh token): an expired secret or a
    misconfigured identity. Not an HTTPStatusError, so `teams.is_permanent` does not treat it as a refused message
    and an outbox row keeps retrying until the credential is fixed."""


class BotNotConfigured(Exception):
    """The bot has no application id or tenant, or the service address is not https."""


def new_client() -> httpx.Client:
    """The HTTP client for one call. Redirects are never followed: the bot token must not be sent anywhere else."""
    return httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---- Tokens --------------------------------------------------------------------------------------------------


def managed_identity_token() -> tuple[str, float]:
    """(token, expiry as epoch seconds) from the user-assigned managed identity that is the bot's identity."""
    from azure.identity import ManagedIdentityCredential

    token = ManagedIdentityCredential(client_id=settings.teams_bot_app_id).get_token(SCOPE)
    return token.token, float(token.expires_on)


def client_secret_token() -> tuple[str, float]:
    with new_client() as client:
        response = client.post(
            TOKEN_URL.format(tenant=settings.teams_bot_tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.teams_bot_app_id,
                "client_secret": settings.teams_bot_client_secret,
                "scope": SCOPE,
            },
        )
    if response.status_code in (400, 401):
        raise BotAuthFailed("The bot's credential was refused when asking for a token") from None
    response.raise_for_status()
    body = response.json()
    return body["access_token"], time.time() + float(body.get("expires_in", 3600))


class TokenCache:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token is None or time.time() >= self._expires_at - TOKEN_EARLY_SECONDS:
                fetch = client_secret_token if settings.teams_bot_client_secret else managed_identity_token
                self._token, self._expires_at = fetch()
            return self._token

    def reset(self) -> None:
        with self._lock:
            self._token, self._expires_at = None, 0.0


TOKENS = TokenCache()


# ---- Calls ---------------------------------------------------------------------------------------------------


def _base(service_url: str) -> str:
    parts = urlsplit(service_url or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise BotNotConfigured("The Teams service address is not an https address")
    return service_url.rstrip("/")


def _wait_for(response: Optional[httpx.Response], attempt: int) -> float:
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    return RETRY_WAITS_SECONDS[min(attempt, len(RETRY_WAITS_SECONDS) - 1)]


def _is_transient(response: httpx.Response) -> bool:
    return response.status_code >= 500 or response.status_code in RETRYABLE_CLIENT_ERRORS


def call(method: str, url: str, body: Optional[dict] = None) -> Any:
    """One Bot Connector call with the bot's token. Returns the decoded JSON body (None when empty). Raises the httpx
    error after the retries described in the module docstring."""
    if not settings.teams_bot_configured:
        raise BotNotConfigured("The Teams bot is not configured")
    refreshed = False
    attempt = 0
    while True:
        response: Optional[httpx.Response] = None
        try:
            token = TOKENS.get()
            with new_client() as client:
                response = client.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401 and not refreshed:
                refreshed = True
                TOKENS.reset()  # the token may have been revoked or rotated; the next pass asks for a new one
                continue
            if response.status_code == 401:
                raise BotAuthFailed("The Bot Connector refused the bot's credential") from None
            if _is_transient(response) and attempt < len(RETRY_WAITS_SECONDS):
                sleep(_wait_for(response, attempt))
                attempt += 1
                continue
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # Only a connection that was never made is tried again: after a read timeout the Connector may already have
            # delivered the message, and sending it twice is worse than telling the outbox it failed.
            if attempt < len(RETRY_WAITS_SECONDS):
                log.warning("Bot Connector call failed (%s), trying again", type(error).__name__)
                sleep(_wait_for(None, attempt))
                attempt += 1
                continue
            raise
        return response.json() if response.content else None


def _conversation_url(ref: ConversationRef, *tail: str) -> str:
    return "/".join([_base(ref.service_url), "v3", "conversations", quote(ref.conversation_id, safe=""), *[quote(t, safe="") for t in tail]])


def send(ref: ConversationRef, activity: dict) -> Optional[str]:
    """Sends an activity into a conversation. Returns the new activity's id."""
    result = call("POST", _conversation_url(ref, "activities"), activity)
    return result.get("id") if isinstance(result, dict) else None


def send_text(ref: ConversationRef, text: str) -> Optional[str]:
    return send(ref, {"type": "message", "text": text, "textFormat": TEXT_FORMAT})


def send_card(ref: ConversationRef, card: dict) -> Optional[str]:
    """Sends one Adaptive Card (the card itself, not the Workflows payload around it)."""
    return send(ref, message(card))


def reply_to(activity: dict, *, text: Optional[str] = None, card: Optional[dict] = None) -> Optional[str]:
    """Replies to an activity Teams sent us, in its conversation, addressed back to its sender."""
    ref = ConversationRef(activity["serviceUrl"], activity["conversation"]["id"])
    reply = message(card) if card is not None else {"type": "message", "text": text or "", "textFormat": TEXT_FORMAT}
    reply.update({"from": activity.get("recipient"), "recipient": activity.get("from"), "conversation": activity["conversation"]})
    if activity.get("id"):
        reply["replyToId"] = activity["id"]
        result = call("POST", _conversation_url(ref, "activities", activity["id"]), reply)
    else:
        result = call("POST", _conversation_url(ref, "activities"), reply)
    return result.get("id") if isinstance(result, dict) else None


def create_conversation(service_url: str, teams_user_id: str, tenant_id: Optional[str] = None) -> ConversationRef:
    """Opens (or finds) the one-to-one chat between the bot and a person, from their Teams user id (the `from.id` of
    an activity they sent, 29:...). The person must have the app installed."""
    tenant = tenant_id or settings.teams_bot_tenant_id
    result = call(
        "POST",
        _base(service_url) + "/v3/conversations",
        {
            "bot": {"id": f"28:{settings.teams_bot_app_id}"},
            "isGroup": False,
            "members": [{"id": teams_user_id}],
            "tenantId": tenant,
            "channelData": {"tenant": {"id": tenant}},
        },
    )
    conversation_id = result.get("id") if isinstance(result, dict) else None
    if not conversation_id:
        raise ValueError("The Bot Connector did not return a conversation")
    return ConversationRef(service_url, conversation_id)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def get_member(service_url: str, conversation_id: str, teams_user_id: str) -> dict:
    """One member of a conversation as Teams knows them (a TeamsChannelAccount: `name`, `email`, `userPrincipalName`,
    `objectId`). The only way to learn the address of a person who has no account here yet, from the chat they wrote in.
    No Graph permission is needed: it is the bot's own token on the Bot Connector."""
    ref = ConversationRef(service_url, conversation_id)
    result = call("GET", _conversation_url(ref, "members", teams_user_id))
    return result if isinstance(result, dict) else {}


FILE_TIMEOUT_SECONDS = 30
FILE_CHUNK = 64 * 1024


def new_file_client() -> httpx.Client:
    return httpx.Client(timeout=FILE_TIMEOUT_SECONDS, follow_redirects=False)


class FileRefused(Exception):
    """A file Teams offered could not be taken: not an https address, larger than the limit, or no longer there."""


def fetch_file(url: str, max_bytes: int, *, service_url: Optional[str] = None) -> bytes:
    """Downloads a file a person sent the bot, never more than `max_bytes`. Raises FileRefused when it cannot be taken
    and the httpx error for a failure of the network. Redirects are not followed.

    Teams gives two kinds of address. A file download address (`file.download.info`) is pre-authenticated and is fetched
    with no credential at all. A picture pasted into the chat has a `contentUrl` on the Bot Connector's own host (the
    activity's service address), and only that one is fetched with the bot's token: the token is never sent to any other
    host. Both must be https and a name, not an IP address."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or _is_ip(host):
        raise FileRefused("The file address is not an https address")
    headers = {}
    if service_url and host == (urlsplit(service_url).hostname or "").lower():
        headers["Authorization"] = f"Bearer {TOKENS.get()}"
    data = bytearray()
    with new_file_client() as client:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in (404, 410):
                raise FileRefused("no longer available")
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FileRefused("larger than the limit")
            for chunk in response.iter_bytes(FILE_CHUNK):
                data += chunk
                if len(data) > max_bytes:
                    raise FileRefused("larger than the limit")
    return bytes(data)

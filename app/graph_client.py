"""Microsoft Graph client for the shared IT support mailbox.

Mimecast sits in front of Exchange Online as a mail gateway, so it never
enters this integration directly: by the time a message reaches the shared
mailbox, Graph API is the only interface this app needs. Auth is app-only
client-credentials against an Entra ID app registration with Mail.Read and
Mail.Send permissions (admin-consented) scoped to the shared mailbox.

Everything below is stubbed pending the actual app registration. Fill in
GRAPH_* values in .env and flesh out the TODOs when that's ready.
"""

import time
from urllib.parse import quote

import httpx

from app.config import settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
PRESENCE_BATCH = 650  # the most ids getPresencesByUserId accepts in one request
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


class GraphClient:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        token_url = TOKEN_URL_TEMPLATE.format(tenant_id=settings.graph_tenant_id)
        response = httpx.post(
            token_url,
            data={
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = time.time() + payload["expires_in"] - 60
        return self._access_token

    def object_id_for(self, email: str) -> str | None:
        """The person's Entra object id (what the presence API wants), or None if the directory has
        no such user. GET /users/{email}?$select=id treats the email as the user principal name, the
        usual case; anyone whose sign-in name differs is not found and so never marked out of office.
        Needs User.Read.All (application)."""
        response = httpx.get(
            f"{GRAPH_BASE_URL}/users/{quote(email)}",
            params={"$select": "id"},
            headers={"Authorization": f"Bearer {self._get_access_token()}"},
            timeout=15,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("id")

    def presences(self, object_ids: list[str]) -> list[dict]:
        """POST /communications/getPresencesByUserId: one reading per id, up to 650 ids a request
        (Presence.Read.All, application). Each reading carries outOfOfficeSettings.isOutOfOffice, which
        covers Outlook automatic replies, Teams scheduled out of office and calendar events marked
        out of office."""
        readings: list[dict] = []
        for start in range(0, len(object_ids), PRESENCE_BATCH):
            response = httpx.post(
                f"{GRAPH_BASE_URL}/communications/getPresencesByUserId",
                json={"ids": object_ids[start : start + PRESENCE_BATCH]},
                headers={"Authorization": f"Bearer {self._get_access_token()}"},
                timeout=30,
            )
            response.raise_for_status()
            readings.extend(response.json().get("value", []))
        return readings

    def send_mail(self, to_address: str, subject: str, body: str, reply_to_message_id: str | None = None) -> None:
        """Send a reply from the shared mailbox so it lands as a normal
        email reply in the requester's inbox.

        TODO: once a real app registration exists, call
        POST /users/{mailbox}/sendMail (new thread) or
        POST /users/{mailbox}/messages/{id}/reply (threaded reply) depending
        on whether reply_to_message_id is set.
        """
        raise NotImplementedError("Wire up Graph credentials in .env before sending mail")

    def create_subscription(self, notification_url: str) -> dict:
        """Create a Graph change notification subscription on the shared
        mailbox so new mail triggers POST /webhooks/graph/notifications.

        TODO: call POST /subscriptions with resource
        "users/{mailbox}/mailFolders('Inbox')/messages", changeType
        "created", and settings.graph_webhook_client_state as clientState.
        Subscriptions expire (max ~3 days for mail) and need renewal via a
        scheduled job.
        """
        raise NotImplementedError("Wire up Graph credentials in .env before creating subscriptions")

    def get_message(self, message_id: str) -> dict:
        """TODO: GET /users/{mailbox}/messages/{message_id} to fetch the
        full message body/sender after a change notification arrives (the
        notification payload itself only carries IDs, not content).
        """
        raise NotImplementedError("Wire up Graph credentials in .env before fetching messages")


graph_client = GraphClient()

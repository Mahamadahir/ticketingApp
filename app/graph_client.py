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
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.config import settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
PRESENCE_BATCH = 650  # the most ids getPresencesByUserId accepts in one request
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def _graph_time(moment: datetime) -> str:
    """Graph wants an ISO 8601 UTC time ("2026-10-27T10:00:00Z")."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


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
            f"{GRAPH_BASE_URL}/users/{quote(email, safe='')}",
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

    def send_mail(
        self,
        to_address: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Sends a plain-text email from the shared mailbox: POST /users/{mailbox}/sendMail (Mail.Send,
        application, limited to that mailbox). Graph answers 202 and the message goes to Sent Items.
        Raises on any failure; the exception text can hold the address, so callers record
        describe_failure() and never str() of it. Graph accepts only X- headers here, and threading
        rests on the ticket reference in the subject, which the inbound side matches on."""
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
            "ccRecipients": [{"emailAddress": {"address": address}} for address in (cc or [])],
        }
        if headers:
            message["internetMessageHeaders"] = [{"name": name, "value": value} for name, value in headers.items()]
        response = httpx.post(
            f"{GRAPH_BASE_URL}/users/{quote(settings.graph_shared_mailbox, safe='@')}/sendMail",
            json={"message": message, "saveToSentItems": True},
            headers={"Authorization": f"Bearer {self._get_access_token()}"},
            timeout=30,
        )
        response.raise_for_status()

    def _authorized(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_access_token()}"}

    def list_subscriptions(self) -> list[dict]:
        """GET /subscriptions: every subscription this app registration holds (following pages)."""
        subscriptions: list[dict] = []
        url: str | None = f"{GRAPH_BASE_URL}/subscriptions"
        while url:
            body = self._get_json(url)
            subscriptions.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
        return subscriptions

    def create_subscription(
        self, resource: str, notification_url: str, lifecycle_url: str, client_state: str, expires_at: datetime
    ) -> dict:
        """POST /subscriptions for new mail. Graph calls notification_url back (validationToken) before it
        answers, so the public app must be reachable. lifecycleNotificationUrl can only be set here.
        The reply carries the subscription id and the expiry Graph actually granted."""
        response = httpx.post(
            f"{GRAPH_BASE_URL}/subscriptions",
            json={
                "changeType": "created",
                "notificationUrl": notification_url,
                "lifecycleNotificationUrl": lifecycle_url,
                "resource": resource,
                "expirationDateTime": _graph_time(expires_at),
                "clientState": client_state,
            },
            headers=self._authorized(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def renew_subscription(self, subscription_id: str, expires_at: datetime) -> dict:
        """PATCH /subscriptions/{id}: the new expiry. 404 means the subscription is gone."""
        response = httpx.patch(
            f"{GRAPH_BASE_URL}/subscriptions/{quote(subscription_id, safe='')}",
            json={"expirationDateTime": _graph_time(expires_at)},
            headers=self._authorized(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def delete_subscription(self, subscription_id: str) -> None:
        """DELETE /subscriptions/{id}. Already gone counts as done."""
        response = httpx.delete(
            f"{GRAPH_BASE_URL}/subscriptions/{quote(subscription_id, safe='')}",
            headers=self._authorized(),
            timeout=30,
        )
        if response.status_code != 404:
            response.raise_for_status()

    def inbox_delta_page(self, link: str | None, since: datetime | None = None) -> dict:
        """One page of the inbox delta query (GET .../mailFolders/inbox/messages/delta). With no link it
        starts a fresh sync of mail received since `since`; with a link (a nextLink or the deltaLink of
        an earlier run) it carries on from there. The reply holds `value` and either `@odata.nextLink`
        or `@odata.deltaLink`. Only ids are asked for: the mail itself is fetched by inbound processing.
        Needs Mail.Read, limited to the mailbox."""
        if link is None:
            mailbox = quote(settings.graph_shared_mailbox, safe="@")
            params = {"$select": "id,receivedDateTime"}
            if since is not None:
                params["$filter"] = f"receivedDateTime ge {_graph_time(since)}"
            return self._get_json(f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders/inbox/messages/delta", params=params)
        return self._get_json(link)

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        # A stored or server-supplied link is only ever followed on Graph itself, so the token is
        # never sent anywhere else.
        if not url.startswith(GRAPH_BASE_URL + "/"):
            raise ValueError("Refusing to send a Graph token to a link outside Graph")
        response = httpx.get(
            url, params=params, headers={**self._authorized(), "Prefer": "odata.maxpagesize=50"}, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def get_message(self, message_id: str) -> dict:
        """TODO: GET /users/{mailbox}/messages/{message_id} to fetch the
        full message body/sender after a change notification arrives (the
        notification payload itself only carries IDs, not content).
        """
        raise NotImplementedError("Wire up Graph credentials in .env before fetching messages")


graph_client = GraphClient()

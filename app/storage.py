"""Where attachment bytes live (docs/plan.md, "Storage decisions").

Two implementations behind one small interface, chosen by settings:

- LocalStorage: a directory. For development and tests; it streams through the app.
- BlobStorage: Azure Blob Storage through the app's managed identity (no account keys, no
  connection strings). A download is a redirect to a user-delegation SAS URL that lives for
  about a minute and a half, with the response headers forced by the link, so the browser is
  served the file from the storage origin, never from the app's.

Keys are made by app/attachments.py from the content hash ("ab/abcd..."); both implementations
refuse anything else, so a key can never be a path.

Azure-only to prove: DefaultAzureCredential with the managed identity, the user-delegation key
and the SAS it signs. Tests use a fake client; Azurite may not support user-delegation SAS.
"""

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.parse import quote

from app.config import settings

_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def read(self, key: str) -> bytes: ...

    def download_url(self, key: str, *, filename: str, content_type: str, inline: bool) -> Optional[str]:
        """A short-lived address the browser can be sent to, or None when the app has to stream
        the bytes itself (the local implementation)."""


class InvalidKey(ValueError):
    pass


def _check_key(key: str) -> str:
    if not _KEY.match(key):
        raise InvalidKey("not a storage key")
    return key


def content_disposition(filename: str, inline: bool) -> str:
    """The header that decides whether the browser shows a file or saves it. The filename is
    sent both as plain ASCII (old clients) and as RFC 5987 UTF-8; quotes and control characters
    are removed first, so a name cannot end the header or add another."""
    kind = "inline" if inline else "attachment"
    clean = "".join(c for c in filename if c.isprintable() and c not in '"\\/;')
    ascii_name = clean.encode("ascii", "replace").decode("ascii").replace("?", "_") or "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean or 'attachment')}"


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(path)  # never leaves a half-written file under the real name

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def download_url(self, key: str, *, filename: str, content_type: str, inline: bool) -> Optional[str]:
        return None


class BlobStorage:
    """service_client is anything with the BlobServiceClient surface used below (a fake in the
    tests). Left out, it is a real client signed in as the app's managed identity."""

    # A delegation key is requested for an hour and reused until ten minutes before it ends,
    # so issuing a link does not call Azure every time.
    _KEY_LIFETIME = timedelta(hours=1)
    _KEY_REFRESH_BEFORE = timedelta(minutes=10)

    def __init__(
        self,
        account_name: str,
        container: str,
        *,
        service_client=None,
        link_seconds: int = 90,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sas_generator=None,
    ):
        self.account_name = account_name
        self.container = container
        self.link_seconds = link_seconds
        self._clock = clock
        self._sas_generator = sas_generator
        self._service = service_client
        self._delegation_key = None
        self._delegation_expiry: Optional[datetime] = None

    @property
    def account_url(self) -> str:
        return f"https://{self.account_name}.blob.core.windows.net"

    @property
    def service(self):
        if self._service is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            # AZURE_CLIENT_ID (set by the Bicep) picks the app's user-assigned identity.
            self._service = BlobServiceClient(self.account_url, credential=DefaultAzureCredential())
        return self._service

    def _blob(self, key: str):
        return self.service.get_blob_client(container=self.container, blob=_check_key(key))

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from azure.storage.blob import ContentSettings

        self._blob(key).upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def exists(self, key: str) -> bool:
        return bool(self._blob(key).exists())

    def read(self, key: str) -> bytes:
        return self._blob(key).download_blob().readall()

    def _key(self, now: datetime):
        if self._delegation_key is None or now >= self._delegation_expiry - self._KEY_REFRESH_BEFORE:
            expiry = now + self._KEY_LIFETIME
            self._delegation_key = self.service.get_user_delegation_key(now - timedelta(minutes=5), expiry)
            self._delegation_expiry = expiry
        return self._delegation_key

    def download_url(self, key: str, *, filename: str, content_type: str, inline: bool) -> Optional[str]:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        generate = self._sas_generator or generate_blob_sas
        now = self._clock()
        token = generate(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=_check_key(key),
            user_delegation_key=self._key(now),
            permission=BlobSasPermissions(read=True),
            # A minute of slack for a clock that runs behind Azure's.
            start=now - timedelta(minutes=1),
            expiry=now + timedelta(seconds=self.link_seconds),
            # The link, not the stored blob, decides how the browser treats the file.
            content_disposition=content_disposition(filename, inline),
            content_type=content_type,
            protocol="https",
        )
        return f"{self.account_url}/{self.container}/{key}?{token}"


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    """The store the settings name. A FastAPI dependency, so tests swap it with
    app.dependency_overrides."""
    if settings.storage_kind == "blob":
        return BlobStorage(
            settings.storage_account_name, settings.storage_container, link_seconds=settings.attachment_link_seconds
        )
    return LocalStorage(settings.storage_local_path)

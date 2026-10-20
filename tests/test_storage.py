import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from azure.storage.blob import UserDelegationKey

from app.storage import BlobStorage, InvalidKey, LocalStorage, content_disposition

KEY = "ab/" + "ab" * 32
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


# ---- Local ----------------------------------------------------------------------


def test_local_storage_round_trips_and_streams(tmp_path):
    storage = LocalStorage(tmp_path)
    assert not storage.exists(KEY)
    storage.put(KEY, b"hello", "text/plain")
    assert storage.exists(KEY)
    assert storage.read(KEY) == b"hello"
    assert storage.download_url(KEY, filename="a.txt", content_type="text/plain", inline=False) is None
    assert not list(tmp_path.rglob("*.part"))  # the temporary file is renamed away


@pytest.mark.parametrize("key", ["../etc/passwd", "ab/../../x", "/abs/path", "ab/cd", KEY + "/x", KEY.upper(), ""])
def test_a_key_that_is_not_a_hash_is_refused(tmp_path, key):
    storage = LocalStorage(tmp_path)
    with pytest.raises(InvalidKey):
        storage.put(key, b"x", "text/plain")
    with pytest.raises(InvalidKey):
        storage.read(key)
    with pytest.raises(InvalidKey):
        storage.exists(key)


# ---- Content-Disposition --------------------------------------------------------


def test_content_disposition_cannot_be_broken_out_of():
    header = content_disposition('evil";\r\nX-Injected: 1/../.pdf', inline=False)
    assert header.startswith("attachment; ")
    assert "\r" not in header and "\n" not in header
    assert header.count('"') == 2  # only the two that wrap the ASCII name


def test_content_disposition_keeps_a_unicode_name_for_modern_clients():
    header = content_disposition("café ✓.pdf", inline=True)
    assert header.startswith("inline; ")
    assert "filename*=UTF-8''caf%C3%A9%20%E2%9C%93.pdf" in header


# ---- Blob, against a fake client -------------------------------------------------


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, data, overwrite=False, content_settings=None):
        self.store[self.name] = (data, content_settings.content_type)

    def exists(self):
        return self.name in self.store

    def download_blob(self):
        data = self.store[self.name][0]

        class Downloaded:
            def readall(self_inner):
                return data

        return Downloaded()


class FakeService:
    def __init__(self):
        self.blobs = {}
        self.key_requests = []

    def get_blob_client(self, container, blob):
        assert container == "attachments"
        return FakeBlob(self.blobs, blob)

    def get_user_delegation_key(self, start, expiry):
        self.key_requests.append((start, expiry))
        key = UserDelegationKey()
        key.signed_oid = "oid"
        key.signed_tid = "tid"
        key.signed_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_expiry = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_service = "b"
        key.signed_version = "2023-11-03"
        key.value = base64.b64encode(b"k" * 32).decode()
        return key


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def blob():
    service = FakeService()
    clock = Clock()
    return BlobStorage("stticketing", "attachments", service_client=service, link_seconds=90, clock=clock), service, clock


def test_blob_put_exists_and_read_use_the_client(blob):
    storage, service, _ = blob
    assert not storage.exists(KEY)
    storage.put(KEY, b"bytes", "application/pdf")
    assert service.blobs[KEY] == (b"bytes", "application/pdf")
    assert storage.exists(KEY)
    assert storage.read(KEY) == b"bytes"


def test_blob_refuses_a_key_that_is_not_a_hash(blob):
    storage, _, _ = blob
    with pytest.raises(InvalidKey):
        storage.put("../escape", b"x", "text/plain")


def test_the_link_is_a_short_lived_read_only_https_user_delegation_sas(blob):
    storage, service, _ = blob
    url = storage.download_url(KEY, filename="report.pdf", content_type="application/pdf", inline=False)

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"https://stticketing.blob.core.windows.net/attachments/{KEY}"
    query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
    assert query["sp"] == "r"  # read only
    assert query["sr"] == "b"
    assert query["spr"] == "https"
    assert query["sig"]
    expires = datetime.fromisoformat(query["se"].replace("Z", "+00:00"))
    assert expires - NOW == timedelta(seconds=90)  # about a minute and a half, not longer
    assert query["rsct"] == "application/pdf"
    assert query["rscd"].startswith("attachment;")  # the link forces a download
    assert "skoid" in query  # signed with a user delegation key, not an account key
    assert len(service.key_requests) == 1


def test_an_inline_picture_link_asks_the_browser_to_show_it(blob):
    storage, _, _ = blob
    url = storage.download_url(KEY, filename="p.png", content_type="image/png", inline=True)
    assert unquote(parse_qs(urlparse(url).query)["rscd"][0]).startswith("inline;")


def test_the_delegation_key_is_reused_and_then_renewed(blob):
    storage, service, clock = blob
    storage.download_url(KEY, filename="a", content_type="x/y", inline=False)
    clock.now = NOW + timedelta(minutes=30)
    storage.download_url(KEY, filename="a", content_type="x/y", inline=False)
    assert len(service.key_requests) == 1  # reused while it has time left

    clock.now = NOW + timedelta(minutes=55)  # inside the ten minutes before it ends
    storage.download_url(KEY, filename="a", content_type="x/y", inline=False)
    assert len(service.key_requests) == 2

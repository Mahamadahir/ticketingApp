import hashlib
import re
from pathlib import Path

from app import static_url

TEMPLATES = Path("app/templates")
VENDOR = Path("app/static/vendor")


def test_no_template_loads_a_script_or_stylesheet_from_another_origin():
    external = re.compile(r"""<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE)
    offenders = [p.name for p in TEMPLATES.rglob("*.html") if external.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_base_template_uses_the_vendored_htmx():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "unpkg.com" not in base
    assert '<script src="/static/vendor/htmx-2.0.11.min.js"></script>' in base


def test_vendored_htmx_is_served_with_long_cache_and_matches_the_recorded_hash(client):
    response = client.get("/static/vendor/htmx-2.0.11.min.js")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "immutable" not in client.get("/static/vendor/README.md").headers.get("cache-control", "")
    recorded = re.search(r"htmx-2\.0\.11\.min\.js.*?`([0-9a-f]{64})`", (VENDOR / "README.md").read_text(encoding="utf-8"))
    assert recorded is not None
    assert hashlib.sha256(response.content).hexdigest() == recorded.group(1)


def _link(html, name):
    found = re.search(rf'<link rel="stylesheet" href="(/static/{re.escape(name)}\?v=([0-9a-f]{{8}}))"', html)
    assert found, f"{name} is not linked with a version"
    return found.group(1), found.group(2)


def test_pages_link_both_stylesheets_with_a_content_hash(client):
    html = client.get("/auth/login").text
    for name in ("tokens.css", "styles.css"):
        href, version = _link(html, name)
        assert version == hashlib.sha256(Path("app/static", name).read_bytes()).hexdigest()[:8]
        assert f'href="/static/{name}"' not in html


def test_the_hash_follows_the_contents_and_is_not_recomputed_while_the_file_is_unchanged(tmp_path, monkeypatch):
    sheet = tmp_path / "x.css"
    sheet.write_text("a{}", encoding="utf-8")
    first = static_url.file_version(sheet)
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    assert static_url.file_version(sheet) == first and reads == []
    sheet.write_text("a{color:red}", encoding="utf-8")  # a different size: a new stamp whatever the clock's grain
    assert static_url.file_version(sheet) != first and reads == [sheet]
    monkeypatch.setattr(static_url, "STATIC_DIR", tmp_path)
    assert static_url.static_url("x.css") == f"/static/x.css?v={static_url.file_version(sheet)}"


def test_a_versioned_stylesheet_address_is_cached_for_a_year_and_the_bare_one_is_revalidated(client):
    for name in ("tokens.css", "styles.css"):
        href, _ = _link(client.get("/auth/login").text, name)
        versioned = client.get(href)
        assert versioned.status_code == 200
        assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert client.get(f"/static/{name}").headers["cache-control"] == "no-cache"
        # an address that does not carry the current hash (an old page during a deploy) is never pinned
        assert client.get(f"/static/{name}?v=00000000").headers["cache-control"] == "no-cache"


def test_vendor_and_fonts_keep_their_long_cache(client):
    for path in ("/static/vendor/htmx-2.0.11.min.js", "/static/fonts/Inter-4.0-variable.woff2"):
        assert client.get(path).headers["cache-control"] == "public, max-age=31536000, immutable"

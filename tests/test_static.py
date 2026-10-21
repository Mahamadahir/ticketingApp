import hashlib
import re
from pathlib import Path

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


def test_other_static_files_keep_default_caching(client):
    response = client.get("/static/styles.css")
    assert response.status_code == 200
    assert "immutable" not in response.headers.get("cache-control", "")

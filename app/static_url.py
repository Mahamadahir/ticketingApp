"""The address of one of our own stylesheets, with a version taken from its contents.

`static_url("styles.css")` gives `/static/styles.css?v=3f2a9c1e` (the first eight hex digits of the file's SHA-256).
The page can then cache it for a year (see AppStaticFiles in app/main.py), and a changed file is a new address, so no
one needs a hard refresh. The hash is computed when a file is first asked for and again only when its size or
modification time changes (one stat per call, no read): in a running deploy the file never changes, so it is computed
once per process; locally, saving a CSS file shows on the next page load with no restart (docs/ui.md)."""

from pathlib import Path

import hashlib

STATIC_DIR = Path(__file__).resolve().parent / "static"
_cache: dict[Path, tuple[tuple[int, int], str]] = {}


def file_version(path: Path) -> str:
    """The short content hash of a file, recomputed only when its size or modification time changes."""
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    _cache[path] = (stamp, version)
    return version


def static_url(name: str) -> str:
    """{{ static_url("styles.css") }}: the versioned address of a file directly in app/static."""
    try:
        return f"/static/{name}?v={file_version(STATIC_DIR / name)}"
    except OSError:  # a missing file is an unstyled page and a 404 for the CSS, not a page that fails to render
        return f"/static/{name}"

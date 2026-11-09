"""The colour theme (9A): a per-person setting. System (the default, stored as None) follows the browser's
`prefers-color-scheme`; Light and Dark are fixed. The server writes the choice as a class on <html> (base.html), so
the page never flashes the wrong theme on load; System needs no script, a CSS media query does it."""

from typing import Optional

from app.models import User

SYSTEM, LIGHT, DARK = "system", "light", "dark"
THEMES = (SYSTEM, LIGHT, DARK)
LABELS = {SYSTEM: "System", LIGHT: "Light", DARK: "Dark"}
# The top-bar button steps through them in this order.
_NEXT = {SYSTEM: LIGHT, LIGHT: DARK, DARK: SYSTEM}

DEFAULT_RETURN = "/settings/notifications"


def theme_for(user: Optional[User]) -> str:
    """The person's theme: system, light or dark. Anyone not signed in, or with nothing stored, gets System."""
    stored = getattr(user, "theme", None)
    return stored if stored in (LIGHT, DARK) else SYSTEM


def next_theme(current: str) -> str:
    return _NEXT.get(current, SYSTEM)


def set_theme(user: User, value: str) -> bool:
    """Saves the choice; False (nothing changed) for a value that is not one of THEMES. System is stored as None."""
    if value not in THEMES:
        return False
    user.theme = None if value == SYSTEM else value
    return True


def safe_return(path: str) -> str:
    """Where to go after a change: only a path on this site (never another site, a scheme or a protocol-relative link)."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(c) < 32 for c in path):
        return DEFAULT_RETURN
    return path

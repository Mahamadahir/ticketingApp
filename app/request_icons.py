"""The icons of the request types (#206). Kept apart from app/request_types.py so the template environment can use
`icon_path` without importing the triage machinery."""

from typing import Optional

# The icons an admin can give a kind of request (#206): a fixed set of named 24x24 outline drawings (an SVG path each),
# drawn inline by `_icons.html`. An unknown or blank choice shows DEFAULT_ICON. PROBLEM_ICON is the "Something is not
# working" card's, which is not a request type.
ICONS: dict[str, tuple[str, str]] = {
    "chat": ("General", "M4 5h16v11H8l-4 4z"),
    "alert": ("Something wrong", "M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"),
    "mail": ("Mailbox", "M4 6h16v12H4zM4 7l8 6 8-6"),
    "user-plus": ("New starter or leaver", "M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM19 8v6M22 11h-6"),
    "monitor": ("Equipment", "M3 5h18v11H3zM8 21h8M12 16v5"),
    "laptop": ("Laptop", "M4 5h16v11H4zM2 20h20"),
    "phone": ("Phone", "M7 3h10v18H7zM11 18h2"),
    "printer": ("Printing", "M6 9V3h12v6M6 18H4v-7h16v7h-2M6 14h12v7H6z"),
    "key": ("Access or password", "M21 2l-2 2m-7.6 7.6a5.5 5.5 0 11-7.8 7.8 5.5 5.5 0 017.8-7.8zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"),
    "shield": ("Security", "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"),
    "file": ("Document or form", "M6 3h8l4 4v14H6zM14 3v4h4M9 13h6M9 17h6"),
    "wifi": ("Network", "M2 9a15 15 0 0120 0M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0M12 19.5h.01"),
    "box": ("Software or delivery", "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"),
}
DEFAULT_ICON, PROBLEM_ICON = "chat", "alert"


def icon_path(name: Optional[str]) -> str:
    """The SVG path for an icon name (the generic icon for a blank or unknown one)."""
    return ICONS.get(name or "", ICONS[DEFAULT_ICON])[1]

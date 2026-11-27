"""Quoted email history (#255): splits a requester's emailed reply into what they wrote and the earlier conversation their
mail program copied under it, so the agent thread can fold the second part behind a "Show quoted text" toggle.

Nothing is ever dropped. `split_quoted` returns both halves and the page shows both (the second inside a `details`); where no
marker is recognised, or the marker is the very first thing in the mail (nothing of their own above it), the whole text comes
back as the visible part and nothing is folded.

What is recognised (English wording, the mail programs the organisation uses):

- Gmail, Apple Mail and Android: a line "On Tue, 17 Nov 2026 at 10:15, Name <a@b.c> wrote:", which a program may wrap over
  two or three lines.
- Outlook (desktop, web and mobile): "-----Original Message-----" (also "--- Original Message ---"), and the header block
  "From: ... / Sent: ... / To: ... / Subject: ..." (with "Date:" in place of "Sent:", and "Cc:" lines allowed), usually under
  a line of underscores.
- Gmail's forward: "---------- Forwarded message ---------" (the header block that follows is read by app/forwards.py).
- Any program: a block of lines starting with ">" that runs to the end of the mail.

Limits: only the English markers above; a reply typed between the lines of the quote (an inline reply) is not split, apart from
a trailing ">" block; a person who writes a line like "On Monday we agreed X, as you wrote:" is folded from there (the toggle
shows it, so nothing is lost); a mail that is a forward starting with a "From:" block is shown whole. "Sent from my iPhone"
and a signature stay in the visible part.
"""

import re
from typing import Optional

# "On <date and sender> wrote:" with the sender's address, possibly wrapped. The date/sender part is limited so a long
# ordinary sentence ending in "wrote:" is unlikely to match.
_WROTE_END = re.compile(r"\bwrote:\s*$", re.IGNORECASE)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_FORWARDED = re.compile(r"^\s*-{3,}\s*Forwarded message\s*-{3,}\s*$", re.IGNORECASE)
_RULE = re.compile(r"^\s*_{10,}\s*$")
_HEADER = re.compile(r"^\s*\**(From|Sent|Date|To|Cc|Subject)\**\s*:\s*\S", re.IGNORECASE)
_FROM = re.compile(r"^\s*\**From\**\s*:\s*\S", re.IGNORECASE)
_QUOTE_LINE = re.compile(r"^\s*>")


def _wrote_line_at(lines: list[str], i: int) -> bool:
    """Whether an "On ... wrote:" attribution starts at line i (it may run over up to three lines)."""
    if not re.match(r"^\s*On\s", lines[i], re.IGNORECASE):
        return False
    joined = ""
    for j in range(i, min(i + 3, len(lines))):
        joined = f"{joined} {lines[j].strip()}".strip()
        if len(joined) > 300:
            return False
        if _WROTE_END.search(joined):
            return True
        if not lines[j].strip():
            return False
    return False


def _outlook_header_at(lines: list[str], i: int) -> bool:
    """Whether an Outlook header block starts at line i: a From: line followed, within the next six lines, by a Sent:/Date:
    line and by a To: or Subject: line."""
    if not _FROM.match(lines[i]):
        return False
    window = lines[i + 1 : i + 7]
    has_when = any(re.match(r"^\s*\**(Sent|Date)\**\s*:", line, re.IGNORECASE) for line in window)
    has_to = any(re.match(r"^\s*\**(To|Subject)\**\s*:", line, re.IGNORECASE) for line in window)
    return has_when and has_to


def _trailing_quote_start(lines: list[str]) -> Optional[int]:
    """The first line of a block of ">" lines that runs to the end of the mail (blank lines inside it allowed), or None."""
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    start = None
    for i in range(end - 1, -1, -1):
        if _QUOTE_LINE.match(lines[i]):
            start = i
        elif lines[i].strip():
            break
    return start


def lines_of(text: str) -> list[str]:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def quote_start(lines: list[str]) -> Optional[int]:
    """The index of the first line of the quoted or forwarded part (the marker, or the rule above it), or None. It may be 0:
    callers decide what to do when nothing of the sender's own comes first."""
    cut: Optional[int] = None
    for i in range(len(lines)):
        if _ORIGINAL.match(lines[i]) or _FORWARDED.match(lines[i]) or _wrote_line_at(lines, i) or _outlook_header_at(lines, i):
            cut = i
            break
        if _RULE.match(lines[i]) and i + 1 < len(lines):
            following = next((j for j in range(i + 1, min(i + 3, len(lines))) if lines[j].strip()), None)
            if following is not None and _outlook_header_at(lines, following):
                cut = i
                break
    if cut is None:
        cut = _trailing_quote_start(lines)
    return cut


def split_quoted(body: str) -> tuple[str, Optional[str]]:
    """(what the sender wrote, the quoted earlier conversation or None). Joining the two with a newline gives the original
    text again apart from trailing blank lines; the whole body is the first part when nothing is recognised or nothing of their
    own comes before the marker."""
    text = body or ""
    lines = lines_of(text)
    cut = quote_start(lines)
    if cut is None:
        return text, None
    kept = "\n".join(lines[:cut]).rstrip()
    quoted = "\n".join(lines[cut:]).strip("\n")
    if not kept.strip() or not quoted.strip():
        return text, None  # nothing of their own above the marker, or nothing below it: show it all
    return kept, quoted

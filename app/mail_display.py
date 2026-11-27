"""Tidy view of an emailed message (#343), display only: the stored `Message.body` is never changed.

Graph gives each email as plain text (`app/graph_client.py` asks for a text body), and its conversion writes a link as
`text<url>` and a picture as its alt text in brackets. `mail_html` turns that text into safe HTML:

- `text<url>` becomes a link labelled with the token just before the `<` (the text of a multi-word link cannot be told
  from the words around it, so only that last word is the label). The target is the URL exactly as received, Safe Links
  wrapper included, so a click still goes through the time-of-click check; it is never unwrapped. The wrapper is decoded
  only to compare the label with the real host: when they differ (or the label is not a domain at all) the real host is
  shown after the label in a muted span.
- A bare `<url>` shows the decoded real address as its label.
- Only http, https and mailto become links; `<tel:...>` is dropped (the number before it stays); any other `<scheme:...>`
  stays as plain escaped text.
- A line that is only a bracketed name with a letter in it (`[Vardags]`, `[cid:image001.png@01D...]`) is hidden.

Everything else is escaped, line breaks are kept, and no network calls are made. The formats are those in the issue's
sample; they are not yet checked against real Graph output (Azure-only check in the PR).
"""

import re
from urllib.parse import parse_qs, urlsplit

from markupsafe import Markup, escape

# `<scheme:rest>` with no spaces inside. Plain `<a@b.c>` has no scheme and is left alone.
_ANGLE = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]*:[^\s<>]*)>")
_PICTURE_LINE = re.compile(r"^\s*\[[^\[\]]*[A-Za-z][^\[\]]*\]\s*$")
_DOMAIN_LABEL = re.compile(r"^(?:https?://)?((?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,})(?::\d+)?(?:[/?#]\S*)?$", re.IGNORECASE)
_LINK_SCHEMES = ("http", "https", "mailto")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _real_url(url: str) -> str:
    """The address a link really goes to, for comparison and for a bare link's label. A Safe Links URL gives its `url`
    parameter; anything unreadable gives the URL itself."""
    host = _host(url)
    if host.endswith(".safelinks.protection.outlook.com"):
        try:
            inner = parse_qs(urlsplit(url).query).get("url", [""])[0]
        except ValueError:
            inner = ""
        if inner.lower().startswith(("http://", "https://")):
            return inner
    return url


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _anchor(url: str, label: str) -> Markup:
    return Markup('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>').format(url, label)


def _link(url: str, label: str) -> Markup:
    """The link for `label<url>` (label may be empty); `url` has an allowed scheme."""
    real = _real_url(url)
    if url.lower().startswith("mailto:"):
        address = url[7:].split("?", 1)[0]
        if not label or label.lower() == address.lower():
            return _anchor(url, label or address)
        if label.lower().removeprefix("mailto:") == address.lower():
            return _anchor(url, label)
        return _anchor(url, label) + Markup(' <span class="muted small">({})</span>').format(address)
    real_host = _strip_www(_host(real))
    if not label or label == url or label == real:
        return _anchor(url, real)
    m = _DOMAIN_LABEL.match(label)
    if m and _strip_www(m.group(1).lower()) == real_host:
        return _anchor(url, label)
    return _anchor(url, label) + Markup(' <span class="muted small">({})</span>').format(_host(real) or real)


def _tidy_line(line: str) -> Markup:
    out = Markup("")
    pos = 0
    for m in _ANGLE.finditer(line):
        target = m.group(1)
        scheme = target.split(":", 1)[0].lower()
        if scheme == "tel":
            out += escape(line[pos : m.start()])
            pos = m.end()
            continue
        if scheme not in _LINK_SCHEMES or scheme != "mailto" and not _host(target):
            continue  # stays as plain text: left in `line[pos:]` for the next piece
        before = line[pos : m.start()]
        token = re.search(r"\S+$", before)
        label = token.group(0) if token else ""
        out += escape(before[: len(before) - len(label)]) + _link(target, label)
        pos = m.end()
    return out + escape(line[pos:])


def mail_html(text) -> Markup:
    """The safe, tidied HTML for an emailed message's text (see the module docstring). Use in templates as `|mail_html`."""
    if not text:
        return Markup("")
    lines = str(text).split("\n")
    kept = [ln for ln in lines if not _PICTURE_LINE.match(ln)]
    return Markup("\n").join(_tidy_line(ln) for ln in kept)


def is_emailed(message) -> bool:
    """Whether a message's text arrived by email, so the tidy view applies: an inbound email, or the MSP's mail (stored as
    outbound, `msp_forward` set). An agent's own reply (outbound) and portal, Teams and note text are shown as they are."""
    if message is None or message.channel != "email":
        return False
    return message.direction.value == "inbound" or bool(message.msp_forward)


def body_html(message, text):
    """`text` of `message` through `mail_html` when it was emailed, else unchanged (the template escapes it)."""
    return mail_html(text) if is_emailed(message) else text

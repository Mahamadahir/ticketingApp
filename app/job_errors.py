"""How the Scheduled jobs page shows a job's stored error and a Run now result (#323), and the result sentences of the
Outage mode page (a result is a key in the URL, never text).

`last_error` holds the whole exception text (up to 500 characters), which for a database error includes the SQL.
The page shows `summarise_error` in the row and the full text only inside a details element. A Run now result is a
short key in the redirect (`?done=`), never text: the page picks the sentence from `RUN_NOW_SENTENCES`.
"""

import re
from datetime import datetime
from typing import Optional

from app.models import ScheduledJob
from app.scheduled_jobs import within_a_tick

SUMMARY_LENGTH = 80

_CLASS = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Timeout|Exit))\b:?\s*")
_DRIVER_PREFIX = re.compile(r"^\((?:[\w.]+)\)\s*")
_MISSING = re.compile(r'(column) "([^"]+)" of relation "[^"]+" does not exist|(relation|column|table) "([^"]+)" does not exist')
_TAIL = re.compile(r"\s*\[(?:SQL|parameters):.*$|\s*\(Background on this error.*$", re.DOTALL)


def summarise_error(text: Optional[str]) -> str:
    """One plain line for a stored error: the exception class and its first line, cut to about 80 characters.
    A missing table or column reads as such ("Table bankholidaycache does not exist"). Never includes SQL or parameters."""
    if not text or not text.strip():
        return ""
    text = _TAIL.sub("", text.strip())
    found = _CLASS.match(text)
    name, rest = (found.group(1).rsplit(".", 1)[-1], text[found.end():]) if found else ("", text)
    lines = rest.strip().splitlines()
    first = _DRIVER_PREFIX.sub("", lines[0] if lines else "").strip()
    missing = _MISSING.search(first)
    if missing:
        kind, name = (missing.group(1), missing.group(2)) if missing.group(1) else (missing.group(3), missing.group(4))
        return f"{'Table' if kind in ('relation', 'table') else 'Column'} {name} does not exist"
    summary = f"{name}: {first}" if name and first else (name or first)
    if len(summary) > SUMMARY_LENGTH:
        summary = summary[: SUMMARY_LENGTH - 1].rstrip() + "…"
    return summary


# The Run now result: the key goes in the redirect, this fixed list gives the sentence. {job} is a registered job's name.
RUN_NOW_SENTENCES = {
    "queued": "Run requested. The worker will start {job} {within}.",
    "due": "{job} is already due; the worker will run it {within}.",
    "running": "{job} is already running.",
    "missing": "The worker has not created {job} yet; it runs at its first tick.",
}


def run_now_key(row: Optional[ScheduledJob], requested: bool, now: datetime) -> str:
    """The result key for a Run now: queued when a run was requested, else why not (the row is unchanged then)."""
    if requested:
        return "queued"
    if row is None:
        return "missing"
    if row.lease_expires_at is not None and row.lease_expires_at > now:
        return "running"
    return "due"


def run_now_sentence(done: str, job: str, registered: set[str]) -> str:
    """The sentence for a result key and job name, or "" when either is not one we know (free text in the URL shows nothing)."""
    template = RUN_NOW_SENTENCES.get(done)
    if template is None or job not in registered:
        return ""
    return template.format(job=job, within=within_a_tick())


# Outage mode results: key -> sentence. The off result also carries two counts, which the page reads as integers.
OUTAGE_SENTENCES = {
    "on": "Outage mode is on.",
    "already-on": "Outage mode is already on.",
    "already-off": "Outage mode is already off.",
    "not-break-glass": "Only a break-glass account can switch outage mode on.",
    "refused": "Outage mode could not be changed.",
}


def outage_sentence(done: str, sessions: int = 0, passwords: int = 0) -> str:
    if done == "off":
        return f"Outage mode is off. {sessions} emergency session(s) ended, {passwords} emergency password(s) cleared."
    return OUTAGE_SENTENCES.get(done, "")


def outage_refusal_key(refusal: Exception) -> str:
    """The result key for an OutageError: the key whose sentence is the refusal's text, else "refused"."""
    return next((key for key, sentence in OUTAGE_SENTENCES.items() if sentence == str(refusal)), "refused")

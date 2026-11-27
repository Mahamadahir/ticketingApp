"""Forwards (#347): an email that opens a ticket and was forwarded to the mailbox by someone inside the organisation.

`read_forward` says whether a mail is a forward (the subject starts FW:, Fw: or Fwd:, and the body has a forwarded header block
with a "From:" line) and what is in it: the words written above the block and the original sender. `decide` applies the owner's
rules (27 Nov 2026) and returns who is the requester and who follows; app/inbound.py acts on it and records the event.

- Only an internal forwarder (an enabled member account with a mailbox, app/people_directory.py; a linked address counts).
  An outside forwarder is the requester as ever, with nobody added.
- Nothing written above the forwarded block: the original sender is the requester, even an outside one, and the forwarder
  follows. Words above it: the forwarder is the requester and the original sender follows.
- The original sender is read from the block's "From:" line (`Name <address>`, `Name [mailto:address]` or a bare address). A
  line with only a name is looked up in the directory and used when exactly one enabled person has that name. When no single
  address can be found, or it is the shared mailbox, the MSP or a never-follow address, the forwarder stays the requester and
  the event says why.

Nothing here writes; it only reads the mail and the directory. Only the real text of Graph forwards is unproved: the staging
checklist in docs/build-plan.md.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session

from app import identity, people_directory
from app.config import settings
from app.followers import InvalidEmail, normalise_email
from app.never_follow import is_listed, is_msp
from app.quoted_text import lines_of, quote_start

log = logging.getLogger("app.forwards")

EVENT_MAIL_FORWARDED = "mail_forwarded"

_FORWARD_SUBJECT = re.compile(r"^\s*(?:fw|fwd)\s*:", re.IGNORECASE)
_FROM_LINE = re.compile(r"^\s*\**From\**\s*:\s*(.*\S)\s*$", re.IGNORECASE)
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BLOCK_LINES = 8  # how far below the marker the From: line may sit


@dataclass
class ForwardParts:
    above: str  # what the forwarder wrote above the forwarded block ("" when nothing)
    from_text: str  # the block's From: line, after "From:"


def read_forward(subject: str, body: str) -> Optional[ForwardParts]:
    """The parts of a forward, or None when the mail is not one: no FW:/Fwd: subject, no forwarded block, or a block with no
    From: line (a plain "wrote:" quote is a reply's history, not a forward)."""
    if not _FORWARD_SUBJECT.match(subject or ""):
        return None
    lines = lines_of(body)
    cut = quote_start(lines)
    if cut is None:
        return None
    for line in lines[cut : cut + _BLOCK_LINES]:
        found = _FROM_LINE.match(line)
        if found:
            return ForwardParts(above="\n".join(lines[:cut]).strip(), from_text=found.group(1))
    return None


@dataclass
class ForwardDecision:
    requester: str  # who is the requester
    follower: Optional[str] = None  # who follows, if anyone
    detail: str = ""  # the event text; empty when nothing changed and nothing needs saying
    applied: bool = False  # the original sender is the requester or a follower (the rule was applied)


def original_address(session: Session, from_text: str) -> tuple[Optional[str], str]:
    """(the original sender's address or None, why not). An address in the line wins; a bare name must match exactly one
    enabled person in the directory."""
    found = _ADDRESS.search(from_text)
    if found:
        try:
            return normalise_email(found.group(0)), ""
        except InvalidEmail:
            return None, "its address is not a valid email address"
    name = from_text.strip().strip("\"'").strip()
    if not name:
        return None, "the From line is empty"
    try:
        people = people_directory.people_named(session, name)
    except people_directory.DirectoryError:
        return None, "the directory could not be asked to find them by name"
    if len(people) == 1:
        return people[0].mail, ""
    return None, ("no one in the directory has that name" if not people else "more than one person has that name")


def decide(session: Session, forwarder: str, subject: str, body: str) -> Optional[ForwardDecision]:
    """What a forward of `body` from `forwarder` means for who the requester is, or None to carry on as for any mail (not a
    forward, an outside forwarder, or the original sender is the forwarder). The forwarder's address is the normalised one."""
    parts = read_forward(subject, body)
    if parts is None:
        return None
    try:
        internal = people_directory.is_internal(session, forwarder)
    except people_directory.DirectoryError:
        log.warning("forward from %s: the directory could not be asked, so it was treated as from outside", forwarder)
        return ForwardDecision(
            requester=forwarder,
            detail="Forwarded mail: the directory could not be asked whether the forwarder is inside the organisation, so the forwarder is the requester.",
        )
    if not internal:
        return None
    address, why = original_address(session, parts.from_text)
    mailbox = settings.graph_shared_mailbox.strip().lower()
    if address is not None and address in identity.addresses_of(session, forwarder):
        return None  # they forwarded their own mail to themselves: nothing to change
    if address is not None and (address == mailbox or is_msp(session, address) or is_listed(session, address)):
        why = "it is the support mailbox, the MSP or an address on the never-follow list"
        address = None
    if address is None:
        return ForwardDecision(
            requester=forwarder,
            detail=f"Forwarded by {forwarder}. The original sender could not be read ({why}), so the forwarder is the requester.",
        )
    if parts.above:
        return ForwardDecision(
            requester=forwarder, follower=address, applied=True,
            detail=f"Forwarded by {forwarder} with a message; {forwarder} is the requester and {address} follows.",
        )
    return ForwardDecision(
        requester=address, follower=forwarder, applied=True,
        detail=f"Forwarded by {forwarder}; {address} is the requester.",
    )

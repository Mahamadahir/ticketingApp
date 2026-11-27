"""The people picker's directory (#309): who a person may choose as the other person of a mailbox change.

Two jobs. `search` answers the picker as the person types (GET /portal/people): enabled member accounts that have a mail
address, by name or address, never the person themselves or an address on the `mailbox_target_blocklist` setting. `resolve`
is the rule: the form stores only an Entra object id, and on submit the server fetches that id again and refuses anyone who is
not found, disabled, a guest, without mail, the requester or on the blocklist. The picker is a convenience; `resolve` is the
check, so a doctored id gets nowhere.

With Graph configured the directory is Microsoft Entra ID (`app.graph_client`, `User.Read.All`). With Graph off (local
development, tests) a fake directory built from the local users stands in: an active person with an address, their object id
being their Entra id if they have one, else `local-<id>`.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app import identity
from app.config import settings
from app.followers import InvalidEmail, normalise_email
from app.models import User
from app.settings_store import MAILBOX_TARGET_BLOCKLIST, get_setting

log = logging.getLogger("app.people")

MIN_QUERY_LENGTH = 2
MAX_RESULTS = 10
LOCAL_PREFIX = "local-"


class DirectoryError(ValueError):
    """The chosen person cannot be used, worded for the person filling in the form."""


@dataclass(frozen=True)
class Person:
    object_id: str
    name: str
    mail: str
    job_title: str = ""
    department: str = ""


def blocklist(session: Session) -> set[str]:
    out = set()
    if settings.graph_shared_mailbox.strip():
        out.add(settings.graph_shared_mailbox.strip().lower())  # the app's own mailbox is never somewhere to send a person's mail
    for part in re.split(r"[,;\s]+", get_setting(session, MAILBOX_TARGET_BLOCKLIST)):
        try:
            out.add(normalise_email(part))
        except InvalidEmail:
            continue
    return out


def _graph_on() -> bool:
    return settings.graph_configured


def _raw_from_user(user: User) -> dict:
    return {
        "id": user.entra_object_id or f"{LOCAL_PREFIX}{user.id}", "displayName": user.display_name, "mail": user.email,
        "jobTitle": "", "department": "", "accountEnabled": bool(user.is_active), "userType": "Member",
    }


def _refusal(raw: dict) -> Optional[str]:
    """Why a directory entry cannot be chosen, or None. The same test for what the picker lists and what a submit accepts."""
    if raw.get("accountEnabled") is not True:
        return "That person's account is switched off, so mail cannot go to them. Choose someone else."
    if str(raw.get("userType") or "").strip().lower() == "guest":  # a blank or missing userType is a member (older accounts have none)
        return "Mail can only go to people inside the organisation. Choose someone else."
    if not (raw.get("mail") or "").strip():
        return "That person has no mailbox to send mail to. Choose someone else."
    return None


def _person(raw: dict) -> Person:
    return Person(
        object_id=str(raw["id"]).lower(), name=(raw.get("displayName") or raw.get("mail") or "").strip(),
        mail=raw["mail"].strip().lower(), job_title=(raw.get("jobTitle") or "").strip(), department=(raw.get("department") or "").strip(),
    )


def _is_self(session: Session, user: User, person: Person) -> bool:
    return person.mail in identity.requester_emails(user) or (
        user.entra_object_id is not None and person.object_id == user.entra_object_id.lower()
    )


def _local_search(session: Session, text: str) -> list[dict]:
    like = f"%{text.lower()}%"
    users = session.exec(
        select(User)
        .where(User.is_active, User.is_break_glass.is_(False), or_(func.lower(User.display_name).like(like), func.lower(User.email).like(like)))
        .order_by(User.display_name)
        .limit(50)
    ).all()
    return [_raw_from_user(u) for u in users]


def search(
    session: Session, user: User, text: str, *, exclude_self: bool = True, use_blocklist: bool = True,
) -> list[Person]:
    """At most MAX_RESULTS people who could be chosen, matching `text` (at least MIN_QUERY_LENGTH characters, else none). Raises
    DirectoryError when the directory could not be asked. The mailbox picker keeps the two exclusions (the person themselves,
    the `mailbox_target_blocklist`); the agent's End user picker (#279) turns both off, since neither is about a requester."""
    text = (text or "").strip()
    if len(text) < MIN_QUERY_LENGTH:
        return []
    if _graph_on():
        from app.graph_client import graph_client  # resolved now, so tests can swap the methods

        try:
            rows = graph_client.search_users(text)
        except Exception as error:  # the network, a throttle, a missing permission: the picker says so, the log has the detail
            log.warning("people search failed: %s", error)
            raise DirectoryError("The directory could not be searched just now. Try again in a minute.")
    else:
        rows = _local_search(session, text)
    blocked = blocklist(session) if use_blocklist else set()
    found: list[Person] = []
    for raw in rows:
        if not raw.get("id") or _refusal(raw) is not None:
            continue
        person = _person(raw)
        if person.mail in blocked or (exclude_self and _is_self(session, user, person)):
            continue
        found.append(person)
        if len(found) == MAX_RESULTS:
            break
    return found


def resolve(session: Session, user: User, object_id: str, *, exclude_self: bool = True, use_blocklist: bool = True) -> Person:
    """The person with this object id, fetched again now, or DirectoryError saying why they cannot be chosen. The same two
    exclusions as `search`, switched off together with it."""
    object_id = (object_id or "").strip().lower()
    if not object_id:
        raise DirectoryError("Choose who to forward your mail to")
    if _graph_on():
        from app.graph_client import graph_client

        try:
            raw = graph_client.get_user(object_id)
        except Exception as error:
            log.warning("people lookup failed: %s", error)
            raise DirectoryError("We could not check that person just now. Try again in a minute.")
    elif object_id.startswith(LOCAL_PREFIX) and object_id[len(LOCAL_PREFIX):].isdigit():
        found = session.get(User, int(object_id[len(LOCAL_PREFIX):]))
        raw = _raw_from_user(found) if found is not None and not found.is_break_glass else None
    else:
        found = session.exec(select(User).where(func.lower(User.entra_object_id) == object_id)).first()
        raw = _raw_from_user(found) if found is not None and not found.is_break_glass else None
    if raw is None or str(raw.get("id") or "").lower() != object_id:  # Graph also reads a sign-in name here: only an id will do
        raise DirectoryError("That person could not be found. Search again and choose them from the list.")
    reason = _refusal(raw)
    if reason is not None:
        raise DirectoryError(reason)
    person = _person(raw)
    if exclude_self and _is_self(session, user, person):
        raise DirectoryError("You cannot send your mail to yourself. Choose someone else.")
    if use_blocklist and person.mail in blocklist(session):
        raise DirectoryError("Mail cannot be sent to that address. Choose someone else.")
    return person

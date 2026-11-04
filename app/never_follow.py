"""The never-follow list: addresses that must never become followers (4B-2).

An admin keeps the shared mailbox and its aliases and any distribution lists here. The list
blocks a Cc on inbound mail and an agent or requester adding the address by hand (both go
through app/followers.add_follower, which is the only place that enforces it). Addresses are
stored lower case and matched exactly, never by domain. An agent's own account address is not
checked, so escalating is never blocked. A listed address that emails IT also gets no
acknowledgement email, and since #122 mail sent from a listed address is ignored whole (no ticket, nothing
added to a ticket) and logged in the audit log (app/inbound.py).
"""

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import NeverFollowAddress
from app.settings_store import MSP_ADDRESS, get_setting

MAX_REASON_LENGTH = 200


class NeverFollowError(ValueError):
    pass


def all_listed(session: Session) -> list[NeverFollowAddress]:
    return list(session.exec(select(NeverFollowAddress).order_by(NeverFollowAddress.email)).all())


def is_msp(session: Session, email: str) -> bool:
    """The MSP's address (an admin setting). The MSP is a party to the ticket, never a follower or a requester
    (app/msp.py); followers.add_follower refuses it, and a copy list never includes it."""
    return _is_address(get_setting(session, MSP_ADDRESS), email)


def _is_address(address: str, email: str) -> bool:
    address = address.strip().lower()
    return bool(address) and email.strip().lower() == address


def is_listed(session: Session, email: str) -> bool:
    return session.exec(
        select(NeverFollowAddress.id).where(NeverFollowAddress.email == email.strip().lower())
    ).first() is not None


def listed_entry(session: Session, email: str) -> Optional[NeverFollowAddress]:
    return session.exec(select(NeverFollowAddress).where(NeverFollowAddress.email == email.strip().lower())).first()


def listed_among(session: Session, emails: list[str]) -> set[str]:
    """Which of these (lower-case) addresses are on the list, in one query."""
    if not emails:
        return set()
    listed = set(session.exec(select(NeverFollowAddress.email).where(NeverFollowAddress.email.in_(emails))).all())
    address = get_setting(session, MSP_ADDRESS)  # read once, not once per address
    return listed | {email for email in emails if _is_address(address, email)}


def add_listed(session: Session, email: str, reason: str, added_by: str) -> NeverFollowAddress:
    """Adds a normalised address. Does not commit. Raises NeverFollowError for a repeat or a long reason."""
    email = email.strip().lower()
    reason = (reason or "").strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise NeverFollowError(f"The reason can be at most {MAX_REASON_LENGTH} characters")
    if is_listed(session, email):
        raise NeverFollowError(f"{email} is already on the list")
    row = NeverFollowAddress(email=email, reason=reason, added_by=added_by)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        raise NeverFollowError(f"{email} is already on the list")
    return row


def remove_listed(session: Session, row_id: int) -> Optional[str]:
    """Removes an entry and returns its address, or None if it was not there. Does not commit.
    Followers already on tickets are left alone."""
    row = session.get(NeverFollowAddress, row_id)
    if row is None:
        return None
    email = row.email
    session.delete(row)
    return email

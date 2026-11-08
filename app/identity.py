"""Which tickets a signed-in person counts as their own in the portal.

Every portal comparison between a ticket and the signed-in person goes through
`requester_emails`, and nothing in the portal compares `user.email` to a stored
address directly. Addresses are matched lower-cased. A person is known by their own address
plus every address linked to them (app/linked_identities.py: an admin link, a self-service link, or
the old address after an Entra email change); `requester_emails` is the one place that list is read,
and `person_for_address` / `addresses_of` go the other way, from an address to the person. Agent
access is not decided here (app/visibility.py).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session
from sqlmodel import Session, select

from app.audit import log_action
from app.config import settings
from app.models import LinkedIdentity, Ticket, TicketFollower, User, UserRole

LINK_ACTIVE = "active"  # LinkedIdentity.status (the other values live in app/linked_identities.py)
LINK_PENDING = "pending"
LINK_UNLINKED = "unlinked"
METHOD_EMAIL_CHANGED = "email_changed"


def requester_emails(user: User, session: Optional[Session] = None) -> set[str]:
    """Every address (lower case) under which this person is the requester or a follower: their own and every
    active linked address. Reads the links through `session`, or the session the user belongs to."""
    emails = {user.email.strip().lower()}
    session = session or object_session(user)
    if session is not None and user.id is not None:
        emails.update(
            session.exec(
                select(LinkedIdentity.email).where(LinkedIdentity.main_user_id == user.id, LinkedIdentity.status == LINK_ACTIVE)
            ).all()
        )
    return emails


def person_for_address(session: Session, address: str) -> Optional[User]:
    """The person an email address belongs to: the main person if it is a linked address, else the account with
    that address. None for someone with no account. Use this wherever an address from a ticket, a follower or an
    email is turned into a person (a bell, a preference, a name), so a linked address is the main person."""
    address = (address or "").strip().lower()
    if not address:
        return None
    main_id = session.exec(
        select(LinkedIdentity.main_user_id).where(LinkedIdentity.email == address, LinkedIdentity.status == LINK_ACTIVE)
    ).first()
    if main_id is not None:
        return session.get(User, main_id)
    return session.exec(select(User).where(func.lower(User.email) == address)).first()


def people_for_addresses(session: Session, addresses) -> list[User]:
    """`person_for_address` for several addresses at once: each person once."""
    addresses = {a.strip().lower() for a in addresses if a and a.strip()}
    if not addresses:
        return []
    main_ids = set(
        session.exec(
            select(LinkedIdentity.main_user_id).where(LinkedIdentity.email.in_(addresses), LinkedIdentity.status == LINK_ACTIVE)
        ).all()
    )
    condition = func.lower(User.email).in_(addresses)
    if main_ids:
        condition = condition | User.id.in_(main_ids)
    people = session.exec(select(User).where(condition).order_by(User.id)).all()
    # A merged-away account's own address is a linked address of the main person, who is already in `main_ids`.
    merged = set(
        session.exec(
            select(LinkedIdentity.secondary_user_id).where(
                LinkedIdentity.status == LINK_ACTIVE, LinkedIdentity.secondary_user_id.is_not(None)
            )
        ).all()
    )
    return [p for p in people if p.id not in merged]


def addresses_of(session: Session, address: str) -> set[str]:
    """Every address of the person this address belongs to (just itself for someone unknown)."""
    address = (address or "").strip().lower()
    person = person_for_address(session, address)
    return requester_emails(person, session) | {address} if person is not None else {address}


def merged_into(session: Session, user: User) -> Optional[User]:
    """The main person this account was merged into by a link, or None. Such an account is kept (nothing is
    deleted) but is no longer anyone to sign in as."""
    main_id = session.exec(
        select(LinkedIdentity.main_user_id).where(
            LinkedIdentity.secondary_user_id == user.id, LinkedIdentity.status == LINK_ACTIVE
        )
    ).first()
    return session.get(User, main_id) if main_id is not None else None


def is_requester(ticket: Ticket, user: User) -> bool:
    return ticket.requester_email.strip().lower() in requester_emails(user)


def is_follower_of(session: Session, ticket: Ticket, user: User, *, include_internal: bool = True) -> bool:
    conditions = [TicketFollower.ticket_id == ticket.id, func.lower(TicketFollower.email).in_(requester_emails(user, session))]
    if not include_internal:
        conditions.append(TicketFollower.is_internal.is_(False))
    return session.exec(select(TicketFollower.id).where(*conditions)).first() is not None


def my_ticket_condition(user: User):
    """SQL: the ticket is one the person raised or follows (the portal's "My requests", before ordering)."""
    emails = requester_emails(user)
    own = func.lower(func.trim(Ticket.requester_email)).in_(emails)
    followed = Ticket.id.in_(select(TicketFollower.ticket_id).where(func.lower(TicketFollower.email).in_(emails)))
    return own | followed


def my_tickets_query(user: User):
    """Tickets the person raised, then tickets they follow, the ones they raised first.
    What they may read of each is decided on the ticket page, through app/messages.py."""
    emails = requester_emails(user)
    own = func.lower(func.trim(Ticket.requester_email)).in_(emails)
    followed = Ticket.id.in_(select(TicketFollower.ticket_id).where(func.lower(TicketFollower.email).in_(emails)))
    return (
        select(Ticket)
        .where(own | followed)
        .order_by(case((own, 0), else_=1), Ticket.updated_at.desc())
    )


class SsoRefused(Exception):
    """An Entra sign-in that must not become a session. `reason` is for the activity log; `message` is
    the plain text the person sees."""

    def __init__(self, reason: str, message: str):
        super().__init__(reason)
        self.reason = reason
        self.message = message


def normal_oid(oid: Optional[str]) -> Optional[str]:
    return oid.strip().lower() if oid and oid.strip() else None


def resolve_sso_user(session: Session, oid: Optional[str], email: str, display_name: str) -> User:
    """The person an Entra sign-in is, matched on the token's object id (`oid`), which never changes,
    and not on the email, which can (a rename, a re-hire, a recycled address). Does not commit.

    0. A linked identity (app/linked_identities.py) with this `oid`, or with this email: the sign-in is the
       main person's, whatever their own address is; nothing about the main account is refreshed from these
       claims. A linked address that has met a different `oid` before is refused (a recycled address).
    1. A user with this `oid`: that is the person (the main person, if that account was merged into one). Their
       display name and email are refreshed from the claims; a changed email is audited with the old address,
       which stays theirs as a linked address (their tickets under it remain in "My requests").
    2. No user has the `oid`: fall back to the email. A user with that email and no `oid` yet is the
       same person; the `oid` is stored on them (audited), so from now on step 1 finds them.
    3. A user with that email who is already bound to a different `oid` is refused and audited: the
       address belongs to someone else's directory account now (a recreated account, a recycled
       address). A super admin settles it by clearing the stored `oid` on Admin > Users.
    4. Nobody matches: a new account, with the `oid` stored.

    A token with no `oid` is refused: matching on it is the whole point."""
    oid = normal_oid(oid)
    email = email.strip().lower()
    if oid is None:
        raise SsoRefused("no_oid_claim", "Sign-in did not return an object id. Ask an administrator.")

    linked = _linked_sign_in(session, oid, email)
    if linked is not None:
        return linked

    user = session.exec(select(User).where(func.lower(User.entra_object_id) == oid)).first()
    if user is not None:
        main = merged_into(session, user)
        if main is not None:
            return main
        _refresh_from_claims(session, user, email, display_name)
        return user

    user = session.exec(select(User).where(User.email == email)).first()
    if user is not None:
        if user.entra_object_id is not None:
            log_action(
                session, email, "user", user.id, "sso_oid_mismatch",
                "refused: the account with this email is bound to a different directory identity",
            )
            raise SsoRefused(
                "oid_mismatch",
                "This email address belongs to a different account. Ask an administrator to sort it out.",
            )
        main = merged_into(session, user)
        if main is not None:
            return main
        user.entra_object_id = oid
        user.display_name = display_name or user.display_name
        session.add(user)
        _flush_or_refuse(session, "oid_in_use")
        log_action(session, email, "user", user.id, "oid_bound", f"Entra object id {oid} stored on first matching sign-in")
        return user

    role = UserRole.SUPER_ADMIN if email in settings.super_admin_email_list else UserRole.END_USER
    user = User(email=email, display_name=display_name or email, role=role, entra_object_id=oid)
    session.add(user)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Two first sign-ins at once: the other one created the person; use that one.
        existing = session.exec(select(User).where(func.lower(User.entra_object_id) == oid)).first()
        if existing is None:
            raise
        return existing
    return user


def _linked_sign_in(session: Session, oid: str, email: str) -> Optional[User]:
    """The main person when this sign-in is a linked identity: by `oid` first, then by the linked email. An address
    linked without a directory id (an emailed code, an email change) takes the `oid` of its first sign-in; a later
    sign-in with that address and another `oid` is refused, since the address now belongs to someone else."""
    # An old-address alias carries the person's own object id (to catch a recycled address); that id is matched as the
    # person themselves, not through the alias, so their email is still refreshed from the claims.
    link = session.exec(
        select(LinkedIdentity).where(
            LinkedIdentity.status == LINK_ACTIVE, LinkedIdentity.method != METHOD_EMAIL_CHANGED,
            func.lower(LinkedIdentity.entra_object_id) == oid,
        )
    ).first()
    if link is None:
        link = session.exec(
            select(LinkedIdentity).where(LinkedIdentity.status == LINK_ACTIVE, LinkedIdentity.email == email)
        ).first()
        if link is None:
            return None
        main = session.get(User, link.main_user_id)
        if main is not None and main.entra_object_id and main.entra_object_id.lower() == oid:
            return None  # the person's own directory account signing in under their old address: step 1 refreshes it
        if link.entra_object_id and link.entra_object_id.lower() != oid:
            log_action(
                session, email, "user", link.main_user_id, "sso_linked_oid_mismatch",
                "refused: this address is linked to another person's account, but the directory identity is different",
            )
            raise SsoRefused(
                "linked_oid_mismatch",
                "This email address is linked to another account. Ask an administrator to sort it out.",
            )
        if not link.entra_object_id:
            if session.exec(select(User.id).where(func.lower(User.entra_object_id) == oid)).first() is not None:
                return None  # a directory account with an account of its own here is not this person's alias
            link.entra_object_id = oid
            session.add(link)
            _flush_or_refuse(session, "oid_in_use")
            log_action(session, email, "user", link.main_user_id, "linked_oid_bound", f"Entra object id {oid} stored on first sign-in with linked address {email}")
    return session.get(User, link.main_user_id)


def keep_as_alias(session: Session, user: User, old_email: str, actor_email: str) -> None:
    """The person's old address becomes a linked address of theirs, so their tickets under it stay in "My requests"
    and mail from it is still them (6E). Does nothing if the address is already someone's link. Does not commit."""
    old_email = old_email.strip().lower()
    if not old_email or old_email == user.email.strip().lower():
        return
    taken = session.exec(
        select(LinkedIdentity.id).where(LinkedIdentity.email == old_email, LinkedIdentity.status.in_([LINK_ACTIVE, LINK_PENDING]))
    ).first()
    if taken is not None:
        return
    session.add(
        LinkedIdentity(
            main_user_id=user.id, email=old_email, entra_object_id=user.entra_object_id, method=METHOD_EMAIL_CHANGED,
            status=LINK_ACTIVE, activated_at=datetime.now(timezone.utc),
        )
    )
    log_action(session, actor_email, "user", user.id, "alias_added", f"{old_email} (their old address after an email change)")


def _refresh_from_claims(session: Session, user: User, email: str, display_name: str) -> None:
    if email and user.email != email:
        taken = session.exec(select(User.id).where(User.email == email, User.id != user.id)).first()
        other = person_for_address(session, email)
        if taken is not None or (other is not None and other.id != user.id):
            log_action(
                session, email, "user", user.id, "sso_email_conflict",
                f"refused: the new address {email} is already another account's",
            )
            raise SsoRefused(
                "email_conflict",
                "Your email address changed, and the new address belongs to another account. Ask an administrator.",
            )
        old_email = user.email
        log_action(session, email, "user", user.id, "email_changed", f"{user.email} -> {email} (from the Entra sign-in)")
        user.email = email
        session.add(user)
        # The new address was one of their own links: it is their main address now, not an alias as well.
        for own in session.exec(
            select(LinkedIdentity).where(
                LinkedIdentity.main_user_id == user.id, LinkedIdentity.email == email, LinkedIdentity.status == LINK_ACTIVE
            )
        ).all():
            own.status, own.unlinked_at = LINK_UNLINKED, datetime.now(timezone.utc)
            session.add(own)
        session.flush()
        keep_as_alias(session, user, old_email, email)
    if display_name and user.display_name != display_name:
        user.display_name = display_name
        session.add(user)
    session.flush()


def _flush_or_refuse(session: Session, reason: str) -> None:
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise SsoRefused(reason, "This directory account is already linked to another user. Ask an administrator.")

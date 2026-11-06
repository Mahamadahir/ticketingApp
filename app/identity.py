"""Which tickets a signed-in person counts as their own in the portal.

Every portal comparison between a ticket and the signed-in person goes through
`requester_emails`, and nothing in the portal compares `user.email` to a stored
address directly. Addresses are matched lower-cased. Today it is one address; it
is a seam because one person may later be known by several (merged accounts), and
then only this function changes. Agent access is not decided here (app/visibility.py).
"""

from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.config import settings
from app.models import Ticket, TicketFollower, User, UserRole


def requester_emails(user: User) -> set[str]:
    """Every address (lower case) under which this person is the requester or a follower."""
    return {user.email.strip().lower()}


def is_requester(ticket: Ticket, user: User) -> bool:
    return ticket.requester_email.strip().lower() in requester_emails(user)


def is_follower_of(session: Session, ticket: Ticket, user: User, *, include_internal: bool = True) -> bool:
    conditions = [TicketFollower.ticket_id == ticket.id, func.lower(TicketFollower.email).in_(requester_emails(user))]
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

    1. A user with this `oid`: that is the person. Their display name and email are refreshed from the
       claims; a changed email is audited with the old address. Tickets already raised under the old
       address are not moved: `requester_emails` is the current address only, so they leave this
       person's "My requests" until linked identities (an alias list) exist.
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

    user = session.exec(select(User).where(func.lower(User.entra_object_id) == oid)).first()
    if user is not None:
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


def _refresh_from_claims(session: Session, user: User, email: str, display_name: str) -> None:
    if email and user.email != email:
        taken = session.exec(select(User.id).where(User.email == email, User.id != user.id)).first()
        if taken is not None:
            log_action(
                session, email, "user", user.id, "sso_email_conflict",
                f"refused: the new address {email} is already another account's",
            )
            raise SsoRefused(
                "email_conflict",
                "Your email address changed, and the new address belongs to another account. Ask an administrator.",
            )
        log_action(session, email, "user", user.id, "email_changed", f"{user.email} -> {email} (from the Entra sign-in)")
        user.email = email
        session.add(user)
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

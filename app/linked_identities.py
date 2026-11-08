"""Linked identities (phase 6E, #165): one person known by a second identity.

A **main** person (a user account) has linked identities, each an email address, its Entra object id once it has
signed in and, when it was a separate account, that account. Nothing is deleted: a merged-away account stays as a
row, inactive, with its audit and activity history under its own id. What the link changes:

- `identity.requester_emails(main)` includes every active linked address, so My requests, requester checks,
  followers, "you cannot approve your own request" and the portal treat the addresses as one person.
- An Entra sign-in with the second identity (by object id, or by email) is the main person (`identity.resolve_sso_user`).
- On linking two accounts the tickets assigned to the merged-away account, and the other records that name it as
  a person (`MOVED_COLUMNS`), move to the main account, with a ticket event on each reassigned ticket. Its team
  memberships, extra permissions and approval group memberships are NOT merged: the admin page shows them and the
  admin grants what the main account needs. Its sessions end and it is made inactive (so it is never offered as an
  assignee or approver, and an unanswered approval vote of its own is dropped as for any inactive approver).
- Unlinking makes the merged-away account its own again (active as it was); tickets that moved stay where they are.

Two ways in. A super admin links two accounts on Admin > Users and chooses the main one. A signed-in person links
a second identity themselves (My settings > Link another account), shown to be theirs by signing in with it or by
an emailed 6-digit code, and chooses the main account. If either side is an agent or a super admin, a
self-service link is a request that a super admin approves; with only ordinary end-user accounts it applies at once.

Every function adds to the caller's session and does not commit, except `request_code` and `check_code`, which
must commit the count of codes and of wrong tries even when they then refuse.
"""

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.audit import log_action
from app.config import settings
from app.identity import LINK_ACTIVE as ACTIVE, LINK_PENDING as PENDING, LINK_UNLINKED as UNLINKED, METHOD_EMAIL_CHANGED
from app.models import (
    ActionToken,
    Attachment,
    LinkCode,
    LinkedIdentity,
    Message,
    Notification,
    RequestSubTemplate,
    Team,
    TeamMembership,
    Ticket,
    TicketEscalator,
    TicketFeedback,
    TicketFollower,
    User,
    UserPermission,
    UserRole,
)
from app.never_follow import is_listed
from app.notifications import AGENT_ROLES, notify_link_requested
from app.sessions import revoke_user_sessions
from app.settings_store import (
    LINK_CODE_ATTEMPTS,
    LINK_CODE_MINUTES,
    LINK_CODE_PER_ADDRESS_HOUR,
    LINK_CODE_PER_USER_HOUR,
    count_setting,
)
from app.ticket_service import EVENT_ASSIGNED, record_event

log = logging.getLogger(__name__)

DECLINED = "declined"  # a request a super admin refused, or one the person took back
ADMIN, SIGN_IN, EMAIL_CODE = "admin", "sign_in", "email_code"
METHODS = (ADMIN, SIGN_IN, EMAIL_CODE, METHOD_EMAIL_CHANGED)
CODE_DIGITS = 6
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Every column that points at a user, and what linking does with it. `test_linked_identities` checks this list
# against the database metadata, so a new column that names a person fails there until it is classified here.
MOVED_COLUMNS = (
    Ticket.created_by_id,
    Ticket.resolved_by_id,
    Ticket.triaged_by_id,
    Ticket.escalated_by_id,
    Message.author_user_id,
    Attachment.uploaded_by_id,
    TicketFollower.user_id,
    TicketFeedback.resolved_by_id,
    ActionToken.resolved_by_id,
    RequestSubTemplate.assignee_id,
    Notification.user_id,
)
# Moved by hand: the assignee needs a ticket event; the escalator's key includes the user.
MOVED_BY_HAND = ("ticket.assigned_agent_id", "ticketescalator.user_id")
# Left on the merged-away account on purpose (the admin page shows them; nothing is merged automatically), or
# history that keeps the original id, or the link bookkeeping itself.
KEPT_COLUMNS = (
    "teammembership.user_id",
    "userpermission.user_id",
    "approvalgroupmember.user_id",
    "approvalvote.voter_id",  # answered votes are history; an unanswered one is dropped (approvals.drop_votes_of)
    "notificationpreference.user_id",
    "user_session.user_id",  # revoked
    "activitylog.actor_user_id",
    "linked_identity.main_user_id",
    "linked_identity.secondary_user_id",
    "linked_identity.requested_by_id",
    "linked_identity.approved_by_id",
    "linked_identity.unlinked_by_id",
    "link_code.user_id",
)


def moved_keys() -> set[str]:
    return {f"{column.class_.__tablename__}.{column.key}" for column in MOVED_COLUMNS} | set(MOVED_BY_HAND)


def classified_keys() -> set[str]:
    return moved_keys() | set(KEPT_COLUMNS)


def user_foreign_keys() -> set[str]:
    """Every `table.column` in the schema that references user.id (for the test that nothing is unclassified)."""
    found = set()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            if any(fk.column.table.name == "user" and fk.column.name == "id" for fk in column.foreign_keys):
                found.add(f"{table.name}.{column.name}")
    return found


class LinkError(Exception):
    """A link that must not happen, in words for the person who asked."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_privileged(user: Optional[User]) -> bool:
    return user is not None and user.role in AGENT_ROLES


def normal_address(address: str) -> str:
    return (address or "").strip().lower()


# ---- Checks ------------------------------------------------------------------------------------------------


def link_of(session: Session, user: User, *statuses: str) -> Optional[LinkedIdentity]:
    """The link that merged this account away (as the secondary), in the given statuses (active by default)."""
    return session.exec(
        select(LinkedIdentity).where(
            LinkedIdentity.secondary_user_id == user.id, LinkedIdentity.status.in_(statuses or (ACTIVE,))
        )
    ).first()


CANNOT_BY_CODE = (
    "That address cannot be linked with an emailed code. Try signing in with that account instead, or ask an administrator."
)  # one message for 'an agent or admin account', 'break-glass' and 'already linked', so the form cannot be used to probe


def check_address(session: Session, address: str, *, quiet: bool = False) -> None:
    """Refuses an address that can never be someone's second identity: not an address, the shared mailbox, a
    never-follow address (the app's list of mailboxes and lists), a break-glass placeholder, or one already linked
    or waiting to be."""
    if not EMAIL_RE.match(address):
        raise LinkError("That is not an email address.")
    if address == settings.graph_shared_mailbox.strip().lower():
        raise LinkError("That is the shared support mailbox, not a person.")
    if is_listed(session, address):
        raise LinkError("That address is on the never-follow list (a shared mailbox or distribution list), so it cannot be linked.")
    if address.endswith("@local.invalid"):
        raise LinkError("That is not a real address.")
    taken = session.exec(
        select(LinkedIdentity.id).where(LinkedIdentity.email == address, LinkedIdentity.status.in_([ACTIVE, PENDING]))
    ).first()
    if taken is not None:
        raise LinkError(CANNOT_BY_CODE if quiet else "That address is already linked to an account, or a request to link it is waiting.")


def check_pair(session: Session, main: User, secondary: User) -> None:
    """Whether these two accounts may be merged, main absorbing secondary."""
    if main.id == secondary.id:
        raise LinkError("That is the same account.")
    if main.is_break_glass or secondary.is_break_glass:
        raise LinkError("A break-glass account is never linked.")
    for account in (main, secondary):
        if link_of(session, account) is not None:
            raise LinkError(f"{account.email} is already linked into another account. Unlink it first.")
    if not main.is_active:
        raise LinkError(f"{main.email} is inactive, so it cannot be the main account.")


def needs_approval(*accounts: Optional[User]) -> bool:
    """A self-service link that involves an agent or a super admin waits for a super admin."""
    return any(is_privileged(account) for account in accounts)


# ---- What the confirm page shows -------------------------------------------------------------------------


@dataclass
class AccountSummary:
    user: User
    teams: list[tuple[str, str]]  # (team name, level)
    permissions: list[str]
    approval_groups: int
    open_tickets: int
    assigned_tickets: int


def summarise(session: Session, user: User) -> AccountSummary:
    from app.models import ApprovalGroupMember
    from app.ticket_service import FINISHED_STATUSES

    teams = [
        (team.name, membership.level.value)
        for membership, team in session.exec(
            select(TeamMembership, Team).join(Team, Team.id == TeamMembership.team_id).where(TeamMembership.user_id == user.id).order_by(Team.name)
        ).all()
    ]
    permissions = list(session.exec(select(UserPermission.permission).where(UserPermission.user_id == user.id).order_by(UserPermission.permission)).all())
    groups = session.exec(select(func.count()).select_from(ApprovalGroupMember).where(ApprovalGroupMember.user_id == user.id)).one()
    assigned = session.exec(select(func.count()).select_from(Ticket).where(Ticket.assigned_agent_id == user.id)).one()
    open_count = session.exec(
        select(func.count()).select_from(Ticket).where(Ticket.assigned_agent_id == user.id, Ticket.status.not_in(FINISHED_STATUSES))
    ).one()
    return AccountSummary(user, teams, permissions, groups, open_count, assigned)


def not_carried_over(main: AccountSummary, secondary: AccountSummary) -> list[str]:
    """What the merged-away account has that is not merged into the main one, in words, for the admin to re-grant."""
    lines = []
    have = {name for name, _ in main.teams}
    for name, level in secondary.teams:
        if name not in have:
            lines.append(f"Team {name} ({level}): add it to {main.user.email} if they need it")
    for permission in secondary.permissions:
        if permission not in main.permissions:
            lines.append(f"Extra permission {permission}: grant it to {main.user.email} if they need it")
    if secondary.approval_groups:
        lines.append(f"Approval group membership ({secondary.approval_groups}): add {main.user.email} to the groups if they should approve")
    return lines


# ---- Linking ---------------------------------------------------------------------------------------------


def _create(
    session: Session, *, main: User, secondary: Optional[User], email: str, oid: Optional[str], method: str,
    requested_by: Optional[User],
) -> LinkedIdentity:
    link = LinkedIdentity(
        main_user_id=main.id, secondary_user_id=secondary.id if secondary else None, email=email,
        entra_object_id=(oid or None), method=method, status=PENDING,
        requested_by_id=requested_by.id if requested_by else None,
    )
    session.add(link)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise LinkError("That identity is already linked, or a request for it is waiting.")
    return link


def link_accounts(session: Session, *, main: User, secondary: User, admin: User) -> LinkedIdentity:
    """A super admin links two accounts, main absorbing secondary. Applies at once. Does not commit."""
    check_pair(session, main, secondary)
    link = _create(
        session, main=main, secondary=secondary, email=secondary.email.strip().lower(),
        oid=secondary.entra_object_id.lower() if secondary.entra_object_id else None, method=ADMIN, requested_by=admin,
    )
    apply_link(session, link, admin)
    return link


def apply_link(session: Session, link: LinkedIdentity, approver: Optional[User]) -> None:
    """Makes a pending link real: activates it and, when it merges an account, moves the records over. `approver`
    is the super admin who linked or approved it, or None for a link that needed nobody. Re-checks everything,
    since a request can wait days. Does not commit."""
    if link.status != PENDING:
        raise LinkError("That request is no longer waiting.")
    main = session.get(User, link.main_user_id)
    secondary = session.get(User, link.secondary_user_id) if link.secondary_user_id else None
    actor = approver.email if approver is not None else (session.get(User, link.requested_by_id).email if link.requested_by_id else "system")
    if main is None or main.is_break_glass or not main.is_active or link_of(session, main) is not None:
        raise LinkError("The main account can no longer take a link (it is inactive or itself linked).")
    if secondary is not None:
        check_pair(session, main, secondary)
    link.status, link.activated_at = ACTIVE, _now()
    link.approved_by_id = approver.id if approver is not None else None
    session.add(link)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise LinkError("That identity is already linked elsewhere.")
    if secondary is None:
        log_action(session, actor, "user", main.id, "identity_linked", f"{link.email} linked to {main.email} as an address ({link.method})")
        return
    link.email = secondary.email.strip().lower()
    link.entra_object_id = (secondary.entra_object_id or link.entra_object_id or "").lower() or None
    link.secondary_was_active = secondary.is_active
    summary = _merge(session, main, secondary, actor)
    link.moved_summary = summary
    session.add(link)
    log_action(
        session, actor, "user", main.id, "identity_linked",
        f"{secondary.email} merged into {main.email} ({link.method}); {summary}",
    )
    log_action(session, actor, "user", secondary.id, "identity_merged_away", f"now linked to {main.email}; account kept, inactive")


def _merge(session: Session, main: User, secondary: User, actor: str) -> str:
    """Moves the secondary's records to the main account and retires the secondary. Returns the counts, in words."""
    from app.approvals import drop_votes_of  # here: approvals imports a lot, and nothing imports this module back

    counts: dict[str, int] = {}
    tickets = session.exec(select(Ticket).where(Ticket.assigned_agent_id == secondary.id).order_by(Ticket.id)).all()
    for ticket in tickets:
        ticket.assigned_agent_id = main.id
        session.add(ticket)
        record_event(
            session, ticket, EVENT_ASSIGNED, actor, from_value=secondary.email, to_value=main.email,
            detail="Accounts linked: the assignee moved to the main account",
        )
    counts["assigned tickets"] = len(tickets)

    for column in MOVED_COLUMNS:
        result = session.exec(update(column.class_).where(column == secondary.id).values({column.key: main.id}))
        if result.rowcount:
            counts[f"{column.class_.__tablename__}.{column.key}"] = result.rowcount
    # The escalator row is keyed by (ticket, user): a ticket both of them escalated keeps the main's row.
    main_tickets = select(TicketEscalator.ticket_id).where(TicketEscalator.user_id == main.id)
    session.exec(delete(TicketEscalator).where(TicketEscalator.user_id == secondary.id, TicketEscalator.ticket_id.in_(main_tickets)))
    result = session.exec(update(TicketEscalator).where(TicketEscalator.user_id == secondary.id).values(user_id=main.id))
    if result.rowcount:
        counts["escalated tickets"] = result.rowcount
    # Addresses linked to the merged-away account (an old address after an email change) are the main person's now.
    result = session.exec(
        update(LinkedIdentity)
        .where(LinkedIdentity.main_user_id == secondary.id, LinkedIdentity.status == ACTIVE)
        .values(main_user_id=main.id)
    )
    if result.rowcount:
        counts["linked addresses"] = result.rowcount
    # Requests that named the merged-away account can no longer be carried out.
    for stale in session.exec(
        select(LinkedIdentity).where(
            LinkedIdentity.status == PENDING,
            (LinkedIdentity.main_user_id == secondary.id) | (LinkedIdentity.secondary_user_id.in_([main.id, secondary.id])),
        )
    ).all():
        stale.status = DECLINED
        session.add(stale)
        log_action(session, actor, "user", stale.main_user_id, "identity_link_declined", f"{stale.email}: an account in it was linked elsewhere")
    session.flush()

    # The merged-away account can no longer sign in, be assigned or be asked to approve.
    secondary.is_active = False
    session.add(secondary)
    ended = revoke_user_sessions(session, secondary.id)
    if ended:
        log_action(session, actor, "user", secondary.id, "sessions_revoked", f"merged into {main.email}; {ended} ended")
    dropped = drop_votes_of(session, secondary, actor)
    if dropped:
        counts["unanswered approval votes dropped"] = dropped
    return ", ".join(f"{n} {what}" for what, n in counts.items()) or "no records to move"


def link_address(session: Session, *, main: User, address: str, oid: Optional[str], method: str, requester: User) -> LinkedIdentity:
    """A pending link of a plain address (no account of its own) to `main`; see `settle`. Does not commit."""
    if main.is_break_glass or link_of(session, main) is not None or not main.is_active:
        raise LinkError("This account cannot take a linked address.")
    return _create(session, main=main, secondary=None, email=address, oid=oid, method=method, requested_by=requester)


@dataclass
class Outcome:
    link: LinkedIdentity
    applied: bool  # true: in force now; false: waits for a super admin
    signed_out: bool = False  # the requester's own account was merged away, so their session is over


def settle(session: Session, link: LinkedIdentity, requester: User, secondary: Optional[User], main: User) -> Outcome:
    """A self-service link: applies now when only ordinary end-user accounts are involved, else asks a super admin."""
    if needs_approval(main, secondary, requester):
        log_action(
            session, requester.email, "user", main.id, "identity_link_requested",
            f"{link.email} to be linked to {main.email} ({link.method}); waits for a super admin",
        )
        for admin in session.exec(
            select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True), User.is_break_glass.is_(False)).order_by(User.id)
        ).all():
            notify_link_requested(
                session, admin, requester,
                f"{requester.email} asks to link {link.email} to {main.email}. Decide on Admin > Users.",
            )
        return Outcome(link, applied=False)
    apply_link(session, link, None)
    return Outcome(link, applied=True, signed_out=secondary is not None and secondary.id == requester.id)


def find_account(session: Session, oid: Optional[str], address: str) -> Optional[User]:
    """The account an identity already has, by directory id first, then by address."""
    if oid:
        user = session.exec(select(User).where(func.lower(User.entra_object_id) == oid.lower())).first()
        if user is not None:
            return user
    return session.exec(select(User).where(func.lower(User.email) == address)).first()


def self_service_link(
    session: Session, me: User, *, address: str, oid: Optional[str], method: str, other_is_main: bool
) -> Outcome:
    """The signed-in person has shown that `address` (and `oid`, when they signed in with it) is theirs. If it has an
    account of its own the two are merged, `me` or the other as main as chosen; if not it becomes a linked address of
    `me`. Raises LinkError for anything refused. Does not commit."""
    if method not in (SIGN_IN, EMAIL_CODE):
        raise LinkError("Unknown way of proving the address.")
    address = normal_address(address)
    oid = oid.strip().lower() if oid and oid.strip() else None
    if me.is_break_glass:
        raise LinkError("A break-glass account is never linked.")
    if address in requester_addresses(session, me):
        raise LinkError("That is already one of your addresses.")
    check_address(session, address)
    if oid and session.exec(select(LinkedIdentity.id).where(LinkedIdentity.status == ACTIVE, func.lower(LinkedIdentity.entra_object_id) == oid)).first():
        raise LinkError("That Microsoft account is already linked.")
    other = find_account(session, oid, address)
    if other is not None and other.id == me.id:
        raise LinkError("That is already your own account.")
    if other is not None:
        if link_of(session, other) is not None:
            raise LinkError("That account is already linked into another account.")
        if oid and other.entra_object_id and other.entra_object_id.lower() != oid:
            raise LinkError("The account with that address belongs to a different Microsoft account. Ask an administrator.")
        address = other.email.strip().lower()
        if address in requester_addresses(session, me):
            raise LinkError("That is already one of your addresses.")
    if method == EMAIL_CODE and (is_privileged(other) or (other is not None and other.is_break_glass)):
        raise LinkError(CANNOT_BY_CODE)
    if other_is_main and other is None:
        raise LinkError("That address has no account of its own, so it cannot be the main account.")
    if other is not None:
        main, secondary = (other, me) if other_is_main else (me, other)
        check_pair(session, main, secondary)
        link = _create(session, main=main, secondary=secondary, email=secondary.email.strip().lower(), oid=secondary.entra_object_id or oid, method=method, requested_by=me)
    else:
        main, secondary = me, None
        link = link_address(session, main=me, address=address, oid=oid, method=method, requester=me)
    return settle(session, link, me, secondary, main)


def requester_addresses(session: Session, user: User) -> set[str]:
    from app.identity import requester_emails

    return requester_emails(user, session)


def approve(session: Session, link: LinkedIdentity, admin: User) -> None:
    """A super admin approves a waiting request. Does not commit."""
    apply_link(session, link, admin)


def decline(session: Session, link: LinkedIdentity, by: User) -> None:
    """A super admin refuses a request, or the person takes it back. Does not commit."""
    if link.status != PENDING:
        raise LinkError("That request is no longer waiting.")
    link.status, link.unlinked_at, link.unlinked_by_id = DECLINED, _now(), by.id
    session.add(link)
    log_action(session, by.email, "user", link.main_user_id, "identity_link_declined", f"{link.email}: declined or withdrawn")


def may_unlink(session: Session, actor: User, link: LinkedIdentity) -> bool:
    """A super admin may unlink anything. The main person (or whoever asked) may unlink an address or a link of
    ordinary accounts; giving an agent or administrator account back its own sign-in is a super admin's decision."""
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    if actor.id not in (link.main_user_id, link.requested_by_id):
        return False
    secondary = session.get(User, link.secondary_user_id) if link.secondary_user_id else None
    return not is_privileged(secondary) and not is_privileged(session.get(User, link.main_user_id))


def unlink(session: Session, link: LinkedIdentity, actor: User) -> None:
    """Ends an active link: the address is its own again; a merged-away account is active as it was (tickets that
    moved stay where they are). Does not commit."""
    from app.approvals import start_waiting_rounds

    if link.status != ACTIVE:
        raise LinkError("That link is not in force.")
    if not may_unlink(session, actor, link):
        raise LinkError("Only a super admin can unlink this one.")
    link.status, link.unlinked_at, link.unlinked_by_id = UNLINKED, _now(), actor.id
    session.add(link)
    secondary = session.get(User, link.secondary_user_id) if link.secondary_user_id else None
    detail = f"{link.email} unlinked from {session.get(User, link.main_user_id).email}"
    if secondary is not None:
        secondary.is_active = link.secondary_was_active
        session.add(secondary)
        detail += "; the account is its own again, tickets that moved stay where they are"
    log_action(session, actor.email, "user", link.main_user_id, "identity_unlinked", detail)
    if secondary is not None:
        log_action(session, actor.email, "user", secondary.id, "identity_unlinked", f"no longer merged into the main account")
        if secondary.is_active:
            start_waiting_rounds(session, actor.email)  # a request waiting for approvers may have one again


# ---- The emailed code -----------------------------------------------------------------------------------------


def _hash(code_id: int, address: str, code: str) -> str:
    return hmac.new(settings.session_secret_key.encode(), f"{code_id}:{address}:{code}".encode(), hashlib.sha256).hexdigest()


def _wrong_tries(session: Session, user_id: int, address: str, since: datetime) -> int:
    """Wrong tries across every code sent to this address for this person lately: asking for a new code does not
    give a new budget of guesses."""
    return session.exec(
        select(func.coalesce(func.sum(LinkCode.attempts), 0)).where(
            LinkCode.user_id == user_id, LinkCode.address == address, LinkCode.created_at > since
        )
    ).one()


def request_code(
    session: Session, me: User, address: str, other_is_main: bool, *,
    send: Optional[Callable] = None, now: Optional[datetime] = None,
) -> LinkCode:
    """Emails a new 6-digit code to `address`. Refused for addresses that can never be linked this way, and past the
    per-person and per-address hourly limits (counted from the stored codes, so they hold across replicas). Commits
    the code before sending, so the limits count even when the send fails. Raises LinkError."""
    from app.mail import send_link_code
    from app.teams import Unsendable

    now = now or _now()
    address = normal_address(address)
    if me.is_break_glass:
        raise LinkError("A break-glass account is never linked.")
    if address in requester_addresses(session, me):
        raise LinkError("That is already one of your addresses.")
    check_address(session, address, quiet=True)
    account = session.exec(select(User).where(func.lower(User.email) == address)).first()
    if account is not None and (is_privileged(account) or account.is_break_glass):
        # Said before any code is sent, and in the same words as an address already linked.
        raise LinkError(CANNOT_BY_CODE)
    if other_is_main and account is None:
        raise LinkError("That address has no account of its own, so it cannot be the main account.")
    hour_ago = now - timedelta(hours=1)
    by_person = session.exec(select(func.count()).select_from(LinkCode).where(LinkCode.user_id == me.id, LinkCode.created_at > hour_ago)).one()
    by_address = session.exec(select(func.count()).select_from(LinkCode).where(LinkCode.address == address, LinkCode.created_at > hour_ago)).one()
    if by_person >= count_setting(session, LINK_CODE_PER_USER_HOUR) or by_address >= count_setting(session, LINK_CODE_PER_ADDRESS_HOUR):
        raise LinkError("Too many codes have been asked for lately. Try again in an hour.")
    if _wrong_tries(session, me.id, address, hour_ago) >= count_setting(session, LINK_CODE_ATTEMPTS):
        raise LinkError("Too many wrong tries for that address lately. Try again in an hour.")
    minutes = count_setting(session, LINK_CODE_MINUTES)
    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    row = LinkCode(user_id=me.id, address=address, code_hash="", other_is_main=other_is_main, created_at=now, expires_at=now + timedelta(minutes=minutes))
    session.add(row)
    session.flush()
    row.code_hash = _hash(row.id, address, code)
    session.add(row)
    log_action(session, me.email, "user", me.id, "identity_link_code_sent", f"to {address}")  # never the code
    session.commit()
    try:
        send_link_code(session, address, code, me.display_name or me.email, minutes, send)
    except Exception as error:
        log.warning("Link code email to %s failed: %s", address, type(error).__name__)
        row = session.get(LinkCode, row.id)
        row.used_at = now  # that code was never delivered, so it is not a code anyone can use
        session.add(row)
        session.commit()
        if isinstance(error, Unsendable):
            raise LinkError(f"The code could not be sent: {error}.")
        raise LinkError("The code could not be sent. Try again later.")
    return row


def check_code(session: Session, me: User, address: str, code: str, *, now: Optional[datetime] = None) -> LinkCode:
    """Checks a typed code against the newest unused one for this person and address. A wrong code counts as a try
    (committed here, because the caller rolls back on the refusal); after the allowed tries, or past the expiry, the
    code is dead and a new one must be asked for. On success the code is marked used (the caller commits)."""
    now = now or _now()
    address = normal_address(address)
    code = re.sub(r"\s+", "", code or "")
    row = session.exec(
        select(LinkCode).where(LinkCode.user_id == me.id, LinkCode.address == address, LinkCode.used_at.is_(None))
        .order_by(LinkCode.id.desc()).limit(1).with_for_update()
    ).first()
    if row is None:
        raise LinkError("There is no code waiting for that address. Ask for a new one.")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise LinkError("That code has expired. Ask for a new one.")
    if _wrong_tries(session, me.id, address, now - timedelta(hours=1)) >= count_setting(session, LINK_CODE_ATTEMPTS):
        raise LinkError("Too many wrong tries for that address. Try again in an hour.")
    row.attempts += 1
    if not hmac.compare_digest(row.code_hash, _hash(row.id, address, code)):
        session.add(row)
        left = count_setting(session, LINK_CODE_ATTEMPTS) - _wrong_tries(session, me.id, address, now - timedelta(hours=1))
        log_action(session, me.email, "user", me.id, "identity_link_code_wrong", f"for {address}")
        session.commit()
        raise LinkError("That code is not right." + (f" You have {left} more {'try' if left == 1 else 'tries'}." if left > 0 else " Try again in an hour."))
    row.used_at = now
    session.add(row)
    return row


def link_by_code(session: Session, me: User, address: str, code: str, *, now: Optional[datetime] = None) -> Outcome:
    """The signed-in person typed the emailed code: the address is theirs. Raises LinkError. Does not commit (but see
    `check_code`)."""
    row = check_code(session, me, address, code, now=now)
    return self_service_link(session, me, address=row.address, oid=None, method=EMAIL_CODE, other_is_main=row.other_is_main)


# ---- What the pages list -----------------------------------------------------------------------------------------


@dataclass
class LinkRow:
    link: LinkedIdentity
    main: User
    secondary: Optional[User]
    requested_by: Optional[User]


def rows_for(
    session: Session, *statuses: str, user: Optional[User] = None, aliases: bool = True, limit: Optional[int] = None
) -> list[LinkRow]:
    """Links in these statuses, newest first; with `user`, only those the person is the main account of or asked for.
    `aliases=False` leaves out the old addresses kept after an email change (the system makes one for every change, so the
    admin page lists them only when asked). The people are read in one query."""
    query = select(LinkedIdentity).where(LinkedIdentity.status.in_(statuses)).order_by(LinkedIdentity.id.desc())
    if user is not None:
        query = query.where((LinkedIdentity.main_user_id == user.id) | (LinkedIdentity.requested_by_id == user.id))
    if not aliases:
        query = query.where(LinkedIdentity.method != METHOD_EMAIL_CHANGED)
    if limit is not None:
        query = query.limit(limit)
    found = session.exec(query).all()
    ids = {i for link in found for i in (link.main_user_id, link.secondary_user_id, link.requested_by_id) if i is not None}
    people = {u.id: u for u in session.exec(select(User).where(User.id.in_(ids))).all()} if ids else {}
    return [
        LinkRow(link, people.get(link.main_user_id), people.get(link.secondary_user_id), people.get(link.requested_by_id))
        for link in found
    ]

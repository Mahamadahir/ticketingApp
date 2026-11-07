"""In-app notifications (phase 2A): the bell. Something happens that an agent should know about,
a row is written for them, and the bell counts what they have not read.

Four things notify: a ticket being assigned to you, an @mention in an internal note, the
requester (or a follower) replying on your ticket, and your ticket being reopened.
Later phases add Teams (an unread notification is also sent to Teams after a delay, P1s and SLA
breaches) and skipping agents who are out of office. Which events reach which channel is each
person's choice (app/notification_settings.py); notify() honours the in-app one. Everything
that notifies goes through notify() so those arrive in one place.

Agents are notified about the tickets they can open: a mention of someone outside the ticket's
team does nothing (access follows the team, and a notification would say what the ticket is).
Everyone signed in also has a bell, for the entries about their own tickets (REQUESTER_KINDS, #130:
the requester and external followers) and requests waiting for their vote; those open the portal,
and notify() refuses every other kind for a person who is not an agent.
"""

import re
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import and_, func, or_, update
from sqlmodel import Session, select

from app.identity import my_ticket_condition
from app.models import FINISHED_STATUSES, Message, Notification, Ticket, TicketStatus, User, UserRole
from app.notification_settings import IN_APP, TEAMS, is_enabled, offers, wants_approval_email
from app.settings_store import TEAMS_UNREAD_MINUTES, get_int_setting
from app.visibility import Access, access_context, team_members, ticket_access, visible_ticket_condition

ASSIGNED = "assigned"
MENTIONED = "mentioned"
REPLIED = "replied"
REOPENED = "reopened"
APPROVAL_WITHDRAWN = "approval_withdrawn"
APPROVAL_OVERDUE = "approval_overdue"
APPROVAL_WAITING = "approval_waiting"
DUE_TODAY = "due_today"
NO_ACTIVE_APPROVERS = "approval_no_active_approvers"  # #161: a request waits because nobody active can approve it (not a listed event type)
OUTAGE_MODE = "outage_mode"  # #164: outage mode was switched on or off (not a listed event type)
BREAK_GLASS_SIGN_IN = "break_glass_sign_in"  # #163: a break-glass account signed in (not a listed event type)
ACCOUNT_DEACTIVATED = "account_deactivated"  # #158: the nightly Entra check made someone inactive
CHASE_GIVEN_UP = "chase_given_up"
MSP_UNACKNOWLEDGED = "msp_unacknowledged"
MSP_HANDOFF_WARNING = "msp_handoff_warning"  # #151: a ticket of yours goes to the MSP tonight unless held
MSP_HANDED_OFF = "msp_handed_off"  # #151: the automatic handoff sent a ticket of yours
# The bell entries for the requester and the external followers of a ticket (#130). They open in the portal.
AGENT_EMAILED = "agent_emailed"
AWAITING_YOU = "awaiting_you"
TICKET_RESOLVED = "ticket_resolved"
TICKET_CLOSED = "ticket_closed"
APPROVAL_OUTCOME = "approval_outcome"
SUBREQUEST_CREATED = "subrequest_created"  # #118: sub-requests were made automatically for the request
REQUESTER_KINDS = (AGENT_EMAILED, AWAITING_YOU, TICKET_RESOLVED, TICKET_CLOSED, APPROVAL_OUTCOME, SUBREQUEST_CREATED)
# Kinds that open in the portal, where the person (agent or not) is the requester, follower or approver.
PORTAL_KINDS = (APPROVAL_WAITING,) + REQUESTER_KINDS
AGENT_ROLES = (UserRole.AGENT, UserRole.SUPER_ADMIN)

# Stored text names its ticket with this marker, never with the number or reference itself. The reference's
# prefix follows the ticket's type (INC-1042, SR-1042), which can change after the notification is written,
# so it is filled in when the text is shown (the bell, the Teams card) with `notification_text`.
REF = "\x1fref\x1f"

# @alice or @alice@example.com: letters, digits, dots, dashes and underscores.
_MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9][A-Za-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9.-]*)?)")


def notify(
    session: Session, recipient: User, kind: str, ticket: Optional[Ticket], text: str, actor_email: Optional[str]
) -> Optional[Notification]:
    """Tells one agent something. Nothing for the person who did it themselves. Does not commit.
    A request waiting for someone's vote is the one kind an ordinary signed-in person (an approver who is
    not an agent) is told too, and so is the requester or follower of a ticket about their own ticket
    (REQUESTER_KINDS); everything else is for agents."""
    if not recipient.is_active:
        return None  # inactive people (app/entra_check.py) are told nothing; their tickets are found on Admin > Users
    if recipient.role not in AGENT_ROLES and kind not in PORTAL_KINDS:
        return None
    if actor_email and recipient.email.lower() == actor_email.lower():
        return None
    bell = is_enabled(session, recipient, kind, IN_APP)
    teams = offers(kind, TEAMS) and is_enabled(session, recipient, kind, TEAMS)
    if not bell and not teams:
        return None  # they turned both off for this (Settings, notifications)
    notification = Notification(
        user_id=recipient.id, kind=kind, ticket_id=ticket.id if ticket is not None else None,
        actor_email=actor_email, text=text, bell=bell, teams_wanted=teams,
    )
    if not bell:
        # Teams only: the row is the worker's record of what to send, so it must exist, but it is
        # written already read and hidden from the bell, which never shows or counts it.
        notification.read_at = datetime.now(timezone.utc)
    session.add(notification)
    return notification


def notification_text(notification: Notification, ticket: Optional[Ticket]) -> str:
    """The text to show: the ticket's current reference (INC-1042) in place of the marker. Only the first
    marker is replaced (it leads the ticket's part of the text, so a subject cannot supply one that counts).
    Without the ticket the marker becomes "A ticket" rather than showing."""
    text = notification.text
    if REF not in text:
        return text
    return text.replace(REF, ticket.reference if ticket is not None else "A ticket", 1)


def bell_references(session: Session, notifications) -> dict[int, Ticket]:
    """The tickets of these notifications by id, for filling in the reference, whether or not the person can
    open them (the rows themselves are already limited by `visible_to`)."""
    ids = {n.ticket_id for n in notifications if n.ticket_id}
    if not ids:
        return {}
    return {t.id: t for t in session.exec(select(Ticket).where(Ticket.id.in_(ids))).all()}


def notify_assigned(session: Session, ticket: Ticket, assignee: User, actor_email: Optional[str]) -> Optional[Notification]:
    if not _can_open(session, assignee, ticket):
        return None  # a notification says what the ticket is, so only someone who can open it gets one
    return notify(session, assignee, ASSIGNED, ticket, f"{REF} {ticket.subject} was assigned to you", actor_email)


def _can_open(session: Session, person: User, ticket: Ticket) -> bool:
    return ticket_access(person, ticket, access_context(session, person)) >= Access.READ


def mentioned_in(session: Session, ticket: Ticket, body: str, author: User) -> list[User]:
    """The agents an internal note mentions who can open the ticket, each once, never the author.
    @alice matches the part of an email address before the @ (or the whole address, @alice@x.com);
    a name two accounts share matches neither, so nobody is notified by mistake."""
    tokens = {match.rstrip(".-_").lower() for match in _MENTION.findall(body or "")}
    tokens.discard("")
    if not tokens:
        return []
    agents = session.exec(select(User).where(User.role.in_(AGENT_ROLES), User.is_active)).all()
    levels_cache: dict[int, object] = {}
    found: list[User] = []
    for token in sorted(tokens):
        if "@" in token:
            matches = [a for a in agents if a.email.lower() == token]
        else:
            matches = [a for a in agents if a.email.lower().partition("@")[0] == token]
        if len(matches) != 1:
            continue
        person = matches[0]
        if person.id == author.id or person in found:
            continue
        levels = levels_cache.setdefault(person.id, access_context(session, person))
        if ticket_access(person, ticket, levels) >= Access.READ:
            found.append(person)
    return found


def notify_mentions(
    session: Session, ticket: Ticket, note: Message, author: User, text: Optional[str] = None
) -> list[Notification]:
    """Tells the agents the note mentions. `text` is what to read mentions from when part of the
    note was written by the system (an escalation's fixed lead-in), so only what the agent wrote counts."""
    notifications = []
    for person in mentioned_in(session, ticket, note.body if text is None else text, author):
        made = notify(
            session, person, MENTIONED, ticket,
            f"{author.display_name.replace(REF, '')} mentioned you on {REF} {ticket.subject}", author.email,
        )
        if made is not None:
            notifications.append(made)
    return notifications


def notify_reply(session: Session, ticket: Ticket, replier_email: str) -> list[Notification]:
    """Tells the assigned agent ("Ticket updated by <name>") that the requester (or a follower) replied, whatever the ticket's status:
    a reply on a ticket with the MSP or a supplier moves nothing, so this is the only sign of it. A reply
    on a split original also tells the assignees of its open parts, since each of them sees the reply.
    Each agent once; never the replier; nobody who cannot open the ticket they are told about. A ticket
    nobody is assigned to tells nobody (an open question in docs/build-plan.md). Does not commit."""
    replier = session.exec(select(User).where(func.lower(User.email) == replier_email.lower())).first()
    name = (replier.display_name or "").strip() if replier is not None else ""
    name = name or replier_email  # a follower without an account, or no name: their email
    targets = [ticket]
    targets += session.exec(
        select(Ticket).where(Ticket.split_from_id == ticket.id, Ticket.status.not_in(FINISHED_STATUSES)).order_by(Ticket.id)
    ).all()
    told: set[int] = set()
    made: list[Notification] = []
    for target in targets:
        if target.assigned_agent_id is None or target.assigned_agent_id in told:
            continue
        agent = session.get(User, target.assigned_agent_id)
        if agent is None or not _can_open(session, agent, target):
            continue
        notification = notify(
            session, agent, REPLIED, target, f"{REF} {target.subject}: Ticket updated by {name}", replier_email
        )
        if notification is not None:
            told.add(agent.id)
            made.append(notification)
    return made


def visible_to(session: Session, user: User):
    """The SQL condition for the notifications this user may still see: their own, leaving out
    those about a ticket they can no longer open (it moved to another team). A notification would
    otherwise keep showing the subject of a ticket they have lost access to."""
    # The portal entries open in the portal, so they need no agent access: being asked to vote always counts, and
    # the requester kinds only while the person is still the requester or a follower (a ticket they no longer follow
    # must not keep showing its subject).
    mine = select(Ticket.id).where(my_ticket_condition(user))
    portal = or_(
        Notification.kind == APPROVAL_WAITING,
        and_(Notification.kind.in_(REQUESTER_KINDS), Notification.ticket_id.in_(mine)),
    )
    if user.role not in AGENT_ROLES:  # not an agent: only the portal entries exist (waiting for your vote, your own tickets)
        return and_(Notification.user_id == user.id, Notification.bell.is_(True), portal)
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is None:  # a super admin can open every ticket
        return and_(Notification.user_id == user.id, Notification.bell.is_(True))
    return and_(
        Notification.user_id == user.id,
        Notification.bell.is_(True),  # not the Teams-only ones
        or_(
            Notification.ticket_id.is_(None),
            portal,
            Notification.ticket_id.in_(select(Ticket.id).where(visible)),
        ),
    )


def count_unread(session: Session, user: User) -> int:
    return session.exec(
        select(func.count()).select_from(Notification).where(visible_to(session, user), Notification.read_at.is_(None))
    ).one()


def bell_tickets(session: Session, user: User, notifications) -> dict[int, Ticket]:
    """The tickets of these notifications that the user can open, by id. The bell shows a ticket's
    number or subject only from here, so one they cannot open never reaches a page, even if its
    notification was read in a moment when they still could."""
    ids = {n.ticket_id for n in notifications if n.ticket_id}
    if not ids or user.role not in AGENT_ROLES:
        return {}
    query = select(Ticket).where(Ticket.id.in_(ids))
    visible = visible_ticket_condition(user, access_context(session, user))
    if visible is not None:  # None: a super admin can open every ticket
        query = query.where(visible)
    return {t.id: t for t in session.exec(query).all()}


def mark_read(session: Session, user: User, notification_id: int) -> Optional[Notification]:
    """Marks one of the user's own notifications read. None when it is not theirs, is Teams-only,
    or is about a ticket they can no longer open (the same rows the bell shows, see visible_to)."""
    notification = session.exec(
        select(Notification).where(Notification.id == notification_id, visible_to(session, user))
    ).first()
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        session.add(notification)
    return notification


def mark_all_read(session: Session, user: User) -> int:
    """Marks all of the user's unread notifications they can see read in one statement. Returns how many.
    Hidden ones (ticket no longer openable) are left alone, so the count shown matches."""
    result = session.exec(
        update(Notification)
        .where(visible_to(session, user), Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    return result.rowcount

# ---- The Teams copy (phase 2C-ii) -----------------------------------------------------------------------
#
# An agent can choose, per event, to be told in the bell, in Teams, or both. A notification that wants
# a Teams copy gets one when it is due: straight away if the bell is off for it (there is nothing to
# wait for), or once it has stayed unread in the bell for the person's delay. Reading it first means no
# copy. The worker picks the due ones up and hands each to whatever sends to Teams (app/teams.py); it is
# handed over once, and only if the person still wants it and can still open the ticket.

TEAMS_BATCH = 50


def default_teams_delay(session: Session) -> int:
    """The delay for everyone who has not set their own: the admin setting, 5 minutes unless changed."""
    return get_int_setting(session, TEAMS_UNREAD_MINUTES) or 5


def retire_read_for_teams(session: Session, now: Optional[datetime] = None) -> int:
    """Marks dealt with every bell notification the person has read before its Teams copy went out:
    they have seen it, so no copy. Without this a row read in time would stay waiting for ever, and
    the index the worker uses would grow with all the read history. Returns how many. Does not commit."""
    now = now or datetime.now(timezone.utc)
    result = session.exec(
        update(Notification)
        .where(
            Notification.teams_wanted.is_(True),
            Notification.teams_handled_at.is_(None),
            Notification.bell.is_(True),
            Notification.read_at.is_not(None),
        )
        .values(teams_handled_at=now)
    )
    return result.rowcount


def due_for_teams(session: Session, now: Optional[datetime] = None, limit: int = TEAMS_BATCH) -> list[Notification]:
    """The notifications whose Teams copy is due, oldest first, locked so a second worker cannot take the
    same rows. Not due: already dealt with, not wanted, read in the bell before the delay ran out."""
    now = now or datetime.now(timezone.utc)
    delay = func.coalesce(User.teams_delay_minutes, default_teams_delay(session))
    due_after_delay = Notification.created_at + func.make_interval(0, 0, 0, 0, 0, delay, 0)
    return list(
        session.exec(
            select(Notification)
            .join(User, User.id == Notification.user_id)
            .where(
                Notification.teams_wanted.is_(True),
                Notification.teams_handled_at.is_(None),
                or_(
                    Notification.bell.is_(False),
                    and_(Notification.read_at.is_(None), due_after_delay <= now),
                ),
            )
            .order_by(Notification.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Notification)
        ).all()
    )


def hand_to_teams(
    session: Session,
    enqueue: Callable[[Session, Notification], None],
    now: Optional[datetime] = None,
    limit: int = TEAMS_BATCH,
) -> tuple[int, int]:
    """Hands each due notification to `enqueue` (which writes the Teams post) and marks it dealt with.
    A notification is dropped, not sent, if the person has since turned Teams off for that event or can
    no longer open its ticket (a Teams message would say what the ticket is). Returns (handed, dropped).
    Does not commit; the worker commits after the job. If `enqueue` raises, nothing from this run is kept."""
    now = now or datetime.now(timezone.utc)
    retire_read_for_teams(session, now)
    handed = dropped = 0
    for notification in due_for_teams(session, now, limit):
        person = session.get(User, notification.user_id)
        ticket = session.get(Ticket, notification.ticket_id) if notification.ticket_id else None
        still_wanted = offers(notification.kind, TEAMS) and is_enabled(session, person, notification.kind, TEAMS)
        can_open = notification.ticket_id is None or (ticket is not None and _can_open(session, person, ticket))
        if still_wanted and can_open:
            enqueue(session, notification)
            handed += 1
        else:
            dropped += 1
        notification.teams_handled_at = now
        session.add(notification)
    return handed, dropped

DROP_BATCH = 1000  # dropping is cheap, so a backlog drains quickly


def drop_due_for_teams(session: Session, now: Optional[datetime] = None, limit: int = DROP_BATCH) -> int:
    """Marks the due Teams copies dealt with without sending them: what the worker does while sending
    notifications to Teams is switched off, so nothing is left waiting to arrive in a flood when it
    is switched on. Returns how many. Does not commit."""
    now = now or datetime.now(timezone.utc)
    retire_read_for_teams(session, now)
    rows = due_for_teams(session, now, limit)
    for notification in rows:
        notification.teams_handled_at = now
        session.add(notification)
    return len(rows)


def notify_reopened(session: Session, ticket: Ticket, actor_email: Optional[str], reason: str) -> Optional[Notification]:
    """Tells the assigned agent their resolved ticket was reopened (never the person who reopened it).
    Nobody assigned tells nobody; nobody who cannot open the ticket is told about it. Does not commit."""
    if ticket.assigned_agent_id is None:
        return None
    agent = session.get(User, ticket.assigned_agent_id)
    if agent is None or not _can_open(session, agent, ticket):
        return None
    return notify(
        session, agent, REOPENED, ticket, f"{REF} {ticket.subject} was reopened: {reason[:200]}", actor_email
    )


def notify_approval_withdrawn(session: Session, ticket: Ticket, approver: User, actor_email: Optional[str]) -> Optional[Notification]:
    """Tells an agent who was asked to approve that the requester withdrew the request. Only an agent has a
    bell, and only one who can open the ticket is told what it is. Does not commit."""
    if not _can_open(session, approver, ticket) or not wants_approval_email(session, approver):
        return None  # the bell has no setting of its own: switching off approval notifications covers it
    return notify(
        session, approver, APPROVAL_WITHDRAWN, ticket,
        f"{REF} {ticket.subject} was withdrawn by the requester, so it no longer needs your approval", actor_email,
    )


def notify_requesters(
    session: Session, ticket: Ticket, kind: str, text: str, actor_email: Optional[str],
    skip: Optional[Callable[[User], bool]] = None,
) -> list[Notification]:
    """Tells the requester and the external followers who have an account something about their own ticket (#130).
    Never the person who caused it (notify() skips them) and nobody without an account (they have no bell; email
    carries on as before). An agent who is the requester is told too, and the entry opens the portal. Does not commit."""
    from app.followers import portal_audience  # here: followers builds on ticket_service, which imports this module

    emails = portal_audience(session, ticket)
    people = session.exec(select(User).where(func.lower(User.email).in_(emails), User.is_active.is_(True))).all()
    made = []
    for person in sorted(people, key=lambda p: p.id):
        if skip is not None and skip(person):
            continue
        notification = notify(session, person, kind, ticket, text, actor_email)
        if notification is not None:
            made.append(notification)
    return made


def notify_agent_emailed(
    session: Session, ticket: Ticket, actor_email: Optional[str], instead_of: Optional[str] = None
) -> list[Notification]:
    """The "IT Support emailed you" entry. `instead_of` is the kind of entry the same action also makes (Awaiting user,
    Resolved): whoever has that one switched on is told by it, so only the others get this, and nobody is told twice
    or not at all."""
    return notify_requesters(
        session, ticket, AGENT_EMAILED, f"{REF} {ticket.subject}: IT Support emailed you", actor_email,
        skip=(lambda person: is_enabled(session, person, instead_of, IN_APP)) if instead_of else None,
    )


def notify_status_for_requesters(
    session: Session, ticket: Ticket, status: TicketStatus, actor_email: Optional[str]
) -> list[Notification]:
    """The requester's entry for a ticket moving to Awaiting user, Resolved or Closed; any other status tells nobody."""
    kind, what = {
        TicketStatus.AWAITING_USER: (AWAITING_YOU, "is waiting for your reply"),
        TicketStatus.RESOLVED: (TICKET_RESOLVED, "was resolved"),
        TicketStatus.CLOSED: (TICKET_CLOSED, "was closed"),
    }.get(status, (None, None))
    if kind is None:
        return []
    return notify_requesters(session, ticket, kind, f"{REF} {ticket.subject} {what}", actor_email)


def notify_approval_outcome(
    session: Session, ticket: Ticket, result: str, actor_email: Optional[str]
) -> list[Notification]:
    """Tells the requester and followers how their request was decided: approved, declined or waived."""
    what = {"approved": "was approved", "declined": "was declined", "waived": "was let through without waiting for every approver", "withdrawn": "was withdrawn"}
    return notify_requesters(
        session, ticket, APPROVAL_OUTCOME, f"{REF} {ticket.subject} {what.get(result, 'was decided')}", actor_email
    )


def notify_subrequests_created(
    session: Session, ticket: Ticket, made: list[Ticket], actor_email: Optional[str]
) -> list[Notification]:
    """Tells the requester and followers that sub-requests were made for their request (one entry however many,
    opening the request; app/subrequests.py)."""
    count = len(made)
    what = "a sub-request was" if count == 1 else f"{count} sub-requests were"
    return notify_requesters(session, ticket, SUBREQUEST_CREATED, f"{REF} {ticket.subject}: {what} created for your request", actor_email)


def notify_approval_waiting(session: Session, ticket: Ticket, approver: User) -> Optional[Notification]:
    """Tells an approver, in the bell, that a request is waiting for their vote (#122). Follows the person's own
    setting for this event only: it is shown even if they turned the approval email off, and whether they are
    an agent or can open the ticket does not matter (it opens in the portal, where they vote). Does not commit."""
    return notify(
        session, approver, APPROVAL_WAITING, ticket, f"{REF} {ticket.subject} is waiting for your approval", None
    )


def notify_approval_overdue(session: Session, ticket: Ticket, super_admin: User, days: int, unanswered: int) -> Optional[Notification]:
    """Tells a super admin a request has waited too long for approval (app/approval_reminders.py), so they can
    chase the approvers or waive it. Only if they can open the ticket. Does not commit."""
    if not _can_open(session, super_admin, ticket):
        return None
    return notify(
        session, super_admin, APPROVAL_OVERDUE, ticket,
        f"{REF} {ticket.subject} has waited {days} days for approval ({unanswered} had not answered)", None,
    )


def notify_no_active_approvers(
    session: Session, super_admin: User, ticket: Ticket, request_type_name: str, group_name: str
) -> Optional[Notification]:
    """Tells a super admin, in the bell, that nobody active can approve a request type (app/approvals.py), so the
    approval group needs fixing. Not a listed event type, so it is always on, like the overdue-approval bell entry
    and the deactivation one. Does not commit."""
    return notify(
        session, super_admin, NO_ACTIVE_APPROVERS, ticket,
        f"Nobody active can approve {request_type_name} requests; update the {group_name} approval group ({REF} is waiting)", None,
    )


def notify_account_deactivated(session: Session, super_admin: User, person: User, reason: str) -> Optional[Notification]:
    """Tells a super admin, in the bell, that the nightly Entra check made someone inactive (app/entra_check.py), so
    their open tickets can be re-assigned. Not a listed event type, so it is always on, like the overdue-approval bell
    entry. Does not commit."""
    return notify(
        session, super_admin, ACCOUNT_DEACTIVATED, None,
        f"{person.display_name or person.email} ({person.email}) was made inactive: {reason}. Re-assign their open tickets on Admin > Users.",
        None,
    )


def notify_break_glass_sign_in(session: Session, super_admin: User, person: User, ip: Optional[str]) -> Optional[Notification]:
    """Tells a super admin, in the bell, that a break-glass account signed in (app/routers/auth.py), so its use is
    always visible. Not a listed event type, so it is always on, like the deactivation entry. Does not commit."""
    where = f" from {ip}" if ip else ""
    return notify(
        session, super_admin, BREAK_GLASS_SIGN_IN, None,
        f"Break-glass account {person.email} signed in{where}. If this was not planned, reset it and check the audit log.",
        person.email,  # the account that signed in is not told of itself
    )


def notify_outage_mode(session: Session, super_admin: User, actor: User, on: bool) -> Optional[Notification]:
    """Tells a super admin, in the bell, that outage mode was switched on or off (app/outage.py). Not a listed
    event type, so it is always on. Not the person who did it. Does not commit."""
    text = (
        f"Outage mode was switched on by {actor.email}: agents can sign in with an emergency password. Check the audit log if this was not planned."
        if on else
        f"Outage mode was switched off by {actor.email}: emergency sessions ended and emergency passwords that were used have been cleared."
    )
    return notify(session, super_admin, OUTAGE_MODE, None, text, actor.email)


def notify_chase_given_up(session: Session, ticket: Ticket) -> list[Notification]:
    """Tells whoever owns a ticket that its chase email could not be sent (app/auto_close_chase.py), so it is not left
    in Awaiting user unseen: the assigned agent, or the whole team (its working members) while nobody is assigned.
    Only people who can open the ticket. Does not commit."""
    if ticket.assigned_agent_id is not None:
        people = [session.get(User, ticket.assigned_agent_id)]
    else:
        people = team_members(session, ticket.team_id)
    text = f"{REF} {ticket.subject}: the chase email to the requester could not be sent, so it is still waiting for them"
    made = []
    for person in people:
        if person is None or not person.is_active or not _can_open(session, person, ticket):
            continue
        notification = notify(session, person, CHASE_GIVEN_UP, ticket, text, None)
        if notification is not None:
            made.append(notification)
    return made


def notify_msp_unacknowledged(session: Session, ticket: Ticket) -> list[Notification]:
    """Tells whoever owns a ticket that the MSP has not confirmed it (app/msp.py `alert_unacknowledged`): the assigned
    agent, or the team's working members while nobody is assigned. Only people who can open the ticket. Does not commit."""
    if ticket.assigned_agent_id is not None:
        people = [session.get(User, ticket.assigned_agent_id)]
    else:
        people = team_members(session, ticket.team_id)
    text = f"The MSP hasn't confirmed {REF}"
    made = []
    for person in people:
        if person is None or not person.is_active or not _can_open(session, person, ticket):
            continue
        notification = notify(session, person, MSP_UNACKNOWLEDGED, ticket, text, None)
        if notification is not None:
            made.append(notification)
    return made


def _assignee_who_can_open(session: Session, ticket: Ticket) -> Optional[User]:
    if ticket.assigned_agent_id is None:
        return None
    agent = session.get(User, ticket.assigned_agent_id)
    return agent if agent is not None and agent.is_active and _can_open(session, agent, ticket) else None


def notify_msp_handoff_warning(
    session: Session, ticket: Ticket, handoff_label: str, since: datetime, now: Optional[datetime] = None
) -> Optional[Notification]:
    """Tells the assignee their ticket will go to the MSP at the handoff hour unless they Hold it (app/msp.py
    `warn_before_handoff`). Once for each ticket and person from `since` (the start of this night's warning), whether or
    not they have read it. Nobody assigned tells nobody. Does not commit."""
    agent = _assignee_who_can_open(session, ticket)
    if agent is None:
        return None
    already = session.exec(
        select(Notification.id).where(
            Notification.kind == MSP_HANDOFF_WARNING, Notification.ticket_id == ticket.id,
            Notification.user_id == agent.id, Notification.created_at >= since,
        ).limit(1)
    ).first()
    if already is not None:
        return None
    made = notify(session, agent, MSP_HANDOFF_WARNING, ticket, f"{REF} will go to the MSP at {handoff_label}. Hold it to keep it.", None)
    if made is not None and now is not None:
        made.created_at = now  # the job's clock, so the once-a-night check compares like with like
    return made


def notify_msp_handed_off(session: Session, ticket: Ticket, sent_label: str) -> Optional[Notification]:
    """Tells the assignee the automatic handoff has just sent their ticket. Does not commit."""
    agent = _assignee_who_can_open(session, ticket)
    if agent is None:
        return None
    return notify(session, agent, MSP_HANDED_OFF, ticket, f"{REF} was sent to the MSP at {sent_label}", None)


def notify_due_today(session: Session, ticket: Ticket) -> Optional[Notification]:
    """Tells the assigned agent their Scheduled ticket is due today (app/due_today.py), as the job moves it back to
    Open. Nobody assigned tells nobody; nobody who cannot open the ticket is told about it. Does not commit."""
    if ticket.assigned_agent_id is None:
        return None
    agent = session.get(User, ticket.assigned_agent_id)
    if agent is None or not _can_open(session, agent, ticket):
        return None
    return notify(session, agent, DUE_TODAY, ticket, f"{REF} {ticket.subject} is due today", None)

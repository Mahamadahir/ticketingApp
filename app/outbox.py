"""Queuing email out of the shared mailbox (phase 3D). Nothing here commits, and nothing here sends:
the worker's mail job (app/mail.py) builds each email when it goes and sends it.

The places that record a reply, a notice, an approval request or a "ticket raised for you" call
these in the same transaction, so what was recorded and what is emailed cannot disagree. With
MAIL_SENDING off nothing is queued, so switching it on later does not send a backlog.

This module imports no other app module that sends or decides what may be emailed (messages.py
does that), so messages.py can call it without a cycle.
"""

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.models import ApprovalVote, Message, OutboundEmail, Ticket, TicketApproval, User, UserRole
from app.notification_settings import APPROVAL_OVERDUE as OVERDUE_EVENT, EMAIL, is_enabled, wants_approval_email

REPLY = "reply"  # an agent's email to the requester (counts as a first response)
NOTICE = "notice"  # an automatic message to the requester (an approval decision, say)
RAISED_FOR = "raised_for_you"  # "an agent raised a ticket for you"
APPROVAL = "approval"  # asks an approver to approve or decline
APPROVAL_REMINDER = "approval_reminder"  # chases an approver who has not answered
APPROVAL_OVERDUE = "approval_overdue"  # tells a super admin a request has waited too long for its approvers
APPROVAL_WITHDRAWN = "approval_withdrawn"  # tells an approver the requester withdrew the request
ACKNOWLEDGEMENT = "acknowledgement"  # "your issue has been logged", once, for a ticket that arrived by email
CHASE = "chase"  # "is this still an issue?" to the requester of a ticket Awaiting user (app/auto_close_chase.py)
MSP_HANDOFF = "msp_handoff"  # a ticket sent to the MSP (app/msp.py); the text is fixed when it is queued
FOLLOWER_UPDATE = "follower_update"  # an inbound update passed to someone who was not on the mail it came in

# Kinds that go to the requester without an agent having just written them. These are what the
# per-ticket cap counts; an agent's own reply never is.
AUTOMATIC_TO_REQUESTER = (NOTICE, RAISED_FOR, ACKNOWLEDGEMENT, CHASE)


def is_on() -> bool:
    return settings.mail_sending != "off"


def queue_message(session: Session, message: Message, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues the email for a public outbound message: an agent's reply (it has an author) or an
    automatic notice (it has none). One email per message, enforced by the database."""
    if not is_on():
        return None
    session.flush()  # the message needs its id
    row = OutboundEmail(
        kind=REPLY if message.author_user_id is not None else NOTICE,
        ticket_id=ticket.id,
        message_id=message.id,
        to_address=ticket.requester_email,
    )
    session.add(row)
    return row


def queue_raised_for(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues "an agent raised a ticket for you". Whether the person is to be told is asked again
    when it is sent (raising.should_tell_requester), so a later opt-out wins."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(kind=RAISED_FOR, ticket_id=ticket.id, to_address=ticket.requester_email)
    session.add(row)
    return row


def queue_approval(session: Session, vote: ApprovalVote, approver: User) -> Optional[OutboundEmail]:
    """Queues the email asking one voter (one email per member). The link in it is made when it is sent.
    Nothing is queued for someone who switched these emails off; the request still shows in their portal."""
    return _queue_to_approver(session, APPROVAL, vote, approver)


def _queue_to_approver(session: Session, kind: str, vote: ApprovalVote, approver: User) -> Optional[OutboundEmail]:
    """The one rule for an email that asks an approver (the request or a reminder): mail on, and the person
    wants approval emails. Nothing else decides who is asked."""
    if not is_on() or not wants_approval_email(session, approver):
        return None
    session.flush()
    approval = session.get(TicketApproval, vote.approval_id)
    row = OutboundEmail(kind=kind, ticket_id=approval.ticket_id, vote_id=vote.id, to_address=approver.email)
    session.add(row)
    return row


def queue_approval_reminder(session: Session, vote: ApprovalVote, approver: User) -> Optional[OutboundEmail]:
    """Queues a reminder to one voter who has not answered (app/approval_reminders.py decides when). None
    when mail is off or the person switched approval emails off; the caller then does not count a reminder.
    Whether it is still wanted is asked again when it is sent."""
    return _queue_to_approver(session, APPROVAL_REMINDER, vote, approver)


def queue_approval_withdrawn(session: Session, vote: ApprovalVote, approver: User) -> Optional[OutboundEmail]:
    """Queues "the request you were asked to approve was withdrawn" for one voter who had not answered.
    Only for a voter whose approval email was sent, and it follows the same preference as that email (wants_approval_email)."""
    if not is_on() or not wants_approval_email(session, approver):
        return None
    session.flush()
    asked = session.exec(
        select(OutboundEmail.id).where(
            OutboundEmail.kind == APPROVAL, OutboundEmail.vote_id == vote.id, OutboundEmail.sent_at.is_not(None)
        ).limit(1)
    ).first()
    if asked is None:
        return None  # the approval email never went out (the one still queued is dropped), so there is nothing to take back
    approval = session.get(TicketApproval, vote.approval_id)
    row = OutboundEmail(kind=APPROVAL_WITHDRAWN, ticket_id=approval.ticket_id, vote_id=vote.id, to_address=approver.email)
    session.add(row)
    return row


def queue_approval_overdue(session: Session, ticket: Ticket, admin: User) -> Optional[OutboundEmail]:
    """Queues the "this request has waited too long" email to one super admin (app/approval_reminders.py decides
    when, once per approval). None when mail is off or they switched it off in their notification settings. Whether
    it is still wanted is asked again when it is sent (mail.build_approval_overdue_mail)."""
    if not is_on() or admin.role != UserRole.SUPER_ADMIN or not is_enabled(session, admin, OVERDUE_EVENT, EMAIL):
        return None
    session.flush()
    row = OutboundEmail(kind=APPROVAL_OVERDUE, ticket_id=ticket.id, to_address=admin.email)
    session.add(row)
    return row


def queue_acknowledgement(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues the one "your issue has been logged" receipt for a ticket opened by someone's email.
    A deliberate exception to "no email until triaged": it carries no ticket content (see
    mail.build_acknowledgement_mail). The caller decides the mail came from a real person
    (inbound.py does, after its loop guards); this only makes sure a ticket is told once."""
    if not is_on():
        return None
    session.flush()
    already = session.exec(
        select(OutboundEmail.id).where(OutboundEmail.ticket_id == ticket.id, OutboundEmail.kind == ACKNOWLEDGEMENT)
    ).first()
    if already is not None:
        return None
    row = OutboundEmail(kind=ACKNOWLEDGEMENT, ticket_id=ticket.id, to_address=ticket.requester_email)
    session.add(row)
    return row


def queue_chase(session: Session, ticket: Ticket) -> Optional[OutboundEmail]:
    """Queues the "is this still an issue?" email (app/auto_close_chase.py decides when and only once per
    wait). Whether it is still wanted is asked again when it is sent (mail.build_chase_mail)."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(kind=CHASE, ticket_id=ticket.id, to_address=ticket.requester_email)
    session.add(row)
    return row


def queue_follower_update(session: Session, ticket: Ticket, message: Message, to_address: str) -> Optional[OutboundEmail]:
    """Queues one person's copy of an inbound update (app/follower_updates.py picks who). One per
    person per message, enforced by the database, so a replay queues nothing. Whether they are still
    to be told, and whether the message is one the requester may see, is asked again when it is sent."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(
        kind=FOLLOWER_UPDATE, ticket_id=ticket.id, about_message_id=message.id, to_address=to_address.strip().lower()
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def queue_msp_handoff(
    session: Session, ticket: Ticket, to_address: str, cc: list[str], subject: str, body: str
) -> Optional[OutboundEmail]:
    """Queues the email to the MSP with the text built by app/msp.py (the one the agent previewed, or the preview is shown again): unlike
    the other kinds it is not rebuilt when sent, since the thread then would differ from what was agreed to go.
    None when mail is off."""
    if not is_on():
        return None
    session.flush()
    row = OutboundEmail(
        kind=MSP_HANDOFF, ticket_id=ticket.id, to_address=to_address, cc=", ".join(cc) or None, subject=subject, body=body
    )
    session.add(row)
    return row

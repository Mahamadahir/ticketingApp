"""Where the app holds a person's data: one lookup and one list of columns (issue #175, reused by #176).

`find_person(session, address)` turns an email address into everything that identifies the person: every
address they have (app/identity.py: their own, linked identities, old-address aliases) and every account id
that is theirs (the main account and any account merged into it). Nothing else in the data protection code
builds that list.

`COLUMNS` classifies every column in the schema that holds an email address, a name, a user id, a network
address or free text a person wrote. `test_person_data` fails when a column that looks like one of those is not
listed, the same idea as MOVED_COLUMNS / KEPT_COLUMNS in app/linked_identities.py, so adding such a column forces
a decision about the subject access export and about anonymisation. A column's `role` says what a hit means:

- SUBJECT: the value is the person's own data. The export includes it; anonymisation (#176) blanks it.
- STAFF: the value names a staff member who did something (resolved, triaged, assigned, decided). The export
  lists it by reference only, and only when the person is that staff member.
- OTHER: the value names someone else (who added a follower, who caused a notification), or is a shared
  mailbox. Never exported for this person; anonymisation leaves it unless it is the person's own address.
- NOT_PERSON: a name or label that is not a person (a team, a category, a file name, a count).
- TEXT: free text a person wrote. It belongs to the row's owner and is handled with that row.
"""

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, or_
from sqlmodel import Session, SQLModel, select

from app.identity import LINK_ACTIVE, addresses_of, person_for_address
from app.models import LinkedIdentity, User

SUBJECT, STAFF, OTHER, NOT_PERSON, TEXT = "subject", "staff", "other", "not_person", "text"
EMAIL, USER_ID, NAME, NETWORK, FREE_TEXT, LABEL = "email", "user_id", "name", "network", "free_text", "label"


@dataclass(frozen=True)
class Person:
    """Who a subject access request is about."""

    address: str  # as asked, lower case
    addresses: frozenset[str]  # every address they have, lower case
    user_ids: frozenset[int]  # the main account and any account merged into it
    main: Optional[User]  # the account, if there is one


def find_person(session: Session, address: str) -> Person:
    address = (address or "").strip().lower()
    addresses = set(addresses_of(session, address)) | {address}
    main = person_for_address(session, address)
    ids: set[int] = set()
    if main is not None:
        ids.add(main.id)
        ids.update(
            session.exec(
                select(LinkedIdentity.secondary_user_id).where(
                    LinkedIdentity.main_user_id == main.id,
                    LinkedIdentity.status == LINK_ACTIVE,
                    LinkedIdentity.secondary_user_id.is_not(None),
                )
            ).all()
        )
    # Any account that carries one of the addresses is theirs too (e.g. a merged-away account's own address).
    for found in session.exec(select(User).where(func.lower(User.email).in_(addresses))).all():
        ids.add(found.id)
    return Person(address, frozenset(addresses), frozenset(ids), main)


def email_matches(column, person: Person):
    """SQL: the column holds exactly one of the person's addresses (any case, spaces trimmed)."""
    return func.lower(func.trim(column)).in_(person.addresses)


def list_matches(column, person: Person):
    """SQL: a column that holds a list of addresses (a cc, a sender such as 'Ann <ann@x>') mentions one of theirs.
    A coarse filter; the caller confirms with `mentions`."""
    return or_(*[func.lower(column).contains(a) for a in sorted(person.addresses)])


_ADDRESS = re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+")


def mentions(text: Optional[str], person: Person) -> bool:
    """Whether a list of addresses, or a 'Name <address>' sender, contains one of theirs (whole address only)."""
    return any(found.lower() in person.addresses for found in _ADDRESS.findall(text or ""))


@dataclass(frozen=True)
class ColumnUse:
    kind: str
    role: str
    note: str = ""


def _use(role: str, kind: str, note: str = "") -> ColumnUse:
    return ColumnUse(kind, role, note)


# Every column that holds an email address, a name, a user id, a network address or free text written by a person.
COLUMNS: dict[str, ColumnUse] = {
    # the account
    "user.email": _use(SUBJECT, EMAIL, "account record"),
    "user.display_name": _use(SUBJECT, NAME, "account record"),
    "user.entra_object_id": _use(SUBJECT, USER_ID, "account record"),
    "user_session.user_id": _use(SUBJECT, USER_ID, "sign-in sessions (the session id is a secret and is never exported)"),
    "user_session.ip": _use(SUBJECT, NETWORK, "sign-in sessions"),
    "user_session.user_agent": _use(SUBJECT, NETWORK, "sign-in sessions"),
    "userpermission.user_id": _use(SUBJECT, USER_ID, "account record"),
    "userpermission.granted_by": _use(OTHER, EMAIL, "the super admin who granted it"),
    "teammembership.user_id": _use(SUBJECT, USER_ID, "account record"),
    "approvalgroupmember.user_id": _use(SUBJECT, USER_ID, "account record"),
    "notificationpreference.user_id": _use(SUBJECT, USER_ID, "notification preferences"),
    "requestertimezone.email": _use(SUBJECT, EMAIL, "time zone read from their mailbox"),
    "linked_identity.main_user_id": _use(SUBJECT, USER_ID, "linked identities"),
    "linked_identity.secondary_user_id": _use(SUBJECT, USER_ID, "linked identities"),
    "linked_identity.email": _use(SUBJECT, EMAIL, "linked identities"),
    "linked_identity.entra_object_id": _use(SUBJECT, USER_ID, "linked identities"),
    "linked_identity.requested_by_id": _use(STAFF, USER_ID),
    "linked_identity.approved_by_id": _use(STAFF, USER_ID),
    "linked_identity.unlinked_by_id": _use(STAFF, USER_ID),
    "link_code.user_id": _use(SUBJECT, USER_ID, "emailed link codes (only a keyed hash of the code is held)"),
    "link_code.address": _use(SUBJECT, EMAIL, "emailed link codes"),
    # logs
    "activitylog.actor_user_id": _use(SUBJECT, USER_ID, "sign-in and activity rows"),
    "activitylog.actor_email": _use(SUBJECT, EMAIL, "sign-in and activity rows"),
    "activitylog.ip": _use(SUBJECT, NETWORK, "sign-in and activity rows"),
    "activitylog.user_agent": _use(SUBJECT, NETWORK, "sign-in and activity rows"),
    "activitylog.detail": _use(TEXT, FREE_TEXT, "sign-in and activity rows"),
    "auditlog.actor_email": _use(SUBJECT, EMAIL, "audit entries the person made"),
    "auditlog.detail": _use(TEXT, FREE_TEXT, "audit entries"),
    "ticketevent.actor_email": _use(SUBJECT, EMAIL, "ticket events the person caused"),
    "ticketevent.from_value": _use(SUBJECT, EMAIL, "an event can name an address (a follower added)"),
    "ticketevent.to_value": _use(SUBJECT, EMAIL, "an event can name an address (a follower added)"),
    "ticketevent.detail": _use(TEXT, FREE_TEXT, "ticket events"),
    # tickets
    "ticket.requester_email": _use(SUBJECT, EMAIL, "tickets the person raised"),
    "ticket.subject": _use(TEXT, FREE_TEXT, "tickets the person raised"),
    "ticket.created_by_id": _use(STAFF, USER_ID),
    "ticket.assigned_agent_id": _use(STAFF, USER_ID),
    "ticket.resolved_by_id": _use(STAFF, USER_ID),
    "ticket.triaged_by_id": _use(STAFF, USER_ID),
    "ticket.escalated_by_id": _use(STAFF, USER_ID),
    "ticket.escalation_reason": _use(TEXT, FREE_TEXT, "agent-written, internal"),
    "ticket.resolution_notes": _use(TEXT, FREE_TEXT, "the resolution the requester is told"),
    "ticket.split_from_id": _use(NOT_PERSON, LABEL, "a ticket id"),
    "ticket.required_by": _use(NOT_PERSON, LABEL, "a date"),
    "ticketescalator.user_id": _use(STAFF, USER_ID),
    "ticketfollower.email": _use(SUBJECT, EMAIL, "tickets the person follows or is copied on"),
    "ticketfollower.user_id": _use(SUBJECT, USER_ID, "tickets the person follows or is copied on"),
    "ticketfollower.added_by_email": _use(OTHER, EMAIL, "whoever added the follower"),
    "ticketanswer.value": _use(TEXT, FREE_TEXT, "the requester's form answers"),
    "message.sender": _use(SUBJECT, NAME, "'Name' or 'Name <address>' as the mail or portal gave it"),
    "message.cc": _use(SUBJECT, EMAIL, "a comma-separated list"),
    "message.author_user_id": _use(SUBJECT, USER_ID, "messages written by an account (agents: listed by reference)"),
    "message.body": _use(TEXT, FREE_TEXT, "the thread"),
    "attachment.filename": _use(NOT_PERSON, LABEL, "a file name; the file goes with its message"),
    "attachment.uploaded_by_id": _use(SUBJECT, USER_ID, "files the person uploaded"),
    # approvals and feedback
    "approvalvote.voter_id": _use(SUBJECT, USER_ID, "approval votes"),
    "approvalvote.comment": _use(TEXT, FREE_TEXT, "approval votes"),
    "ticketapproval.voters": _use(NOT_PERSON, LABEL, "a count"),
    "actiontoken.resolved_by_id": _use(STAFF, USER_ID, "the agent a feedback link is about"),
    "ticketfeedback.resolved_by_id": _use(STAFF, USER_ID, "the agent a rating is about"),
    "ticketfeedback.comment": _use(TEXT, FREE_TEXT, "the requester's own words (build-plan, 22 Oct 2026)"),
    # notifications and mail
    "notification.user_id": _use(SUBJECT, USER_ID, "the person's bell"),
    "notification.actor_email": _use(OTHER, EMAIL, "who caused the notification"),
    "notification.text": _use(TEXT, FREE_TEXT, "the person's bell"),
    "outboundemail.to_address": _use(SUBJECT, EMAIL, "emails sent to the person (subject and addresses, not the body)"),
    "outboundemail.cc": _use(SUBJECT, EMAIL, "a comma-separated list"),
    "outboundemail.subject": _use(TEXT, FREE_TEXT, "emails sent to the person"),
    "outboundemail.body": _use(TEXT, FREE_TEXT, "emails sent to the person; not exported (see the PR decisions)"),
    "inboundmail.sender": _use(SUBJECT, NAME, "mail the person sent to the shared mailbox (ids and outcome only)"),
    "inboundmail.to_addresses": _use(SUBJECT, EMAIL, "a comma-separated list"),
    "inboundmail.cc_addresses": _use(SUBJECT, EMAIL, "a comma-separated list"),
    "mspunmatchedmail.sender": _use(OTHER, NAME, "MSP staff"),
    "mspunmatchedmail.to_addresses": _use(OTHER, EMAIL, "MSP mail"),
    "mspunmatchedmail.cc_addresses": _use(OTHER, EMAIL, "MSP mail"),
    "mspunmatchedmail.resolved_by": _use(STAFF, EMAIL),
    "mspunmatchedmail.subject": _use(TEXT, FREE_TEXT, "MSP mail"),
    "mspunmatchedmail.body": _use(TEXT, FREE_TEXT, "MSP mail"),
    # configuration that names people or things
    "neverfollowaddress.email": _use(OTHER, EMAIL, "shared mailboxes and lists, not people"),
    "neverfollowaddress.added_by": _use(STAFF, EMAIL),
    "thankscandidate.decided_by": _use(STAFF, EMAIL),
    "thankscandidate.sample_text": _use(TEXT, FREE_TEXT, "a reply an agent reclosed; may quote a requester"),
    "thanksrule.created_by": _use(STAFF, EMAIL),
    "emailtemplate.updated_by": _use(STAFF, EMAIL),
    "requestsubtemplate.assignee_id": _use(STAFF, USER_ID),
    "cannedresponse.body": _use(NOT_PERSON, LABEL, "admin-written wording"),
    "emailtemplate.body": _use(NOT_PERSON, LABEL, "admin-written wording"),
    "emailtemplate.subject": _use(NOT_PERSON, LABEL, "admin-written wording"),
    "neverfollowaddress.reason": _use(NOT_PERSON, LABEL, "admin-written note about a shared mailbox"),
    "requestsubtemplate.assign_to_parent_assignee": _use(NOT_PERSON, LABEL, "a flag"),
    "requestsubtemplate.subject_pattern": _use(NOT_PERSON, LABEL, "admin-written wording"),
    "setting.value": _use(NOT_PERSON, LABEL, "an admin setting"),
    "approvalgroup.name": _use(NOT_PERSON, LABEL),
    "category.name": _use(NOT_PERSON, LABEL),
    "team.name": _use(NOT_PERSON, LABEL),
    "requesttype.name": _use(NOT_PERSON, LABEL),
    "requesttype.offers_required_by": _use(NOT_PERSON, LABEL, "a flag"),
    "numbercounter.name": _use(NOT_PERSON, LABEL),
    "scheduledjob.name": _use(NOT_PERSON, LABEL),
}

# Column names that suggest a person's data. A column the pattern finds that is not in COLUMNS fails the test.
_NAME_HINT = re.compile(
    r"email|address|sender|display_name|(^|_)name$|_by$|_by_id$|author|actor|^cc$|^ip$|user_agent|voter|^user_id$"
    r"|object_id|assignee|body|comment|detail|notes|reason|^text$|^value$|subject|sample_text|^from_value$|^to_value$",
    re.IGNORECASE,
)


def user_id_columns() -> set[str]:
    found = set()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            if any(fk.column.table.name == "user" and fk.column.name == "id" for fk in column.foreign_keys):
                found.add(f"{table.name}.{column.name}")
    return found


def candidate_keys() -> set[str]:
    """Every `table.column` in the schema that looks like it holds a person's data: a column that points at a user,
    or whose name suggests an address, a name, an actor or free text."""
    found = user_id_columns()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            if _NAME_HINT.search(column.name):
                found.add(f"{table.name}.{column.name}")
    return found

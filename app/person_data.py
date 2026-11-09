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

Each column also says what anonymisation (#176, app/anonymisation.py) does with it, in `anon`:

- REPLACE: set to the placeholder name or address. SCRUB: the person's addresses inside the text become the
  placeholder address. BLANK: the text is removed ("[removed]" or empty). NULL: cleared. REMOVE: the person's
  addresses are taken out of a list of addresses.
- DELETE: the row is deleted (this column is the one that finds it). WITH_ROW: goes with a deleted row.
- KEEP: stays, because it is not the person's data once the account is anonymised (a number, a staff member, a
  label). The note says why. NOT_PERSON and STAFF columns are KEEP unless they say otherwise.
`tests/test_anonymisation.py` fails when a decision has no step in `app/anonymisation.py`.
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
REPLACE, SCRUB, BLANK, NULL, REMOVE, DELETE, WITH_ROW, KEEP = (
    "replace", "scrub", "blank", "null", "remove", "delete", "with_row", "keep",
)


PLACEHOLDER_DOMAIN = "invalid"  # RFC 2606: never delivered; what an anonymised requester's address ends with


def placeholder_address(n: int) -> str:
    return f"anonymised-{n}@{PLACEHOLDER_DOMAIN}"


def is_placeholder_address(address: Optional[str]) -> bool:
    return (address or "").strip().lower().endswith("@" + PLACEHOLDER_DOMAIN)


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


def object_ids(session: Session, person: Person) -> set[str]:
    """Every Entra object id (lower case) that is the person's: those of their accounts and of their linked identities.
    Deliberately wider than identity.object_ids_of (which finds who to message): any link status, and links where they are
    the merged-away side, so an export or an erasure also covers ids that are no longer in use."""
    ids = {
        i.lower()
        for i in session.exec(select(User.entra_object_id).where(User.id.in_(person.user_ids), User.entra_object_id.is_not(None))).all()
    }
    if person.user_ids:
        ids.update(
            i.lower()
            for i in session.exec(
                select(LinkedIdentity.entra_object_id).where(
                    or_(LinkedIdentity.main_user_id.in_(person.user_ids), LinkedIdentity.secondary_user_id.in_(person.user_ids)),
                    LinkedIdentity.entra_object_id.is_not(None),
                )
            ).all()
        )
    return ids


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
    anon: str = KEEP  # what anonymisation does with it (see the module docstring)


def _use(role: str, kind: str, note: str = "", anon: str = KEEP) -> ColumnUse:
    return ColumnUse(kind, role, note, anon)


# Every column that holds an email address, a name, a user id, a network address or free text written by a person.
COLUMNS: dict[str, ColumnUse] = {
    # the account
    "user.email": _use(SUBJECT, EMAIL, "account record", anon=REPLACE),
    "user.display_name": _use(SUBJECT, NAME, "account record", anon=REPLACE),
    "user.entra_object_id": _use(SUBJECT, USER_ID, "account record", anon=NULL),
    "user.teams_replies_also_email": _use(NOT_PERSON, LABEL, "a flag: replies to Teams requests also by email (My settings); not the person's data once the account is anonymised"),
    "user_session.user_id": _use(SUBJECT, USER_ID, "sign-in sessions (the session id is a secret and is never exported)", anon=DELETE),
    "user_session.ip": _use(SUBJECT, NETWORK, "sign-in sessions", anon=WITH_ROW),
    "user_session.user_agent": _use(SUBJECT, NETWORK, "sign-in sessions", anon=WITH_ROW),
    "userpermission.user_id": _use(SUBJECT, USER_ID, "account record", anon=DELETE),
    "userpermission.granted_by": _use(OTHER, EMAIL, "the super admin who granted it", anon=KEEP),
    "teammembership.user_id": _use(SUBJECT, USER_ID, "account record", anon=DELETE),
    "approvalgroupmember.user_id": _use(SUBJECT, USER_ID, "account record", anon=DELETE),
    "notificationpreference.user_id": _use(SUBJECT, USER_ID, "notification preferences", anon=DELETE),
    "teams_conversation.user_id": _use(SUBJECT, USER_ID, "their chat with the Teams bot", anon=DELETE),
    "teams_conversation.entra_object_id": _use(SUBJECT, USER_ID, "their chat with the Teams bot (found by this)", anon=DELETE),
    "teams_conversation.teams_user_id": _use(SUBJECT, USER_ID, "their chat with the Teams bot", anon=WITH_ROW),
    "requestertimezone.email": _use(SUBJECT, EMAIL, "time zone read from their mailbox", anon=DELETE),
    "linked_identity.main_user_id": _use(SUBJECT, USER_ID, "linked identities", anon=DELETE),
    "linked_identity.secondary_user_id": _use(SUBJECT, USER_ID, "linked identities", anon=WITH_ROW),
    "linked_identity.email": _use(SUBJECT, EMAIL, "linked identities", anon=WITH_ROW),
    "linked_identity.entra_object_id": _use(SUBJECT, USER_ID, "linked identities", anon=WITH_ROW),
    "linked_identity.moved_summary": _use(TEXT, FREE_TEXT, "what moved when two accounts were linked", anon=WITH_ROW),
    "linked_identity.requested_by_id": _use(STAFF, USER_ID),
    "linked_identity.approved_by_id": _use(STAFF, USER_ID),
    "linked_identity.unlinked_by_id": _use(STAFF, USER_ID),
    "link_code.user_id": _use(SUBJECT, USER_ID, "emailed link codes (only a keyed hash of the code is held)", anon=DELETE),
    "link_code.address": _use(SUBJECT, EMAIL, "emailed link codes", anon=WITH_ROW),
    # logs
    "activitylog.actor_user_id": _use(SUBJECT, USER_ID, "sign-in and activity rows", anon=KEEP),
    "activitylog.actor_email": _use(SUBJECT, EMAIL, "sign-in and activity rows", anon=SCRUB),
    "activitylog.ip": _use(SUBJECT, NETWORK, "sign-in and activity rows", anon=NULL),
    "activitylog.user_agent": _use(SUBJECT, NETWORK, "sign-in and activity rows", anon=NULL),
    "activitylog.detail": _use(TEXT, FREE_TEXT, "sign-in and activity rows", anon=SCRUB),
    "auditlog.actor_email": _use(SUBJECT, EMAIL, "audit entries the person made", anon=SCRUB),
    "auditlog.detail": _use(TEXT, FREE_TEXT, "audit entries", anon=SCRUB),
    "ticketevent.actor_email": _use(SUBJECT, EMAIL, "ticket events the person caused", anon=SCRUB),
    "ticketevent.from_value": _use(SUBJECT, EMAIL, "an event can name an address (a follower added)", anon=SCRUB),
    "ticketevent.to_value": _use(SUBJECT, EMAIL, "an event can name an address (a follower added)", anon=SCRUB),
    "ticketevent.detail": _use(TEXT, FREE_TEXT, "ticket events", anon=SCRUB),
    # tickets
    "ticket.requester_email": _use(SUBJECT, EMAIL, "tickets the person raised", anon=REPLACE),
    "ticket.subject": _use(TEXT, FREE_TEXT, "tickets the person raised", anon=SCRUB),
    "ticket.created_by_id": _use(STAFF, USER_ID),
    "ticket.assigned_agent_id": _use(STAFF, USER_ID),
    "ticket.resolved_by_id": _use(STAFF, USER_ID),
    "ticket.triaged_by_id": _use(STAFF, USER_ID),
    "ticket.escalated_by_id": _use(STAFF, USER_ID),
    "ticket.escalation_reason": _use(TEXT, FREE_TEXT, "agent-written, internal", anon=SCRUB),
    "ticket.resolution_notes": _use(TEXT, FREE_TEXT, "the resolution the requester is told", anon=SCRUB),
    "ticket.graph_conversation_id": _use(NOT_PERSON, LABEL, "the mail thread; cleared so mail from the old address cannot join it", anon=NULL),
    "inboundmail.conversation_id": _use(NOT_PERSON, LABEL, "the mail thread; cleared so mail from the old address cannot join it", anon=NULL),
    "inboundmail.internet_message_id": _use(NOT_PERSON, LABEL, "the Message-ID a reply points at; cleared for the same reason", anon=NULL),
    "teams_held_file.filename": _use(
        NOT_PERSON, LABEL, "a file name held (with the file) while the Teams bot asks which request a message is for; dropped when "
        "the question is answered or expires, and by anonymisation", anon=DELETE,
    ),
    "reply_held_file.filename": _use(
        NOT_PERSON, LABEL, "a file name held (with the file) while an agent checks the preview of an email to the requester; dropped "
        "when the preview is confirmed or expires, and by anonymisation", anon=DELETE,
    ),
    "reply_held_file.user_id": _use(STAFF, USER_ID),
    "message.channel": _use(NOT_PERSON, LABEL, "where the message came from or went (email, portal, teams, agent, system); names no one", anon=KEEP),
    "teamsintake.activity_key": _use(NOT_PERSON, LABEL, "one-way hash of an object id and a Teams activity id; names no one and holds no words"),
    "anonymised_requester.address_hash": _use(NOT_PERSON, LABEL, "keyed one-way hash of an anonymised address; the only trace of it, kept so a repeat is recognised"),
    "ticket.split_from_id": _use(NOT_PERSON, LABEL, "a ticket id"),
    "ticket.required_by": _use(NOT_PERSON, LABEL, "a date"),
    "ticketescalator.user_id": _use(STAFF, USER_ID),
    "ticketfollower.email": _use(SUBJECT, EMAIL, "tickets the person follows or is copied on", anon=DELETE),
    "ticketfollower.user_id": _use(SUBJECT, USER_ID, "tickets the person follows or is copied on", anon=WITH_ROW),
    "ticketfollower.added_by_email": _use(OTHER, EMAIL, "whoever added the follower", anon=SCRUB),
    "ticketanswer.value": _use(TEXT, FREE_TEXT, "the requester's form answers", anon=BLANK),
    "message.sender": _use(SUBJECT, NAME, "'Name' or 'Name <address>' as the mail or portal gave it", anon=REPLACE),
    "message.cc": _use(SUBJECT, EMAIL, "a comma-separated list", anon=REMOVE),
    "message.author_user_id": _use(SUBJECT, USER_ID, "messages written by an account (agents: listed by reference)", anon=KEEP),
    "message.body": _use(TEXT, FREE_TEXT, "the thread", anon=BLANK),
    "attachment.filename": _use(NOT_PERSON, LABEL, "a file name; the file goes with its message", anon=WITH_ROW),
    "attachment.uploaded_by_id": _use(SUBJECT, USER_ID, "files the person uploaded", anon=DELETE),
    # approvals and feedback
    "approvalvote.voter_id": _use(SUBJECT, USER_ID, "approval votes", anon=KEEP),
    "approvalvote.comment": _use(TEXT, FREE_TEXT, "approval votes", anon=BLANK),
    "ticketapproval.voters": _use(NOT_PERSON, LABEL, "a count"),
    "actiontoken.resolved_by_id": _use(STAFF, USER_ID, "the agent a feedback link is about"),
    "ticketfeedback.resolved_by_id": _use(STAFF, USER_ID, "the agent a rating is about"),
    "ticketfeedback.comment": _use(TEXT, FREE_TEXT, "the requester's own words (build-plan, 22 Oct 2026)", anon=BLANK),
    # notifications and mail
    "notification.user_id": _use(SUBJECT, USER_ID, "the person's bell", anon=DELETE),
    "notification.actor_email": _use(OTHER, EMAIL, "who caused the notification", anon=SCRUB),
    "notification.text": _use(TEXT, FREE_TEXT, "the person's bell", anon=SCRUB),
    "outboundemail.to_address": _use(SUBJECT, EMAIL, "emails sent to the person (subject and addresses, not the body)", anon=REPLACE),
    "outboundemail.cc": _use(SUBJECT, EMAIL, "a comma-separated list", anon=REMOVE),
    "outboundemail.subject": _use(TEXT, FREE_TEXT, "emails sent to the person", anon=SCRUB),
    "outboundemail.last_error": _use(TEXT, FREE_TEXT, "why a send failed; Graph's wording can name the recipient", anon=SCRUB),
    "outboundemail.body": _use(TEXT, FREE_TEXT, "emails sent to the person; not exported (see the PR decisions)", anon=BLANK),
    "inboundmail.sender": _use(SUBJECT, NAME, "mail the person sent to the shared mailbox (ids and outcome only)", anon=REPLACE),
    "inboundmail.to_addresses": _use(SUBJECT, EMAIL, "a comma-separated list", anon=REMOVE),
    "inboundmail.cc_addresses": _use(SUBJECT, EMAIL, "a comma-separated list", anon=REMOVE),
    "mspunmatchedmail.sender": _use(OTHER, NAME, "MSP staff", anon=SCRUB),
    "mspunmatchedmail.to_addresses": _use(OTHER, EMAIL, "MSP mail", anon=REMOVE),
    "mspunmatchedmail.cc_addresses": _use(OTHER, EMAIL, "MSP mail", anon=REMOVE),
    "mspunmatchedmail.resolved_by": _use(STAFF, EMAIL),
    "mspunmatchedmail.subject": _use(TEXT, FREE_TEXT, "MSP mail", anon=SCRUB),
    "mspunmatchedmail.body": _use(TEXT, FREE_TEXT, "MSP mail", anon=SCRUB),
    # configuration that names people or things
    "neverfollowaddress.email": _use(OTHER, EMAIL, "shared mailboxes and lists, not people", anon=DELETE),
    "neverfollowaddress.added_by": _use(STAFF, EMAIL),
    "thankscandidate.decided_by": _use(STAFF, EMAIL),
    "thankscandidate.normalised_text": _use(
        TEXT, FREE_TEXT, "a short thanks-like reply in lower case without punctuation (an address in it is only words, "
        "so it is not matched); one row stands for many replies, so it is kept and the readable sample is scrubbed",
    ),
    "thankscandidate.sample_text": _use(TEXT, FREE_TEXT, "a reply an agent reclosed; may quote a requester", anon=SCRUB),
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

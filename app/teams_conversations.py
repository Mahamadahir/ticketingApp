"""The Teams bot's conversation store (phase 8A): where to reach each person as the bot.

One row per person in `TeamsConversation`, keyed by their Entra object id, which Teams sends with every activity.
This module owns those rows and the audit actions about them (`teams_bot_installed`, `teams_bot_uninstalled`,
entity type `teams_conversation`). Later phases (8B messages to people, 8C approvals, 8D intake) read it through
`reference_for(session, user)` and send with app.bot_connector; nothing else writes the table.

People are matched by object id, through app/identity.py (`person_for_object_id`: the account that holds the id,
or the main person of a linked identity that holds it), never by display name. A person with no account here yet
still gets a row (`user_id` empty): the object id is what matters, and `reference_for` finds the row by the object
ids the person has, so it works once they have signed in. `user_id` is refreshed each time they message the bot.

Nothing here commits; the caller does.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app import bot_connector
from app.bot_connector import ConversationRef
from app.followers import InvalidEmail, normalise_email
from app.identity import normal_oid, object_ids_of, person_for_address, person_for_object_id
from app.person_data import is_placeholder_address
from app.models import TeamsConversation, User

log = logging.getLogger("app.teams_bot")

BOT_ACTOR = "teams-bot"  # the actor in the audit log when the person has no account here yet
INSTALLED = "teams_bot_installed"
UNINSTALLED = "teams_bot_uninstalled"
LINKED = "teams_bot_linked"
ENTITY = "teams_conversation"
MAX_FIELD = 512


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(person: Optional[User]) -> str:
    return person.email if person is not None else BOT_ACTOR


def save_reference(
    session: Session, oid: str, service_url: str, conversation_id: str, teams_user_id: Optional[str] = None
) -> tuple[Optional[TeamsConversation], bool]:
    """Remembers (or refreshes) where to reach the person with this object id. Returns (row, newly_stored); the row is
    None when the values are not usable (no object id, or values too long to be real). A first save is audited."""
    oid = normal_oid(oid)
    if oid is None or not service_url or not conversation_id or max(len(service_url), len(conversation_id)) > MAX_FIELD:
        return None, False
    person = person_for_object_id(session, oid)
    row = session.exec(select(TeamsConversation).where(TeamsConversation.entra_object_id == oid)).first()
    created = row is None
    if created:
        row = TeamsConversation(entra_object_id=oid, service_url=service_url, conversation_id=conversation_id)
    row.service_url, row.conversation_id = service_url, conversation_id
    row.teams_user_id = (teams_user_id or row.teams_user_id or None)
    if person is not None:
        row.user_id = person.id
    elif row.user_id is not None:
        # Matched by address at install (`link_by_address`): kept, unless that account now belongs to another object id.
        linked = session.get(User, row.user_id)
        if linked is None or (linked.entra_object_id and normal_oid(linked.entra_object_id) != oid):
            row.user_id = None
    row.updated_at = _now()
    session.add(row)
    if created:
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            # The install event and the first message arrived together: the other one stored it.
            row = session.exec(select(TeamsConversation).where(TeamsConversation.entra_object_id == oid)).first()
            if row is None:
                raise
            row.service_url, row.conversation_id = service_url, conversation_id
            session.add(row)
            return row, False
        log_action(session, _actor(person), ENTITY, row.id, INSTALLED, "Conversation with the bot stored")
    return row, created


def remove_reference(session: Session, oid: str) -> bool:
    """Forgets the person's conversation (they uninstalled the app). True when there was one; that is audited."""
    oid = normal_oid(oid)
    row = session.exec(select(TeamsConversation).where(TeamsConversation.entra_object_id == oid)).first() if oid else None
    if row is None:
        return False
    person = session.get(User, row.user_id) if row.user_id else person_for_object_id(session, oid)
    log_action(session, _actor(person), ENTITY, row.id, UNINSTALLED, "Conversation with the bot removed")
    session.delete(row)
    return True


def reference_for(session: Session, user: User) -> Optional[TeamsConversation]:
    """The person's conversation with the bot, or None when they have not installed the app. Found by any of their
    object ids (linked identities included), else by the account stored on the row."""
    oids = object_ids_of(session, user)
    rows = session.exec(
        select(TeamsConversation)
        .where((TeamsConversation.entra_object_id.in_(oids)) | (TeamsConversation.user_id == user.id))
        .order_by(TeamsConversation.updated_at.desc())
    ).all()
    return rows[0] if rows else None


def conversation_ref(row: TeamsConversation) -> ConversationRef:
    return ConversationRef(row.service_url, row.conversation_id)


def link_by_address(session: Session, oid: str) -> Optional[User]:
    """At install (8F): the app was added for someone whose object id is not on any account here. Reads their address from
    Teams (the bot's own token on the Bot Connector, `bot_connector.get_member`), matches it through app/identity.py
    (`person_for_address`: their address, or a linked one) and links the conversation to that account. Never by display
    name. Nothing is linked when Teams gives no usable address, describes a different object id, the account is inactive or
    anonymised, or the account already belongs to another object id (the same rule as an Entra sign-in). The object id itself
    is not written to the account: signing in does that. Returns the person linked, else None. Does not commit."""
    oid = normal_oid(oid)
    row = session.exec(select(TeamsConversation).where(TeamsConversation.entra_object_id == oid)).first() if oid else None
    if row is None or row.user_id is not None or not row.teams_user_id or person_for_object_id(session, oid) is not None:
        return None
    try:
        member = bot_connector.get_member(row.service_url, row.conversation_id, row.teams_user_id)
    except Exception as error:  # httpx, the token credential or a bad address: nothing is linked, the first message tries again
        log.warning("Teams bot: could not read the installer's address (%s)", type(error).__name__)
        return None
    member_oid = normal_oid(member.get("objectId") if isinstance(member.get("objectId"), str) else None)
    if member_oid is not None and member_oid != oid:
        return None
    raw = next((member[k] for k in ("email", "userPrincipalName") if isinstance(member.get(k), str) and member[k].strip()), "")
    try:
        address = normalise_email(raw)
    except InvalidEmail:
        return None
    person = person_for_address(session, address)
    if person is None or not person.is_active or is_placeholder_address(person.email):
        return None
    if person.entra_object_id and normal_oid(person.entra_object_id) != oid:
        return None
    row.user_id = person.id
    row.updated_at = _now()
    session.add(row)
    log_action(session, person.email, ENTITY, row.id, LINKED, "Conversation with the bot linked to the account with the address Teams gave")
    return person

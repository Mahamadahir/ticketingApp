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

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_action
from app.bot_connector import ConversationRef
from app.identity import normal_oid, object_ids_of, person_for_object_id
from app.models import TeamsConversation, User

BOT_ACTOR = "teams-bot"  # the actor in the audit log when the person has no account here yet
INSTALLED = "teams_bot_installed"
UNINSTALLED = "teams_bot_uninstalled"
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
    row.user_id = person.id if person is not None else None
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

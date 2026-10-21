"""Telling followers about an update they were not sent (4B-6).

When the requester or a follower adds to a ticket, by email or in the portal, everyone else who
follows it should hear. Anyone who was on that mail's To or Cc already has it, so only the others
are told: one email each, queued through the outbox as `follower_update`. Who they are is worked out
in app/followers.uncopied_recipients; whether the text may go out, whether the person is still a
follower and whether they have opted out is asked again when it is sent (app/mail.py). Internal
(agent) followers are told in the app, not here.
"""

from typing import Iterable

from sqlmodel import Session

from app.followers import uncopied_recipients
from app.models import Message, OutboundEmail, Ticket
from app.outbox import queue_follower_update


def queue_follower_updates(
    session: Session, ticket: Ticket, message: Message, sender: str, copied: Iterable[str] = ()
) -> list[OutboundEmail]:
    """Queues the update for everyone who was not sent it. `sender` is the address of whoever wrote
    the message; `copied` is the To and Cc of the mail it came in (none for a portal comment). Does
    not commit. A replay queues nothing more."""
    queued = []
    for address in uncopied_recipients(session, ticket, sender, copied):
        row = queue_follower_update(session, ticket, message, address)
        if row is not None:
            queued.append(row)
    return queued

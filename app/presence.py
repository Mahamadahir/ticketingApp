"""Out-of-office detection (phase 2E).

The worker asks Microsoft Graph for each agent's presence every few minutes and keeps the answer on
the user (`out_of_office`, `out_of_office_checked_at`). Graph's `outOfOfficeSettings.isOutOfOffice`
covers Outlook automatic replies, Teams scheduled out of office and calendar events marked out of
office. Everything else reads the stored flag through `is_out_of_office`, so no request waits on
Graph.

An out-of-office agent is labelled in the assignee pickers and left out of team-wide notifications
(`notifiable_members`). They can still be picked by someone who knows better, and still get a
notification meant for them personally. A reading older than STALE_AFTER is ignored, so if the job
or Graph stops working nobody stays "out of office" for ever.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlmodel import Session, select

from app.models import User, UserRole

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=3)  # the job runs every 15 minutes; a reading this old is not trusted
CHECK_EVERY_SECONDS = 15 * 60


def is_out_of_office(user: User, now: Optional[datetime] = None) -> bool:
    if not user.out_of_office or user.out_of_office_checked_at is None:
        return False
    checked = user.out_of_office_checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - checked < STALE_AFTER


def notifiable_members(users: list[User], now: Optional[datetime] = None) -> list[User]:
    """Who a message to a whole team should reach: the team's agents who are not out of office."""
    return [user for user in users if not is_out_of_office(user, now)]


def agent_label(user: User, now: Optional[datetime] = None) -> str:
    return f"{user.display_name} (out of office)" if is_out_of_office(user, now) else user.display_name


def refresh_presence(
    session: Session,
    object_id_for: Optional[Callable[[str], Optional[str]]] = None,
    presences: Optional[Callable[[list[str]], list[dict]]] = None,
    now: Optional[datetime] = None,
) -> int:
    """Reads every agent's presence and stores it. Returns how many readings came back. An agent
    Graph does not know is left alone. Does not commit."""
    from app.graph_client import graph_client  # resolved now, so tests can swap the functions

    object_id_for = object_id_for or graph_client.object_id_for
    presences = presences or graph_client.presences
    now = now or datetime.now(timezone.utc)
    agents = session.exec(
        select(User).where(User.role.in_((UserRole.AGENT, UserRole.SUPER_ADMIN))).order_by(User.id)
    ).all()
    for agent in agents:
        if agent.entra_object_id is None:
            try:
                agent.entra_object_id = object_id_for(agent.email)
            except Exception as error:
                logger.warning("presence: could not look up an agent in the directory (%s)", type(error).__name__)
            session.add(agent)
    known = {agent.entra_object_id.lower(): agent for agent in agents if agent.entra_object_id}
    if not known:
        return 0
    readings = presences(list(known))
    read = set()
    for reading in readings:
        agent = known.get(str(reading.get("id") or "").lower())
        if agent is None:
            continue
        read.add(agent.id)
        agent.out_of_office = bool((reading.get("outOfOfficeSettings") or {}).get("isOutOfOffice"))
        agent.out_of_office_checked_at = now
        session.add(agent)
    for agent in known.values():
        if agent.id not in read and agent.out_of_office:
            agent.out_of_office = False  # Graph no longer reports them: do not keep the old reading alive
            session.add(agent)
    return len(readings)

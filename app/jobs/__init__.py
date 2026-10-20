"""The jobs the background worker runs (phase 2C).

A job is a name, how often it runs, and a function that takes a database session. The worker
(app/worker.py) opens the session, calls the function, commits if it returns and rolls back if it
raises, and writes a heartbeat line when it succeeds. Jobs therefore never commit or log their own
success; they just do their work against the session they are given.

To add a job, write its function (in its own module under app/jobs/ once there are several),
add a Job to JOBS, and test it by calling the function with a test session. Jobs must be safe to
run twice in a row and to miss a run: the worker is one process that restarts, and nothing queues
a run it missed.
"""

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text
from sqlmodel import Session

from app.config import settings
from app.mail import deliver_mail
from app.presence import CHECK_EVERY_SECONDS, refresh_presence
from app.subscriptions import CATCH_UP_EVERY_SECONDS, SUBSCRIPTION_EVERY_SECONDS, catch_up, keep_subscription_alive
from app.notifications import drop_due_for_teams, hand_to_teams
from app.teams import deliver_pending, queue_notification


@dataclass(frozen=True)
class Job:
    name: str  # unique; appears in the heartbeat line the monitoring looks for
    every_seconds: int
    run: Callable[[Session], None]


def heartbeat(session: Session) -> None:
    """Proves the worker is alive and can reach the database. Every other job's heartbeat says that
    job ran; this one keeps the signal coming when no other job is due."""
    session.exec(text("select 1"))


def teams_posts(session: Session) -> None:
    """Sends the Teams posts that are waiting (app/teams.py). With no webhook URL it sends nothing and
    logs a warning if posts are waiting."""
    deliver_pending(session)


def teams_copies(session: Session) -> None:
    """Queues the Teams copy of each notification that is due (app.notifications.hand_to_teams); the
    teams-posts job then sends them. With the switch off, due copies are dropped, not left to pile up
    and arrive in a flood the day it is switched on."""
    if settings.teams_notifications_enabled:
        hand_to_teams(session, queue_notification)
    else:
        drop_due_for_teams(session)


def presence(session: Session) -> None:
    """Reads agents' out-of-office status from Graph (app/presence.py). Does nothing unless switched on
    and Graph is configured. If Graph fails the job fails and the worker rolls back; the stored
    readings go stale and are then ignored."""
    if settings.presence_enabled and settings.graph_configured:
        refresh_presence(session)


def mail_out(session: Session) -> None:
    """Sends the emails that are waiting (app/mail.py). With MAIL_SENDING off it does nothing. It
    commits after each email itself, so one that went is recorded even if a later one fails."""
    deliver_mail(session)


def graph_subscription(session: Session) -> None:
    """Keeps the Graph mail subscription alive (app/subscriptions.py): creates it, renews it before it
    runs low, recreates it when Microsoft lost it, and acts on the lifecycle notifications. Does nothing
    unless switched on. On failure it records why (shown at Admin > Mail intake) and the job fails."""
    if settings.graph_subscription_enabled:
        outcome = keep_subscription_alive(session)
        if outcome.sync_wanted and settings.mail_catch_up_enabled:
            catch_up(session)  # a missed or removed subscription may have cost us mail


def mail_catch_up(session: Session) -> None:
    """Finds mail the webhook missed with a delta query on the inbox and hands the ids to inbound mail
    (app/subscriptions.py). Does nothing unless switched on."""
    if settings.mail_catch_up_enabled:
        catch_up(session)


TEAMS_POSTS_EVERY_SECONDS = 60
MAIL_OUT_EVERY_SECONDS = 30

JOBS: list[Job] = [
    Job("heartbeat", settings.worker_heartbeat_seconds, heartbeat),
    Job("teams-copies", TEAMS_POSTS_EVERY_SECONDS, teams_copies),
    Job("teams-posts", TEAMS_POSTS_EVERY_SECONDS, teams_posts),
    Job("presence", CHECK_EVERY_SECONDS, presence),
    Job("mail-out", MAIL_OUT_EVERY_SECONDS, mail_out),
    Job("graph-subscription", SUBSCRIPTION_EVERY_SECONDS, graph_subscription),
    Job("mail-catch-up", CATCH_UP_EVERY_SECONDS, mail_catch_up),
]

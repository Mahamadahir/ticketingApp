"""The one place the Jinja2 environment is built, so every router renders the same way."""

from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import Request
from jinja2 import pass_context
from sqlmodel import Session
from fastapi.templating import Jinja2Templates

from app.backdating import FORMAT as MOMENT_FORMAT
from app.database import engine
from app.forms import MAX_MESSAGE_LENGTH
from app.presence import agent_label
from app.time_zones import default_zone, format_date, format_moment, valid_zone, zone_for_user


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _htmx_partial(request: Request) -> dict:
    """An htmx form swaps the response into <main>, so a full page rendered for it must come
    back as its content block only. base.html reads this and leaves out the header and scripts.
    Every full page an htmx request gets today targets <main>; htmx sends no HX-Target for it
    because <main> has no id, so the header alone is the rule."""
    return {"htmx_partial": is_htmx(request)}


templates = Jinja2Templates(directory="app/templates", context_processors=[_htmx_partial])
templates.env.globals["max_message_length"] = MAX_MESSAGE_LENGTH
templates.env.globals["agent_label"] = agent_label  # a name, plus "(out of office)" when they are


@pass_context
def _viewer_zone(context) -> str:
    """The viewer's time zone. For a signed-in person `get_current_user` has already worked it out with the
    request's own database session (request.state.viewer_zone), so rendering opens no second connection.
    Only a page with no signed-in user (or a render outside a request) falls back to reading it here."""
    request = context.get("request")
    state = getattr(request, "state", None)
    cached = getattr(state, "viewer_zone", None)
    if cached:
        return cached
    user = context.get("user") or getattr(state, "user", None)
    zone = valid_zone(user.time_zone) if user is not None else None
    if zone is None:
        with Session(engine) as session:
            zone = zone_for_user(session, user) if user is not None else default_zone(session)
    return zone


@pass_context
def _local_time(context, moment, seconds: bool = False) -> str:
    """{{ moment|local_time }}: the moment in the viewer's time zone, with the zone shown."""
    return format_moment(moment, _viewer_zone(context), seconds=seconds) if moment is not None else ""


@pass_context
def _local_date(context, moment) -> str:
    """{{ moment|local_date }}: only the date, in the viewer's time zone."""
    return format_date(moment, _viewer_zone(context)) if moment is not None else ""


@pass_context
def _local_input(context, moment) -> str:
    """{{ moment|local_input }}: the value of a datetime-local box, in the viewer's time zone."""
    if moment is None:
        return ""
    zone = ZoneInfo(_viewer_zone(context))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(zone).strftime(MOMENT_FORMAT)


templates.env.filters["local_time"] = _local_time
templates.env.filters["local_input"] = _local_input
templates.env.filters["local_date"] = _local_date
templates.env.globals["viewer_zone"] = _viewer_zone

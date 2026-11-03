"""The one place the Jinja2 environment is built, so every router renders the same way."""

from fastapi import Request
from jinja2 import pass_context
from sqlmodel import Session
from fastapi.templating import Jinja2Templates

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
    """The signed-in viewer's time zone, worked out once per request. The person's own zone needs no
    lookup; otherwise (no zone yet, or nobody signed in) the mailbox reading or the default is read."""
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
    if state is not None:
        state.viewer_zone = zone
    return zone


@pass_context
def _local_time(context, moment, seconds: bool = False) -> str:
    """{{ moment|local_time }}: the moment in the viewer's time zone, with the zone shown."""
    return format_moment(moment, _viewer_zone(context), seconds=seconds) if moment is not None else ""


@pass_context
def _local_date(context, moment) -> str:
    """{{ moment|local_date }}: only the date, in the viewer's time zone."""
    return format_date(moment, _viewer_zone(context)) if moment is not None else ""


templates.env.filters["local_time"] = _local_time
templates.env.filters["local_date"] = _local_date
templates.env.globals["viewer_zone"] = _viewer_zone

"""The one place the Jinja2 environment is built, so every router renders the same way."""

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.forms import MAX_MESSAGE_LENGTH
from app.presence import agent_label


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

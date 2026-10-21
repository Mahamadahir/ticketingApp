"""The one place the Jinja2 environment is built, so every router renders the same way."""

from fastapi import Request
from fastapi.templating import Jinja2Templates


def _htmx_partial(request: Request) -> dict:
    """An htmx form swaps the response into <main>, so a full page rendered for it must come
    back as its content block only. base.html reads this and leaves out the header and scripts."""
    return {"htmx_partial": request.headers.get("HX-Request") == "true"}


templates = Jinja2Templates(directory="app/templates", context_processors=[_htmx_partial])

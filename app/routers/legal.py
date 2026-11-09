"""The privacy and terms pages the Teams app manifest points at (phase 8F). No sign-in: anyone with the address can read them.

The text is an admin-edited template like the emails (Admin > Email templates: `legal_privacy`, `legal_terms`), so the
wording is changed without a deploy; until it is written it is a placeholder that says so. Served by the public app
(APP_ROLE=public) and `all`, rate limited per caller address like the other public pages.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.database import get_session
from app.email_templates import current_text
from app.rate_limit import LEGAL, rate_limited
from app.templating import templates

router = APIRouter(prefix="/legal", tags=["legal"], dependencies=[Depends(rate_limited(LEGAL))])

PAGES = {
    "privacy": ("legal_privacy", "Privacy"),
    "terms": ("legal_terms", "Terms of use"),
}


def paragraphs(text: str) -> list[str]:
    """The text as paragraphs (separated by a blank line). The template escapes each one."""
    return [" ".join(block.split()) for block in text.split("\n\n") if block.strip()]


@router.get("/{page}", response_class=HTMLResponse)
def legal_page(page: str, request: Request, session: Session = Depends(get_session)):
    if page not in PAGES:
        raise HTTPException(status_code=404)
    key, title = PAGES[page]
    _, body = current_text(session, key)
    return templates.TemplateResponse(request, "legal_page.html", {"title": title, "paragraphs": paragraphs(body), "user": None})

"""An agent's own feedback (phase 4G). Admins see all of it under Admin > Feedback instead."""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.auth import require_roles
from app.database import get_session
from app.feedback import feedback_page
from app.forms import optional_int
from app.models import User
from app.notifications import AGENT_ROLES
from app.templating import templates

router = APIRouter(tags=["feedback"])


@router.get("/my-feedback", response_class=HTMLResponse)
def my_feedback(
    request: Request,
    rating: Optional[str] = None,
    page: int = 1,
    user: User = Depends(require_roles(*AGENT_ROLES)),
    session: Session = Depends(get_session),
):
    """Always the signed-in agent's own: there is no agent parameter to change. (An admin's own
    page is also only their own; the all-feedback view is Admin > Feedback.)"""
    chosen = optional_int(rating)
    entries = feedback_page(session, user, page, {"rating": rating or ""}, agent_id=user.id, rating=chosen)
    return templates.TemplateResponse(
        request, "my_feedback.html", {"page": entries, "rating": chosen, "user": user},
    )

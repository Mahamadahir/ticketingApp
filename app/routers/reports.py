"""Reports (issue #173): a menu, one page per report with a date range and filters, and a CSV download of
the same filters. Who may open them is the `view_reports` permission; which tickets they count is decided by
app/visibility.py (via app/report_kit.py), never here."""

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session

from app.auth import require_permission
from app.database import get_session
from app.models import TicketSource, TicketType, User
from app.permissions import VIEW_REPORTS
from app.report_kit import (
    PRIORITIES,
    ReportInputError,
    audit_download,
    category_choices,
    csv_text,
    parse_filters,
    team_choices,
)
from app.reports import REPORTS, ReportDef
from app.templating import templates
from app.visibility import access_context

router = APIRouter(prefix="/reports", tags=["reports"])
require_reports = require_permission(VIEW_REPORTS)


def _definition(key: str) -> ReportDef:
    definition = REPORTS.get(key)
    if definition is None:
        raise HTTPException(status_code=404, detail="No such report")
    return definition


@router.get("", response_class=HTMLResponse)
def menu(request: Request, user: User = Depends(require_reports)):
    return templates.TemplateResponse(request, "reports_menu.html", {"reports": REPORTS.values(), "user": user})


@router.get("/{key}", response_class=HTMLResponse)
def show(
    key: str,
    request: Request,
    user: User = Depends(require_reports),
    session: Session = Depends(get_session),
):
    definition = _definition(key)
    levels = access_context(session, user)
    context = {
        "definition": definition, "user": user, "report": None, "error": None, "filters": None,
        "teams": team_choices(session, user, levels), "categories": category_choices(session),
        "types": list(TicketType), "sources": list(TicketSource), "priorities": PRIORITIES,
        "values": dict(request.query_params), "csv_link": None,
    }
    status = 200
    try:
        filters = parse_filters(session, user, request.query_params)
    except ReportInputError as error:
        context["error"], status = str(error), 400
    else:
        context["filters"] = filters
        context["values"] = filters.as_params()
        context["report"] = definition.build(session, user, levels, filters, datetime.now(timezone.utc))
        context["csv_link"] = f"/reports/{key}/csv?{urlencode(filters.as_params())}"
    return templates.TemplateResponse(request, "reports.html", context, status_code=status)


@router.get("/{key}/csv")
def download(
    key: str,
    request: Request,
    user: User = Depends(require_reports),
    session: Session = Depends(get_session),
):
    definition = _definition(key)
    levels = access_context(session, user)
    try:
        filters = parse_filters(session, user, request.query_params)
    except ReportInputError as error:
        raise HTTPException(status_code=400, detail=str(error))
    report = definition.build(session, user, levels, filters, datetime.now(timezone.utc))
    audit_download(session, user, key, filters)
    session.commit()
    name = f"report-{key}-{filters.from_day}-{filters.to_day}.csv"
    return Response(
        csv_text(report, filters),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from app.database import get_session
from app.subscriptions import health_warnings

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
def health(session: Session = Depends(get_session)):
    """Unauthenticated so the load balancer and external monitors can poll
    it. Returns 503 when the database can't be reached so the instance is
    marked unhealthy.
    """
    try:
        session.exec(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Health check could not reach the database")
        return JSONResponse({"status": "unhealthy", "database": "unreachable"}, status_code=503)
    body = {"status": "ok", "database": "ok"}
    # Mail intake trouble is a warning, never a failure: the app still works, and a load balancer
    # must not take it out of service because Graph is unhappy.
    try:
        warnings = health_warnings(session)
    except SQLAlchemyError:
        session.rollback()
        warnings = []
    if warnings:
        body["warnings"] = warnings
    return body

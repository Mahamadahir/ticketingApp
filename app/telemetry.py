"""Application Insights, switched on by APPLICATIONINSIGHTS_CONNECTION_STRING (the Bicep sets it).

Without the connection string nothing is sent, so development and the tests are unaffected. With
it, requests, dependencies (database, Graph, Blob), exceptions and log lines go to the workspace.

The emailed approval link carries a working credential in its path (/approvals/{token}). Request
telemetry records the address, so those requests are excluded from it: the page still works and
is still logged by uvicorn, whose access log is redacted (app/log_redaction.py), but
Application Insights never receives the token.
"""

import logging
import os
from typing import Callable, Optional

from app.config import settings

log = logging.getLogger(__name__)

# Regular expressions, matched anywhere in the request address, that request telemetry skips.
EXCLUDED_URLS = ("/approvals/", "/feedback/", "/reopen/")

_EXCLUSION_VARIABLES = ("OTEL_PYTHON_EXCLUDED_URLS", "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")


def _exclude_credentials_from_requests() -> None:
    """Adds the patterns to whatever the environment already excludes (a comma-separated list)."""
    for name in _EXCLUSION_VARIABLES:
        present = [part for part in os.environ.get(name, "").split(",") if part.strip()]
        os.environ[name] = ",".join(present + [pattern for pattern in EXCLUDED_URLS if pattern not in present])


def start_telemetry(configure: Optional[Callable[..., None]] = None) -> bool:
    """Starts telemetry if it is configured. Returns whether it started. Never raises: losing
    telemetry must not stop the app or the worker. Call it before the web app is built, so the
    framework is instrumented. `configure` is for tests."""
    connection_string = settings.applicationinsights_connection_string
    if not connection_string:
        return False
    # Before configuring: the instrumentation reads the exclusions when it starts.
    _exclude_credentials_from_requests()
    try:
        if configure is None:
            from azure.monitor.opentelemetry import configure_azure_monitor as configure
        configure(connection_string=connection_string)
    except Exception:
        log.warning("Application Insights could not be started; carrying on without telemetry", exc_info=True)
        return False
    return True

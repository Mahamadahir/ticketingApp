"""Keeps credentials that travel in an address out of the logs (phase 3A).

The emailed approval link carries its token in the path (/approvals/<token>), and the token is
what proves who is answering. uvicorn's access log writes the path of every request, so without
this a working link would sit in the container logs. The filter rewrites the token to {token}
before the record is written. Azure's own ingress logs and Application Insights record the path
too; those are checked on staging (docs/build-plan.md).
"""

import logging
import re

_TOKEN_IN_PATH = re.compile(r"(/approvals/)[^/?#\s\"']+")


def _redacted(value):
    return _TOKEN_IN_PATH.sub(r"\1{token}", value) if isinstance(value, str) else value


class ApprovalTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redacted(arg) for arg in record.args)
        record.msg = _redacted(record.msg)
        return True


def redact_approval_tokens() -> None:
    """Adds the filter to uvicorn's access log, once however many times this is called."""
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, ApprovalTokenFilter) for f in log.filters):
        log.addFilter(ApprovalTokenFilter())

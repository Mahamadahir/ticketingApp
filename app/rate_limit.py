"""Rate limiting for the routes open to the internet (phase 3A).

A sliding window per caller address, kept in memory. Each replica counts for itself, so with two
public replicas a caller can do up to twice the limit; the limit is there to make guessing an
emailed approval link impractical and to blunt a flood, not to be exact. Behind the ingress the
address is the real client only because uvicorn trusts forwarded headers from that subnet alone
(FORWARDED_ALLOW_IPS), the same as the activity log relies on.

A limit is a dependency (`rate_limited`), so a route says which bucket it counts against and a
refused request never reaches the handler or the database.
"""

import threading
import time
from collections import deque
from typing import Callable, Optional

from fastapi import HTTPException, Request

from app.config import settings

WINDOW_SECONDS = 60
MAX_TRACKED_CALLERS = 10_000  # memory bound: past this, callers with no recent hits are dropped first


class RateLimiter:
    def __init__(self, limit: Callable[[], int], window_seconds: int = WINDOW_SECONDS) -> None:
        self._limit = limit  # read on each hit, so a changed setting applies without a restart
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: Optional[float] = None) -> Optional[int]:
        """Counts a request. Returns None when it is allowed, or the seconds to wait when it is not."""
        now = time.monotonic() if now is None else now
        limit = self._limit()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if limit > 0 and len(hits) >= limit:
                return max(1, int(self._window - (now - hits[0])) + 1)
            hits.append(now)
            if len(self._hits) > MAX_TRACKED_CALLERS:
                self._forget_idle(now)
            return None

    def _forget_idle(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= self._window]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def rate_limited(limiter: RateLimiter) -> Callable[[Request], None]:
    """A dependency that refuses the request with 429 and Retry-After once the caller is over the limit."""

    def check(request: Request) -> None:
        caller = request.client.host if request.client else "unknown"
        wait = limiter.hit(caller)
        if wait is not None:
            raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": str(wait)})

    return check


# The buckets the public routes count against (limits are settings, read on each request).
APPROVALS = RateLimiter(lambda: settings.rate_limit_approvals_per_minute)
ACTION_LINKS = RateLimiter(lambda: settings.rate_limit_action_links_per_minute)
WEBHOOK = RateLimiter(lambda: settings.rate_limit_webhook_per_minute)
BREAK_GLASS = RateLimiter(lambda: settings.rate_limit_break_glass_per_minute)
EMERGENCY = RateLimiter(lambda: settings.rate_limit_emergency_per_minute)
LEGAL = RateLimiter(lambda: settings.rate_limit_legal_per_minute)  # the privacy and terms pages (app/routers/legal.py)
TEAMS_BOT = RateLimiter(lambda: settings.rate_limit_teams_bot_per_minute)
TEAMS_PERSON = RateLimiter(lambda: settings.rate_limit_teams_person_per_minute)  # one person's messages to the bot (app/teams_intake.py)
TEAMS_PERSON_NOTICE = RateLimiter(lambda: 1)  # and the "slow down" answer to them: at most one a minute

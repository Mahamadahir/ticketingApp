"""The only code that talks to Exchange about scheduled mailbox changes (#307; the real executor is #308).

Everything else (app/mailbox_changes.py, the worker job) works with `MailboxChange` rows and asks an *executor* to make,
remove or look at the change. An executor never touches the database and never raises for an Exchange problem: it
answers with an `ExecutorResult` whose `outcome` says what happened, so the caller can decide what to do next.

THE INTERFACE (what #308 implements)
------------------------------------

    class MailboxExecutor(ABC):
        def apply(self, change: MailboxChange) -> ExecutorResult
        def remove(self, change: MailboxChange) -> ExecutorResult
        def read(self, change: MailboxChange) -> ExecutorResult

An executor reads only these fields of `change`: `kind` ("forwarding", "full_access" or "send_as"), `owner_object_id` and
`owner_address` (whose mailbox), `target_object_id` and `target_address` (who it is shared with, or where forwarding goes),
and `keep_copy` (forwarding only: leave a copy in the owner's mailbox). The object ids are the Entra ids the app checked when
the request was made; an executor re-checks that the object id still belongs to the address if it can, and answers
`permanent` if it does not. Times are the app's business, not the executor's.

`ExecutorResult` (frozen dataclass):

    outcome       one of the five OUTCOME_* words below
    found         True: the read-back shows the change IS in Exchange as the row describes it; False: it shows it is NOT
                  (for a forwarding change this includes the keep-a-copy setting); None: nothing could be read
    found_detail  a short plain-text account of what the read-back showed ("ForwardingSmtpAddress is set to ..."), kept in the
                  internal note; None when there is nothing to say
    error         the raw Exchange / automation text, for the internal note only: it is NEVER shown to the requester
    job_id        the id of the automation job (or any trace id) that did the work, for the internal note; None if none

The outcomes:

    succeeded   the call did what was asked AND the read-back confirms it. Already set (apply) or already removed (remove)
                counts as success: both operations are idempotent. For `read`, succeeded means "the read worked"; `found`
                says what it found.
    retryable   a temporary failure (throttling, the service is down, a transient automation error). Nothing is known to have
                changed; the caller retries with backoff.
    permanent   Exchange or the automation refused for a reason that trying again will not fix (the mailbox or the recipient
                does not exist, the permission is missing, the object id no longer matches the address). Final.
    mismatch    Exchange accepted the call but the read-back does not show the change (apply: it is not there; remove: it is
                still there). Final: the app does not guess.
    no_answer   the call got no usable answer (timeout, the job vanished) so it is not known whether the change happened. The
                caller then calls `read`: if that shows the change already in place it counts as success, otherwise it retries
                as `retryable`.

An executor call must return within the lease the job gives each change (the `scheduled_job_lease_minutes` setting, 10 by
default): a row whose call has not been answered by then is taken up again by the next run, which calls the executor a second
time, and the first answer is dropped. So a call that waits for an automation job gives up (answers `no_answer`) before that.

`apply` and `remove` read the change back before they return (that read-back is what makes `succeeded` and `mismatch`
possible). `read` makes no change. All three may be called twice for one change (after a crash, or by Try again), and must be
safe to: apply on something already set succeeds, remove on something already removed succeeds.

CHOOSING AN EXECUTOR
--------------------

`get_executor()` returns the one the `MAILBOX_EXECUTOR` environment value names: `fake` (the default; local development and
tests; `FakeExecutor` below keeps its state in memory) or `azure` (#308). The worker refuses to start with `fake` anywhere but
the dev environment (`app.config.unsafe_setting_errors`). #308 plugs in by replacing the `azure` branch of `make_executor`;
nothing else changes. Only the worker process calls `get_executor()`.
"""

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.models import MailboxChange

log = logging.getLogger("app.worker")

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_RETRYABLE = "retryable"
OUTCOME_PERMANENT = "permanent"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_NO_ANSWER = "no_answer"
OUTCOMES = (OUTCOME_SUCCEEDED, OUTCOME_RETRYABLE, OUTCOME_PERMANENT, OUTCOME_MISMATCH, OUTCOME_NO_ANSWER)

OPERATIONS = ("apply", "remove", "read")


@dataclass(frozen=True)
class ExecutorResult:
    outcome: str
    found: Optional[bool] = None
    found_detail: Optional[str] = None
    error: Optional[str] = None
    job_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"Unknown mailbox executor outcome {self.outcome!r}")


class MailboxExecutor(ABC):
    """See the module docstring for what each method must do and answer."""

    @abstractmethod
    def apply(self, change: MailboxChange) -> ExecutorResult:
        """Makes the change in Exchange, then reads it back."""

    @abstractmethod
    def remove(self, change: MailboxChange) -> ExecutorResult:
        """Takes the change out of Exchange, then reads it back."""

    @abstractmethod
    def read(self, change: MailboxChange) -> ExecutorResult:
        """Reads what Exchange holds for this change and changes nothing."""


class ExecutorNotAvailable(RuntimeError):
    """The configured executor cannot be built (the real one is #308). The worker job treats it as a failure of the run, not of the change."""


def _key(change: MailboxChange) -> tuple:
    return (change.kind, change.owner_address.strip().lower(), change.target_address.strip().lower(), bool(change.keep_copy))


class FakeExecutor(MailboxExecutor):
    """Exchange in memory, for local development and tests. `held` is the set of changes it holds. It does what the real one is
    asked to do (apply stores, remove takes out, both idempotent) unless told to fail: `script(operation, outcome, ...)` queues
    a failure for the next call(s) of `apply`, `remove` or `read`.

    `effect` says whether a scripted call still changes what is held: by default a retryable, permanent or mismatch answer
    changes nothing (a mismatch is "accepted but not stored"), and a no_answer changes nothing; pass `effect=True` to
    make a no_answer (or retryable) call change it anyway, which is how a call that worked but whose answer was lost is made.
    `calls` records every call as (operation, kind, owner, target)."""

    def __init__(self) -> None:
        self.held: set[tuple] = set()
        self.calls: list[tuple] = []
        self._scripted: dict[str, deque] = {op: deque() for op in OPERATIONS}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.held.clear()
            self.calls.clear()
            for queue in self._scripted.values():
                queue.clear()

    def script(
        self, operation: str, outcome: str, *, times: int = 1, error: Optional[str] = None, effect: bool = False
    ) -> None:
        if operation not in OPERATIONS:
            raise ValueError(f"Unknown operation {operation!r}")
        if outcome == OUTCOME_SUCCEEDED:
            raise ValueError("Script a failure; a call that is not scripted succeeds")
        ExecutorResult(outcome)  # validates the word
        with self._lock:
            for _ in range(times):
                self._scripted[operation].append((outcome, error, effect))

    def holds(self, change: MailboxChange) -> bool:
        return _key(change) in self.held

    def _result(self, change: MailboxChange, outcome: str, error: Optional[str] = None) -> ExecutorResult:
        present = self.holds(change)
        return ExecutorResult(
            outcome, found=present,
            found_detail=f"{change.kind} to {change.target_address} is {'in place' if present else 'not in place'}",
            error=error, job_id=f"fake-{uuid.uuid4().hex[:8]}",
        )

    def _call(self, operation: str, change: MailboxChange, wanted: bool) -> ExecutorResult:
        """`wanted`: whether the operation leaves the change held (apply) or not (remove)."""
        with self._lock:
            self.calls.append((operation, change.kind, change.owner_address, change.target_address))
            scripted = self._scripted[operation].popleft() if self._scripted[operation] else None
            if scripted is None:
                if wanted:
                    self.held.add(_key(change))
                else:
                    self.held.discard(_key(change))
                return self._result(change, OUTCOME_SUCCEEDED)
            outcome, error, effect = scripted
            if effect and operation != "read":
                if wanted:
                    self.held.add(_key(change))
                else:
                    self.held.discard(_key(change))
            if outcome == OUTCOME_RETRYABLE:
                return ExecutorResult(outcome, error=error or "The service is busy (fake)", job_id=f"fake-{uuid.uuid4().hex[:8]}")
            if outcome == OUTCOME_PERMANENT:
                return ExecutorResult(outcome, error=error or "The recipient was not found (fake)", job_id=f"fake-{uuid.uuid4().hex[:8]}")
            if outcome == OUTCOME_NO_ANSWER:
                return ExecutorResult(outcome, error=error or "The job did not answer (fake)", job_id=f"fake-{uuid.uuid4().hex[:8]}")
            return self._result(change, outcome, error or "The read-back does not show the change (fake)")  # mismatch

    def apply(self, change: MailboxChange) -> ExecutorResult:
        return self._call("apply", change, wanted=True)

    def remove(self, change: MailboxChange) -> ExecutorResult:
        return self._call("remove", change, wanted=False)

    def read(self, change: MailboxChange) -> ExecutorResult:
        with self._lock:
            self.calls.append(("read", change.kind, change.owner_address, change.target_address))
            scripted = self._scripted["read"].popleft() if self._scripted["read"] else None
            if scripted is None:
                return self._result(change, OUTCOME_SUCCEEDED)
            outcome, error, _effect = scripted
            return ExecutorResult(outcome, error=error or "Could not read (fake)", job_id=f"fake-{uuid.uuid4().hex[:8]}")


_executor: Optional[MailboxExecutor] = None
_executor_lock = threading.Lock()


def make_executor(kind: str) -> MailboxExecutor:
    """Builds the executor `MAILBOX_EXECUTOR` names. #308 replaces the `azure` branch."""
    if kind == "fake":
        return FakeExecutor()
    if kind == "azure":
        raise ExecutorNotAvailable("MAILBOX_EXECUTOR=azure is not built yet (#308)")
    raise ExecutorNotAvailable(f"Unknown MAILBOX_EXECUTOR {kind!r}")


def get_executor() -> MailboxExecutor:
    """The process's executor, built once from `settings.mailbox_executor`."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = make_executor(settings.mailbox_executor)
        return _executor


def set_executor(executor: Optional[MailboxExecutor]) -> None:
    """Puts an executor in place (tests), or None to build it again from the configuration."""
    global _executor
    with _executor_lock:
        _executor = executor

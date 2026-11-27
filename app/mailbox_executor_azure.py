"""The real route to Exchange for mailbox changes: an Azure Automation account runs a fixed runbook.

The app never holds Exchange rights. `AzureAutomationExecutor` starts one runbook job per call through the Azure
Resource Manager REST API, signed in as the worker's managed identity, polls the job until it ends, reads the
JSON the runbook wrote (infra/runbooks/Common.ps1 describes it) and maps it to an `ExecutorResult`. The runbooks
connect to Exchange Online as the Automation account's own identity; this process never sees those rights.

Only the runbooks in `RUNBOOKS` can be started: the job request is built from a change's fields, never from text a
caller supplies, and `AzureAutomationExecutor` refuses any other runbook name. The worker's role on the account
(Automation Job Operator) cannot be narrowed to single runbooks (Azure Automation allows role assignments only at the
account scope), so this allow-list and the account holding nothing else are the limit (infra/README.md).

Azure-only, unproved until staging: the ARM calls (job create, status, output), the role being enough, and the
runbooks themselves. The tests use recorded ARM responses (tests/test_mailbox_executor_azure.py).

Implements `MailboxExecutor` from app/mailbox_executor.py (#307) and is what `make_executor('azure')` builds.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import Settings
from .mailbox_executor import (
    OUTCOME_MISMATCH,
    OUTCOME_NO_ANSWER,
    OUTCOME_PERMANENT,
    OUTCOME_RETRYABLE,
    OUTCOME_SUCCEEDED,
    OUTCOMES,
    ExecutorResult,
    MailboxExecutor,
)
from .models import MailboxChange

log = logging.getLogger("app.mailbox_executor_azure")

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
JOBS_API_VERSION = "2023-11-01"

# ---- What can be asked of Exchange ----------------------------------------------------------------------------------

KINDS = ("forwarding", "full_access", "send_as")
ACTIONS = ("apply", "remove", "read")

# (kind, action) -> runbook. The whole list of what the worker's role can start (infra/modules/automation.bicep names the
# same runbooks; a test keeps the two in step).
RUNBOOKS: dict[tuple[str, str], str] = {
    ("forwarding", "apply"): "Set-MailForwarding",
    ("forwarding", "remove"): "Clear-MailForwarding",
    ("full_access", "apply"): "Grant-FullAccess",
    ("full_access", "remove"): "Remove-FullAccess",
    ("send_as", "apply"): "Grant-SendAs",
    ("send_as", "remove"): "Remove-SendAs",
}
READ_RUNBOOK = "Read-MailboxChange"
ALLOWED_RUNBOOKS = frozenset(RUNBOOKS.values()) | {READ_RUNBOOK}

# Job states after which nothing more will happen, from the Job - Get API.
# https://learn.microsoft.com/rest/api/automation/job/get
_ENDED_BADLY = {"Failed", "Stopped", "Suspended"}

_RESOURCE_ID = re.compile(
    r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[\w.()-]{1,90}/providers/Microsoft\.Automation/automationAccounts/[\w-]{1,50}$",
    re.IGNORECASE,
)
_ORGANIZATION = re.compile(r"^[a-z0-9-]+\.onmicrosoft\.com$", re.IGNORECASE)


@dataclass(frozen=True)
class ChangeRequest:
    """The plain fields of a mailbox change that a job needs. `owner` is the mailbox that changes; `target` is who the
    mail is forwarded to (forwarding) or who gets the access (full_access, send_as)."""

    change_id: str
    kind: str
    owner: str
    target: str
    keep_copy: bool = True


def request_from_change(change: MailboxChange) -> ChangeRequest:
    """The plain fields a job needs, read from a (detached) MailboxChange."""
    return ChangeRequest(
        change_id=str(change.id),
        kind=str(change.kind),
        owner=str(change.owner_address),
        target=str(change.target_address),
        keep_copy=bool(change.keep_copy),
    )


@dataclass(frozen=True)
class JobRequest:
    runbook: str
    parameters: dict[str, str]


def job_request(request: ChangeRequest, action: str, organization: str) -> JobRequest:
    """Which runbook, with which parameters, for this kind and direction. Everything is a plain string: Azure Automation
    passes job parameters as JSON strings and the runbooks check each one again."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    if request.kind not in KINDS:
        raise ValueError(f"Unknown kind {request.kind!r}")
    if action == "read":
        parameters = {"Organization": organization, "Kind": request.kind, "Mailbox": request.owner, "Target": request.target}
        if request.kind == "forwarding":  # "in place" for forwarding includes the keep-a-copy setting
            parameters["KeepCopy"] = "true" if request.keep_copy else "false"
        return JobRequest(READ_RUNBOOK, parameters)
    runbook = RUNBOOKS[(request.kind, action)]
    if request.kind == "forwarding":
        parameters = {"Organization": organization, "Mailbox": request.owner, "Target": request.target}
        if action == "apply":
            parameters["KeepCopy"] = "true" if request.keep_copy else "false"
        return JobRequest(runbook, parameters)
    return JobRequest(runbook, {"Organization": organization, "Mailbox": request.owner, "Trustee": request.target})


# ---- The executor ---------------------------------------------------------------------------------------------------


class _AzureError(Exception):
    """A call to Azure Resource Manager that did not give what we need; carries the result to return."""

    def __init__(self, outcome: str, message: str, job_id: str = ""):
        super().__init__(message)
        self.outcome = outcome
        self.message = message
        self.job_id = job_id


def _arm_token_from_managed_identity() -> Callable[[], str]:
    """A token source using the worker's managed identity (AZURE_CLIENT_ID, set by the Bicep, picks the user-assigned
    one). Imported late so processes that never run mailbox changes do not need the Azure SDK ready."""
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    def token() -> str:
        return credential.get_token(ARM_SCOPE).token

    return token


class AzureAutomationExecutor(MailboxExecutor):
    """apply, remove and read a mailbox change by running a runbook in an Azure Automation account."""

    def __init__(
        self,
        *,
        automation_account_id: str,
        organization: str,
        timeout_seconds: float,
        poll_seconds: float = 5,
        token_provider: Callable[[], str] | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not _RESOURCE_ID.match(automation_account_id):
            raise ValueError("MAILBOX_AUTOMATION_RESOURCE_ID is not an Automation account resource id")
        if not _ORGANIZATION.match(organization):
            raise ValueError("MAILBOX_TENANT_DOMAIN must be the tenant's <name>.onmicrosoft.com name")
        if timeout_seconds <= 0:
            raise ValueError("The Automation job timeout must be positive")
        if poll_seconds < 1:  # 0 would poll Azure in a tight loop for the whole timeout
            raise ValueError("The Automation job poll interval must be at least 1 second")
        self.account_id = automation_account_id.rstrip("/")
        self.organization = organization
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._token_provider = token_provider
        self._http = http or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._clock = clock

    # The interface of #307 (apply and remove read back inside the runbook before it answers).
    def apply(self, change: MailboxChange) -> ExecutorResult:
        return self._run(request_from_change(change), "apply")

    def remove(self, change: MailboxChange) -> ExecutorResult:
        return self._run(request_from_change(change), "remove")

    def read(self, change: MailboxChange) -> ExecutorResult:
        return self._run(request_from_change(change), "read")

    # -- one call = one job --

    def _run(self, request: ChangeRequest, action: str) -> ExecutorResult:
        job = job_request(request, action, self.organization)
        job_name = ""
        try:
            job_name, job_id = self._start(job)
            self._wait(job_name, job_id)
            output = self._output(job_name, job_id)
        except _AzureError as exc:
            log.warning(
                "mailbox executor change=%s action=%s runbook=%s job=%s outcome=%s reason=%s",
                request.change_id, action, job.runbook, exc.job_id or job_name, exc.outcome, exc.message,
            )
            return ExecutorResult(exc.outcome, error=exc.message, job_id=exc.job_id or job_name or None)
        result = self._map_output(output, action, job_id)
        log.info(
            "mailbox executor change=%s action=%s runbook=%s job=%s outcome=%s",
            request.change_id, action, job.runbook, job_id, result.outcome,
        )
        return result

    def _url(self, *parts: str) -> str:
        return f"{ARM}{self.account_id}/jobs/" + "/".join(parts) + f"?api-version={JOBS_API_VERSION}"

    def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            self._token_provider = _arm_token_from_managed_identity()
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise _AzureError(OUTCOME_RETRYABLE, f"Could not reach Azure Resource Manager: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # the identity could not get a token: a setup problem, retried, then a final failure
            raise _AzureError(OUTCOME_RETRYABLE, f"Could not get an Azure token: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _body_text(response: httpx.Response) -> str:
        text = response.text.strip().replace("\n", " ")
        return text[:500]

    def _properties(self, response: httpx.Response, job_id: str = "") -> dict[str, Any]:
        """The `properties` object of a job reply, or no answer when the body is not what the API documents (a proxy's
        HTML page, an empty body): a raw parse error must never leave this class."""
        try:
            properties = response.json().get("properties", {})
        except (ValueError, AttributeError):
            properties = None
        if not isinstance(properties, dict):
            raise _AzureError(OUTCOME_NO_ANSWER, f"Azure answered with something that is not a job: {self._body_text(response)}", job_id)
        return properties

    def _start(self, job: JobRequest) -> tuple[str, str]:
        if job.runbook not in ALLOWED_RUNBOOKS:  # defence in depth: job_request only ever builds these
            raise _AzureError(OUTCOME_PERMANENT, f"Runbook {job.runbook!r} is not one the app may start")
        name = str(uuid.uuid4())
        body = {"properties": {"runbook": {"name": job.runbook}, "parameters": job.parameters, "runOn": ""}}
        response = self._send("PUT", self._url(name), json=body)
        if response.status_code in (200, 201):
            # The job exists now whatever the body says; a body we cannot read only costs us the service's own job id.
            try:
                job_id = str(self._properties(response).get("jobId") or name)
            except _AzureError:
                job_id = name
            return name, job_id
        code, text = response.status_code, self._body_text(response)
        if code == 429 or code >= 500:
            raise _AzureError(OUTCOME_RETRYABLE, f"Azure refused to start the job for now (HTTP {code}): {text}")
        if code in (401, 403):
            raise _AzureError(
                OUTCOME_PERMANENT,
                f"The worker's identity may not start jobs (HTTP {code}); it needs Automation Job Operator on the account: {text}",
            )
        raise _AzureError(OUTCOME_PERMANENT, f"Azure rejected the job (HTTP {code}); is the runbook published? {text}")

    def _wait(self, job_name: str, job_id: str) -> None:
        deadline = self._clock() + self.timeout_seconds
        while True:
            try:
                response = self._send("GET", self._url(job_name))
            except _AzureError:
                response = None  # a blip while polling: ask again until the deadline
            if response is not None:
                code = response.status_code
                if code == 200:
                    properties = self._properties(response, job_id)
                    status = properties.get("status", "")
                    if status == "Completed":
                        return
                    if status in _ENDED_BADLY:
                        detail = properties.get("exception") or properties.get("statusDetails") or ""
                        raise _AzureError(OUTCOME_RETRYABLE, f"The job ended as {status}. {detail}".strip(), job_id)
                elif code not in (404, 429) and code < 500:
                    # 404 is asked again: a job can be briefly unreadable just after it is created.
                    raise _AzureError(
                        OUTCOME_NO_ANSWER, f"Could not read the job (HTTP {code}): {self._body_text(response)}", job_id
                    )
            if self._clock() >= deadline:
                raise _AzureError(
                    OUTCOME_NO_ANSWER, f"No answer from the job within {self.timeout_seconds:g} seconds", job_id
                )
            self._sleep(self.poll_seconds)

    def _output(self, job_name: str, job_id: str) -> str:
        last = ""
        for attempt in range(3):
            try:
                response = self._send("GET", self._url(job_name, "output"))
            except _AzureError as exc:
                last = exc.message
            else:
                if response.status_code == 200:
                    return response.text
                last = f"HTTP {response.status_code}: {self._body_text(response)}"
                if response.status_code not in (429,) and response.status_code < 500:
                    break
            self._sleep(self.poll_seconds)
        raise _AzureError(OUTCOME_NO_ANSWER, f"The job completed but its output could not be read ({last})", job_id)

    # -- the runbook's one JSON object -> ExecutorResult --

    @staticmethod
    def _parse_output(output: str) -> dict[str, Any] | None:
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and "outcome" in data:
                return data
        return None

    def _map_output(self, output: str, action: str, job_id: str) -> ExecutorResult:
        data = self._parse_output(output)
        if data is None:
            return ExecutorResult(OUTCOME_NO_ANSWER, error="The runbook finished without a result line", job_id=job_id)
        raw = data.get("outcome")
        found_data = data.get("found") if isinstance(data.get("found"), dict) else {}
        present = found_data.get("present")
        found = present if isinstance(present, bool) else None
        detail = json.dumps(found_data, sort_keys=True) if found_data else None
        category, text = str(data.get("error_category") or ""), str(data.get("error") or "")
        error = (f"{category}: {text}" if category and text else (text or category)) or None
        if raw not in OUTCOMES:
            return ExecutorResult(OUTCOME_PERMANENT, found, detail, f"The runbook returned an unknown outcome {raw!r}", job_id)
        if raw == OUTCOME_NO_ANSWER:  # a runbook never says this; treat it as unreadable
            return ExecutorResult(OUTCOME_NO_ANSWER, found, detail, error or "The runbook gave no answer", job_id)
        # A success must also say what was asked: present after apply, gone after remove.
        if raw == OUTCOME_SUCCEEDED and action in ("apply", "remove") and found is not (action == "apply"):
            return ExecutorResult(
                OUTCOME_MISMATCH, found, detail, "The runbook reported success but its read-back does not show the change", job_id
            )
        return ExecutorResult(raw, found, detail, error, job_id)


# ---- Configuration --------------------------------------------------------------------------------------------------


def azure_executor_config_errors(config: Settings) -> list[str]:
    """What is missing or malformed for MAILBOX_EXECUTOR=azure, for the worker's start-up check (#307 wires it in)."""
    errors: list[str] = []
    if not _RESOURCE_ID.match(config.mailbox_automation_resource_id):
        errors.append("MAILBOX_EXECUTOR=azure needs MAILBOX_AUTOMATION_RESOURCE_ID, the Automation account's resource id")
    if not _ORGANIZATION.match(config.mailbox_tenant_domain):
        errors.append("MAILBOX_EXECUTOR=azure needs MAILBOX_TENANT_DOMAIN, the tenant's <name>.onmicrosoft.com name")
    if config.mailbox_automation_timeout_seconds <= 0:
        errors.append("MAILBOX_AUTOMATION_TIMEOUT_SECONDS must be positive")
    if config.mailbox_automation_poll_seconds < 1:
        errors.append("MAILBOX_AUTOMATION_POLL_SECONDS must be at least 1")
    return errors


def executor_from_settings(config: Settings) -> AzureAutomationExecutor:
    errors = azure_executor_config_errors(config)
    if errors:
        raise ValueError("; ".join(errors))
    return AzureAutomationExecutor(
        automation_account_id=config.mailbox_automation_resource_id,
        organization=config.mailbox_tenant_domain,
        timeout_seconds=config.mailbox_automation_timeout_seconds,
        poll_seconds=config.mailbox_automation_poll_seconds,
    )

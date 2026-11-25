"""AzureAutomationExecutor against recorded Azure Resource Manager responses (no network, no Azure).

The shapes follow the Automation REST API (Job - Create, Get, Get Output, api-version 2023-11-01). That the real service
answers like this, and that the worker's role is enough, are staging checks (docs/azure-setup.md).
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app import mailbox_executor_azure as azure
from app.config import Settings
from app.mailbox_executor_azure import AzureAutomationExecutor, Outcome

ACCOUNT = "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-ticketing-staging/providers/Microsoft.Automation/automationAccounts/aa-ticketing-staging-mailbox"
ORG = "contoso.onmicrosoft.com"


def change(kind="forwarding", keep_copy=True):
    return SimpleNamespace(id=42, kind=kind, owner_address="jane@contoso.com", target_address="sam@contoso.com", keep_copy=keep_copy)


def job_body(status, **extra):
    return {"name": "x", "properties": {"jobId": "job-guid-1", "status": status, **extra}}


def runbook_output(outcome="succeeded", present=True, category="", error="", **found):
    line = json.dumps({"outcome": outcome, "found": {"present": present, **found}, "error_category": category, "error": error})
    return f"Connecting...\n{line}\n"


class FakeArm:
    """A scripted Azure: records requests; `statuses` are the job states returned by successive polls."""

    def __init__(self, statuses=("Completed",), output=None, put=None, output_status=200, poll_status=200):
        self.statuses = list(statuses)
        self.output = runbook_output() if output is None else output
        self.put = put
        self.output_status = output_status
        self.poll_status = poll_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert request.headers["Authorization"] == "Bearer token-1"
        assert request.url.params["api-version"] == "2023-11-01"
        assert path.startswith(ACCOUNT + "/jobs/"), path
        if request.method == "PUT":
            return self.put or httpx.Response(201, json=job_body("New"))
        if path.endswith("/output"):
            return httpx.Response(self.output_status, text=self.output if self.output_status == 200 else "boom")
        if self.poll_status != 200:
            return httpx.Response(self.poll_status, text="nope")
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        extra = {"exception": "The runbook threw an exception."} if status == "Failed" else {}
        return httpx.Response(200, json=job_body(status, **extra))

    @property
    def puts(self):
        return [r for r in self.requests if r.method == "PUT"]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def executor(arm, timeout=60, clock=None):
    clock = clock or Clock()
    return AzureAutomationExecutor(
        automation_account_id=ACCOUNT,
        organization=ORG,
        timeout_seconds=timeout,
        poll_seconds=5,
        token_provider=lambda: "token-1",
        http=httpx.Client(transport=httpx.MockTransport(arm)),
        sleep=clock.sleep,
        clock=clock.time,
    )


# ---- the job request: which runbook, which parameters ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "action", "runbook", "parameters"),
    [
        ("forwarding", "apply", "Set-MailForwarding", {"Mailbox": "jane@contoso.com", "Target": "sam@contoso.com", "KeepCopy": "true"}),
        ("forwarding", "remove", "Clear-MailForwarding", {"Mailbox": "jane@contoso.com", "Target": "sam@contoso.com"}),
        ("full_access", "apply", "Grant-FullAccess", {"Mailbox": "jane@contoso.com", "Trustee": "sam@contoso.com"}),
        ("full_access", "remove", "Remove-FullAccess", {"Mailbox": "jane@contoso.com", "Trustee": "sam@contoso.com"}),
        ("send_as", "apply", "Grant-SendAs", {"Mailbox": "jane@contoso.com", "Trustee": "sam@contoso.com"}),
        ("send_as", "remove", "Remove-SendAs", {"Mailbox": "jane@contoso.com", "Trustee": "sam@contoso.com"}),
        ("forwarding", "read", "Read-MailboxChange", {"Kind": "forwarding", "Mailbox": "jane@contoso.com", "Target": "sam@contoso.com"}),
        ("full_access", "read", "Read-MailboxChange", {"Kind": "full_access", "Mailbox": "jane@contoso.com", "Target": "sam@contoso.com"}),
        ("send_as", "read", "Read-MailboxChange", {"Kind": "send_as", "Mailbox": "jane@contoso.com", "Target": "sam@contoso.com"}),
    ],
)
def test_the_job_names_the_right_runbook_and_parameters(kind, action, runbook, parameters):
    present = action != "remove"
    arm = FakeArm(output=runbook_output(present=present))
    result = getattr(executor(arm), action)(change(kind))
    assert result.outcome is Outcome.SUCCEEDED
    (put,) = arm.puts
    body = json.loads(put.content)["properties"]
    assert body["runbook"] == {"name": runbook}
    assert body["parameters"] == {"Organization": ORG, **parameters}
    assert body["runOn"] == ""


def test_keep_copy_off_is_sent_as_false():
    arm = FakeArm()
    executor(arm).apply(change(keep_copy=False))
    assert json.loads(arm.puts[0].content)["properties"]["parameters"]["KeepCopy"] == "false"


def test_every_job_is_a_new_name_and_only_the_account_in_the_config_is_called():
    arm = FakeArm()
    ex = executor(arm)
    ex.apply(change())
    ex.apply(change())
    names = {r.url.path for r in arm.puts}
    assert len(names) == 2
    assert all(r.url.host == "management.azure.com" and r.url.path.startswith(ACCOUNT + "/jobs/") for r in arm.requests)


def test_a_runbook_outside_the_allow_list_is_never_started():
    arm = FakeArm()
    ex = executor(arm)
    with pytest.raises(azure._AzureError) as caught:
        ex._start(azure.JobRequest("Get-Everything", {}))
    assert caught.value.outcome is Outcome.PERMANENT
    assert not arm.requests


def test_an_unknown_kind_is_refused_before_any_call():
    arm = FakeArm()
    with pytest.raises(ValueError):
        executor(arm).apply(change("calendar"))
    assert not arm.requests


# ---- the answer -----------------------------------------------------------------------------------------------------------


def test_a_completed_job_maps_to_succeeded_with_what_was_found_and_the_job_id():
    arm = FakeArm(statuses=["New", "Running", "Completed"], output=runbook_output(forwarding_to="sam@contoso.com", keep_copy=True))
    clock = Clock()
    result = executor(arm, clock=clock).apply(change())
    assert result.outcome is Outcome.SUCCEEDED
    assert result.found == {"present": True, "forwarding_to": "sam@contoso.com", "keep_copy": True}
    assert result.job_id == "job-guid-1"
    assert result.error == ""
    assert clock.sleeps == [5, 5]  # polled twice more after the first look


@pytest.mark.parametrize(
    ("outcome", "category", "expected"),
    [
        ("retryable", "throttled", Outcome.RETRYABLE),
        ("retryable", "timeout", Outcome.RETRYABLE),
        ("permanent", "access_denied", Outcome.PERMANENT),
        ("permanent", "not_found", Outcome.PERMANENT),
        ("mismatch", "mismatch", Outcome.MISMATCH),
    ],
)
def test_each_runbook_outcome_maps_with_its_error_text(outcome, category, expected):
    arm = FakeArm(output=runbook_output(outcome=outcome, present=False, category=category, error="raw Exchange text"))
    result = executor(arm).apply(change())
    assert result.outcome is expected
    assert result.error == f"{category}: raw Exchange text"
    assert result.found == {"present": False}


def test_success_that_does_not_show_the_change_is_a_mismatch():
    result = executor(FakeArm(output=runbook_output(present=False))).apply(change())
    assert result.outcome is Outcome.MISMATCH


def test_a_remove_that_still_shows_the_change_is_a_mismatch():
    result = executor(FakeArm(output=runbook_output(present=True))).remove(change())
    assert result.outcome is Outcome.MISMATCH


def test_read_passes_what_was_found_through_whether_or_not_it_is_there():
    present = executor(FakeArm(output=runbook_output(present=True))).read(change())
    absent = executor(FakeArm(output=runbook_output(present=False))).read(change())
    assert (present.outcome, present.found["present"]) == (Outcome.SUCCEEDED, True)
    assert (absent.outcome, absent.found["present"]) == (Outcome.SUCCEEDED, False)


def test_the_result_is_the_last_json_line_even_with_other_output_around_it():
    noisy = 'WARNING: {"outcome": "permanent"} was a lie\n' + runbook_output() + "done\n"
    assert executor(FakeArm(output=noisy)).apply(change()).outcome is Outcome.SUCCEEDED


@pytest.mark.parametrize("output", ["", "nothing useful\n", "{not json}\n", '{"found": {}}\n'])
def test_a_completed_job_without_a_readable_result_is_no_answer(output):
    result = executor(FakeArm(output=output)).apply(change())
    assert result.outcome is Outcome.NO_ANSWER
    assert result.job_id == "job-guid-1"


def test_an_unknown_outcome_word_is_permanent():
    result = executor(FakeArm(output=runbook_output(outcome="fine"))).apply(change())
    assert result.outcome is Outcome.PERMANENT


@pytest.mark.parametrize("status", ["Failed", "Stopped", "Suspended"])
def test_a_job_that_ends_badly_is_retryable_with_the_reason(status):
    result = executor(FakeArm(statuses=["Running", status])).apply(change())
    assert result.outcome is Outcome.RETRYABLE
    assert status in result.error
    assert result.job_id == "job-guid-1"


def test_a_job_with_no_answer_in_time_is_no_answer_and_stops_asking():
    arm = FakeArm(statuses=["Running"])
    clock = Clock()
    result = executor(arm, timeout=30, clock=clock).apply(change())
    assert result.outcome is Outcome.NO_ANSWER
    assert "30 seconds" in result.error
    assert result.job_id == "job-guid-1"
    assert clock.now <= 35
    assert not [r for r in arm.requests if r.url.path.endswith("/output")]


def test_a_blip_while_polling_is_asked_again():
    calls = {"n": 0}
    arm = FakeArm()
    original = arm.__call__

    def flaky(request):
        if request.method == "GET" and not request.url.path.endswith("/output"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="busy")
            if calls["n"] == 2:
                raise httpx.ConnectError("reset")
        return original(request)

    ex = AzureAutomationExecutor(
        automation_account_id=ACCOUNT, organization=ORG, timeout_seconds=60, poll_seconds=1,
        token_provider=lambda: "token-1", http=httpx.Client(transport=httpx.MockTransport(flaky)), sleep=lambda s: None,
    )
    assert ex.apply(change()).outcome is Outcome.SUCCEEDED
    assert calls["n"] == 3


def test_a_poll_the_service_refuses_is_no_answer():
    result = executor(FakeArm(poll_status=403)).apply(change())
    assert result.outcome is Outcome.NO_ANSWER
    assert "403" in result.error


def test_output_that_cannot_be_read_is_no_answer_after_a_few_tries():
    arm = FakeArm(output_status=500)
    result = executor(arm).apply(change())
    assert result.outcome is Outcome.NO_ANSWER
    assert len([r for r in arm.requests if r.url.path.endswith("/output")]) == 3


# ---- starting the job fails -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, Outcome.RETRYABLE), (500, Outcome.RETRYABLE), (503, Outcome.RETRYABLE), (400, Outcome.PERMANENT), (404, Outcome.PERMANENT)],
)
def test_a_job_that_cannot_be_started_is_classified_by_the_status(status, expected):
    arm = FakeArm(put=httpx.Response(status, text="no"))
    result = executor(arm).apply(change())
    assert result.outcome is expected
    assert str(status) in result.error
    assert len(arm.requests) == 1  # nothing was polled


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_start_says_which_role_is_missing(status):
    result = executor(FakeArm(put=httpx.Response(status, text="AuthorizationFailed"))).apply(change())
    assert result.outcome is Outcome.PERMANENT
    assert "Automation Job Operator" in result.error


def test_azure_unreachable_when_starting_is_retryable():
    def down(request):
        raise httpx.ConnectTimeout("timed out")

    ex = AzureAutomationExecutor(
        automation_account_id=ACCOUNT, organization=ORG, timeout_seconds=10,
        token_provider=lambda: "t", http=httpx.Client(transport=httpx.MockTransport(down)), sleep=lambda s: None,
    )
    result = ex.apply(change())
    assert result.outcome is Outcome.RETRYABLE
    assert "Could not reach Azure" in result.error


def test_a_token_that_cannot_be_got_is_retryable_and_calls_nothing():
    def no_token():
        raise RuntimeError("no managed identity endpoint")

    arm = FakeArm()
    ex = AzureAutomationExecutor(
        automation_account_id=ACCOUNT, organization=ORG, timeout_seconds=10, token_provider=no_token,
        http=httpx.Client(transport=httpx.MockTransport(arm)), sleep=lambda s: None,
    )
    result = ex.apply(change())
    assert result.outcome is Outcome.RETRYABLE
    assert "Azure token" in result.error
    assert not arm.requests


# ---- configuration --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "/subscriptions/x", ACCOUNT + "/jobs/1", "https://evil.example/" + ACCOUNT,
                                 ACCOUNT.replace("Microsoft.Automation/automationAccounts", "Microsoft.Compute/virtualMachines")])
def test_only_an_automation_account_resource_id_is_accepted(bad):
    with pytest.raises(ValueError):
        AzureAutomationExecutor(automation_account_id=bad, organization=ORG, timeout_seconds=10)


@pytest.mark.parametrize("bad", ["", "contoso.com", "contoso.onmicrosoft.com.evil.com", "a b.onmicrosoft.com"])
def test_the_organization_must_be_an_onmicrosoft_name(bad):
    with pytest.raises(ValueError):
        AzureAutomationExecutor(automation_account_id=ACCOUNT, organization=bad, timeout_seconds=10)


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_the_executor_is_built_from_settings_and_the_timeout_is_a_setting():
    config = settings(mailbox_automation_resource_id=ACCOUNT, mailbox_tenant_domain=ORG, mailbox_automation_timeout_seconds=123, mailbox_automation_poll_seconds=7)
    ex = azure.executor_from_settings(config)
    assert (ex.timeout_seconds, ex.poll_seconds, ex.organization, ex.account_id) == (123, 7, ORG, ACCOUNT)


def test_missing_settings_are_listed_not_guessed():
    errors = azure.azure_executor_config_errors(settings())
    assert any("MAILBOX_AUTOMATION_RESOURCE_ID" in e for e in errors)
    assert any("MAILBOX_TENANT_DOMAIN" in e for e in errors)
    with pytest.raises(ValueError):
        azure.executor_from_settings(settings())
    assert not azure.azure_executor_config_errors(settings(mailbox_automation_resource_id=ACCOUNT, mailbox_tenant_domain=ORG))


def test_settings_read_the_environment_names(monkeypatch):
    monkeypatch.setenv("MAILBOX_AUTOMATION_RESOURCE_ID", ACCOUNT)
    monkeypatch.setenv("MAILBOX_TENANT_DOMAIN", ORG)
    monkeypatch.setenv("MAILBOX_AUTOMATION_TIMEOUT_SECONDS", "90")
    config = Settings(_env_file=None)
    assert (config.mailbox_automation_resource_id, config.mailbox_tenant_domain, config.mailbox_automation_timeout_seconds) == (ACCOUNT, ORG, 90)

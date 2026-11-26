"""The Azure Automation runbooks (infra/runbooks) and what ties them to the app and the Bicep.

Static checks always run. The behaviour checks run each built runbook in PowerShell against fake Exchange cmdlets
(tests/runbooks/harness.ps1) and are skipped where `pwsh` is not installed (CI's Ubuntu runners have it). What real
Exchange does is a staging check (docs/azure-setup.md).
"""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import mailbox_executor_azure as azure

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK_DIR = ROOT / "infra" / "runbooks"
HARNESS = ROOT / "tests" / "runbooks" / "harness.ps1"

_spec = importlib.util.spec_from_file_location("build_runbooks", ROOT / "tools" / "build_runbooks.py")
build_runbooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_runbooks)

ORG = "contoso.onmicrosoft.com"


def param_names(text: str) -> list[str]:
    block = re.search(r"\bparam\s*\((.*?)\n\)", text, re.S).group(1)
    return re.findall(r"\$([A-Za-z]+)\b", block.replace("[Parameter(Mandatory)]", ""))


# ---- static: names and parameters line up -------------------------------------------------------------------------------


def test_every_runbook_the_app_can_start_exists_and_nothing_else_does():
    names = {p.stem for p in build_runbooks.runbook_files()}
    assert names == set(azure.ALLOWED_RUNBOOKS)


def test_the_bicep_creates_exactly_these_runbooks():
    bicep = (ROOT / "infra" / "modules" / "automation.bicep").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\s+'([A-Za-z]+-[A-Za-z]+)'$", bicep.split("var runbookNames = [")[1].split("]")[0], re.M))
    assert listed == set(azure.ALLOWED_RUNBOOKS)


@pytest.mark.parametrize(("kind", "action"), sorted(azure.RUNBOOKS))
def test_the_job_parameters_are_the_runbooks_parameters(kind, action):
    request = azure.ChangeRequest("7", kind, "owner@contoso.com", "target@contoso.com", keep_copy=True)
    job = azure.job_request(request, action, ORG)
    text = (RUNBOOK_DIR / f"{job.runbook}.ps1").read_text(encoding="utf-8")
    assert set(job.parameters) == set(param_names(text))


@pytest.mark.parametrize("kind", azure.KINDS)
def test_the_read_job_parameters_are_the_read_runbooks_parameters(kind):
    request = azure.ChangeRequest("7", kind, "owner@contoso.com", "target@contoso.com")
    job = azure.job_request(request, "read", ORG)
    text = (RUNBOOK_DIR / f"{job.runbook}.ps1").read_text(encoding="utf-8")
    assert set(job.parameters) == set(param_names(text))


@pytest.mark.parametrize("path", build_runbooks.runbook_files(), ids=lambda p: p.stem)
def test_each_runbook_includes_the_helpers_once_and_builds(path):
    built = build_runbooks.build(path)
    assert "# @include" not in built
    assert "function Invoke-MailboxRunbook" in built
    assert built.index("param(") < built.index("function Invoke-MailboxRunbook")  # param stays the first statement
    assert "Connect-ExchangeOnline -ManagedIdentity -Organization $Organization" in built


@pytest.mark.parametrize("path", sorted(RUNBOOK_DIR.glob("*.ps1")), ids=lambda p: p.stem)
def test_no_runbook_runs_text_it_was_given_or_holds_a_secret(path):
    text = path.read_text(encoding="utf-8")
    for forbidden in ("Invoke-Expression", "iex ", "Add-Type", "Start-Process", "-Credential", "ConvertTo-SecureString", "password", "secret"):
        assert forbidden.lower() not in text.lower(), forbidden


def test_a_runbook_without_the_include_line_is_refused(tmp_path):
    bad = tmp_path / "Bad.ps1"
    bad.write_text("param()\n", encoding="utf-8")
    with pytest.raises(ValueError):
        build_runbooks.build(bad, tmp_path)


# ---- behaviour: the built runbooks against fake Exchange ------------------------------------------------------------------

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell (pwsh) is not installed")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("runbooks")
    for path in build_runbooks.runbook_files():
        (out / path.name).write_text(build_runbooks.build(path), encoding="utf-8", newline="\n")
    return out


def run(built, tmp_path, runbook, params, **scenario):
    scenario = {"params": {"Organization": ORG, **params}, "mailboxes": {}, "recipients": [], **scenario}
    file = tmp_path / "scenario.json"
    file.write_text(json.dumps(scenario), encoding="utf-8")
    done = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(HARNESS), "-Script", str(built / f"{runbook}.ps1"), "-Scenario", str(file)],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    state = json.loads(lines[-1].removeprefix("STATE "))
    assert lines[-1].startswith("STATE ")
    # The one JSON object is the last thing the runbook wrote, and the only thing besides our STATE line.
    assert len(lines) == 2, done.stdout
    return json.loads(lines[0]), state


OWNER, TARGET = "a@contoso.com", "b@contoso.com"
FWD = {"Mailbox": OWNER, "Target": TARGET}
BOXES = {OWNER: {"forwarding": "", "keep_copy": False}}


@needs_pwsh
def test_set_forwarding_connects_as_the_managed_identity_changes_and_reads_back(built, tmp_path):
    result, state = run(built, tmp_path, "Set-MailForwarding", {**FWD, "KeepCopy": "false"}, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "succeeded"
    assert result["found"]["present"] is True and result["found"]["forwarding_to"] == TARGET
    assert result["found"]["keep_copy"] is False
    assert state["mailboxes"][OWNER] == {"forwarding": TARGET, "keep_copy": False}
    assert state["connect"] == {"managed_identity": True, "organization": ORG}
    assert state["called"][-1] == "Disconnect-ExchangeOnline"


@needs_pwsh
def test_set_forwarding_already_set_is_success_without_a_write(built, tmp_path):
    boxes = {OWNER: {"forwarding": TARGET, "keep_copy": True}}
    result, state = run(built, tmp_path, "Set-MailForwarding", {**FWD, "KeepCopy": "true"}, mailboxes=boxes, recipients=[TARGET])
    assert result["outcome"] == "succeeded"
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
def test_set_forwarding_keep_copy_off_is_written_as_off(built, tmp_path):
    boxes = {OWNER: {"forwarding": TARGET, "keep_copy": True}}
    result, state = run(built, tmp_path, "Set-MailForwarding", {**FWD, "KeepCopy": "false"}, mailboxes=boxes, recipients=[TARGET])
    assert result["outcome"] == "succeeded" and result["found"]["keep_copy"] is False
    assert "Set-Mailbox" in state["called"]


@needs_pwsh
def test_set_forwarding_that_exchange_does_not_show_is_a_mismatch(built, tmp_path):
    result, _ = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET], ignore=["Set-Mailbox"])
    assert result["outcome"] == "mismatch" and result["error_category"] == "mismatch"
    assert result["found"]["present"] is False


@needs_pwsh
@pytest.mark.parametrize(
    ("params", "category"),
    [
        ({"Mailbox": "not-an-address", "Target": TARGET}, "invalid_parameter"),
        ({"Mailbox": OWNER, "Target": "x@y.com; Remove-Mailbox -Identity z"}, "invalid_parameter"),
        ({"Mailbox": OWNER, "Target": OWNER}, "invalid_parameter"),
        ({"Mailbox": OWNER, "Target": TARGET, "KeepCopy": "maybe"}, "invalid_parameter"),
        ({"Mailbox": OWNER, "Target": "ghost@contoso.com"}, "not_found"),
    ],
)
def test_bad_parameters_and_unknown_recipients_are_permanent_and_change_nothing(built, tmp_path, params, category):
    result, state = run(built, tmp_path, "Set-MailForwarding", params, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "permanent" and result["error_category"] == category
    assert state["mailboxes"][OWNER] == BOXES[OWNER]
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
def test_an_organization_that_is_not_an_onmicrosoft_name_is_refused_before_connecting(built, tmp_path):
    result, state = run(built, tmp_path, "Set-MailForwarding", {**FWD, "Organization": "evil.example.com"}, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "permanent" and result["error_category"] == "invalid_parameter"
    assert "Connect-ExchangeOnline" not in state["called"]


@needs_pwsh
@pytest.mark.parametrize(
    ("message", "outcome", "category"),
    [
        ("The remote server returned an error: (429) Too Many Requests.", "retryable", "throttled"),
        ("The operation has timed out.", "retryable", "timeout"),
        ("The role assigned to application x isn't supported in this scenario.", "permanent", "access_denied"),
        ("Insufficient permissions to run this command.", "permanent", "access_denied"),
        ("Something nobody has seen before", "retryable", "unknown"),
    ],
)
def test_errors_are_classified_in_the_runbook(built, tmp_path, message, outcome, category):
    result, _ = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET], fail={"Set-Mailbox": message})
    assert result["outcome"] == outcome and result["error_category"] == category
    assert result["error"] == message


@needs_pwsh
def test_a_connection_failure_is_classified_too(built, tmp_path):
    result, state = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET], fail={"Connect-ExchangeOnline": "Unauthorized"})
    assert result["outcome"] == "permanent" and result["error_category"] == "access_denied"
    assert state["called"][-1] == "Disconnect-ExchangeOnline"


@needs_pwsh
def test_an_unknown_mailbox_is_permanent_not_found(built, tmp_path):
    result, _ = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes={}, recipients=[TARGET])
    assert result["outcome"] == "permanent" and result["error_category"] == "not_found"


@needs_pwsh
def test_clear_forwarding_removes_what_points_at_the_target(built, tmp_path):
    boxes = {OWNER: {"forwarding": TARGET, "keep_copy": True}}
    result, state = run(built, tmp_path, "Clear-MailForwarding", FWD, mailboxes=boxes, recipients=[TARGET])
    assert result["outcome"] == "succeeded" and result["found"]["present"] is False
    assert state["mailboxes"][OWNER] == {"forwarding": "", "keep_copy": False}


@needs_pwsh
def test_clear_forwarding_already_removed_is_success_without_a_write(built, tmp_path):
    result, state = run(built, tmp_path, "Clear-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "succeeded"
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
def test_clear_forwarding_leaves_forwarding_changed_by_hand_alone(built, tmp_path):
    boxes = {OWNER: {"forwarding": "other@contoso.com", "keep_copy": False}}
    result, state = run(built, tmp_path, "Clear-MailForwarding", FWD, mailboxes=boxes, recipients=[TARGET, "other@contoso.com"])
    assert result["outcome"] == "mismatch" and result["error_category"] == "changed_elsewhere"
    assert state["mailboxes"][OWNER]["forwarding"] == "other@contoso.com"
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
def test_clear_forwarding_that_stays_is_a_mismatch(built, tmp_path):
    boxes = {OWNER: {"forwarding": TARGET, "keep_copy": True}}
    result, _ = run(built, tmp_path, "Clear-MailForwarding", FWD, mailboxes=boxes, recipients=[TARGET], ignore=["Set-Mailbox"])
    assert result["outcome"] == "mismatch"


ACCESS = [
    ("Grant-FullAccess", "Remove-FullAccess", "full_access", "Add-MailboxPermission", "Remove-MailboxPermission"),
    ("Grant-SendAs", "Remove-SendAs", "send_as", "Add-RecipientPermission", "Remove-RecipientPermission"),
]
TRUSTEE = {"Mailbox": OWNER, "Trustee": TARGET}


@needs_pwsh
@pytest.mark.parametrize(("grant", "remove", "key", "add_cmd", "remove_cmd"), ACCESS)
def test_grant_then_remove_with_read_back_and_idempotence(built, tmp_path, grant, remove, key, add_cmd, remove_cmd):
    result, state = run(built, tmp_path, grant, TRUSTEE, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "succeeded" and result["found"]["present"] is True
    assert state[key] == [f"{OWNER}|{TARGET}"] and add_cmd in state["called"]

    again, state = run(built, tmp_path, grant, TRUSTEE, mailboxes=BOXES, recipients=[TARGET], **{key: [[OWNER, TARGET]]})
    assert again["outcome"] == "succeeded" and add_cmd not in state["called"]

    gone, state = run(built, tmp_path, remove, TRUSTEE, mailboxes=BOXES, recipients=[TARGET], **{key: [[OWNER, TARGET]]})
    assert gone["outcome"] == "succeeded" and gone["found"]["present"] is False
    assert state[key] == [] and remove_cmd in state["called"]

    already, state = run(built, tmp_path, remove, TRUSTEE, mailboxes=BOXES, recipients=[TARGET])
    assert already["outcome"] == "succeeded" and remove_cmd not in state["called"]


@needs_pwsh
@pytest.mark.parametrize(("grant", "remove", "key", "add_cmd", "remove_cmd"), ACCESS)
def test_access_that_exchange_does_not_show_is_a_mismatch(built, tmp_path, grant, remove, key, add_cmd, remove_cmd):
    granted, _ = run(built, tmp_path, grant, TRUSTEE, mailboxes=BOXES, recipients=[TARGET], ignore=[add_cmd])
    assert granted["outcome"] == "mismatch"
    kept, _ = run(built, tmp_path, remove, TRUSTEE, mailboxes=BOXES, recipients=[TARGET], ignore=[remove_cmd], **{key: [[OWNER, TARGET]]})
    assert kept["outcome"] == "mismatch" and kept["found"]["present"] is True


@needs_pwsh
@pytest.mark.parametrize(("grant", "remove", "key", "add_cmd", "remove_cmd"), ACCESS)
def test_access_runbooks_check_their_parameters(built, tmp_path, grant, remove, key, add_cmd, remove_cmd):
    for params in ({"Mailbox": OWNER, "Trustee": "nope"}, {"Mailbox": OWNER, "Trustee": OWNER}):  # malformed; to itself
        bad, state = run(built, tmp_path, grant, params, mailboxes=BOXES, recipients=[TARGET])
        assert bad["outcome"] == "permanent" and bad["error_category"] == "invalid_parameter"
        assert add_cmd not in state["called"]
    malformed, state = run(built, tmp_path, remove, {"Mailbox": OWNER, "Trustee": "nope"}, mailboxes=BOXES, recipients=[TARGET])
    assert malformed["outcome"] == "permanent" and malformed["error_category"] == "invalid_parameter"
    assert remove_cmd not in state["called"]
    ghost, _ = run(built, tmp_path, grant, {"Mailbox": OWNER, "Trustee": "ghost@contoso.com"}, mailboxes=BOXES, recipients=[TARGET])
    assert ghost["outcome"] == "permanent" and ghost["error_category"] == "not_found"


@needs_pwsh
@pytest.mark.parametrize(
    ("kind", "mailboxes", "extra", "present"),
    [
        ("forwarding", {OWNER: {"forwarding": TARGET, "keep_copy": True}}, {}, True),
        ("forwarding", {OWNER: {"forwarding": "", "keep_copy": False}}, {}, False),
        ("full_access", BOXES, {"full_access": [[OWNER, TARGET]]}, True),
        ("full_access", BOXES, {}, False),
        ("send_as", BOXES, {"send_as": [[OWNER, TARGET]]}, True),
        ("send_as", BOXES, {}, False),
    ],
)
def test_read_says_what_exchange_holds_and_changes_nothing(built, tmp_path, kind, mailboxes, extra, present):
    result, state = run(
        built, tmp_path, "Read-MailboxChange", {"Kind": kind, "Mailbox": OWNER, "Target": TARGET}, mailboxes=mailboxes, recipients=[TARGET], **extra
    )
    assert result["outcome"] == "succeeded" and result["found"]["present"] is present
    assert not [c for c in state["called"] if c.startswith(("Set-", "Add-", "Remove-"))]


@needs_pwsh
def test_read_refuses_an_unknown_kind(built, tmp_path):
    result, _ = run(built, tmp_path, "Read-MailboxChange", {"Kind": "calendar", "Mailbox": OWNER, "Target": TARGET}, mailboxes=BOXES, recipients=[TARGET])
    assert result["outcome"] == "permanent" and result["error_category"] == "invalid_parameter"


# ---- review follow-ups: who may be targeted, and removals when the other end has left -------------------------------------


@needs_pwsh
@pytest.mark.parametrize("runbook", ["Set-MailForwarding", "Grant-FullAccess", "Grant-SendAs"])
@pytest.mark.parametrize("kind", ["MailContact", "MailUser", "MailUniversalDistributionGroup"])
def test_only_mailboxes_of_the_organisation_can_be_targeted(built, tmp_path, runbook, kind):
    params = {"Mailbox": OWNER, "Target" if runbook == "Set-MailForwarding" else "Trustee": "c@outside.example.com"}
    result, state = run(
        built, tmp_path, runbook, params, mailboxes=BOXES, recipients=["c@outside.example.com"],
        recipient_types={"c@outside.example.com": kind},
    )
    assert result["outcome"] == "permanent" and result["error_category"] == "invalid_parameter"
    assert not [c for c in state["called"] if c.startswith(("Set-", "Add-"))]


@needs_pwsh
def test_a_shared_mailbox_can_be_a_forwarding_target(built, tmp_path):
    result, _ = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET], recipient_types={TARGET: "SharedMailbox"})
    assert result["outcome"] == "succeeded"


GONE = {"Mailbox": OWNER, "Target": "gone@contoso.com"}


@needs_pwsh
def test_clear_forwarding_clears_a_stale_forward_when_the_target_has_left(built, tmp_path):
    boxes = {OWNER: {"forwarding": "gone@contoso.com", "keep_copy": True}}  # no longer a recipient: it cannot be resolved
    result, state = run(built, tmp_path, "Clear-MailForwarding", GONE, mailboxes=boxes)
    assert result["outcome"] == "succeeded" and result["found"]["present"] is False
    assert state["mailboxes"][OWNER]["forwarding"] == ""


@needs_pwsh
def test_clear_forwarding_with_a_departed_target_still_leaves_someone_elses_forward_alone(built, tmp_path):
    boxes = {OWNER: {"forwarding": "other@contoso.com", "keep_copy": True}}
    result, state = run(built, tmp_path, "Clear-MailForwarding", GONE, mailboxes=boxes, recipients=["other@contoso.com"])
    assert result["outcome"] == "mismatch" and result["error_category"] == "changed_elsewhere"
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
def test_clear_forwarding_with_a_departed_target_and_no_forward_is_already_removed(built, tmp_path):
    result, state = run(built, tmp_path, "Clear-MailForwarding", GONE, mailboxes=BOXES)
    assert result["outcome"] == "succeeded"
    assert "Set-Mailbox" not in state["called"]


@needs_pwsh
@pytest.mark.parametrize(
    ("runbook", "remove_cmd"), [("Remove-FullAccess", "Remove-MailboxPermission"), ("Remove-SendAs", "Remove-RecipientPermission")]
)
def test_removing_access_for_someone_who_has_left_is_reported_removed(built, tmp_path, runbook, remove_cmd):
    result, state = run(built, tmp_path, runbook, {"Mailbox": OWNER, "Trustee": "gone@contoso.com"}, mailboxes=BOXES)
    assert result["outcome"] == "succeeded"
    assert result["found"]["present"] is False and result["found"]["trustee_not_found"] is True
    assert remove_cmd not in state["called"]


@needs_pwsh
@pytest.mark.parametrize("kind", azure.KINDS)
def test_read_for_a_target_who_has_left_says_nothing_is_held(built, tmp_path, kind):
    result, _ = run(built, tmp_path, "Read-MailboxChange", {"Kind": kind, "Mailbox": OWNER, "Target": "gone@contoso.com"}, mailboxes=BOXES)
    assert result["outcome"] == "succeeded" and result["found"]["present"] is False and result["found"]["target_not_found"] is True


@needs_pwsh
def test_status_numbers_inside_other_text_are_not_taken_for_http_errors(built, tmp_path):
    message = "Something is wrong with user503@contoso.com and object 429"
    result, _ = run(built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET], fail={"Set-Mailbox": message})
    assert result["error_category"] == "unknown"
    gate, _ = run(
        built, tmp_path, "Set-MailForwarding", FWD, mailboxes=BOXES, recipients=[TARGET],
        fail={"Set-Mailbox": "The remote server returned an error: (503) Server Unavailable."},
    )
    assert gate["error_category"] == "timeout"

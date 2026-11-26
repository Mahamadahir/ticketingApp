# Runs one built runbook (tools/build_runbooks.py) against fake Exchange cmdlets and prints what it wrote, so the
# runbooks' own logic (parameter checks, idempotence, read-back, error classes) is tested without Exchange.
# Used by tests/test_runbooks.py; never run by the app. Real Exchange behaviour is a staging check.
#
#   pwsh -NoProfile -File harness.ps1 -Script <built runbook.ps1> -Scenario <scenario.json>
#
# scenario.json: { "params": {...runbook parameters...},
#                  "mailboxes": { "a@contoso.com": { "forwarding": "", "keep_copy": false } },
#                  "recipients": ["b@contoso.com"], "recipient_types": { "c@contoso.com": "MailContact" },   # default UserMailbox
#                  "full_access": [["mailbox", "trustee"]], "send_as": [["mailbox", "trustee"]],
#                  "fail": { "Set-Mailbox": "message to throw" },
#                  "ignore": ["Add-MailboxPermission"] }     # cmdlets that succeed but change nothing
# Output: the runbook's output lines, then one line "STATE {json}" with the fake Exchange afterwards and the cmdlets called.
param(
    [Parameter(Mandatory)][string]$Script,
    [Parameter(Mandatory)][string]$Scenario
)
$ErrorActionPreference = 'Stop'
$s = Get-Content -Raw -Path $Scenario | ConvertFrom-Json -AsHashtable

$global:Mailboxes = @{}
foreach ($k in $s.mailboxes.Keys) { $global:Mailboxes[$k] = @{ forwarding = [string]$s.mailboxes[$k].forwarding; keep_copy = [bool]$s.mailboxes[$k].keep_copy } }
$global:Recipients = @($s.recipients) + @($s.mailboxes.Keys)
$global:RecipientTypes = if ($s.recipient_types) { $s.recipient_types } else { @{} }
$global:FullAccess = [System.Collections.ArrayList]@()
foreach ($e in @($s.full_access)) { if ($e) { [void]$global:FullAccess.Add("$($e[0])|$($e[1])") } }
$global:SendAs = [System.Collections.ArrayList]@()
foreach ($e in @($s.send_as)) { if ($e) { [void]$global:SendAs.Add("$($e[0])|$($e[1])") } }
$global:Fail = if ($s.fail) { $s.fail } else { @{} }
$global:Ignore = if ($s.ignore) { @($s.ignore) } else { @() }
$global:Called = [System.Collections.ArrayList]@()
$global:ConnectArgs = @{}

function Enter-Fake([string]$Name) {
    [void]$global:Called.Add($Name)
    if ($global:Fail.ContainsKey($Name)) { throw $global:Fail[$Name] }
}
function Test-Ignored([string]$Name) { return $global:Ignore -contains $Name }

function Start-Sleep { param($Seconds) }
function Connect-ExchangeOnline {
    param([switch]$ManagedIdentity, [string]$Organization, [switch]$ShowBanner, [string[]]$CommandName)
    Enter-Fake 'Connect-ExchangeOnline'
    $global:ConnectArgs = @{ managed_identity = [bool]$ManagedIdentity; organization = $Organization }
}
function Disconnect-ExchangeOnline { param([switch]$Confirm) [void]$global:Called.Add('Disconnect-ExchangeOnline') }
function Get-Mailbox {
    param([string]$Identity)
    Enter-Fake 'Get-Mailbox'
    if (-not $global:Mailboxes.ContainsKey($Identity)) { throw "The operation couldn't be performed because object '$Identity' couldn't be found on 'x'." }
    $m = $global:Mailboxes[$Identity]
    return [pscustomobject]@{
        ForwardingAddress          = if ($m.forwarding) { $m.forwarding } else { $null }
        ForwardingSmtpAddress      = $null
        DeliverToMailboxAndForward = $m.keep_copy
    }
}
function Get-Recipient {
    param([string]$Identity)
    Enter-Fake 'Get-Recipient'
    if ($global:Recipients -notcontains $Identity) { throw "The operation couldn't be performed because object '$Identity' couldn't be found on 'x'." }
    $type = if ($global:RecipientTypes.ContainsKey($Identity)) { $global:RecipientTypes[$Identity] } else { 'UserMailbox' }
    return [pscustomobject]@{ PrimarySmtpAddress = $Identity; RecipientTypeDetails = $type }
}
function Set-Mailbox {
    param([string]$Identity, $ForwardingAddress, $ForwardingSmtpAddress, $DeliverToMailboxAndForward)
    Enter-Fake 'Set-Mailbox'
    if (Test-Ignored 'Set-Mailbox') { return }
    $global:Mailboxes[$Identity].forwarding = [string]$ForwardingAddress
    $global:Mailboxes[$Identity].keep_copy = [bool]$DeliverToMailboxAndForward
}
function Get-MailboxPermission {
    param([string]$Identity, [string]$User)
    Enter-Fake 'Get-MailboxPermission'
    if ($global:FullAccess -contains "$Identity|$User") { [pscustomobject]@{ Deny = $false; AccessRights = @('FullAccess') } }
}
function Add-MailboxPermission {
    param([string]$Identity, [string]$User, [string]$AccessRights, [string]$InheritanceType, $Confirm)
    Enter-Fake 'Add-MailboxPermission'
    if (-not (Test-Ignored 'Add-MailboxPermission')) { [void]$global:FullAccess.Add("$Identity|$User") }
}
function Remove-MailboxPermission {
    param([string]$Identity, [string]$User, [string]$AccessRights, [string]$InheritanceType, $Confirm)
    Enter-Fake 'Remove-MailboxPermission'
    if (-not (Test-Ignored 'Remove-MailboxPermission')) { $global:FullAccess.Remove("$Identity|$User") }
}
function Get-RecipientPermission {
    param([string]$Identity, [string]$Trustee)
    Enter-Fake 'Get-RecipientPermission'
    if ($global:SendAs -contains "$Identity|$Trustee") { [pscustomobject]@{ AccessRights = @('SendAs') } }
}
function Add-RecipientPermission {
    param([string]$Identity, [string]$Trustee, [string]$AccessRights, $Confirm)
    Enter-Fake 'Add-RecipientPermission'
    if (-not (Test-Ignored 'Add-RecipientPermission')) { [void]$global:SendAs.Add("$Identity|$Trustee") }
}
function Remove-RecipientPermission {
    param([string]$Identity, [string]$Trustee, [string]$AccessRights, $Confirm)
    Enter-Fake 'Remove-RecipientPermission'
    if (-not (Test-Ignored 'Remove-RecipientPermission')) { $global:SendAs.Remove("$Identity|$Trustee") }
}

$params = $s.params
& $Script @params

$state = [ordered]@{
    mailboxes   = $global:Mailboxes
    full_access = @($global:FullAccess)
    send_as     = @($global:SendAs)
    called      = @($global:Called)
    connect     = $global:ConnectArgs
}
Write-Output ('STATE ' + ($state | ConvertTo-Json -Compress -Depth 6))

# Shared by every runbook in this folder. It is not a runbook itself: tools/build_runbooks.py puts this text in
# place of each runbook's include line before it is published (Azure Automation cannot dot-source a sibling file).
#
# Contract with the app (app/mailbox_executor_azure.py): a runbook writes exactly one JSON object as the last
# thing on its output stream:
#   { "outcome": "succeeded" | "retryable" | "permanent" | "mismatch",
#     "found": { ... what Exchange holds after the call ... },
#     "error_category": "" | "throttled" | "timeout" | "not_found" | "access_denied" | "invalid_parameter"
#                          | "changed_elsewhere" | "unknown",
#     "error": "" | the raw message }
# Nothing here takes a script or a command from the caller: parameters are addresses and flags, checked again below.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# After a change Exchange can take a moment to show it. Read back a few times before saying "mismatch".
$ReadBackAttempts = 4
$ReadBackDelaySeconds = 5

$AddressPattern = '^[A-Za-z0-9._%+''-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$'
$OrganizationPattern = '^[A-Za-z0-9-]+\.onmicrosoft\.com$'

function Write-Result {
    param(
        [Parameter(Mandatory)][string]$Outcome,
        [hashtable]$Found = @{},
        [string]$ErrorCategory = '',
        [string]$ErrorText = ''
    )
    $result = [ordered]@{
        outcome        = $Outcome
        found          = $Found
        error_category = $ErrorCategory
        error          = $ErrorText
    }
    Write-Output ($result | ConvertTo-Json -Compress -Depth 5)
}

function Assert-Parameter {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw [System.ArgumentException]::new($Message) }
}

function Assert-Address {
    param([string]$Value, [string]$Name)
    Assert-Parameter (($Value.Length -le 254) -and ($Value -match $AddressPattern)) "$Name is not a well-formed mail address."
}

function ConvertTo-Flag {
    param([string]$Value, [string]$Name)
    switch ($Value.Trim().ToLowerInvariant()) {
        'true' { return $true }
        'false' { return $false }
        default { throw [System.ArgumentException]::new("$Name must be true or false.") }
    }
}

# Throttling and timeouts are worth another try; a missing object or a refused call is not.
function Get-ErrorCategory {
    param($ErrorRecord)
    if ($ErrorRecord.Exception -is [System.ArgumentException]) { return 'invalid_parameter' }
    $text = "$($ErrorRecord.Exception.GetType().FullName) $($ErrorRecord.Exception.Message) $($ErrorRecord.FullyQualifiedErrorId)"
    if ($text -match 'ManagementObjectNotFound|couldn''t be found|could not be found|wasn''t found|was not found|doesn''t exist|does not exist|Couldn''t find object') { return 'not_found' }
    if ($text -match 'Unauthorized|Forbidden|access is denied|access denied|insufficient (access|permissions|privileges)|not authorized|isn''t supported in this scenario|AuthorizationFailed|You don''t have permission') { return 'access_denied' }
    # Status numbers only count in the form Exchange writes them, "(429) Too Many Requests", never as bare digits.
    if ($text -match 'throttl|TooManyRequests|Too many requests|\(429\)|try again later|exceeded the (maximum|allowed)') { return 'throttled' }
    if ($text -match 'timed out|timeout|TaskCanceled|ServiceUnavailable|temporarily unavailable|The server is busy|\((502|503|504)\)|connection (was )?(closed|reset|aborted)|socket|transient') { return 'timeout' }
    return 'unknown'
}

# Unknown errors are retried (the app bounds the attempts and every runbook is safe to run twice).
function Get-OutcomeForCategory {
    param([string]$Category)
    switch ($Category) {
        { $_ -in 'not_found', 'access_denied', 'invalid_parameter' } { return 'permanent' }
        default { return 'retryable' }
    }
}

# Runs the body, connects first, disconnects afterwards and turns any error into the one JSON result.
function Invoke-MailboxRunbook {
    param([Parameter(Mandatory)][string]$Organization, [Parameter(Mandatory)][scriptblock]$Body)
    try {
        Assert-Parameter ($Organization -match $OrganizationPattern) 'Organization must be the tenant''s .onmicrosoft.com name.'
        & $Body
    }
    catch {
        $category = Get-ErrorCategory $_
        Write-Result -Outcome (Get-OutcomeForCategory $category) -ErrorCategory $category -ErrorText ($_.Exception.Message)
    }
    finally {
        try { Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null } catch { }
    }
}

function Connect-Exchange {
    param([string]$Organization)
    $commands = @(
        'Get-Mailbox', 'Set-Mailbox', 'Get-Recipient',
        'Get-MailboxPermission', 'Add-MailboxPermission', 'Remove-MailboxPermission',
        'Get-RecipientPermission', 'Add-RecipientPermission', 'Remove-RecipientPermission'
    )
    Connect-ExchangeOnline -ManagedIdentity -Organization $Organization -ShowBanner:$false -CommandName $commands | Out-Null
}

# Where mail is forwarded to, or who is given access, must be a real mailbox of this organisation, whatever the caller said.
# Get-Recipient also finds mail contacts and mail users that point outside the tenant; those are refused.
$MailboxTypes = @('UserMailbox', 'SharedMailbox')

function Get-ExistingRecipient {
    param([string]$Address, [string]$Name)
    $recipient = Get-Recipient -Identity $Address -ErrorAction Stop
    if (-not $recipient) { throw "The $Name recipient $Address couldn't be found." }
    if ($MailboxTypes -notcontains [string]$recipient.RecipientTypeDetails) {
        throw [System.ArgumentException]::new("The $Name $Address is a $($recipient.RecipientTypeDetails), not a mailbox of this organisation.")
    }
    return $recipient
}

# For removals and reads: the other end may have left, so a recipient that no longer exists is an answer, not an error.
# Returns its primary address, or $null when it does not exist (any other error is thrown).
function Find-RecipientAddress {
    param([string]$Address)
    try {
        return [string](Get-Recipient -Identity $Address -ErrorAction Stop).PrimarySmtpAddress
    }
    catch {
        if ((Get-ErrorCategory $_) -eq 'not_found') { return $null }
        throw
    }
}

function Get-ExistingMailbox {
    param([string]$Address)
    $mailbox = Get-Mailbox -Identity $Address -ErrorAction Stop
    if (-not $mailbox) { throw "The mailbox $Address couldn't be found." }
    return $mailbox
}

function Get-ForwardingState {
    param([string]$Mailbox)
    $mb = Get-ExistingMailbox $Mailbox
    $to = ''
    $resolved = $true
    if ($mb.ForwardingAddress) {
        $to = [string]$mb.ForwardingAddress
        # A forward to a recipient that no longer exists cannot be resolved to an address: "resolved" says so.
        try { $to = [string](Get-Recipient -Identity $to -ErrorAction Stop).PrimarySmtpAddress } catch { $resolved = $false }
    }
    elseif ($mb.ForwardingSmtpAddress) {
        $to = ([string]$mb.ForwardingSmtpAddress) -replace '^(?i)smtp:', ''
    }
    return @{
        kind          = 'forwarding'
        present       = [bool]$to
        forwarding_to = $to
        keep_copy     = [bool]$mb.DeliverToMailboxAndForward
        resolved      = $resolved
    }
}

function Test-FullAccess {
    param([string]$Mailbox, [string]$Trustee)
    $entries = @(Get-MailboxPermission -Identity $Mailbox -User $Trustee -ErrorAction Stop)
    return [bool]($entries | Where-Object { (-not $_.Deny) -and ($_.AccessRights -contains 'FullAccess') })
}

function Test-SendAs {
    param([string]$Mailbox, [string]$Trustee)
    $entries = @(Get-RecipientPermission -Identity $Mailbox -Trustee $Trustee -ErrorAction Stop)
    return [bool]($entries | Where-Object { $_.AccessRights -contains 'SendAs' })
}

function Get-AccessState {
    param([string]$Kind, [string]$Mailbox, [string]$Trustee)
    $present = if ($Kind -eq 'full_access') { Test-FullAccess $Mailbox $Trustee } else { Test-SendAs $Mailbox $Trustee }
    return @{ kind = $Kind; present = $present; mailbox = $Mailbox; trustee = $Trustee }
}

# Polls a read-back until it says what we expect or the attempts run out; returns the last state read.
function Wait-ForState {
    param([Parameter(Mandatory)][scriptblock]$Read, [Parameter(Mandatory)][scriptblock]$IsExpected)
    $state = & $Read
    for ($i = 1; ($i -lt $ReadBackAttempts) -and (-not (& $IsExpected $state)); $i++) {
        Start-Sleep -Seconds $ReadBackDelaySeconds
        $state = & $Read
    }
    return $state
}

<#
.SYNOPSIS
  Remove the forwarding this app set on a mailbox, then read it back.
.DESCRIPTION
  Clears only forwarding that still points at Target. No forwarding at all means it is already removed (success).
  Forwarding to someone else (changed by hand since) is left alone and reported as a mismatch.
#>
param(
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$Mailbox,
    [Parameter(Mandatory)][string]$Target
)
# @include Common.ps1

Invoke-MailboxRunbook -Organization $Organization -Body {
    Assert-Address $Mailbox 'Mailbox'
    Assert-Address $Target 'Target'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $recipient = Get-ExistingRecipient $Target 'Target'
    $wanted = [string]$recipient.PrimarySmtpAddress

    $before = Get-ForwardingState $Mailbox
    if ($before.present -and ($before.forwarding_to -ne $wanted)) {
        Write-Result -Outcome 'mismatch' -Found $before -ErrorCategory 'changed_elsewhere' -ErrorText "The mailbox forwards to $($before.forwarding_to), not to $wanted. Nothing was changed."
        return
    }
    if ($before.present) {
        Set-Mailbox -Identity $Mailbox -ForwardingAddress $null -ForwardingSmtpAddress $null -DeliverToMailboxAndForward $false -ErrorAction Stop
    }

    $after = Wait-ForState -Read { Get-ForwardingState $Mailbox } -IsExpected { param($s) -not $s.present }
    if (-not $after.present) {
        Write-Result -Outcome 'succeeded' -Found $after
    }
    else {
        Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back still shows forwarding.'
    }
}

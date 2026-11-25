<#
.SYNOPSIS
  Forward a mailbox's mail to another recipient of this organisation, then read it back.
.DESCRIPTION
  Set-Mailbox -ForwardingAddress / -DeliverToMailboxAndForward. Already set means success.
  Parameters are fixed: addresses and a true/false flag, never a script or a command.
#>
param(
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$Mailbox,
    [Parameter(Mandatory)][string]$Target,
    [string]$KeepCopy = 'true'
)
# @include Common.ps1

Invoke-MailboxRunbook -Organization $Organization -Body {
    Assert-Address $Mailbox 'Mailbox'
    Assert-Address $Target 'Target'
    $keep = ConvertTo-Flag $KeepCopy 'KeepCopy'
    Assert-Parameter ($Mailbox -ne $Target) 'A mailbox cannot forward to itself.'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $recipient = Get-ExistingRecipient $Target 'Target'
    $wanted = [string]$recipient.PrimarySmtpAddress

    $before = Get-ForwardingState $Mailbox
    $alreadySet = $before.present -and ($before.forwarding_to -eq $wanted) -and ($before.keep_copy -eq $keep)
    if (-not $alreadySet) {
        Set-Mailbox -Identity $Mailbox -ForwardingAddress $wanted -ForwardingSmtpAddress $null -DeliverToMailboxAndForward $keep -ErrorAction Stop
    }

    $after = Wait-ForState -Read { Get-ForwardingState $Mailbox } -IsExpected {
        param($s) $s.present -and ($s.forwarding_to -eq $wanted) -and ($s.keep_copy -eq $keep)
    }
    if ($after.present -and ($after.forwarding_to -eq $wanted) -and ($after.keep_copy -eq $keep)) {
        if ($before.present -and -not $alreadySet) { $after.previous_forwarding_to = $before.forwarding_to }
        Write-Result -Outcome 'succeeded' -Found $after
    }
    else {
        Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back does not show it.'
    }
}

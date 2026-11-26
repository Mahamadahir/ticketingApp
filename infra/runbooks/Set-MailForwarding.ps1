<#
.SYNOPSIS
  Forward a mailbox's mail to another recipient of this organisation, then read it back.
.DESCRIPTION
  Set-Mailbox -ForwardingAddress / -DeliverToMailboxAndForward. Already set means success.
  Parameters are fixed: addresses and a true/false flag, never a script or a command.
  A loop is refused before anything changes: when the TARGET mailbox already forwards to this mailbox, each would send the
  other's mail back, so the result is a permanent error with category 'loop'. Both ends are compared as primary SMTP addresses.
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

    # Read only: does the target already send its mail back to this mailbox? (Get-Mailbox on the target, Get-Recipient for both ends.)
    $ownerAddress = Find-RecipientAddress $Mailbox
    $targetForward = Get-ForwardingState $wanted
    if ($ownerAddress -and $targetForward.present -and ($targetForward.forwarding_to -ieq $ownerAddress)) {
        Write-Result -Outcome 'permanent' -ErrorCategory 'loop' -ErrorText "$wanted already forwards its mail to $ownerAddress, so forwarding $ownerAddress to $wanted would create a loop. Nothing was changed."
        return
    }

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

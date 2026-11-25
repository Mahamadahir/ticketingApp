<#
.SYNOPSIS
  Say what Exchange holds for one change, without changing anything.
.DESCRIPTION
  Kind is forwarding, full_access or send_as. For forwarding, Target is who the mail would go to and found.present
  is true only when the mailbox forwards to that address. For the other two, Target is the trustee.
#>
param(
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$Kind,
    [Parameter(Mandatory)][string]$Mailbox,
    [Parameter(Mandatory)][string]$Target
)
# @include Common.ps1

Invoke-MailboxRunbook -Organization $Organization -Body {
    Assert-Parameter ($Kind -in 'forwarding', 'full_access', 'send_as') 'Kind must be forwarding, full_access or send_as.'
    Assert-Address $Mailbox 'Mailbox'
    Assert-Address $Target 'Target'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $who = [string](Get-ExistingRecipient $Target 'Target').PrimarySmtpAddress

    if ($Kind -eq 'forwarding') {
        $state = Get-ForwardingState $Mailbox
        # What the mailbox forwards to is reported either way; "present" means it is the change asked about.
        $state.present = $state.present -and ($state.forwarding_to -eq $who)
        Write-Result -Outcome 'succeeded' -Found $state
    }
    else {
        Write-Result -Outcome 'succeeded' -Found (Get-AccessState $Kind $Mailbox $who)
    }
}

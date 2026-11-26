<#
.SYNOPSIS
  Say what Exchange holds for one change, without changing anything.
.DESCRIPTION
  Kind is forwarding, full_access or send_as. For forwarding, Target is who the mail would go to and found.present
  is true only when the mailbox forwards to that address. For the other two, Target is the trustee. A target that has
  left the organisation holds nothing (found.present is false, found.target_not_found is true), except that forwarding to
  a recipient that no longer resolves counts as forwarding to it.
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
    $who = Find-RecipientAddress $Target   # $null when the target has left

    if ($Kind -eq 'forwarding') {
        $state = Get-ForwardingState $Mailbox
        # What the mailbox forwards to is reported either way; "present" means it is the change asked about.
        $state.present = $state.present -and (($who -and ($state.forwarding_to -eq $who)) -or ((-not $who) -and (-not $state.resolved)))
        if (-not $who) { $state.target_not_found = $true }
        Write-Result -Outcome 'succeeded' -Found $state
    }
    elseif (-not $who) {
        Write-Result -Outcome 'succeeded' -Found @{ kind = $Kind; present = $false; mailbox = $Mailbox; trustee = $Target; target_not_found = $true }
    }
    else {
        Write-Result -Outcome 'succeeded' -Found (Get-AccessState $Kind $Mailbox $who)
    }
}

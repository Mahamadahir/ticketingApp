<#
.SYNOPSIS
  Take Full Access to Mailbox away from Trustee, then read it back. Already removed means success.
.DESCRIPTION
  If Trustee has left the organisation there is no account left to hold the access: that is reported as removed
  (found.trustee_not_found).
#>
param(
    [Parameter(Mandatory)][string]$Organization,
    [Parameter(Mandatory)][string]$Mailbox,
    [Parameter(Mandatory)][string]$Trustee
)
# @include Common.ps1

Invoke-MailboxRunbook -Organization $Organization -Body {
    Assert-Address $Mailbox 'Mailbox'
    Assert-Address $Trustee 'Trustee'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $who = Find-RecipientAddress $Trustee
    if (-not $who) {
        Write-Result -Outcome 'succeeded' -Found @{ kind = 'full_access'; present = $false; mailbox = $Mailbox; trustee = $Trustee; trustee_not_found = $true }
        return
    }

    if (Test-FullAccess $Mailbox $who) {
        Remove-MailboxPermission -Identity $Mailbox -User $who -AccessRights FullAccess -InheritanceType All -Confirm:$false -ErrorAction Stop | Out-Null
    }

    $after = Wait-ForState -Read { Get-AccessState 'full_access' $Mailbox $who } -IsExpected { param($s) -not $s.present }
    if (-not $after.present) { Write-Result -Outcome 'succeeded' -Found $after }
    else { Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back still shows the access.' }
}

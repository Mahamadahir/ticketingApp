<#
.SYNOPSIS
  Take Full Access to Mailbox away from Trustee, then read it back. Already removed means success.
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
    $who = [string](Get-ExistingRecipient $Trustee 'Trustee').PrimarySmtpAddress

    if (Test-FullAccess $Mailbox $who) {
        Remove-MailboxPermission -Identity $Mailbox -User $who -AccessRights FullAccess -InheritanceType All -Confirm:$false -ErrorAction Stop | Out-Null
    }

    $after = Wait-ForState -Read { Get-AccessState 'full_access' $Mailbox $who } -IsExpected { param($s) -not $s.present }
    if (-not $after.present) { Write-Result -Outcome 'succeeded' -Found $after }
    else { Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back still shows the access.' }
}

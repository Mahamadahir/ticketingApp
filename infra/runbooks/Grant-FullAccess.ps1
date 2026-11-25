<#
.SYNOPSIS
  Give Trustee Full Access to Mailbox, then read it back. Already granted means success.
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
    Assert-Parameter ($Mailbox -ne $Trustee) 'A mailbox cannot be granted access to itself.'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $who = [string](Get-ExistingRecipient $Trustee 'Trustee').PrimarySmtpAddress

    if (-not (Test-FullAccess $Mailbox $who)) {
        Add-MailboxPermission -Identity $Mailbox -User $who -AccessRights FullAccess -InheritanceType All -Confirm:$false -ErrorAction Stop | Out-Null
    }

    $after = Wait-ForState -Read { Get-AccessState 'full_access' $Mailbox $who } -IsExpected { param($s) $s.present }
    if ($after.present) { Write-Result -Outcome 'succeeded' -Found $after }
    else { Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back does not show it.' }
}

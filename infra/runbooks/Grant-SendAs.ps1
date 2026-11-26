<#
.SYNOPSIS
  Give Trustee the Send As right on Mailbox, then read it back. Already granted means success.
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
    Assert-Parameter ($Mailbox -ne $Trustee) 'A mailbox cannot be granted Send As on itself.'
    Connect-Exchange $Organization
    $null = Get-ExistingMailbox $Mailbox
    $who = [string](Get-ExistingRecipient $Trustee 'Trustee').PrimarySmtpAddress

    if (-not (Test-SendAs $Mailbox $who)) {
        Add-RecipientPermission -Identity $Mailbox -Trustee $who -AccessRights SendAs -Confirm:$false -ErrorAction Stop | Out-Null
    }

    $after = Wait-ForState -Read { Get-AccessState 'send_as' $Mailbox $who } -IsExpected { param($s) $s.present }
    if ($after.present) { Write-Result -Outcome 'succeeded' -Found $after }
    else { Write-Result -Outcome 'mismatch' -Found $after -ErrorCategory 'mismatch' -ErrorText 'Exchange accepted the change but the read-back does not show it.' }
}

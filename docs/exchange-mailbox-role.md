# Exchange rights for mailbox changes: the least-privilege setup

The Automation account's managed identity (the "robot" that runs the mailbox runbooks, `infra/README.md`, "Mailbox
changes") needs rights in Exchange Online. This page sets up the **smallest** rights that let the seven runbooks work: a
custom Exchange role that allows only the commands and parameters they use, limited to a set of mailboxes, and **no Entra
directory role at all**.

Status: **not done; owner to do by hand later.** On staging, the robot may change only mailboxes in a group that holds
**test accounts only**. Production's scope is still to be decided (see the end).

Why not a built-in role: Exchange Administrator can change almost anything in Exchange. Exchange Recipient Administrator
is smaller but can still create, change and delete any recipient in the tenant. The app needs nine commands.

Source: Microsoft, "App-only authentication for unattended scripts", step 5, option 2 ("Assign custom role groups to the
application using service principals", recommended to "restrict the available commands" and "limit which recipients can
be modified"): https://learn.microsoft.com/powershell/exchange/app-only-auth-powershell-v2#step-5-assign-role-permissions-to-the-application

## Who can do what

| Step | Needs |
|---|---|
| 1. `Exchange.ManageAsApp` for the robot (admin consent) | A privileged Entra admin: Global Administrator or Privileged Role Administrator (confirm whether Cloud Application Administrator is enough in your tenant) |
| 2 to 7. The role, scope, role group and service principal in Exchange | Exchange Administrator (Organization Management in Exchange). No Entra directory role is assigned to the robot. |

## What the runbooks call (keep this list in step with `infra/runbooks/*.ps1`)

| Command | Parameters used |
|---|---|
| `Get-Recipient` | `Identity` |
| `Get-Mailbox` | `Identity` |
| `Set-Mailbox` | `Identity`, `ForwardingAddress`, `ForwardingSmtpAddress`, `DeliverToMailboxAndForward` |
| `Get-MailboxPermission` | `Identity`, `User` |
| `Add-MailboxPermission` / `Remove-MailboxPermission` | `Identity`, `User`, `AccessRights`, `InheritanceType` |
| `Get-RecipientPermission` | `Identity`, `Trustee` |
| `Add-RecipientPermission` / `Remove-RecipientPermission` | `Identity`, `Trustee`, `AccessRights` |

Every call also passes `-ErrorAction`, and the add and remove calls pass `-Confirm`. If a runbook changes, update this
table and the role (step 4).

## Steps

Placeholders: `<robot>` is the Automation account's managed identity (deployment output
`mailboxAutomationIdentityPrincipalId`, and its application id from Entra, Enterprise applications, filter "Managed
identities"). Names below use "staging"; use your own.

### 1. Let the robot sign in to Exchange Online (privileged Entra admin)

From Microsoft's managed identity page (https://learn.microsoft.com/powershell/exchange/connect-exo-powershell-managed-identity):

```powershell
Connect-MgGraph -Scopes AppRoleAssignment.ReadWrite.All,Application.Read.All
$MI_ID = '<mailboxAutomationIdentityPrincipalId>'
$exo = Get-MgServicePrincipal -Filter "AppId eq '00000002-0000-0ff1-ce00-000000000000'"   # Office 365 Exchange Online
New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $MI_ID -PrincipalId $MI_ID `
  -AppRoleId 'dc50a0fb-09a3-484d-be87-e023b12c6440' -ResourceId $exo.Id                   # Exchange.ManageAsApp
```

**Do not** run the directory role assignment shown in `infra/README.md` (Exchange Administrator) when using this page.

### 2. Connect as an Exchange admin and check the parent role

```powershell
Connect-ExchangeOnline -UserPrincipalName <your admin account>
$cmds = 'Get-Recipient','Get-Mailbox','Set-Mailbox','Get-MailboxPermission','Add-MailboxPermission',
        'Remove-MailboxPermission','Get-RecipientPermission','Add-RecipientPermission','Remove-RecipientPermission'
foreach ($c in $cmds) { Get-ManagementRole -Cmdlet $c | Select-Object @{n='Cmdlet';e={$c}}, Name }
```

Expected: one built-in role, very likely **Mail Recipients**, holds all nine. If they are split across roles, make one
custom role per parent (steps 3 to 5 for each) and put them all in the role group in step 7.

### 3. Create the custom role

```powershell
New-ManagementRole -Name 'Ticketing Mailbox Changes' -Parent 'Mail Recipients'
```

### 4. Remove every command the runbooks do not use

```powershell
Get-ManagementRoleEntry 'Ticketing Mailbox Changes\*' |
  Where-Object { $cmds -notcontains $_.Name } |
  ForEach-Object { Remove-ManagementRoleEntry -Identity "$($_.Role)\$($_.Name)" -Confirm:$false }
Get-ManagementRoleEntry 'Ticketing Mailbox Changes\*' | Select-Object Name   # should list exactly the nine
```

### 5. Trim the parameters

`Set-ManagementRoleEntry -Parameters` replaces the whole list, so include the common parameters the runbooks pass
(`ErrorAction`, `Confirm`; keep `WarningAction`, `Verbose` and `Debug` too, they are harmless). Check that each name exists
on the entry first (`(Get-ManagementRoleEntry 'Ticketing Mailbox Changes\Set-Mailbox').Parameters`).

```powershell
$common = 'ErrorAction','WarningAction','Confirm','Verbose','Debug'
$params = @{
  'Get-Recipient'              = @('Identity')
  'Get-Mailbox'                = @('Identity')
  'Set-Mailbox'                = @('Identity','ForwardingAddress','ForwardingSmtpAddress','DeliverToMailboxAndForward')
  'Get-MailboxPermission'      = @('Identity','User')
  'Add-MailboxPermission'      = @('Identity','User','AccessRights','InheritanceType')
  'Remove-MailboxPermission'   = @('Identity','User','AccessRights','InheritanceType')
  'Get-RecipientPermission'    = @('Identity','Trustee')
  'Add-RecipientPermission'    = @('Identity','Trustee','AccessRights')
  'Remove-RecipientPermission' = @('Identity','Trustee','AccessRights')
}
foreach ($c in $params.Keys) {
  Set-ManagementRoleEntry "Ticketing Mailbox Changes\$c" -Parameters ($params[$c] + $common)
}
```

The important one is `Set-Mailbox`: with this list the robot can change forwarding and nothing else on a mailbox.

### 6. Limit which mailboxes it can change (the write scope)

**Staging:** a mail-enabled security group (or distribution group) containing **only test accounts**, for example
`Ticketing staging test mailboxes`. Membership is direct only (nested groups are not followed).

```powershell
$dn = (Get-Group 'Ticketing staging test mailboxes').DistinguishedName
New-ManagementScope -Name 'Ticketing staging test mailboxes' -RecipientRestrictionFilter "MemberOfGroup -eq '$dn'"
```

The scope limits the mailbox being **changed** (`Identity`). Put every test account that may be a target or a trustee in
the group as well, so staging tests never touch a real person.

### 7. The role group, and the robot as its member

```powershell
New-RoleGroup -Name 'Ticketing Mailbox Changes (staging)' -Roles 'Ticketing Mailbox Changes' `
  -CustomRecipientWriteScope 'Ticketing staging test mailboxes' `
  -Description 'The ticketing app''s Automation identity: mailbox forwarding, full access and Send As only, test mailboxes only.'

New-ServicePrincipal -AppId '<robot application id>' -ObjectId '<mailboxAutomationIdentityPrincipalId>' `
  -DisplayName 'Ticketing mailbox automation (staging)'
Add-RoleGroupMember -Identity 'Ticketing Mailbox Changes (staging)' -Member '<mailboxAutomationIdentityPrincipalId>'
```

## Check it

1. `Get-ManagementRoleEntry 'Ticketing Mailbox Changes\*' | Format-Table Name, Parameters` shows the nine commands and
   their trimmed parameters.
2. Run each runbook from the Automation account (or through the app with the switch on) against two test accounts:
   set and clear forwarding (with and without a copy), grant and remove full access, grant and remove Send As, and a read.
   Each should report succeeded and read back correctly.
3. Try a change on a mailbox **outside** the group: it must fail as access denied (the app records it as permanent).
4. Permission changes in Exchange are not instant. If a runbook fails with access denied straight after setup, wait and
   try again before changing anything.

## If the managed identity will not take a custom role group

Microsoft documents custom role groups for an app registration with a certificate. The managed identity page shows only
directory roles. A managed identity is a service principal, so this should work, **but it is not documented: this is the
first thing to test on staging.** If `Connect-ExchangeOnline -ManagedIdentity` fails or the runbooks get no commands:

- short term: give the robot **Exchange Recipient Administrator** (smaller than Exchange Administrator) and keep the
  app's own limits (only the seven runbooks can be started);
- long term: an app registration with a certificate in Key Vault, which this custom role group is documented for.

Record what happened in `docs/azure-setup.md` (step 9).

## Production (to decide)

Which mailboxes the robot may change in production: everyone, or everyone except a list (break-glass, service, executive
mailboxes), via a scope such as `RecipientRestrictionFilter` on a group or a custom attribute. Create a separate role group
and scope for production; never reuse the staging test group.

# Azure setup: from an empty subscription to a running staging

This is the by-hand checklist for the person with access to the subscription. Nothing here
is run by the app or by Claude. The templates and what they create are in `infra/README.md`;
the design is in `docs/plan.md` ("Hosting on Azure"). Nothing has ever been deployed, so
expect fixes on the first `what-if`.

Do staging first. Production is the same steps with `production` in the names, after staging
has passed the checks at the end of this page.

**Conventions.** Region UK South. Resource group `rg-ticketing-staging` (production:
`rg-ticketing-production`). Secrets are typed into your shell or the GitHub Environment,
never into a file in the repo, and a copy of each is kept in IT Glue (marked sensitive).

**Where to run it.** Azure Cloud Shell (bash) is enough: `az` and Bicep are installed and you
are already signed in. Its session is ephemeral and times out after about 20 idle minutes,
taking your shell variables and the clone with it; re-clone (`gh auth login`, then
`gh repo clone Mahamadahir/ticketingApp`) and paste the saved values back in, never new ones.
The commands below are bash; the PowerShell steps (Exchange) run in Cloud Shell's `pwsh`.

## Who does what

The person running the deploy (the **deployer**) has **Contributor** on `rg-ticketing-staging`
only, and is also the Entra, Exchange and Global admin and the GitHub admin. Contributor creates
and changes every resource in the group but **cannot create role assignments**, and has no
rights on the subscription itself. The deployer has been promised **Owner on the group** (not
yet granted, Nov 2026); once it lands, every [Owner] step below that is scoped to the group is
theirs. The steps below are marked:

- **[Contributor]**: the deployer can do it with Contributor on the group.
- **[Owner]**: needs someone with Owner (or User Access Administrator) on the subscription or
  the group.
- **[Entra admin]**, **[Global admin]**, **[Exchange admin]**: needs that Microsoft 365 role, not
  an Azure role.
- **[GitHub admin]**: needs admin on the repository.

| Step | Who | Status (Nov 2026) |
|---|---|---|
| 1.2 Register resource providers | [Owner] (subscription) | done: all nine registered |
| 1.3 Create the resource group | [Owner] | done, with tags |
| 1.4 The deployer's own rights on the group | [Owner] | Contributor granted; Owner on the group requested, not yet granted |
| 1.5 Check quota | [Owner] or anyone with Reader on the subscription | not needed: the first deploy hit no quota or SKU error |
| 2 `what-if` | [Contributor] | done: 20 to create, no template errors |
| 2 Platform deploy | [Contributor] **plus** the restricted role-assignment role (1.4), or an [Owner] runs it | partly done (`platform-1`): network, Postgres, Container Apps environment, monitoring, identities, alerts. Registry, storage and Key Vault wait for the role grant |
| 3 App registrations and secrets | [Entra admin] (or Application Developer for creating them) | done: `ticketing-sso-staging`, `ticketing-graph-staging`, `ticketing-deploy-staging`; secrets in GitHub and IT Glue, expire Nov 2027 |
| 3 Admin consent for the Graph permissions | [Global admin] or Privileged Role Administrator | done (`Presence.Read.All`, `User.Read.All`; Mail is granted in Exchange, step 4) |
| 4 Shared mailbox and scoping | [Exchange admin] | done: `itsupport-staging@vardags.com`, Mail.Read and Mail.Send scoped to it |
| 5.1 Federated credential on the GitHub app registration | [Entra admin] or the registration's owner | done (`environment:staging`) |
| 5.2 Contributor and the restricted role for the GitHub identity | [Owner] | waiting for the Owner grant |
| 5.2 AcrPush for the GitHub identity | [Owner], or the deployer once they hold the restricted role (AcrPush is in its list) | after the registry exists |
| 6 GitHub Environments, variables and secrets | [GitHub admin] | done for `staging` (Graph and SSO included; Teams not yet) |
| 7 Teams Workflows webhook | the Teams channel owner | not started (optional) |
| 8 Run the Deploy workflow | [GitHub admin] (the workflow uses the GitHub identity) | after 2 and 5.2 |
| 9 Checks | the deployer, with the MSP for the end-to-end mail checks | |
| 10 Budget alert on the group | [Contributor] | done: `budget-ticketing-staging`, 100 a month, to `ALERT_EMAIL` |
| 10 Delete the group | [Contributor] (on the group) | |

**What to ask the Owner for, once:** on `rg-ticketing-staging`, either Owner for the deployer
(what was asked for here), or **Role Based Access Control Administrator** with the condition
"Allow user to only assign selected roles to selected principals", limited to **AcrPull, AcrPush,
Key Vault Secrets User, Storage Blob Data Contributor and Storage Blob Delegator** (role IDs
`7f951dda-4ed3-4680-a7ca-43fe172d538d`, `8311e382-0749-4cb8-b61a-304f252e45ec`,
`4633458b-17de-406a-b58b-ae56cf9b3dff`, `ba92f5b4-2d11-453d-a403-e96b0029c9fe`,
`db58b8e5-c6ad-4a2a-8342-4190687cbf4a`; the portal's Conditions tab avoids typing them). The
GitHub identity gets **Contributor** plus that same restricted role, never Owner, so a
compromised workflow cannot take the group over. With that, every other Azure step here is
[Contributor].

**Not covered by Owner on the group** (ask only when needed): purging a deleted Key Vault
(subscription scope, see step 10), creating `rg-ticketing-production`, quota requests, policy
exemptions and subscription-wide alerts.

## 0. Decide first (one line each, write the answers down)

| Decision | Needed for |
|---|---|
| Shared mailbox address (plan says TBC) | Phase 3 mail, `GRAPH_SHARED_MAILBOX` |
| Office and VPN IP ranges (CIDRs) | `WEB_ALLOWED_CIDRS`, the agent console is IP restricted. Staging: `0.0.0.0/0` for now (the VPN is split tunnel, see step 9) |
| Alert email address | `ALERT_EMAIL`, a real monitored mailbox |
| Public address for staging (generated `*.azurecontainerapps.io` name is fine to start) | `PUBLIC_BASE_URL`, Entra redirect URI, Graph webhook |
| Teams channel for new-ticket and notification posts | Teams Workflows webhook (step 7) |

## 1. Subscription and the resource group

You have a subscription; this app has no resource group yet.

1. Sign in: `az login` then `az account set --subscription <subscription id>`.
2. **[Owner]** Register the resource providers once per subscription (needs Owner or Contributor
   on the subscription, not the group; skip any already registered; check with
   `az provider show --namespace <name> --query registrationState -o tsv`):

   ```powershell
   foreach ($p in 'Microsoft.App','Microsoft.ContainerRegistry','Microsoft.DBforPostgreSQL',
                  'Microsoft.KeyVault','Microsoft.Storage','Microsoft.Network',
                  'Microsoft.OperationalInsights','Microsoft.Insights','Microsoft.ManagedIdentity') {
     az provider register --namespace $p
   }
   az provider show --namespace Microsoft.App --query registrationState   # wait for Registered
   ```
3. **[Owner]** Create the group:

   ```powershell
   az group create -n rg-ticketing-staging -l uksouth
   ```
4. **[Owner]** Give the deployer rights on the group. The templates create role assignments, so
   Contributor alone fails at those steps. Either Owner on the group, or (preferred) Contributor
   plus the restricted **Role Based Access Control Administrator** described in "Who does what":

   ```powershell
   az role assignment create --assignee <your user object id> --role Owner `
     --scope /subscriptions/<subscription id>/resourceGroups/rg-ticketing-staging
   ```
5. **[Owner or subscription Reader]** Check the quota for Container Apps and PostgreSQL Flexible Server (burstable `Standard_B1ms`)
   in UK South. If the first deploy fails with a quota or SKU error, that is the place to look.

## 2. Platform-only deploy (no apps yet)

**[Contributor]** for `what-if`. The `create` needs the restricted role-assignment role as well
(step 1.4). With Contributor alone, Azure rejects the `registry`, `storage` and `keyvault`
modules **whole** before creating anything in them (`InvalidTemplateDeployment`,
`roleAssignments/write` not allowed), and creates the rest. That is what `platform-1` did in
Nov 2026. Re-running the same `create` once the role is granted fills in the three modules.

`what-if` shows the role assignments as "Unsupported" and leaves out the Key Vault module
(`NestedDeploymentShortCircuited`): their IDs depend on identities that do not exist yet. Both
are noise, not errors.

Leave `IMAGE` unset so only the platform (network, monitoring, registry, Postgres, storage,
Key Vault, Container Apps environment, alerts) is created. Full text is in `infra/README.md`,
"First deploy". In short:

```bash
read -rsp "Postgres admin password: " POSTGRES_ADMIN_PASSWORD && echo && export POSTGRES_ADMIN_PASSWORD
# first time only; afterwards paste the saved values from IT Glue
export SESSION_SECRET_KEY=$(openssl rand -hex 32) INTERNAL_SESSION_SECRET_KEY=$(openssl rand -hex 32) GRAPH_WEBHOOK_CLIENT_STATE=$(openssl rand -hex 16)
export ALERT_EMAIL='<the monitored address>' WEB_ALLOWED_CIDRS='<CIDRs, comma separated>'

# check: expect pw 24+, s:64, i:64, g:32
echo "$ALERT_EMAIL | $WEB_ALLOWED_CIDRS | pw:${#POSTGRES_ADMIN_PASSWORD} s:${#SESSION_SECRET_KEY} i:${#INTERNAL_SESSION_SECRET_KEY} g:${#GRAPH_WEBHOOK_CLIENT_STATE}"

az deployment group what-if -g rg-ticketing-staging -f infra/main.bicep -p infra/staging.bicepparam
# Postgres takes 10-15 minutes, longer than Cloud Shell stays idle: run it on Azure's side
az deployment group create -g rg-ticketing-staging -n platform-2 -f infra/main.bicep -p infra/staging.bicepparam --no-wait
az deployment operation group list -g rg-ticketing-staging -n platform-2 --query "[].{resource:properties.targetResource.resourceName, state:properties.provisioningState}" -o table
az deployment operation group list -g rg-ticketing-staging -n platform-2 --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName, message:properties.statusMessage.error.message}" -o json
```

Keep the same secret values: you will put them in GitHub in step 6 and the next deploys must
use identical values. Store them in IT Glue now (print them with `echo "$SESSION_SECRET_KEY"`
and so on, then `clear`). The values used for `platform-1` are the ones in IT Glue and GitHub.

Write down the deployment outputs (`registryLoginServer`, `migrateJobName`, the Key Vault
name, the storage account name).

## 3. Entra app registrations (three, all in the same tenant)

**[Entra admin]** for the registrations and secrets; **[Global admin]** for admin consent.

| App registration | Purpose | Permissions (application unless noted) | Redirect URI |
|---|---|---|---|
| **ticketing-sso-staging** | Agents sign in with Microsoft | delegated `openid`, `profile`, `email` | `https://<staging web address>/auth/callback` |
| **ticketing-graph-staging** | The app reads and sends the shared mailbox, checks presence | In Entra: `Presence.Read.All`, `User.Read.All` (admin consent). **Not** `Mail.Read` / `Mail.Send`: those are granted in Exchange for the one mailbox (step 4). `MailboxSettings.Read` only for the later requester time-zone feature | none |
| **ticketing-deploy-staging** | GitHub Actions deploys with OIDC | none in Entra; Azure roles in step 5 | none |

What each Graph permission is for: `Mail.Read` reads new mail, attachments and the new-mail
subscription (inbound); `Mail.Send` sends from the shared mailbox (replies, acknowledgements,
approval emails, the MSP handoff); `Presence.Read.All` marks agents out of office in the
pickers; `User.Read.All` turns an agent's email into the Entra ID the presence API needs. The
app never deletes, moves or marks mail, so `Mail.ReadWrite` is not needed. `User.Read.All` is
wider than the one field it reads; a narrower permission is a later tidy-up.

`az ad app create` makes only the registration; `az ad sp create --id <appId>` makes the
enterprise app (service principal) that permissions, consent and Azure roles attach to. The
portal makes both at once.

```bash
# SSO: the web address is fixed once the Container Apps environment exists
WEB_URL="https://ca-ticketing-staging-web.$(az containerapp env show -n cae-ticketing-staging -g rg-ticketing-staging --query properties.defaultDomain -o tsv)"
SSO_APP_ID=$(az ad app create --display-name ticketing-sso-staging --web-redirect-uris "$WEB_URL/auth/callback" --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad sp create --id $SSO_APP_ID

# Graph: the two Entra permissions, looked up by name
GRAPH_APP_ID=$(az ad app create --display-name ticketing-graph-staging --sign-in-audience AzureADMyOrg --query appId -o tsv)
GRAPH_SP_ID=$(az ad sp create --id $GRAPH_APP_ID --query id -o tsv)
for p in Presence.Read.All User.Read.All; do az ad app permission add --id $GRAPH_APP_ID --api 00000003-0000-0000-c000-000000000000 --api-permissions "$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query "appRoles[?value=='$p'].id" -o tsv)=Role"; done

# A client secret, set in GitHub and printed once for IT Glue (Entra never shows it again)
SECRET=$(az ad app credential reset --id $GRAPH_APP_ID --display-name github-staging --years 1 --query password -o tsv) && gh secret set GRAPH_CLIENT_SECRET --env staging --body "$SECRET" && echo "$SECRET"
```

After a Cloud Shell restart, get the IDs back with
`az ad app list --display-name ticketing- --query "[].{name:displayName, appId:appId}" -o table`.

Notes:

- Create a client secret for the first two. Put the secret in the GitHub Environment (step 6)
  and IT Glue, not anywhere in the repo. Record the expiry date (a year) as an IT Glue document
  expiry and in a calendar, a month early; a lapsed secret silently stops sign-in or mail.
  To rotate: the `credential reset` line above, then redeploy.
- Grant admin consent for the Graph permissions (needs a Global Administrator or Privileged
  Role Administrator). `az ad app permission admin-consent` **fails in Cloud Shell** ("Audience
  74658136-... is not a supported MSI token audience"); use the portal instead: Entra admin
  centre > App registrations > ticketing-graph-staging > API permissions > Grant admin consent.
  Check: `az rest --url "https://graph.microsoft.com/v1.0/servicePrincipals/$GRAPH_SP_ID/appRoleAssignments" --query "value[].appRoleId" -o tsv`
  prints two IDs.
- **Scope the mail permissions to the one mailbox.** `Mail.Read` and `Mail.Send` as application
  permissions reach every mailbox in the tenant unless restricted. Here they are granted only
  through Exchange Online RBAC for Applications, scoped to the shared mailbox (step 4). RBAC for
  Applications **adds to** what Entra grants, so the same permissions must **not** also be
  consented in Entra, or the app can still read every mailbox. (The older Application Access
  Policy works the other way, restricting an Entra grant; it is not used here.) This is a
  security step, not optional.
- **Limit who can sign in.** Enterprise applications > ticketing-sso-staging > Properties:
  **Assignment required? Yes**, then add the testers (or an IT group) under Users and groups.
  Without it anyone in the tenant can sign in to the portal. Production: the agent group and
  everyone who uses the portal. For the Graph and deploy apps set **Visible to users? No**.
- `SUPER_ADMIN_EMAILS` (optional) lists who becomes super admin on first sign-in, so there is a
  way into `/admin`. It must match the email you sign in with.

## 4. The shared mailbox

**[Exchange admin]** for all of this step.

Staging uses its **own** shared mailbox (for example `itsupport-staging@vardags.com`; a shared
mailbox needs no licence) with **no forwarding rule**. Do not point staging at
`ITSupport@vardags.com`: that address forwards to the MSP today, and at go-live the app takes it
over (see "Go-live: the IT support mailbox" below).

In Cloud Shell, `pwsh`, then `Connect-ExchangeOnline -Device`:

```powershell
# 1. The mailbox. "Prepopulate ... 0x8004010F" warnings are normal for a new mailbox; it settles
#    within minutes (Get-Mailbox shows RecipientTypeDetails SharedMailbox).
New-Mailbox -Shared -Name "IT Support Staging" -Alias itsupport-staging -PrimarySmtpAddress itsupport-staging@vardags.com

# 2. Register the Graph app in Exchange (its appId and the service principal's object id, step 3)
New-ServicePrincipal -AppId <GRAPH_APP_ID> -ObjectId <GRAPH_SP_ID> -DisplayName "ticketing-graph-staging"

# 3. Mail.Read and Mail.Send for that one mailbox only
New-ManagementScope -Name "ticketing-staging-mailbox" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'itsupport-staging@vardags.com'"
New-ManagementRoleAssignment -App <GRAPH_APP_ID> -Role "Application Mail.Read" -CustomResourceScope "ticketing-staging-mailbox"
New-ManagementRoleAssignment -App <GRAPH_APP_ID> -Role "Application Mail.Send" -CustomResourceScope "ticketing-staging-mailbox"

# 4. Prove it (allow up to an hour): True for the staging mailbox, False for anyone else's
Test-ServicePrincipalAuthorization -Identity <GRAPH_APP_ID> -Resource itsupport-staging@vardags.com | Format-Table RoleName,InScope
Test-ServicePrincipalAuthorization -Identity <GRAPH_APP_ID> -Resource <your mailbox> | Format-Table RoleName,InScope
```

Optionally give yourself Full Access to the mailbox (`Add-MailboxPermission`) to watch what
arrives and what the app sends during testing.

**Mimecast needs no steps.** It sits in front of Exchange Online, and its directory sync runs
hourly: once it has picked up the new mailbox it accepts inbound mail for the address and
allows sending from it. Allow an hour after creating the mailbox before the mail tests.

## 5. GitHub Actions can deploy

**[Entra admin]** for the federated credential; **[Owner]** for the role assignments on the group.

Described in `infra/README.md`, "GitHub Actions identity". In short:

1. On `ticketing-deploy-staging` add a **federated credential**: issuer
   `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`,
   subject `repo:Mahamadahir/ticketingApp:environment:staging` (and one for `production`
   later). The subject must match the repository and GitHub Environment names exactly,
   capitals included.
2. On `rg-ticketing-staging` grant its service principal
   (`az ad sp show --id <appId> --query id -o tsv` gives the object id):
   - **Contributor**;
   - **Role Based Access Control Administrator**, with the same condition as the deployer's
     (AcrPull, AcrPush, Key Vault Secrets User, Storage Blob Data Contributor, Storage Blob
     Delegator), never Owner;
   - **AcrPush** on the registry created in step 2.

## 6. GitHub Environments

**[GitHub admin]**.

Settings > Environments > `staging` (and later `production`, with **required reviewers**; that
is the approval gate; optionally restrict both to the `main` branch).

From Cloud Shell, `gh` copies the values straight from the shell, so nothing is pasted by hand
and the secrets never show on screen:

```bash
gh api -X PUT repos/Mahamadahir/ticketingApp/environments/staging
gh variable set ALERT_EMAIL --env staging --body "$ALERT_EMAIL"            # and so on per variable
gh secret set SESSION_SECRET_KEY --env staging --body "$SESSION_SECRET_KEY" # and so on per secret
gh variable list --env staging; gh secret list --env staging
```

Variables are shown in plain text in settings and logs; anything secret goes in as a Secret.

| Kind | Names |
|---|---|
| Variables (required) | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `ALERT_EMAIL`, `WEB_ALLOWED_CIDRS` |
| Variables (optional) | `SSO_TENANT_ID`, `SSO_CLIENT_ID`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_SHARED_MAILBOX`, `SUPER_ADMIN_EMAILS`, `HEALTH_URL` |
| Secrets (required) | `POSTGRES_ADMIN_PASSWORD`, `SESSION_SECRET_KEY`, `INTERNAL_SESSION_SECRET_KEY`, `GRAPH_WEBHOOK_CLIENT_STATE` (same values as step 2) |
| Secrets (once you have them) | `SSO_CLIENT_SECRET`, `GRAPH_CLIENT_SECRET`; supply on **every** deploy once set, or the new revision loses them |

More variables and secrets arrive with the Teams and presence switches and with Phase 3
(public address, mailbox, storage); `infra/README.md` is kept current by those PRs.

## 7. Teams (optional until Phase 2 features are switched on)

1. In the Teams channel, add a **Workflows** webhook ("Post to a channel when a webhook
   request is received"). Copy the URL; it is a credential (anyone holding it can post).
2. Store it only as the Key Vault secret `teams-new-ticket-webhook-url` (the deploy puts it
   there from a GitHub secret). Never in the repo, a parameter file or a chat.
3. Switch on with `TEAMS_NEW_TICKET_ENABLED` and `TEAMS_NOTIFICATIONS_ENABLED`, and set
   `PUBLIC_BASE_URL` so cards link to tickets. Out of office needs `PRESENCE_ENABLED` on the
   worker and the Graph permissions from step 3.

## 8. Deploy the apps

Run the **Deploy** workflow (Actions tab, `workflow_dispatch`, target `staging`) on a commit
whose CI is green. It builds and pushes the image, runs what-if, runs the migrate job, deploys
the apps and probes `/health`. Details and the known limits are in `infra/README.md`.

`DEPLOY_PUBLIC_APP` and `DEPLOY_WORKER` default to `true` on staging and `false` on production
(`infra/README.md`, "The three apps"). With the mail and Teams switches unset the worker idles,
so the first deploy can leave them all unset.

## 8a. Create the break-glass accounts (once, after the first deploy)

Two local super admins that are not anyone's Entra account, so there is always a way in when
Entra is down or the admins are locked out. They are made by a command, never from a web page, so
no secret crosses the web app. Run it once per account, after the first deploy has migrated the
database (the `web` app must be running: its `SESSION_SECRET_KEY` protects the stored TOTP secret).

```bash
# <webAppName> is the deployment output `webAppName`. Run it where you can read the output:
# it is shown once and goes nowhere else.
az containerapp exec -g rg-ticketing-staging -n <webAppName> --command "python -m app.breakglass create breakglass1"
az containerapp exec -g rg-ticketing-staging -n <webAppName> --command "python -m app.breakglass create breakglass2"
```

Each prints the account (`breakglass1@local.invalid`), a 32 character password, the TOTP secret (base32,
SHA-1, 6 digits, 30 seconds) and an `otpauth://` URI. **Store the password and the TOTP secret in IT
Glue straight away** (IT Glue holds the live code; or add the secret to an authenticator app). They
cannot be shown again. If one is lost, `reset` replaces both. There is no QR code (no dependency
for it); paste the URI or secret into IT Glue.

Other commands: `python -m app.breakglass list`, `reset <name>` (new password and secret, printed
once; ends the account's sessions), `unlock <name>` (clears a lockout). `create` refuses a third
account. `az containerapp exec` runs a one-off shell in the running container, so its output does not
reach Log Analytics. If exec is not available, the migrate job can run the command instead
(`az containerapp job start ... --args`), but a job's console output **is** collected in Log
Analytics, so the password would be written there: if you do this, run `reset` for that account
afterwards through exec and treat the first output as burned.

Sign in at `/auth/break-glass` on the web app with the username, password and the current code.
Every use is audited (`break_glass_sign_in`) and every active super admin gets a bell entry. If
`SESSION_SECRET_KEY` is ever rotated, the stored TOTP secrets become unreadable: run `reset` on both
accounts afterwards and update IT Glue.

## 9. Check it, then add what failed to the list

Everything below can only be proved here. Tick each, note the date, and say what you saw.

- [ ] `what-if` and the platform deploy succeed in a clean group (expect a first round of fixes).
      Nov 2026: `what-if` clean; `platform-1` created everything but the registry, storage and
      Key Vault, which failed only on the missing role-assignment permission. No Bicep fixes
      so far; re-run once the role is granted.
- [ ] The deploy workflow logs in with OIDC (no stored Azure secret) and the migrate job runs.
- [ ] `/health` is ok from an allowed address and the console is refused from an other address.
- [ ] Microsoft sign-in works against the real redirect URI (the dev login is a different path).
- [ ] The app reaches Postgres over the private network with SSL.
- [ ] Managed identities read Key Vault secrets and reach Blob Storage; a user-delegation SAS
      link downloads a file (Azurite may not support these).
- [ ] Forwarded headers: the HTTPS-only cookie works, and a forged `X-Forwarded-For` is
      recorded with the real address.
- [ ] A failed rollout leaves the old revision serving; rollback by re-running on the earlier commit.
- [ ] Teams: a new ticket posts a card; an unread notification posts after the delay with the
      @mention resolving to the person.
- [ ] Presence: out of office by Outlook automatic reply, Teams scheduled out of office and a
      calendar event each show "(out of office)" in the pickers within 15 minutes and clear again;
      a 403 from Graph shows as `job=presence status=failed`.
- [ ] Time zones: a browser reports its zone on sign-in (My settings shows it). Only if
      `MAILBOX_TIME_ZONES_ENABLED` is turned on (off by default): `MailboxSettings.Read` is consented
      and a requester who emails the shared mailbox gets their Outlook mailbox zone stored
      (`requestertimezone`, source `mailbox`) within about 5 minutes
      (`heartbeat job=requester-time-zones`); a sender outside the tenant gets the default.
- [ ] Break-glass (6C, step 8a): create both accounts with `az containerapp exec`, store the password
      and TOTP in IT Glue, then sign in at `/auth/break-glass` with each using IT Glue's current
      code. Check the audit log (`break_glass_sign_in`, a success and a deliberate failure), the bell
      entry for the other super admins, that the session ends after `break_glass_idle_minutes`, that
      `az containerapp exec` output does not appear in Log Analytics, and that `/auth/break-glass` is
      a 404 on the public app.
- [ ] Alerts reach `ALERT_EMAIL`; the worker heartbeat line appears in Log Analytics;
      Application Insights shows data.
- [ ] Point-in-time restore of the database to a scratch server works.
- [ ] Decide the console IP approach before production. Staging runs with `WEB_ALLOWED_CIDRS`
      `0.0.0.0/0` (open to everyone, test data only) because the VPN is split tunnel: remote
      staff reach the app from home or mobile addresses, so there is no VPN range to allow.
      Options: IT routes the Container Apps environment's IP through the VPN; office only; or
      Entra Conditional Access instead of IP filtering. The office line is `5.148.5.52` (its full
      static range is still to get from IT). The "console is refused from an other address"
      check above cannot pass until this is narrowed.
- [ ] Mailbox scoping: `Test-ServicePrincipalAuthorization` shows Mail.Read and Mail.Send in
      scope for the staging mailbox and out of scope for any other; a Graph read of another
      mailbox with the app's token is refused (403).
- [ ] Graph mail under RBAC for Applications: the subscription validation handshake, webhook
      delivery to the public URL, renewal and lifecycle events, the catch-up sweep, and mail in
      and out through Mimecast (allow an hour after creating the mailbox for its sync).

## 10. Costs and clean-up

Staging is sized small (burstable Postgres, 1 to 2 replicas, 30-day logs capped at 1 GB a day).
To remove staging entirely: `az group delete -n rg-ticketing-staging` (this deletes the
database; take a backup first if anything matters). Key Vault purge protection is off in
staging, but a deleted vault is still held for 90 days, and a recreated group gets the **same
vault name** (it comes from the group's ID), so the next deploy fails until the old vault is
purged. Purging needs a permission at subscription scope (e.g. Key Vault Contributor on the
subscription), which Owner on the group does not have. Not planned for staging.

**[Contributor]** can add a budget with an email alert on the group and can delete the group.
The staging budget (`budget-ticketing-staging`, 100 a month in the billing currency; emails at
50%, 80%, 100% actual and 100% forecast) was made with:

```bash
az rest --method put --url "https://management.azure.com/subscriptions/<subscription id>/resourceGroups/rg-ticketing-staging/providers/Microsoft.Consumption/budgets/budget-ticketing-staging?api-version=2023-05-01" --body '<budget JSON: category Cost, amount, timeGrain Monthly, timePeriod.startDate first of the month, notifications with contactEmails>'
```

A budget only warns, never stops anything, and cost data lags by up to a day.

## Go-live: the IT support mailbox

Today `ITSupport@vardags.com` forwards every email to the MSP (Kocho), who log it in their own
system. At go-live the app owns that mailbox:

1. **Before go-live:** end-to-end tests on staging with the MSP, using the staging shared mailbox:
   a handoff, the MSP's "has been received" acknowledgement (their ticket number is learned
   from it; it is believed by Microsoft's `compauth=pass` in the `Authentication-Results` header
   Graph returns, so check that header on the staging mailbox), a "has been updated" mail, a reply
   that leaves the user off, and a ticket resolved by IT while with the MSP.
2. **[Exchange admin]** Remove the forward from `ITSupport@vardags.com` to the MSP. Leaving it
   in place sends every email to both the MSP and the app, and the MSP opens a duplicate ticket
   for each.
3. **[Exchange admin]** Scope the production Graph app to `ITSupport@vardags.com` (step 3) and set
   `GRAPH_SHARED_MAILBOX` to it.
4. Tell the MSP: from that date tickets come from `ITSupport@vardags.com` with an `[INC-n]`
   reference in the subject; their acknowledgements and updates go back to that address.

# Infrastructure (Bicep)

One resource group per environment (`staging`, `production`), UK South. The
design and the reasons for it are in `docs/plan.md` under "Hosting on Azure".

Nothing here has been deployed. The templates compile and lint
(`bicep lint infra/main.bicep`) but have not been run against a subscription,
so expect a round of fixes on the first `what-if`.

## What gets created

| Module | Resources |
|---|---|
| `network` | VNet, a subnet for Container Apps, a delegated subnet and private DNS zone for Postgres |
| `monitoring` | Log Analytics workspace, Application Insights, action group (email) |
| `registry` | Container registry (Basic); each app identity can pull |
| `postgres` | PostgreSQL 16 Flexible Server, private access only, point-in-time restore |
| `storage` | Private blob container `attachments`, no account keys, lifecycle: cool tier after `attachmentsCoolAfterDays` (365), old versions dropped after 90 days |
| `keyvault` | Key Vault (RBAC) holding the database URL, session secret and other secrets |
| `container-apps-environment` | VNet-injected environment wired to Log Analytics |
| `container-app` | Used for `web`, `public` and `worker` |
| `migrate-job` | Manual job that runs `alembic upgrade head` |
| `alerts` | Database storage and CPU alerts to the action group |
| `automation` | Only with `MAILBOX_AUTOMATION_ENABLED=true`: the Automation account for scheduled mailbox changes, its own managed identity, the PowerShell 7.4 runtime with `ExchangeOnlineManagement` pinned, seven runbook shells, and the worker's role on it (see "Mailbox changes") |

Each app and the migrate job has its own user-assigned managed identity, so a
compromised app can use only what it was granted. This matters most for
`public`, which faces the internet.

| Identity | Pulls images | Reads these Key Vault secrets | Blob access |
|---|---|---|---|
| web | yes | database URL, session key, webhook state, SSO secret, Graph secret | yes |
| public | yes | database URL, internal session key, webhook state | no |
| worker | yes | same as public, plus the Graph secret and the Teams webhook URL when set | yes |
| migrate | yes | database URL only | no |

Secrets are granted one by one (role assignment on the secret, not the vault).
The public app and worker use a separate `INTERNAL_SESSION_SECRET_KEY`, so they
never hold the key that signs agent cookies and the two rotate independently.
The settings check only needs a non-default value in them; once the app has
`APP_ROLE` the check can skip it for those roles and this secret can go. The
database URL still carries the server admin login; a least-privilege database
role is a later step.

## First deploy (platform only)

Leave `IMAGE` unset: with no image only the platform is created (the registry
has no image yet), and no apps or job are deployed.

1. Create the resource group and give your deploying identity Owner (or
   Contributor plus User Access Administrator) on it. The templates create role
   assignments, so Contributor alone is not enough. Leave `ASSIGN_ROLES` unset
   (see "Role assignments").
2. Set the secrets in your shell (never in a file in the repo):

   ```powershell
   $env:POSTGRES_ADMIN_PASSWORD = '<letters and digits, 24+ characters>'
   $env:SESSION_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
   $env:INTERNAL_SESSION_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
   $env:GRAPH_WEBHOOK_CLIENT_STATE = python -c "import secrets; print(secrets.token_hex(16))"
   $env:ALERT_EMAIL = 'it-support@example.org'
   $env:WEB_ALLOWED_CIDRS = '203.0.113.0/24'   # office and VPN, comma separated
   ```

   `ALERT_EMAIL` and `WEB_ALLOWED_CIDRS` have no defaults: the deploy fails
   without them, so alerts never go to a dead address and the agent console is
   never opened to the internet by leaving the list out. For a deliberate
   throwaway test, `WEB_ALLOWED_CIDRS = '0.0.0.0/0'` says so explicitly.

   The database password goes into a connection URL, so the template
   URL-encodes it and the app and Alembic both accept the encoded form. Plain
   letters and digits are still the safer choice.
3. Preview, then deploy:

   ```powershell
   az deployment group what-if -g rg-ticketing-staging -f infra/main.bicep -p infra/staging.bicepparam
   az deployment group create  -g rg-ticketing-staging -f infra/main.bicep -p infra/staging.bicepparam
   ```

## Deploying the apps

1. Build and push the image to the registry named in the deployment output
   `registryLoginServer`. The identity that pushes needs `AcrPush` on the
   registry (assign it to the GitHub Actions app registration, see below).
2. Re-run the deployment with `IMAGE=<registry>/ticketing:<sha>` set. Setting
   the image is what deploys the apps and the migrate job.
3. Start the migrate job: `az containerapp job start -n <migrateJobName> -g <rg>`.
   The apps run with `RUN_MIGRATIONS=false`, so the job is the only migrator.

   **First image deploy:** the apps and the job are created together, and nothing
   starts the job for you. Until you run step 3 the apps are up but the database
   has no tables, so requests fail (`/health` still reports ok, because it only
   checks the connection). Run the job straight after the deployment. Once the
   deploy workflow does this itself on an environment that already has a job
   (job first, then apps); on the very first image deploy the job does not exist
   until the template creates it, so the workflow runs it straight after.

## The three apps (web, public, worker)

All three run the same image; `APP_ROLE` (set by the template) picks what each does.

| App | Role | Address | Does |
|---|---|---|---|
| `web` | `web` | IP restricted | the agent console and portal, attachments, sign-in |
| `public` | `public` | open to the internet | only `/health`, the Graph webhook and the emailed approval links; no sign-in, no files |
| `worker` | `worker` | none | Teams posts, presence, mail (once the mail jobs exist), the heartbeat; exactly one replica |

`DEPLOY_PUBLIC_APP` and `DEPLOY_WORKER` (`true`/`false`, read by `infra/*.bicepparam`) choose
which of the last two exist. Staging defaults both to `true`; production defaults both to `false`
until staging has proved them. With the public app off there is nowhere for Microsoft to deliver
the Graph webhook; with the worker off, Teams posts and mail queue and nothing sends them.

**Two addresses.** The emailed approval link must point at the public app and a Teams card's
"Open ticket" at the web app, so the worker is given both: `PUBLIC_BASE_URL` (the public app) and
`WEB_BASE_URL` (the web app). By default they are the generated Azure addresses, printed as the
deployment outputs `publicUrl` and `webUrl`. When a custom domain is set up, pass it as
`PUBLIC_BASE_URL` (the public app) and `WEB_BASE_URL` (the web app) to the deploy.

**Mail.** `MAIL_SENDING` is `off` (default, nothing is queued) or `graph` (the worker sends from
`GRAPH_SHARED_MAILBOX` through Graph). `graph` needs the `GRAPH_*` tenant, client and secret and the
mailbox, with `Mail.Send` granted in Entra and restricted to that mailbox (an application access
policy). The deploy workflow refuses `graph` without them. The app also has a `dev` mode that
sends nothing; it only works in the dev environment and the template does not allow it.

**Application Insights.** Every app receives `APPLICATIONINSIGHTS_CONNECTION_STRING` and the app
starts telemetry from it (`app/telemetry.py`; blank means off). The approval link carries a working
credential in its path, so `/approvals/`, `/feedback/` and `/reopen/` requests are excluded from request telemetry; uvicorn's
own access log redacts the token (`app/log_redaction.py`). Ingress logs are Azure's own and still
need checking on staging (docs/build-plan.md checklist).

**Storage.** `STORAGE_ACCOUNT_NAME` is set on every app, so the app uses Blob Storage with its own
managed identity (no keys). Only web (downloads, uploads) and worker (mail attachments) hold the
Blob Data Contributor and Delegator roles; the public app and migrate job have none, and the public
app cannot serve files (no route).

**Cool tier.** A lifecycle rule on the `attachments` container moves each blob to the cool access tier
`attachmentsCoolAfterDays` days after it was created (default 365; the staging parameter file reads
`ATTACHMENTS_COOL_AFTER_DAYS` if set, production fixes 365). Nothing is deleted by it; the only delete rule removes superseded
versions after 90 days. The app never sets a tier, and cool blobs are online, so reads and
user-delegation SAS links work unchanged (cool costs slightly more per read). Identical files share one blob, so a file re-attached to a new ticket after a year points at a blob that is already cool; that only costs the dearer reads. The 30-day early-deletion
charge on cool does not matter at 365 days, and an anonymisation delete (7C) of a cool blob only pays
the remainder of that charge. To prove the policy on staging, deploy a throwaway account with
`ATTACHMENTS_COOL_AFTER_DAYS=1` and look at a day-old blob's tier (policies run about once a day).

## Every deploy after that

`.github/workflows/deploy.yml` (a draft, never run) does, per environment: log in
with OIDC, push the image tagged with the commit SHA to that environment's
registry, run `what-if`, point the migrate job at the new image and wait for it,
deploy `infra/main.bicep` with the new `IMAGE` (this moves web, public and worker,
whichever the template deploys), then check each app's newest revision is ready,
healthy and on the new image, and probe `/health` on any app open to the
internet. Redeploying the template always sets the image, so it passes it every
time. Run it from the Actions tab ("Deploy", `workflow_dispatch`): `staging`, or
`staging-then-production`, where production starts only if staging succeeded and
then waits for the reviewers on the `production` environment. The image is built
once and the same build is pushed to both registries. A push-to-main trigger for
staging is in the file, commented out.

A failed rollout does not take traffic (Single revision mode keeps the old
revision serving), but the run goes red. If a migration has already run, it stays
applied, so write migrations that the previous release can live with. To go back,
re-run the workflow on the earlier commit, or `az containerapp revision activate`
the previous revision.

Limits: production deploys only from `main` (the run fails otherwise); the
migrate job is repointed at the new image but its other settings (env vars,
secrets) only change when the template runs afterwards, so a release that adds a
setting its migrations need must be deployed in two steps; a re-run of the same
commit overwrites the registry tag with a rebuilt image (apps keep the old one if
the template is unchanged); the workflow does not wait for CI, so run it only on a commit whose CI is
green; a failed `what-if` or deploy leaves the migration it already ran in place;
the web app is IP restricted so the runner cannot probe it (set the optional
`HEALTH_URL` variable if the runner's address is allowed).

Things that bite on redeploys and first runs:

- **Supply every secret on every deploy.** Each deploy rewrites the Key Vault
  secrets and each app's secret list from the variables in your shell. If
  `SSO_CLIENT_SECRET` or `GRAPH_CLIENT_SECRET` is missing from one run, the new
  revision silently loses it (the vault copy stays), SSO or Graph counts as
  unconfigured, and agents can no longer sign in with Microsoft. The deploy
  workflow should read all of them from the GitHub Environment's secrets.
- **Rotated secrets take effect on the next revision.** Every deploy writes a new
  version of each secret, but a running revision keeps the value it started with.
- **Role assignments can lag.** A brand new identity or role assignment can take
  a few minutes to propagate. Setting `IMAGE` on the very first deployment can
  therefore fail with a forbidden error reading a secret or a 401 pulling the
  image. Do the platform-only deploy first (as above) and retry if it happens.
- **Role assignments are never removed by a redeploy.** The template is applied
  incrementally, so taking a secret out of an app's list in `main.bicep` leaves
  the old Key Vault Secrets User assignment in place. Delete it by hand, or the
  "exactly its own secrets" guarantee drifts upward over time.

## GitHub Actions identity (one-off, by hand)

Create an app registration with a federated credential for the repository's
`staging` and `production` GitHub Environments. Store the client, tenant and
subscription IDs as GitHub Environment variables. No secrets are needed for Azure.

One federated credential per environment: issuer
`https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`,
subject `repo:<owner>@<owner id>/<repo>@<repo id>:environment:staging` (and
`...:environment:production`). This repository's GitHub tokens carry the numeric owner and
repository IDs; the plain `repo:<owner>/<repo>:...` form fails with `AADSTS700213`. The
exact subject is printed by the `azure/login` step ("subject claim") of a failed run.
The subject must match the GitHub Environment name exactly.

**GitHub setup** (Settings > Environments), for both `staging` and `production`:

| Kind | Name | Notes |
|---|---|---|
| Variable | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | the app registration and subscription |
| Variable | `AZURE_RESOURCE_GROUP` | e.g. `rg-ticketing-staging` |
| Variable | `ALERT_EMAIL`, `WEB_ALLOWED_CIDRS` | required, as in "First deploy" |
| Variable | `SSO_TENANT_ID`, `SSO_CLIENT_ID`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_SHARED_MAILBOX`, `SUPER_ADMIN_EMAILS` | optional; empty if unset |
| Variable | `TEAMS_NEW_TICKET_ENABLED`, `TEAMS_NOTIFICATIONS_ENABLED`, `PRESENCE_ENABLED`, `ENTRA_ACCOUNT_CHECK_ENABLED` | optional; `true` switches the feature on, unset means off (see "Teams and out-of-office") |
| Variable | `TEAMS_BOT_ENABLED`, `TEAMS_BOT_IDENTITY`, `TEAMS_BOT_APP_ID`, `TEAMS_BOT_TENANT_ID` | optional; `TEAMS_BOT_ENABLED=true` switches the Teams bot on (needs `DEPLOY_PUBLIC_APP`), `TEAMS_BOT_IDENTITY` is `managedIdentity` (default) or `clientSecret`, the app id only for `clientSecret`, the tenant defaults to the subscription's (see "Teams bot") |
| Variable | `DEPLOY_PUBLIC_APP`, `DEPLOY_WORKER` | optional; `true`/`false`. Empty: staging `true`, production `false` |
| Variable | `MAIL_SENDING` | optional; `off` (default) or `graph` (needs the `GRAPH_*` values and `GRAPH_SHARED_MAILBOX`) |
| Variable | `GRAPH_SUBSCRIPTION_ENABLED`, `MAIL_CATCH_UP_ENABLED`, `INBOUND_ENABLED` | optional; `true` turns on, on the worker only (phases 3B/3C: the Graph subscription pointing at the public app's `/webhooks/graph/notifications`, the missed-mail sweep, and turning fetched mail into replies and tickets). Off by default |
| Variable | `WEB_BASE_URL`, `PUBLIC_BASE_URL` | optional; custom domains of the web and public apps. Empty: the generated Azure addresses |
| Variable | `HEALTH_URL` | optional extra base URL to probe |
| Variable | `ASSIGN_ROLES` | optional; unset means `false` in the workflow (see "Role assignments"). `true` only once the identity holds the restricted Role Based Access Control Administrator |
| Secret | `POSTGRES_ADMIN_PASSWORD`, `SESSION_SECRET_KEY`, `INTERNAL_SESSION_SECRET_KEY`, `GRAPH_WEBHOOK_CLIENT_STATE` | required, same values as the platform deploy |
| Secret | `SSO_CLIENT_SECRET`, `GRAPH_CLIENT_SECRET`, `TEAMS_NEW_TICKET_WEBHOOK_URL`, `TEAMS_BOT_CLIENT_SECRET` | optional; supply on every deploy once set, or the revision loses them |

Add **required reviewers** on `production` (that is the approval gate) and,
optionally, restrict both environments to the `main` branch. The platform-only
deploy (README above) must have run first: the workflow expects the registry to exist.

Grant it, on each resource group:

- **Contributor**, to create and update the resources.
- **AcrPush** on each registry, to push images.

Contributor also covers what the workflow does besides deploying: listing the
registry, job and apps, updating the job image, and starting and reading job
executions.

### Role assignments

The templates give each app identity AcrPull on the registry, Key Vault Secrets
User on each of its own secrets, and the two Storage Blob roles. Contributor
cannot write role assignments, so the workflow deploys with `ASSIGN_ROLES=false`
(`assignRoles` in the template) and skips them. The person with Owner on the
group writes them by deploying from their own shell with `ASSIGN_ROLES` unset:
the platform deploy above, and again **whenever an identity or a secret is
added**, before the workflow deploys the change. Otherwise the new revision
cannot read its new secret and fails to start. That covers the first time
`SSO_CLIENT_SECRET`, `GRAPH_CLIENT_SECRET`, `TEAMS_NEW_TICKET_WEBHOOK_URL` or
`TEAMS_BOT_CLIENT_SECRET` is set. For that run, set the same secrets and
switches as the GitHub Environment holds (which secrets exist, and who may read
them, follows from them) and leave `IMAGE` unset: the apps are not touched, and
the next workflow deploy wires the new secret in.

With `MAILBOX_AUTOMATION_ENABLED=true` there is one more assignment here, **Automation Job Operator** for the worker on the
Automation account, and one that is not Azure RBAC (the Automation identity's Exchange rights, which need a Global or
Privileged Role Administrator). Both are described under "Mailbox changes: the Automation account", below.

To hand this to the pipeline instead, grant its identity **Role Based Access
Control Administrator** on the group, restricted with the condition "Allow user
to only assign selected roles to selected principals" to AcrPull, AcrPush, Key
Vault Secrets User, Storage Blob Data Contributor and Storage Blob Delegator (plus Automation Job Operator once the mailbox
switch is on; so it cannot grant itself Owner), then set the `ASSIGN_ROLES` variable to `true`
in the GitHub Environment. An Owner grant made with the portal's default
condition cannot give out that role; the subscription's Owner has to.

## Teams and out-of-office (phase 2)

Four switches, all off unless set to `true` (environment variables in your shell,
or GitHub Environment variables for the workflow; `infra/*.bicepparam` reads them):

| Variable | Where it lands | What it does |
|---|---|---|
| `TEAMS_NEW_TICKET_ENABLED` | web, public, worker, migrate job | A Teams channel post for each new ticket |
| `TEAMS_NOTIFICATIONS_ENABLED` | web, public, worker, migrate job | Also copies an unread notification to Teams after the person's delay |
| `PRESENCE_ENABLED` | worker only | The `presence` job marks agents who are out of office |
| `MAILBOX_TIME_ZONES_ENABLED` | worker only | The `requester-time-zones` job reads requesters' Outlook mailbox time zones (a later feature, see below) |
| `ENTRA_ACCOUNT_CHECK_ENABLED` | worker only | The nightly `entra-account-check` job makes agents whose Entra account is disabled or deleted inactive (needs `User.Read.All`, already consented; run time is the `entra_check_time` setting, 02:00) |

`TEAMS_NEW_TICKET_WEBHOOK_URL` (a Teams Workflows webhook for the support channel)
is a secret: the template stores it in Key Vault as `teams-new-ticket-webhook-url`
and only the worker identity may read it, since only the worker posts. It is
supplied at deploy time like the other secrets, never put in a file, and optional:
without it posts wait in the outbox and the worker logs a warning every minute.
Create the webhook in Teams ("Post to a channel when a webhook request is received")
and keep the channel IT-only, since cards show ticket subjects. Supply it on every
deploy once set, or the new revision loses it. To remove the webhook, delete the `teams-new-ticket-webhook-url` secret and the worker's role assignment on it by hand: deploying with it empty only stops wiring it, and incremental deployments never delete either.

The deploy workflow refuses a switch that is on without what it needs (Teams on with no webhook secret, presence on without the `GRAPH_*` settings) or a value other than `true`/`false`. A manual deploy has no such check. Turning a Teams switch on while `deployWorker` is false queues posts that nothing sends until the worker runs.

All of these only act in the worker, so `deployWorker` in the param files must be
`true` as well (it is `false` until the worker has the jobs you want).

**Out-of-office detection needs Graph permissions that the Bicep does not grant.**
Nothing here manages the Entra app registration, so in Entra, on the registration
behind `GRAPH_CLIENT_ID`, add the Microsoft Graph **application** permissions
`Presence.Read.All` and `User.Read.All` and grant admin consent. The `GRAPH_*`
tenant, client and secret must also be set. Without the consent the job logs
`job=presence status=failed` (a 403) and marks nobody.

**Later feature: requesters' mailbox time zones.** Not required now. The worker's `requester-time-zones`
job reads each requester's Outlook mailbox time zone with `GET /users/{id}/mailboxSettings`, but only when
`MAILBOX_TIME_ZONES_ENABLED=true` (default off; with it off the job does nothing and makes no Graph call, and
requesters use their account zone, set from the portal or browser, or the `default_time_zone` setting). To
roll it out after a stable release: add the Microsoft Graph **application** permission `MailboxSettings.Read`
to the same registration, grant admin consent, and set the variable. It conflicts with scoping the
registration to the shared mailbox (an application access policy that limits it to that mailbox refuses the
read, docs/plan.md security item 4), so decide the scoping first. Without the consent the reads are refused
(403): requesters keep the default zone and, when every lookup in a run is refused, the job logs `status=failed`.

## Teams bot (phase 8)

Switch: `TEAMS_BOT_ENABLED=true` (off by default; with it off nothing about the bot is created and the route answers 404).
It needs the public app (`DEPLOY_PUBLIC_APP=true`), which serves `POST /api/teams/messages`, and the worker for messages to
people (later phases). The template makes the **Azure Bot** (F0, single tenant) with the Teams channel on and the messaging
endpoint set to `<public app>/api/teams/messages` (`modules/teams-bot.bicep`), and gives the apps `TEAMS_BOT_ENABLED`,
`TEAMS_BOT_APP_ID` and `TEAMS_BOT_TENANT_ID`.

Who the bot is, `TEAMS_BOT_IDENTITY`:

- `managedIdentity` (default): the template creates the user-assigned identity `id-ticketing-<env>-bot`, attaches it to the
  public app and the worker, and registers the bot as it (`UserAssignedMSI`). No app registration and no secret. The
  application id is the identity's client id (output `teamsBotAppId`).
- `clientSecret`: make an Entra app registration by hand (single tenant, no redirect URI, no API permissions), set its client
  id as `TEAMS_BOT_APP_ID` and supply a client secret as `TEAMS_BOT_CLIENT_SECRET`. The secret is stored in Key Vault as
  `teams-bot-client-secret` and only the public app and worker identities may read it. Supply it on every deploy once set.

Build the Teams app package with the application id (`python teams-app/build_package.py --environment staging --bot-app-id
<teamsBotAppId> --public-base-url https://<public app> --developer-name "Vardags IT"`: the privacy, terms and website addresses
default to `<base>/legal/privacy`, `<base>/legal/terms` and `<base>/portal`, and each can be set by hand), upload it in the Teams
admin center (Manage apps) and install it for a test user. An app installed for people by an admin setup policy cannot be
uninstalled by them (Microsoft's documentation); if someone removes it themselves, the next message to them is refused with a 403
or 404 and the bot falls back to the channel mention, where that is switched on. The deploy workflow refuses the switch on without the public app, or `clientSecret` without its id
and secret.

## Mailbox changes: the Automation account

Scheduled mailbox changes (forwarding, full access, Send As; tracker #305) reach Exchange Online only through an **Azure
Automation account** that runs fixed runbooks. The app never holds Exchange rights. Nothing here is deployed or proved:
every check is on the staging list (`docs/azure-setup.md`, step 9).

Switch: `MAILBOX_AUTOMATION_ENABLED=true` (off by default; off creates nothing and leaves the worker alone) with
`MAILBOX_TENANT_DOMAIN`, the tenant's `<name>.onmicrosoft.com` name (a GitHub Environment variable; the Deploy workflow
refuses the switch without it). It needs the `Microsoft.Automation` resource provider registered (an Owner, once).

What the template makes (`modules/automation.bicep`, `aa-ticketing-<env>-mailbox`):

- the account with its own **system-assigned managed identity**, never shared with the web, public or worker identities,
  and account-key authentication off;
- a PowerShell **7.4** runtime environment (7.2 is deprecated; 7.4 exists only as a runtime environment) with the
  `ExchangeOnlineManagement` module pinned (`exchangeModuleVersion`, 3.9.2), loaded from the PowerShell Gallery;
- seven runbooks: `Set-MailForwarding`, `Clear-MailForwarding`, `Grant-FullAccess`, `Remove-FullAccess`, `Grant-SendAs`,
  `Remove-SendAs` and `Read-MailboxChange`. Each takes fixed parameters (addresses and a flag, never a script),
  connects with `Connect-ExchangeOnline -ManagedIdentity`, checks its parameters again, treats "already set" as success,
  reads back and writes one JSON object. Their source is `infra/runbooks/*.ps1` (`Common.ps1` holds the shared helpers);
- no secret anywhere.

ARM takes runbook text only from a URL and the repository is private, so the template creates the runbooks empty and the
Deploy workflow uploads and publishes them after every deploy (`.github/scripts/publish-runbooks.sh`, using
`tools/build_runbooks.py` to put `Common.ps1` into each; Contributor on the group is enough). To publish by hand:
`python tools/build_runbooks.py <folder>` then the same three calls (`draft/content`, `publish`) with `az rest`.

The worker gets `MAILBOX_EXECUTOR=azure`, `MAILBOX_AUTOMATION_RESOURCE_ID` (the account) and `MAILBOX_TENANT_DOMAIN`. The
job timeout is `MAILBOX_AUTOMATION_TIMEOUT_SECONDS` (default 600, the poll interval `MAILBOX_AUTOMATION_POLL_SECONDS`, 5).

### The two role assignments (not the Deploy workflow's job)

The Deploy workflow runs with `ASSIGN_ROLES=false`, so the first is written by the owner's shell deploy (or by hand), and the
second is not Azure RBAC at all.

1. **The worker's identity on the Automation account, only: Automation Job Operator** (role id
   `4fe576fe-1146-4730-92eb-48519fa6bf9f`). It is the smallest built-in role that creates jobs and reads their output: it
   holds `jobs/write`, `jobs/read` and `jobs/output/read` (plus stop, suspend, resume and stream read) and nothing that
   edits runbooks, assets or the account. Microsoft documents it as "create and manage jobs for all runbooks in the
   account": Azure Automation allows role assignments only at the account scope, so it cannot be limited to single
   runbooks. The limit is that the app only ever asks for the seven runbooks above (`ALLOWED_RUNBOOKS` in
   `app/mailbox_executor_azure.py`) and the account holds nothing else. Docs:
   [role permissions](https://learn.microsoft.com/azure/automation/automation-role-based-access-control#automation-job-operator),
   [built-in roles](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#automation-job-operator).
   Written by `automation.bicep` when `ASSIGN_ROLES` is unset (the owner's shell deploy, as for every other identity here), or by
   hand:

   ```powershell
   az role assignment create --assignee-object-id <worker identity principal id> --assignee-principal-type ServicePrincipal `
     --role "Automation Job Operator" --scope <MAILBOX_AUTOMATION_RESOURCE_ID>
   ```

   Not confirmed (staging check): the role does not list `runbooks/read` or `automationAccounts/read`, and the job-create
   call names the runbook without reading it, so it should work. If Azure refuses, the next step up is **Automation Operator**
   (`d3881f73-407a-4167-8283-e981cbba0404`), which adds read of the account and runbooks. Record which one worked.

2. **The Automation account's identity in Microsoft Entra: the `Exchange.ManageAsApp` application permission and an Exchange
   directory role.** **Preferred instead of the directory role:** the least-privilege setup in
   [`docs/exchange-mailbox-role.md`](../docs/exchange-mailbox-role.md) (a custom Exchange role with only the commands and
   parameters the runbooks use, limited to a group of mailboxes; on staging, test accounts only). Use the directory role
   below only if that setup does not work with a managed identity. Both need a **Global Administrator or Privileged Role Administrator**; the deployer's Azure Owner does
   not give that. **Who grants them: to be named here before go-live** (`docs/azure-setup.md` records the
   deployer as a Global admin, the issue (#308) says the owner is not one: confirm which). Take the identity's object id from the
   deployment output `mailboxAutomationIdentityPrincipalId`. From
   [Microsoft's steps](https://learn.microsoft.com/powershell/exchange/connect-exo-powershell-managed-identity):

   ```powershell
   Connect-MgGraph -Scopes AppRoleAssignment.ReadWrite.All,Application.Read.All,RoleManagement.ReadWrite.Directory
   $MI_ID = '<mailboxAutomationIdentityPrincipalId>'
   $exo = Get-MgServicePrincipal -Filter "AppId eq '00000002-0000-0ff1-ce00-000000000000'"   # Office 365 Exchange Online
   New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $MI_ID -PrincipalId $MI_ID `
     -AppRoleId 'dc50a0fb-09a3-484d-be87-e023b12c6440' -ResourceId $exo.Id                   # Exchange.ManageAsApp
   $role = Get-MgRoleManagementDirectoryRoleDefinition -Filter "DisplayName eq 'Exchange Administrator'"
   New-MgRoleManagementDirectoryRoleAssignment -PrincipalId $MI_ID -RoleDefinitionId $role.Id -DirectoryScopeId '/'
   ```

   Microsoft's page documents **Exchange Administrator** for this (and lists Exchange Recipient Administrator, Global Reader and
   others as supported directory roles without saying what each can do). Exchange Administrator is broad: the issue asks for a
   staging test of a narrower role that still lets all seven runbooks work (try Exchange Recipient Administrator first,
   `Add-RecipientPermission` and `Add-MailboxPermission` being recipient management). Record the answer in
   `docs/azure-setup.md`. If `Get-MgServicePrincipal` returns nothing, follow the section "What to do if the Office 365
   Exchange Online resource is not available" on the Microsoft page.

## Custom domain (at roll-out)

`web` is IP restricted, and Azure's free managed certificate needs the app to
be reachable from the certificate authority, including at every renewal. So
`web` uses a certificate you supply (kept in Key Vault) rather than a managed
one. The `public` app has no IP restriction and can use a managed certificate.
Both need a CNAME to the app's generated name plus a TXT record for ownership.
Update the Entra redirect URI and the web address (`WEB_BASE_URL`, Bicep `webAppBaseUrl`) at the same
time, and `PUBLIC_BASE_URL` for the public app, which the emailed approval links use.

## Not covered yet

- Custom domains and certificates.
- Front Door and WAF (considered and not chosen, see the plan).
- Private endpoints for Key Vault and Storage (both are Entra-only and
  reachable over the public endpoint for now).
- Worker heartbeat and Graph expiry alerts (arrive with those features).

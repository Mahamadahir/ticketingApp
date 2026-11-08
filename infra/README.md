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
   assignments, so Contributor alone is not enough.
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
`attachmentsCoolAfterDays` days after it was created (default 365; the parameter file reads
`ATTACHMENTS_COOL_AFTER_DAYS` if set). Nothing is deleted by it; the only delete rule removes superseded
versions after 90 days. The app never sets a tier, and cool blobs are online, so reads and
user-delegation SAS links work unchanged (cool costs slightly more per read). The 30-day early-deletion
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
subject `repo:<owner>/<repo>:environment:staging` (and `...:environment:production`).
The subject must match the GitHub Environment name exactly.

**GitHub setup** (Settings > Environments), for both `staging` and `production`:

| Kind | Name | Notes |
|---|---|---|
| Variable | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | the app registration and subscription |
| Variable | `AZURE_RESOURCE_GROUP` | e.g. `rg-ticketing-staging` |
| Variable | `ALERT_EMAIL`, `WEB_ALLOWED_CIDRS` | required, as in "First deploy" |
| Variable | `SSO_TENANT_ID`, `SSO_CLIENT_ID`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_SHARED_MAILBOX`, `SUPER_ADMIN_EMAILS` | optional; empty if unset |
| Variable | `TEAMS_NEW_TICKET_ENABLED`, `TEAMS_NOTIFICATIONS_ENABLED`, `PRESENCE_ENABLED`, `ENTRA_ACCOUNT_CHECK_ENABLED` | optional; `true` switches the feature on, unset means off (see "Teams and out-of-office") |
| Variable | `DEPLOY_PUBLIC_APP`, `DEPLOY_WORKER` | optional; `true`/`false`. Empty: staging `true`, production `false` |
| Variable | `MAIL_SENDING` | optional; `off` (default) or `graph` (needs the `GRAPH_*` values and `GRAPH_SHARED_MAILBOX`) |
| Variable | `GRAPH_SUBSCRIPTION_ENABLED`, `MAIL_CATCH_UP_ENABLED`, `INBOUND_ENABLED` | optional; `true` turns on, on the worker only (phases 3B/3C: the Graph subscription pointing at the public app's `/webhooks/graph/notifications`, the missed-mail sweep, and turning fetched mail into replies and tickets). Off by default |
| Variable | `WEB_BASE_URL`, `PUBLIC_BASE_URL` | optional; custom domains of the web and public apps. Empty: the generated Azure addresses |
| Variable | `HEALTH_URL` | optional extra base URL to probe |
| Secret | `POSTGRES_ADMIN_PASSWORD`, `SESSION_SECRET_KEY`, `INTERNAL_SESSION_SECRET_KEY`, `GRAPH_WEBHOOK_CLIENT_STATE` | required, same values as the platform deploy |
| Secret | `SSO_CLIENT_SECRET`, `GRAPH_CLIENT_SECRET`, `TEAMS_NEW_TICKET_WEBHOOK_URL` | optional; supply on every deploy once set, or the revision loses them |

Add **required reviewers** on `production` (that is the approval gate) and,
optionally, restrict both environments to the `main` branch. The platform-only
deploy (README above) must have run first: the workflow expects the registry to exist.

Grant it, on each resource group:

- **Contributor**, to create and update the resources.
- **Role Based Access Control Administrator**, restricted with a condition to
  assigning only AcrPull, Key Vault Secrets User, Storage Blob Data Contributor
  and Storage Blob Delegator. The templates write role assignments on every
  deploy (identities to registry, vault secrets and storage). Contributor
  cannot do that, so without this the first redeploy from CI fails with an
  authorization error. The condition stops the pipeline granting itself Owner.
- **AcrPush** on each registry, to push images.

Contributor also covers what the workflow does besides deploying: listing the
registry, job and apps, updating the job image, and starting and reading job
executions.

The alternative is to move all role assignments into a separate template run
by hand, and keep the pipeline at Contributor.

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

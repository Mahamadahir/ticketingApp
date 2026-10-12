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
| `storage` | Private blob container `attachments`, no account keys, lifecycle to cool tier after a year |
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
| public | yes | database URL, internal session key, webhook state, Graph secret | no |
| worker | yes | same as public | yes |
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

   The database password goes into a connection URL, so avoid `@ : / ? #`
   (the template URL-encodes it, but plain characters are safer).
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
   deploy workflow exists it does this in order: start the job, wait for it,
   then update the apps.

`deployPublicApp` and `deployWorker` stay false until the app has the
`APP_ROLE=public` mode and the worker entrypoint. Turned on earlier, `public`
would serve the whole agent console on an open address and the worker would
crash-loop.

## Every deploy after that

The GitHub workflow (not written yet) will: log in with OIDC, build and push
the image tagged with the commit SHA, start the migrate job and wait for it,
then deploy with the new `IMAGE`. Redeploying the template always sets the
image, so pass it every time.

## GitHub Actions identity (one-off, by hand)

Create an app registration with a federated credential for the repository's
`staging` and `production` GitHub Environments. Store the client, tenant and
subscription IDs as GitHub Environment variables. No secrets are needed.

Grant it, on each resource group:

- **Contributor**, to create and update the resources.
- **Role Based Access Control Administrator**, restricted with a condition to
  assigning only AcrPull, Key Vault Secrets User, Storage Blob Data Contributor
  and Storage Blob Delegator. The templates write role assignments on every
  deploy (identities to registry, vault secrets and storage). Contributor
  cannot do that, so without this the first redeploy from CI fails with an
  authorization error. The condition stops the pipeline granting itself Owner.
- **AcrPush** on each registry, to push images.

The alternative is to move all role assignments into a separate template run
by hand, and keep the pipeline at Contributor.

## Custom domain (at roll-out)

`web` is IP restricted, and Azure's free managed certificate needs the app to
be reachable from the certificate authority, including at every renewal. So
`web` uses a certificate you supply (kept in Key Vault) rather than a managed
one. The `public` app has no IP restriction and can use a managed certificate.
Both need a CNAME to the app's generated name plus a TXT record for ownership.
Update the Entra redirect URI and `publicBaseUrl` at the same time.

## Not covered yet

- Custom domains and certificates.
- Front Door and WAF (considered and not chosen, see the plan).
- Private endpoints for Key Vault and Storage (both are Entra-only and
  reachable over the public endpoint for now).
- Worker heartbeat and Graph expiry alerts (arrive with those features).

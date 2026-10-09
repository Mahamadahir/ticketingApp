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
| `registry` | Container registry (Basic); the shared identity can pull |
| `postgres` | PostgreSQL 16 Flexible Server, private access only, point-in-time restore |
| `storage` | Private blob container `attachments`, no account keys, lifecycle to cool tier after a year |
| `keyvault` | Key Vault (RBAC) holding the database URL, session secret and other secrets |
| `container-apps-environment` | VNet-injected environment wired to Log Analytics |
| `container-app` | Used for `web`, `public` and `worker` |
| `migrate-job` | Manual job that runs `alembic upgrade head` |
| `alerts` | Database storage and CPU alerts to the action group |

One user-assigned managed identity is shared by every app and the job. It pulls
images, reads Key Vault secrets and reads and writes blobs.

## First deploy (platform only)

`deployApps` is false by default because the registry has no image yet.

1. Create the resource group and give your deploying identity Owner (or
   Contributor plus User Access Administrator) on it. The templates create role
   assignments, so Contributor alone is not enough.
2. Set the secrets in your shell (never in a file in the repo):

   ```powershell
   $env:POSTGRES_ADMIN_PASSWORD = '<letters and digits, 24+ characters>'
   $env:SESSION_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
   $env:GRAPH_WEBHOOK_CLIENT_STATE = python -c "import secrets; print(secrets.token_hex(16))"
   $env:ALERT_EMAIL = 'it-support@example.org'
   ```

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
2. Re-run the deployment with `deployApps=true` and `IMAGE=<registry>/ticketing:<sha>`.
3. Run the migrations before traffic moves: `az containerapp job start -n <migrateJobName> -g <rg>`.
   (The app entrypoint still runs `alembic upgrade head` itself; see "Code
   changes the Azure plan needs" in `docs/plan.md`.)

## Every deploy after that

The GitHub workflow (not written yet) will: log in with OIDC, build and push
the image tagged with the commit SHA, start the migrate job and wait for it,
then deploy with the new `IMAGE`. Redeploying the template always sets the
image, so pass it every time.

## GitHub Actions identity (one-off, by hand)

Create an app registration with a federated credential for the repository's
`staging` and `production` GitHub Environments. Grant it Contributor on each
resource group plus `AcrPush` on each registry. Store the client, tenant and
subscription IDs as GitHub Environment variables. No secrets are needed.

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

# Azure setup: from an empty subscription to a running staging

This is the by-hand checklist for the person with access to the subscription. Nothing here
is run by the app or by Claude. The templates and what they create are in `infra/README.md`;
the design is in `docs/plan.md` ("Hosting on Azure"). Nothing has ever been deployed, so
expect fixes on the first `what-if`.

Do staging first. Production is the same steps with `production` in the names, after staging
has passed the checks at the end of this page.

**Conventions.** Region UK South. Resource group `rg-ticketing-staging` (production:
`rg-ticketing-production`). Secrets are typed into your shell or the GitHub Environment,
never into a file in the repo.

## 0. Decide first (one line each, write the answers down)

| Decision | Needed for |
|---|---|
| Shared mailbox address (plan says TBC) | Phase 3 mail, `GRAPH_SHARED_MAILBOX` |
| Office and VPN IP ranges (CIDRs) | `WEB_ALLOWED_CIDRS`, the agent console is IP restricted |
| Alert email address | `ALERT_EMAIL`, a real monitored mailbox |
| Public address for staging (generated `*.azurecontainerapps.io` name is fine to start) | `PUBLIC_BASE_URL`, Entra redirect URI, Graph webhook |
| Teams channel for new-ticket and notification posts | Teams Workflows webhook (step 7) |

## 1. Subscription and the resource group

You have a subscription; this app has no resource group yet.

1. Sign in: `az login` then `az account set --subscription <subscription id>`.
2. Register the resource providers once per subscription (needs Owner or Contributor on the
   subscription; skip any already registered):

   ```powershell
   foreach ($p in 'Microsoft.App','Microsoft.ContainerRegistry','Microsoft.DBforPostgreSQL',
                  'Microsoft.KeyVault','Microsoft.Storage','Microsoft.Network',
                  'Microsoft.OperationalInsights','Microsoft.Insights','Microsoft.ManagedIdentity') {
     az provider register --namespace $p
   }
   az provider show --namespace Microsoft.App --query registrationState   # wait for Registered
   ```
3. Create the group:

   ```powershell
   az group create -n rg-ticketing-staging -l uksouth
   ```
4. Give **your own** account Owner on that group (the templates create role assignments, so
   Contributor alone fails):

   ```powershell
   az role assignment create --assignee <your user object id> --role Owner `
     --scope /subscriptions/<subscription id>/resourceGroups/rg-ticketing-staging
   ```
5. Check the quota for Container Apps and PostgreSQL Flexible Server (burstable `Standard_B1ms`)
   in UK South. If the first deploy fails with a quota or SKU error, that is the place to look.

## 2. Platform-only deploy (no apps yet)

Leave `IMAGE` unset so only the platform (network, monitoring, registry, Postgres, storage,
Key Vault, Container Apps environment, alerts) is created. Full text is in `infra/README.md`,
"First deploy". In short:

```powershell
$env:POSTGRES_ADMIN_PASSWORD = '<letters and digits, 24+ characters>'
$env:SESSION_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:INTERNAL_SESSION_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:GRAPH_WEBHOOK_CLIENT_STATE = python -c "import secrets; print(secrets.token_hex(16))"
$env:ALERT_EMAIL = '<the monitored address>'
$env:WEB_ALLOWED_CIDRS = '<office/VPN CIDRs, comma separated>'

az deployment group what-if -g rg-ticketing-staging -f infra/main.bicep -p infra/staging.bicepparam
az deployment group create  -g rg-ticketing-staging -f infra/main.bicep -p infra/staging.bicepparam
```

Keep the same secret values: you will put them in GitHub in step 6 and the next deploys must
use identical values. Store them in your password manager now.

Write down the deployment outputs (`registryLoginServer`, `migrateJobName`, the Key Vault
name, the storage account name).

## 3. Entra app registrations (three, all in the same tenant)

| App registration | Purpose | Permissions (application unless noted) | Redirect URI |
|---|---|---|---|
| **ticketing-sso-staging** | Agents sign in with Microsoft | delegated `openid`, `profile`, `email` | `https://<staging web address>/auth/callback` |
| **ticketing-graph-staging** | The app reads and sends the shared mailbox, checks presence | `Mail.Read`, `Mail.Send`, `Presence.Read.All`, `User.Read.All` (admin consent; `MailboxSettings.Read` only for the later requester time-zone feature, see below) | none |
| **ticketing-github-staging** | GitHub Actions deploys with OIDC | none in Entra; Azure roles in step 5 | none |

Notes:

- Create a client secret for the first two. Put the secret in the GitHub Environment (step 6),
  not anywhere in the repo. Record the expiry date in a calendar; a lapsed secret silently
  stops sign-in or mail.
- Grant admin consent for the Graph permissions (needs a Global Administrator or Privileged
  Role Administrator).
- **Scope the mail permissions to the one mailbox.** `Mail.Read` and `Mail.Send` as application
  permissions reach every mailbox in the tenant unless restricted. Restrict the Graph app to the
  shared mailbox only, using Exchange Online RBAC for Applications (or, on older tenants, an
  Application Access Policy). Check Microsoft's current instructions for the one your tenant
  supports; this is a security step, not optional.
- `SUPER_ADMIN_EMAILS` (optional) lists who becomes super admin on first sign-in, so there is a
  way into `/admin`.

## 4. The shared mailbox

1. Create the shared mailbox in Exchange Online (the decision from step 0).
2. Apply the mailbox scoping from step 3 to the Graph app.
3. Mimecast sits in front of Exchange Online as the mail gateway. Nothing in the app talks to
   it, but confirm with whoever runs Mimecast that inbound mail to this address is delivered
   to the mailbox and that outbound mail from it is allowed (SPF/DKIM for the sending domain),
   or replies will land in spam.

Phase 3 (Graph mail) is not built yet; this step can wait until 3B/3D are merged, but the
mailbox and the app registration take time to approve, so start early.

## 5. GitHub Actions can deploy

Described in `infra/README.md`, "GitHub Actions identity". In short:

1. On `ticketing-github-staging` add a **federated credential**: issuer
   `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`,
   subject `repo:<owner>/<repo>:environment:staging` (and one for `production` later). The
   subject must match the GitHub Environment name exactly.
2. On `rg-ticketing-staging` grant that app registration:
   - **Contributor**;
   - **Role Based Access Control Administrator**, with a condition limiting it to assigning
     AcrPull, Key Vault Secrets User, Storage Blob Data Contributor and Storage Blob Delegator;
   - **AcrPush** on the registry created in step 2.

## 6. GitHub Environments

Settings > Environments > `staging` (and later `production`, with **required reviewers**; that
is the approval gate; optionally restrict both to the `main` branch).

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

`deployPublicApp` and `deployWorker` stay off until the public mode (Phase 3A) and the worker
entrypoint are wanted in the environment; turned on earlier, `public` would serve the whole
console on an open address.

## 9. Check it, then add what failed to the list

Everything below can only be proved here. Tick each, note the date, and say what you saw.

- [ ] `what-if` and the platform deploy succeed in a clean group (expect a first round of fixes).
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
- [ ] Alerts reach `ALERT_EMAIL`; the worker heartbeat line appears in Log Analytics;
      Application Insights shows data.
- [ ] Point-in-time restore of the database to a scratch server works.
- [ ] Phase 3 when built: subscription validation handshake, webhook delivery to the public URL,
      renewal and lifecycle events, mail in and out through Mimecast.

## 10. Costs and clean-up

Staging is sized small (burstable Postgres, 1 to 2 replicas, 30-day logs capped at 1 GB a day).
To remove staging entirely: `az group delete -n rg-ticketing-staging` (this deletes the
database; take a backup first if anything matters). Key Vault purge protection is off in
staging so a deleted vault can be recreated with the same name.

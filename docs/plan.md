# Delivery plan

Agreed scope and build order for the ticketing app, as at 9 October 2026.
Hosting moved from AWS (EC2) to Azure on 9 October. Nothing has been deployed
anywhere: the app is built and tested locally until it is ready for staging.
Values marked **TBC** are decided later and stored as admin settings or
environment config, not hard-coded.

## Platform

**Database.** PostgreSQL stays. Alembic replaces `create_all` before any
schema change, since almost every phase below alters tables.

**Background worker.** A separate `worker` service runs APScheduler: a
service in `docker-compose.yml` locally, a Container App fixed at one replica
in Azure. Running jobs inside the web process would execute them once per
uvicorn worker. Jobs: Graph subscription renewal, mail catch-up, SLA timers,
MSP handoff, chase emails, auto-close, delayed Teams notifications, presence
refresh.

**Jobs are driven by the database, not by APScheduler memory** (agreed 14 Oct 2026). The
worker restarts on every deploy and any crash, and a single replica is a single point of
failure, so nothing may depend on a timer that lived only in the process.
- *Recurring jobs* (renewal, catch-up poll, 18:00 MSP handoff, chase, auto-close, presence)
  are rows in a `scheduled_job` table: name, `next_run_at`, `last_success_at`, lease owner and
  expiry. APScheduler (or a plain loop) only wakes every minute and runs whatever is due. After
  downtime the job simply runs late, and each job is written to be safe to run late and twice
  (it works out what is due from ticket data, e.g. "tickets that will breach before 08:00 and
  have not been handed over", rather than "what happened since the last tick").
- *One-off work* (a Graph notification to fetch, a Teams message to send after N minutes, an
  email to send) is a row in a `job` table with status, attempts, `run_after`, `last_error`. The
  worker claims rows with `SELECT ... FOR UPDATE SKIP LOCKED`, so a second replica (or a
  rolling deploy overlap) cannot double-process. Failure schedules a retry with exponential
  backoff up to a limit, then marks the row `dead` and raises an alert and an admin page entry.
- *Missed-run guard.* The 18:00 handoff also has a late-run rule: if the worker is found past
  18:00 with the job not yet run, it runs at once (and says so in the audit log); a heartbeat
  alert covers the case where the worker is down entirely.
- The Graph webhook (public app) only inserts a `job` row and returns 202; the worker does
  the fetch. This is the queue the Mail section relies on.
- Uses Postgres only, no extra Azure service. A Service Bus queue is the upgrade path if
  volume ever needs it.
- *Where this stands (20 Oct 2026).* One-off work is built this way: the Teams and email outboxes
  (`TeamsPost`, `OutboundEmail`) are rows the worker claims with `FOR UPDATE SKIP LOCKED` and retries
  with backoff, and the Graph webhook (3A) will only insert a row and return 202. The recurring-jobs
  table is built (phase 4A, `app/scheduled_jobs.py`): one worker tick a minute claims due rows with
  leases, runs each late job once (an audit line says it was late) and records failures. Only the new phase 4 jobs
  use it; the worker's existing jobs (`presence`, `mail-out`, ...) still run from APScheduler on a fixed interval and a
  single-instance lock, which is acceptable while each is safe to run late and twice. Moving them over, and the
  18:00 MSP handoff (which must not be missed), are phase 5.

**Attachments.** Private Azure Blob Storage container. Files are served
through short-lived user-delegation SAS links after a ticket visibility check
(details under "Hosting on Azure"). One upload size limit (**TBC**) for portal
uploads and email attachments, including inline images. Defender for Storage
malware scanning is optional, to switch on once costs are known.

**Backups.** Azure Database for PostgreSQL takes automated backups and
supports point-in-time restore anywhere in the retention window (7 to 35
days, **TBC**; the window is the maximum data loss). No `pg_dump` job. Monthly
restore test into a scratch server. A second, independent copy (scheduled
`pg_dump` to Blob Storage) is a later option if the retention window is not
enough.

**Data retention.** Tickets, messages, attachments and the audit log are kept
indefinitely. Raise with the data protection lead: UK GDPR's storage
limitation principle needs a written justification. Regardless of outcome,
build a subject access export (everything held on one person) and requester
anonymisation (keep the ticket, remove name, email and attachments).
Attachments older than a year move to the cool storage tier.

**Health and monitoring.**

- `/health` checks the app and database. Container Apps readiness probes poll
  it, and an Azure Monitor alert on replica restarts or failed requests
  notifies the action group (email now, Teams webhook later).
- Dead man's switch: the worker and each scheduled job write a heartbeat log
  line (or custom event) on success, with a log-query alert that fires when
  none has arrived within the expected interval. The line is exactly
  `heartbeat job=<name> status=ok duration_ms=<n>`; alert on no `heartbeat job=heartbeat`
  line for about three `WORKER_HEARTBEAT_SECONDS` (the alert itself is Bicep, not built yet).
- `/health` also warns when: Graph subscription expiry is under 24 hours or
  the last catch-up poll is over 15 minutes old. The database alerts cover
  storage and CPU. Backup age and disk checks are no longer needed: backups
  and disks are managed by Azure.
- Telemetry goes to Application Insights (requests, exceptions, calls to
  Postgres and Graph) and Log Analytics (container logs), kept 30 days in
  staging and 90 in production, with a daily ingestion cap.

**Environments.** The current environment is staging until go-live, then a
separate production environment is built. Staging has its own Entra app
registrations and a test mailbox, and never touches the real shared mailbox.
The deploy workflow (not written yet) will target an environment: staging from
`main`, production from a release tag. Until the app is ready, everything runs
locally with `docker compose`.

## Hosting on Azure

Replaces the earlier AWS design (EC2, ALB, WAF, RDS, S3, CloudWatch). Region
UK South, one resource group per environment, all defined in Bicep under
`infra/` (see `infra/README.md`). Deployed from GitHub Actions with OIDC; no
long-lived cloud credentials.

**Who uses what.** The web app is for agents and admins. Everyone else mostly
emails or uses Teams, so the end-user portal is a secondary channel and
nothing relies on requesters reaching it.

**Apps.** One image, run as separate Azure Container Apps so each gets the
right exposure:

| App | Serves | Ingress | Replicas |
|---|---|---|---|
| `web` | Agent console, admin, SSO callback, portal, break-glass login, outage mode | Public URL, restricted to office and VPN ranges | 1 to 2, scaled on concurrent HTTP requests |
| `public` | Graph webhook, `/feedback/*`, `/reopen/*`, Teams bot endpoint, `/health` | Public, unrestricted | 1 to 2, never zero (Graph expects an answer within 3 seconds) |
| `worker` | APScheduler jobs | None | Exactly 1 |
| `migrate` job | `alembic upgrade head` | None | Run on demand before each deploy |

`public` exists because feedback and reopen links must work from anywhere and
Microsoft must reach the webhook, while Container Apps IP restrictions apply
per app, not per path. It ships with `APP_ROLE=public`, which mounts only
those routes. "Scaled on HTTP" means a scaling rule on concurrent requests,
not a protocol choice.

**HTTPS only.** TLS ends at the Container Apps ingress, which redirects HTTP
to HTTPS (`allowInsecure` is false). Inside the platform the ingress talks to
the container over plain HTTP on port 8000. uvicorn must trust the forwarded
headers or the HTTPS-only session cookie and redirects break.

**Addresses.** Staging and early testing use the generated
`*.azurecontainerapps.io` URLs, which are stable for the life of the
environment. At initial roll-out `web` moves to the internal domain. A free
managed certificate needs the app reachable by the certificate authority, at
issuance and every renewal, so IP-restricted `web` uses a certificate we
supply (kept in Key Vault); `public` can use a managed one. Entra redirect
URIs, `SSO_REDIRECT_URI` and the Graph notification URL change with the
domain.

**Database.** Azure Database for PostgreSQL Flexible Server, version 16, in a
delegated subnet with no public endpoint (burstable SKU in staging).
`sslmode=require`. Admin password lives in Key Vault. Entra authentication
for the database is a later option.

**Storage decisions.**

- Authentication: managed identity only. Shared key access and public blob
  access are off.
- Download links: pages link to `/attachments/{id}`, which checks ticket
  visibility on every request and redirects to a user-delegation SAS URL valid
  for one to two minutes. Inline images use the same route. Streaming through
  the app instead is the fallback if a client cannot follow the redirect.
- One storage path: every attachment is a blob, with a metadata row in the
  database (filename, size, detected type, content hash, blob key). No
  database tier for small files. Inline images such as signature logos are
  skipped when tiny and deduplicated on content hash.
- In-app viewing: PNG, JPEG, GIF and WebP display inline in the ticket thread.
  Everything else downloads. Never inline SVG or HTML. The content type is
  detected server-side, not taken from the sender; responses carry
  `X-Content-Type-Options: nosniff`; non-images are forced to download with
  the SAS response-header overrides; the storage domain is allowed in the
  app's image content-security policy. Files are served from the storage
  origin, not the app's, so script in a file cannot reach the session.
- No server-side conversion: no PDF or Office previews and no thumbnail
  generation in v1 (parsers on hostile files). Email body HTML is sanitised or
  shown as text.
- Redundancy: ZRS (zone-redundant, three copies across zones in UK South) in
  both environments. GRS/GZRS if a second region is ever wanted.
- Versioning and soft delete on; superseded versions are deleted after 90
  days; blobs move to the cool tier after a year.
- Uploads go through the app (size limit and type checks in one place), not
  directly from the browser.
- Downloads of anything that is not an image are served as attachments, never
  rendered in the browser.
- Network: public endpoint with Entra-only access for now; a private endpoint
  is a later hardening step.
- Malware scanning (Defender for Storage): off until costs are known.

**Secrets.** Key Vault, read by the apps through managed identities. Nothing
secret in the repository, the Bicep parameters or the app definitions. Each app
and the migrate job has its own identity and is granted read access to only the
secrets it needs, one secret at a time; the public app and worker cannot read
the web session key or the SSO secret, and the public app and migrate job have
no blob access. The database URL still uses the server admin login; a
least-privilege database role is a later hardening step.

**Considered and not chosen.**

- Front Door Premium with WAF and Private Link: would add managed rule sets,
  per-path rules and an app with no public address, but costs several hundred
  pounds a month, which is out of proportion for a few agents behind IP
  restrictions. Revisit if the public surface grows or an audit asks.
- App Service for Containers: workable and simpler, but Container Apps was
  preferred.
- Static Web Apps: the UI is server-rendered Jinja with htmx, not a SPA.
- AKS: far more than this needs.

**Supply chain.** Python dependencies are locked with hashes (`requirements.lock`, installed with
`--require-hashes`), the base image is pinned by digest, GitHub Actions are pinned by commit SHA and htmx is
served from `app/static/vendor/`, so nothing runs that was not reviewed. Dependabot and a nightly `pip-audit`
keep them current (details: `docs/build-plan.md`, "Dependency lock").

**Code changes the Azure plan needs:**

- `APP_ROLE` (`web`, `public`, `worker`) selects which routers are mounted. **Built in 3A (20 Oct 2026):**
  `build_app(role)` in `app/main.py`. `web` serves the console, portal, admin and the dev helper; `public` serves
  only `/health`, `/webhooks/graph/notifications` and `/approvals/{token}` and has no session cookie; `worker`
  serves nothing; `all` (the default) serves everything for local development and tests and is refused outside
  dev, so a deployment that forgets to say what it is does not put the agent console on the internet.
  The webhook stores one `GraphNotification` row per notification (ids only, no mail content) after checking
  `clientState` in constant time, and answers 202; 3B reads the rows. Public routes are rate limited per caller
  address, per replica (`app/rate_limit.py`; approvals 30 a minute, webhook 600, both settings). The access log
  replaces an approval token with `{token}` (`app/log_redaction.py`).
- `RUN_MIGRATIONS=false` makes the entrypoint skip `alembic upgrade head`, so
  replicas do not race; the migrate job runs it instead. Done.
- uvicorn proxy headers: uvicorn trusts `X-Forwarded-*` from the addresses in
  `FORWARDED_ALLOW_IPS`, which the Bicep sets to the Container Apps subnet (where
  the ingress proxies run), not `*`. With `*` uvicorn takes the leftmost
  `X-Forwarded-For` entry as the client address, and a client can forge that.
  No code change needed. Verify on staging: the HTTPS-only cookie works, and a
  request with a forged `X-Forwarded-For` is recorded with the real address.
- Settings check per role: **built in 3A.** `unsafe_setting_errors` refuses an unknown role, `all` outside dev, a default
  `SESSION_SECRET_KEY`, a default `GRAPH_WEBHOOK_CLIENT_STATE` on `public` (even before Graph is configured), and the SSO
  secret on `public`. Decision: the internal session key is not dropped. On `public` and `worker` there is no cookie, but
  the same value signs and checks the emailed approval links (`app/approval_links.py`), which the worker mints (3D) and
  the public app checks, so it stays as a shared link-signing secret. Rename it if that is confusing (the Bicep still
  calls it `INTERNAL_SESSION_SECRET_KEY`); a separate `APPROVAL_LINK_SECRET_KEY` is a possible tidy-up.
- Worker entrypoint (`python -m app.worker`), phase 2. **Skeleton built (2C-i, 19 Oct 2026):**
  `app/worker.py` runs the jobs listed in `app/jobs/__init__.py` with APScheduler. Each job gets
  its own session, is committed on success and rolled back on failure, never overlaps itself, and
  is first due at start. It writes `heartbeat job=<name> status=ok duration_ms=<n>` on success
  and `job=<name> status=failed` with the traceback on failure; a failing job does not stop the
  others. The worker takes a Postgres advisory lock for its whole life, so a second copy refuses to
  start (exit 1) instead of running every job twice; unsafe settings or a job interval under a
  second exit 2. The lock is a session lock that could vanish silently if its idle connection is dropped
  (an idle timeout, a failover), so a watchdog job checks every 30 seconds that the connection still
  holds it and, if not, stops the worker (exit 3) so two never run together; the compose service is
  `restart: unless-stopped`, and the restarted worker waits for the lock like any other. SIGTERM/SIGINT
  let running jobs finish, and are safe before the scheduler has started or if sent twice. The only job so far is `heartbeat` (every `WORKER_HEARTBEAT_SECONDS`,
  default 60). `docker compose up worker` runs it locally; the Bicep already starts it with this
  command at one replica. Real jobs (unread notification to Teams, new-ticket Teams cards,
  presence refresh, mail catch-up, SLA timers ...) are added to `JOBS` by their own PRs.
- Storage layer behind an interface, phase 3. **Built (3E-storage):** `app/storage.py` has a
  local-directory implementation (development and tests, streamed by the app) and a Blob one
  (`DefaultAzureCredential`, no account keys, user-delegation SAS for 90 seconds with the
  Content-Disposition and Content-Type forced by the link), chosen by `STORAGE_BACKEND` (blank:
  blob when `STORAGE_ACCOUNT_NAME` is set, which the Bicep does). `app/attachments.py` is the one
  place a file is checked and stored (size limit, blocked program types, type detected from the
  bytes, tiny inline pictures skipped, dedupe by content hash); `/attachments/{id}` asks only
  `visibility.may_view_attachment`. No upload form yet and mail ingestion joins in 3B.
- Application Insights via the `azure-monitor-opentelemetry` package. **Built (3F):** `app/telemetry.py` starts it when
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (web, public and worker), and `/approvals/` requests are excluded from request
  telemetry because the emailed link carries a credential in its path.
- App-level rate limiting on the public routes. Built in 3A (see above).
- Deploy workflow (the old SSH-to-EC2 one was removed): **drafted, never run**
  (`.github/workflows/deploy.yml`, helper scripts in `.github/scripts/`): OIDC login,
  build once and push, `what-if`, migrate job, template deploy, rollout and `/health`
  check, staging then a production approval gate. Setup is in `infra/README.md`.

**Azure-only verification.** The app is tested locally until it is ready.
These cannot be proved locally and are checked on staging, flagged as each
feature is reached:

- Real Entra sign-in against the generated redirect URI, and later the
  internal domain.
- IP restrictions, HTTPS redirect and the forwarded-header handling.
- Managed identity access to Blob Storage and Key Vault; user-delegation SAS
  (Azurite may not support it).
- Graph webhook delivery, validation token handshake, 3-second response, and
  subscription renewal. Needs a public URL, so only on staging.
- Teams bot messages (phase 8) and Teams Workflows webhooks.
- Postgres SSL, private networking and point-in-time restore. Also that the
  server has no public endpoint: the Postgres module deliberately omits
  `publicNetworkAccess` (a VNet-integrated server has none, and setting both may
  be rejected), which was not confirmed against the docs. Check the first
  `what-if`, then try to connect from outside the VNet.
- Per-app identities: each app can read only its own Key Vault secrets (try
  reading another app's secret with its token), and `public` has no blob access.
- The migrate job and the deploy ordering; revision rollout and rollback.
- Alerts, the heartbeat alert, Log Analytics and Application Insights data.
- Custom domain and certificate at roll-out.
- Scale rules and the single-replica worker.

## Security and request flooding

**Audit and activity log** (agreed 12 Oct 2026). Super admins can see what
happened, who did it and when. Two layers, both kept indefinitely and viewable
under Admin > Audit log with the existing filters plus date range and result:

- **Audit entries (built, grows with every feature).** A named entry for each
  meaningful action: ticket changes, emails, notes, triage, followers, user, team,
  role, membership, category and setting changes, unlocks. Every feature that
  changes data writes its entries as it is built; a review checks it.
- **Security and request activity (built: Admin > Activity log).** Sign-in, sign-out, failed
  sign-in, lockout, Entra sign-in failures, access
  denied (403), and every state-changing request, recording who, what route, the
  result, the IP address (taken from the forwarded header once only the ingress is
  trusted) and the browser. Never request bodies, passwords or tokens. This second
  layer is a safety net: it catches an action whose feature forgot its audit
  entry. It is built early rather than at the end, because it is cheap now and
  the history it would miss cannot be recovered later. CSV export arrives with
  reporting in phase 7. Still to add: outage-mode changes and unlock events (when
  those exist), and a retention decision. It lives on a separate page from the audit log, with filters for
  event type, person, date range and problems only. Staging check: the IP is only
  trustworthy once `FORWARDED_ALLOW_IPS` is the ingress subnet.

Fixes, first:

- Dev login and `/webhooks/dev/simulate-inbound` only exist when an explicit
  `ENV=dev` is set, instead of whenever SSO is unconfigured.
- The app refuses to start outside dev with the default session secret or
  default Graph `clientState`.
- Remove the published Postgres port from `docker-compose.yml`.
- Session cookie set to HTTPS only.

Azure:

- Ingress is HTTPS only. Postgres has no public endpoint. Blob Storage and
  Key Vault accept Entra identities only, with no keys.
- `web` accepts only office and VPN ranges (Container Apps IP restrictions,
  no extra cost). Agents off the VPN cannot sign in, by design.
- `public` is open to the internet and serves only the Graph webhook,
  feedback and reopen links, the Teams bot endpoint and `/health`. Its
  protection is in the app: single-use expiring tokens, `clientState` and
  validation-token checks on the webhook, and rate limiting. Microsoft's Graph
  source ranges are not allow-listed: IP rules cannot be applied per path, and
  the ranges change.
- No WAF. Front Door Premium is documented under "Hosting on Azure" as an
  option, not the plan.

App:

- Account lockout with increasing delay after about 5 failed logins.
- Every list paginated, message body length capped.
- Tokenised links (feedback, reopen) single-use and expiring.
- Rate limiting on the public routes.
- `uvicorn` runs 2 to 4 workers per replica, sized to the container's CPU,
  and trusts the ingress's forwarded headers.

## Identity and access

**Users.** Entra SSO is the primary sign-in. Store the Entra object ID
(`oid`) as the stable identifier, the UPN as the username and the email for
ticket matching. One user can be an agent and a requester: agents use the
portal for their own tickets.

**Accounts** (decided 22 Oct 2026). A requester needs no account to email or use Teams. An account is created the first time
someone signs in to the portal, from the Entra claims (name, email, object id), and refreshed at each sign-in. There is no bulk
sync and no auto-delete of end-user accounts: Entra disabling a person already stops sign-in, and audit and activity rows
reference users. "My requests" matches tickets by lower-cased email through one helper (`requester_emails` in
`app/identity.py`) and also lists tickets the person follows. For agents, a nightly Entra `accountEnabled` check sets an `is_active`
flag (the same flag as the out-of-office item under Notifications, and the leaver step of the session work in "Security
hardening phase"); it is built in phase 6, alongside matching users on the Entra object id. An admin-managed **never-follow
list** (the shared mailbox, distribution lists; built in 4B-2, `app/never_follow.py`) keeps those addresses from being added as followers, by an inbound Cc or by hand. Mail **sent from** a listed address is ignored whole (no ticket, nothing added to a ticket) and logged in the audit log with the sender and subject (#122).

**Linked identities** (decided 22 Oct 2026; phase 6; manual, super admin only). One person can have two Entra accounts on the
same tenant (for example upn@companyA.com and upn@companyB.com). A super admin links a secondary account to a primary person:
- Nothing is deleted. The secondary address becomes an alias, so `requester_emails` returns both and My requests, requester
  checks, followers and approvals treat the two as one person.
- Signing in as either account resolves to the primary person's account and role.
- Audit and activity rows keep the original user ids.
- Ticket assignments on the secondary should move to the primary (open point).
- The link page shows both accounts' roles and team memberships before the super admin confirms.
- Linking and unlinking are audited, and a link can be undone (unlink). Never automatic.

**Roles.** `super_admin`, `agent`, `end_user`. Moving role assignment to
Entra app roles (the `roles` claim) comes later; assigning whole groups to an
app role needs Entra ID P1.

**Extra permissions (#132).** Not RBAC: on top of the role a super admin can grant an agent a named
extra (registry in `app/permissions.py`; the first is `manage_request_templates`, which opens Admin >
Request types and nothing else under Admin). Super admins hold every permission; end users cannot hold
one; approval groups stay super-admin only. Granting is on Admin > Users and audited. The check is one
function (`visibility.has_permission`), so phase 6 can feed it from an Entra app-role claim instead.

**Break-glass.** Two local super admin accounts not tied to anyone's UPN,
with long passwords and TOTP. Always able to sign in.

**Outage mode** for Entra outages:

1. Agents set an emergency password in advance, prompted at SSO sign-in.
   Stored hashed, rejected unless outage mode is on. End users get no
   fallback.
2. A break-glass admin switches outage mode on. The login page shows a
   UPN and password form, every page shows a banner, and the toggle is
   audit-logged.
3. Switching it off ends every password-based session, invalidates every
   emergency password used during the outage, and asks those agents to set
   a new one by email and at their next SSO sign-in.

**Visibility.** Team memberships carry a level: `member` (sees and works the
team's tickets) or `viewer` (read-only). Agents see only tickets for teams
they belong to, plus every untriaged ticket. Restricted work (HR, security)
lives in its own team. Opening a ticket link without access shows a "no
access" page. **Access comes from the ticket's team and nothing else** (decided 13 Oct
2026): being its assignee, having triaged it, or following it grants nothing, so
when a ticket is re-assigned to another team everyone outside that team (and its parent
team) loses it at once (the agent who moved it is told where it went). The exceptions are
the shared triage queue (untriaged tickets), which narrows with per-inbox routing below,
and
**escalation** (below): an agent who escalated a ticket keeps working access to that one
ticket.

**Per-inbox routing (later addition, agreed 12 Oct 2026).** Each team that is
onboarded can have its own inbox (a shared mailbox, and later a Teams channel).
Mail or a Teams message sent to team B's inbox creates a ticket that arrives
already in team B and is visible only to team B (plus super admins), not to
every agent. Only the shared triage inbox feeds the untriaged queue that
everyone can see. This is how restricted work (HR, security) and other teams
are brought onto the system without exposing their tickets.

Not every team needs its own inbox. A main team can have one inbox that its
sub-teams share, so sub-teams are onboarded without new mailboxes.

**Sub-teams** (agreed 12 Oct 2026). A team can have a parent team. Members of a
parent team see all of its sub-teams' tickets. This needs a parent on `team`,
managed in the admin portal, and is applied in one place: the effective team
levels computed in `app/visibility.py` include the sub-teams of every team a
user belongs to, so no route changes. A parent member has full access to the
sub-teams' tickets: the same level they hold in the parent, so a member can work
them, and a viewer can read them. **Built** (1B-ii): `team.parent_team_id`, the
visibility rule and the admin portal; one level deep. A sub-team sharing its parent's
inbox waits for per-inbox routing, which is not built.

What it needs, none of it built yet: an inbox-to-team mapping managed in the
admin portal (inbox address, owning team, whether tickets start in the shared
triage queue), the Graph subscription and catch-up job per mailbox, and the
reply sent from the mailbox the ticket arrived in. Today's rule, "New or no
team means visible to every agent", is a stand-in for the single inbox. It
lives in one function, `is_untriaged` in `app/visibility.py`, which narrows to
"in the shared triage queue" when this arrives, so nothing else has to change.
Until then, restricted work is only private once triaged into its own team.

**Sign-in follow-ups** (agreed 9 Oct 2026, from the review of Phase 0; build
alongside 1B, which already touches the users page):

- **Unlock button.** A super admin can unlock a locked local account from
  `/admin/users`, which shows the lock expiry and failed-attempt count. Unlocking
  resets both and is audit-logged. Until it exists a locked contractor waits out
  the lock, which an attacker can keep renewing about once per lock period.
- **Clear the Entra sign-in state.** The `oauth_state` value stored in the
  session during the Entra redirect is removed as soon as the callback has
  checked it, so a state value cannot be replayed. Logout becomes a POST, so a
  cross-site image tag cannot sign someone out.
- **Match users on the Entra object id** rather than email or
  `preferred_username` (Phase 6, with Entra app roles).

## Tickets

**Types.** Incident, Service Request, Major Incident, Problem. Each has its
own display prefix (`INC-`, `SR-`, `MI-`, `PRB-`). Tickets arrive with a default
type, and triage may change it.

**Ticket numbers** (agreed 13 Oct 2026, replacing the per-type references and aliases
of 12 Oct). One counter is shared by every ticket, whatever its type, and the number
never changes. The reference (`INC-1042`) is the number with the current type's
prefix, for display only: when triage changes the type the prefix follows (it shows as
`SR-1042`) and nothing else changes, so no alias is needed. Lookups use the number
alone, so `/t/1042`, `/t/INC-1042` and `/t/sr-1042` all open the same ticket, and a
link or an email subject written before a retype still finds it. Only the four real
prefixes are accepted before the number. Sub-tickets from splitting are simply the
next numbers. Agent, portal and admin routes use the number too (/tickets/1042, /portal/1042), never the internal id.
Projects are a separate entity tickets can link to, with Azure DevOps
integration later. Change control is later.

**Statuses.** New (untriaged), Open, In progress, Awaiting user, Awaiting approval
(a service request waiting for its approvers, 14 Oct 2026), With supplier (added 20 Oct 2026:
waiting on an outside supplier or vendor we raised it with; not the MSP, which has With MSP
and its own phase 5 handoff), With MSP, Scheduled (added 22 Oct 2026, SRP-2: work planned for a later date, see "Scheduled status" below), Resolved, Closed. Reopening is a recorded event, not a status.
With supplier is set by hand (the Status buttons, or the Status dropdown once it exists) and is
an open status: it is not finished, nobody's access changes, and the SLA clock keeps running
(only Awaiting user pauses it; decided 21 Oct 2026 that With supplier does not pause it either,
see the build plan's decisions log). Emailing the requester leaves it as it is, like With MSP.

**Status follows the conversation** (agreed 12 Oct 2026). The SLA clock pauses in
Awaiting user and chase emails are sent from it, so the status must be right
without anyone remembering to set it by hand.
- **Agent emails the user or adds a note.** *Changed 14 Oct 2026* (was: always moves to
  Awaiting user with no choice). The Email user and Add note forms end with a **Status**
  dropdown. For an email on an Open or In progress ticket it defaults to **Awaiting user**, so
  the usual case is still one click, but the agent can pick any other status (for example In
  progress for "we're looking into it", which keeps the SLA clock running and stops the chase
  timer). For an internal note the default is "no change". The ticket takes the chosen status
  when the message is saved, in one step, as an ordinary status event naming the agent. On a
  ticket With supplier, With MSP, Resolved or Closed the dropdown defaults to the current status (nothing
  moves unless the agent picks something), and Resolved or Closed are not offered here:
  resolving is the resolve action (built, 4D: notes required, the resolution email is the admin template "resolution" with the preview step, `app/resolution.py`; closing a ticket that is not resolved is the Close action, which needs closing notes and sends the admin template "closed" with the same preview step, `app/closing.py`, #131). The Status buttons no longer offer Resolved and stay for changes made without
  a message. For a split original the dropdown is not shown, since its status is derived; an
  email from a part sets that part's status. The dropdown is also not shown (and no status
  change is accepted) on an untriaged ticket, one Awaiting approval, or a Resolved or Closed one,
  so a message never reopens a ticket. *Built (4B-5):* `app/messages.py` decides the offered
  statuses and defaults, the route applies the choice through `set_status`, and the preview step
  (1G-ii) shows the chosen status.
- **Followers hear of updates they were not sent.** When the requester or a follower updates a ticket by email or portal
  comment, every other external follower (and the requester, when a follower wrote) who was not on that mail's To or Cc gets one
  automatic email, admin template `follower_update`, requester-visible text only, subject to the automatic-email cap and each
  person's email preference. *Built (4B-6):* `app/follower_updates.py`, `followers.uncopied_recipients`.
- **The user replies.** When the requester, or a follower, replies while the ticket
  is Awaiting user, it moves back to In progress automatically and the SLA clock
  resumes. This covers portal comments now and email replies once mail intake
  exists. A reply on a New ticket changes nothing; on a With supplier, With MSP or Scheduled ticket it changes
  nothing (the agent decides, and the assigned agent is notified); on a Resolved ticket it reopens it and on a Closed ticket it opens a new, linked ticket
  (*built, 4E-1:* `app/reopening.py`).
- Both moves are ordinary status events in the timeline and the audit log, with
  the person who caused them.

**Who counts as "me" in the portal.** The portal matches the signed-in person to a ticket by lower-cased email, never by an exact string, and every such comparison goes through one helper (`requester_emails` in `app/identity.py`). It is one address today. It is a deliberate seam because merging accounts is planned, and then only that helper changes. "My requests" lists tickets the person raised first, then tickets they follow, marked "following". Agent access is separate and stays in `app/visibility.py`. Tickets store the requester lower-cased (`create_ticket`).

**Raising a ticket in the portal** (phase 1G, agreed 12 Oct 2026). One form with
two modes. "For myself" is the normal end-user case (source: portal). Agents also
get "For someone else": the requester's email, subject and description, with
source agent. The ticket records who created it as well as who it is for. Where
it is shown it reads by source: "Received by email", "Raised in the portal by
Jane", "Raised by Agent X for Jane", "Raised in Teams by Jane", on the ticket
page, in the timeline and in the requester's portal view. The agent's
description is stored as the first message, marked as logged on the requester's
behalf. The agent can triage and assign in the same form (optional), so a ticket
they already understand skips the triage queue. The requester is told a ticket
was raised for them (once email is wired up in phase 3): on by default, and it
can be turned off globally in Settings or by each person in their own
notification settings. The ticket list shows the channel as a small label and
can be filtered by it.
*Built in 1G-i (13 Oct 2026):* `/portal/new` (`app/raising.py`), linked from both lists. Agents get a
"For someone else" section: the person's email, optional type/category/impact/urgency/assignee (all
or none of the classification), and optional reported/resolved times for a fix made in person (a
resolved time creates the ticket already resolved, backdated, with no SLA). `Ticket.created_by_id`
records who raised it; the description is the first message, "Logged by X on behalf of Y"; the page
and the requester's view read "Raised by X for Y" / "Raised in the portal by Y". Channel label and
filter on the agent list. Not yet: the email preview step and the "requester is told" setting (1G-ii).

**Source.** Email, portal, Teams or agent. Drives the default channel for
notifying the requester.

**Triage** (agreed 12 Oct 2026). Manual for now, automated later. Triage and
assignment are separate actions. Triaging a ticket classifies it: type,
category (which decides the team), impact and urgency. It does not assign it,
and assigning does not triage it. So a ticket can be untriaged and unassigned,
untriaged and assigned (handed to whoever will triage it), triaged and
unassigned (waiting in its team's queue for anyone to pick up), or triaged and
assigned. A New ticket shows a triage panel; saving it moves the ticket out of
New in one step with one history event. The panel has an optional "assign to"
dropdown, so a triager can also assign in the same action, or leave it for the
team. Any agent can triage and any agent can assign during triage. New tickets
arrive in a triage queue owned by a default team (**TBC**), which is notified.

Until a ticket is triaged an agent can read it and add internal notes, but
cannot email the requester. An internal note does not triage a ticket, so an
agent can record "I spoke to the user, they told me X" without committing to a
category.

**Categories.** A main category with optional sub-categories; the main category
decides the team. When triaging, any super admin can create a new category on
the spot. An agent who cannot find a fit picks "Other" (a main category, never a
sub-category). The ticket is not held up: "Other" belongs to a configurable
default team (**TBC**, the service desk), so it is worked and resolved on time
under its normal SLA. After resolution it goes to the super admin category
review queue, as already planned. A super admin there creates a new category or
maps the ticket to an existing one and corrects it, so the next ticket like it
has a proper home and reports are accurate. The only change from the earlier
plan is that the agent no longer flags "no good category" separately: choosing
"Other" is the flag.

**Priority and SLA.** Impact (number of users affected) Ã— urgency gives
P1â€“P4. SLA targets (first response and resolution, **TBC** per priority)
come from priority. Every ticket runs on a 24/7 clock that pauses while
Awaiting user. Times display in each viewer's own timezone.

*Built in phase 1D-ii (13 Oct 2026).* Impact and urgency are low/medium/high; the
grid is high/high P1, high/medium and medium/high P2, high/low, medium/medium and
low/high P3, the rest P4. Targets are 8 settings (first response and resolution per
priority, minutes) with **placeholder** values: P1 15m/4h, P2 1h/8h, P3 4h/3d,
P4 8h/7d; blank means not tracked. The clock starts at `created_at`, so time spent
waiting for triage counts against the target. Backdating (1E) will exclude those
tickets from SLA figures. Amber means the "at risk" setting (25%) or less of the
target is left. Times are shown in each viewer's time zone (see "Time zones").
**Changed targets never touch existing tickets** (agreed 13 Oct 2026): a ticket's deadlines
are fixed when it is triaged, when its priority is changed, or when a pause ends, so editing a
target in Settings only affects tickets from then on and never rewrites breach history. **First
response** is recorded only when an agent emails from the ticket page today; when replies also come
from Teams (and mail through Graph in phase 3) each of those paths must call
`note_first_response` in `app/sla.py`. Not yet
built: breach notifications and Teams alerts (phase 3 and later), business hours,
and SLA figures in reports (phase 7).

**Priority queue.** Agents' queue sorts by time to breach, then priority,
with a countdown that turns amber when at risk and red when breached.
Untriaged tickets pin to the top.

**Messages.** One table with a `visibility` column: `public` and
`agents_only` now, `portal_only` later. The portal and outbound mail read
through a single function that returns only public entries, with tests that
agent-only notes never reach either. In the agent screen a public message is
sent with an **Email user** button (not "Reply"), which is unavailable until the
ticket is triaged, and an **Add note** button records an internal note at any
time. When the requester replies by email (including to the automatic "your
ticket is open" message) the reply is added to the ticket as an inbound message
and counts as an update: it refreshes the ticket and wakes it from Awaiting
user.

**Thread.** The ticket page shows one timeline in time order: emails, internal
notes and events (created, triaged, assigned, re-assigned, unassigned, category
or priority changed, status changed, escalated, split, linked), each with who
and when. Today events sit in a separate History table.

**Followers.** Requesters and agents can add followers by email. Followers
are CC'd on public messages and see the ticket under "Following" in the
portal. Followers are not read-only (corrected 12 Oct 2026): they can respond,
by replying to the email or by commenting in the portal, and their reply is
added to the ticket as a message from that follower, labelled as such in the
agent timeline. They cannot manage the follower list or the ticket itself.

- **Automatic followers.** Everyone CC'd on an email that creates a ticket (for
  example A raises it and copies B and C) becomes a follower. This happens in the
  mail intake in phase 3; until then followers are added by hand.
- **Controlling who is copied.** When an agent emails the user, every follower is
  copied by default, and the agent can untick any of them for that one email.
  That does not remove them as followers; removing someone is a separate action.
  The list actually copied is recorded on the email.

**Assignment.** Any agent who can work a ticket can assign or re-assign it. The
control is a team and then an agent: choose a team (it starts on the ticket's
current team), then either an agent in that team or "leave unassigned" so the
ticket waits in that team's queue for anyone to pick up. The ticket moves to the
chosen team and a chosen assignee is notified. It needs no reason and is not an
escalation. "Assign to me" is a shortcut offered only on an unassigned ticket;
once a ticket has an assignee the control reads "Re-assign". Taking a ticket
from its assignee always needs the explicit re-assign, never the claim button.
Every change (team, assignee, unassigned) is an event on the ticket.

**Escalation** (revised 14 Oct 2026). A separate action from re-assigning, and only
for a triaged ticket (an untriaged one has no category, so no route). The agent gives a
reason, and the ticket goes to the **escalation target of its category**: they do not
pick a team. Re-assign is the tool for handing work to the right team (for example mail
that should have gone to Finance goes to Finance, left unassigned); it needs no reason,
is not an escalation, and the agent loses sight of the ticket afterwards unless they
already have visibility of that team through membership or a parent team.

- Every main category has a defined escalation target, and the default for all of them
  is the MSP. A sub-category inherits its parent's target or overrides it. Targets
  are configured by an admin in settings; none are seeded or hard-coded.
- A target is one of three kinds: a **sub-team** of the agent's team (first line to
  second line; second line is a sub-team, so first line keeps access through the
  parent-team rule), **another team** (support to the in-house dev team), or the
  **MSP**. Sub-teams are created by an admin in the portal.
- The target depends on where the ticket is now as well as its category, so a second-line
  ticket can escalate on again. Built so far: the category's target, or the MSP if the
  ticket is already with that team. A (category, current team) table for longer chains is
  not built.
- The MSP handoff is built (phase 5A, 4 Nov 2026): a category whose target is the MSP (the default) is escalated by
  Send to MSP. With no MSP address set (an admin setting) it is refused and the control says so.
- Escalating to another team leaves the ticket unassigned there, and the escalator keeps
  working access to that ticket (and only that ticket).
- Escalating to the MSP is the same action as Send to MSP (see MSP): the ticket stays in
  its team with status With MSP and nobody's access changes.
- The escalating agent becomes an internal follower: CC'd on every outgoing message,
  including emails to the MSP, notified about notes only when @mentioned. Whoever works
  the ticket can remove any follower.
- Changing a ticket's category after triage changes its escalation target, and is an
  event on the ticket.
- *Tidy-up, built 19 Oct 2026.* **The escalation target is chosen when a category is created**
  (Admin > Categories, both creation forms: a main category picks a team or the MSP, defaulting to
  the MSP; a sub-category defaults to "same as its main category", or overrides), instead of every
  new category starting as "cannot escalate" until someone edits it; editing it later is as before.
  **The escalation reason is also written as an internal note** ("Escalated to Dev. Reason: ..."),
  so the team receiving the ticket reads it in the conversation and an @mention in it notifies the
  person named if they can open the ticket in its new team (the reason is still on the escalation
  event and the ticket banner). **Escalate and re-assign share one move** (`move_ticket` in
  `app/triage.py`): the team change, unassigning, the history and touching the ticket are one code
  path; they differ in who chose the team (the agent, or the category) and in what escalation adds
  (the reason, the escalator's access, the internal follower, the MSP refusal). A move records
  one event by kind: `team_changed` for a re-assign, `escalated` (with the reason) for an
  escalation, so a move is never recorded twice. Still not built: telling the team an escalation
  lands in (with the team notifications of phase 2).

**Linking.** Tickets link to a parent (Major Incident or Problem). Resolving
the parent offers to resolve every linked ticket with the same notes.
*Linking built in 1E-i (13 Oct 2026):* one level deep, by ticket number, with events on both
tickets; visibility filters what each viewer sees of the links. The resolve offer arrives
with the resolve action in phase 4. Phase 1E is delivered as 1E-i (linking, claim fix),
1E-ii (backdating), 1E-iii (splitting into sub-tickets).

**Splitting.** One request that asks for several things can be split into
sub-tickets, so each part is tracked, assigned and resolved on its own (better
accountability). The agent clicks Split ticket, chooses how many parts, and
enters a subject and details for each. As each sub-ticket is created the triage
panel opens with the original's type, category, impact, urgency and team
prefilled, and the agent can amend any of it (or assign). Each sub-ticket has its
own reference, type, category, assignee and status, is linked to the original,
and can email the requester itself. In the agent portal they are separate
tickets, and the original lists them all.

*Splitting built in 1E-iii part 1 (13 Oct 2026):* `app/splitting.py`, `ticket.split_from_id`
(separate from the Major Incident/Problem link). The Split ticket control (any agent who works
the original) takes up to four parts at a time; blank subjects are skipped and a ticket can be
split again to add more, up to 10 sub-tickets in all and at least 2. Each part is created and then
triaged with the original's type, category (so team), impact and urgency, so it has its own
priority and SLA clock from the moment it is made; it is not left for the agent to triage, who
amends it with the usual controls. It keeps the requester and source. The details box becomes an
internal note. One level only. Events `split` / `split_from`, and an audit entry.

*Part 2 built (13 Oct 2026):* the original's status is derived (`derived_status`,
`sync_original_status` in `app/splitting.py`) with a system event "from its sub-tickets"; a part's
status change re-derives it under a row lock, and a split derives it once. Agents cannot set an
original's status by hand: only Resolved or Closed, and only once every part is finished (so
reopening a resolved original works only through the reopen flow, which reopens its resolved parts: *built, 4E-1*). Emailing from the
original does not move it to Awaiting user; emailing from a part moves that part. A requester reply
on the original hands every open part back (Awaiting user to In progress) and moves it up the queue.
The page says "All sub-tickets are done: resolve this ticket" and the original never resolves itself.
Conversation rules live in `app/messages.py`: the original's public messages (emails and replies)
show on each open part, a part's show on the original (only for parts the viewer can open, in the
agent timeline), siblings stay apart, finished parts stop showing the original's, internal notes
never cross, and nothing is copied. `thread_subject` is the original's subject and `about_labels`
gives each family email its "About: SR-14 Replace keyboard" line (shown in the portal now; sent with
the mail in phase 3). The original's own SLA clock is untouched by its derived status, apart from
the usual pause while it is Awaiting user. A part's agents see the original's public messages even
if they cannot open the original, because it is the requester's one conversation.

**Status of the original while it has sub-tickets.** It is derived, so agents do
not set it by hand: if every sub-ticket has the same status, the original takes
it; if exactly one sub-ticket is still unfinished, the original takes that one's
status; otherwise it is In progress. Each derived change is recorded as a system
event. Resolved and Closed are never derived. When every sub-ticket is finished
the original shows "All sub-tickets are done: resolve this ticket"; the agent
resolves it explicitly, which sends the single resolution email (with the usual
preview), and the normal auto-close after N days then applies. It does not
resolve itself, because the requester would otherwise never be told it was done.

To the requester it is one email chain. Every email, from the original or any
sub-ticket, is sent as a reply in the same mail conversation with the same
subject (Graph replies keep the thread), and carries a line naming the part it
is about, e.g. "About: SR-14 Replace keyboard". Mail from the requester lands on
the original, which owns the conversation. Messages are stored once and shown by
rule, not copied: the original's emails and the requester's replies show in
every open sub-ticket; a sub-ticket's emails show in the original but not in its
siblings; a requester reply refreshes every open sub-ticket. The system cannot
tell which part a reply is about, so each open sub-ticket sees it and the agents
sort it out (matching on the part label is a later option).

Sub-tickets can belong to different teams, and the normal visibility rules apply
to each one. The original's timeline hides notes and events from sub-tickets the
viewer cannot see, and internal notes stay on the ticket they were written on.
Resolving the original is offered once every sub-ticket is resolved, and their
notes feed the resolution email. Splitting is recorded as an event on both
sides.

**Backdating.** Only for a ticket an agent raised on someone's behalf (source `agent`),
e.g. the person came to the desk and it was fixed in person, so there is nothing to track
(decided 13 Oct 2026). A ticket a requester opened themselves (email, portal, Teams) can never
be backdated, so an agent cannot drop a live ticket out of SLA. The agent raise-a-ticket form
(1G) is what creates these tickets; until then none exist and the control never shows.
Because the rule reads `source`, nothing else may create a `source=agent` ticket: sub-tickets
from splitting (1E-iii) inherit the original's source, and an agent logging a requester's email
by hand is not "on their behalf" for this purpose. When 1G adds the real creation path, the
backdating tests (which set `source` by hand today) should switch to it rather than keep two
versions of the same test.
Agents can log tickets and fixes after the fact.
`reported_at` and `resolved_at` hold the dates entered; `created_at` holds
the real entry time, which the audit log also records. Up to 30 days back,
never in the future. Backdated tickets are excluded from SLA figures but
count towards volume. Sending the user the resolution email is on by
default.
*Built in 1E-ii (13 Oct 2026):* a Backdate control on the ticket page for anyone who
works the ticket, shown only on agent-raised tickets (`may_backdate`); reported time on any ticket, resolved time only on a finished one, never
before the report; the flag is permanent and removes the SLA deadlines. The ticket page shows times in the viewer's zone; the backdate form still takes UTC. The resolution email (default on) comes with the resolve action, phase 4.

**Service Requests.** Approvers are configured per request type in the
admin portal.
*Built in 1H-i (14 Oct 2026; the plan said no more than the line above, so these are working
defaults to confirm):* a **request catalogue**. Admin > Request types (super admin) lists the
things people can ask for ("New starter", "Software install"): a name, a short description
shown on the form, an approval group (see below; before 4C-1 it was up to 10 named approvers) and an
on/off switch (off hides it from the form; tickets keep it). The raise form (`/portal/new`) asks
"What do you need?" with those types plus "Something is not working"; choosing a type makes the
ticket a Service Request (`SR-`) and records `request_type_id` and a `request_type` event. Agents
raising for someone else can pick one too. The agent page shows the request and its approvers; the
requester sees the request name only. Sub-tickets from a split do not inherit the request type.
*Request type defaults (#133, built 4 Nov 2026):* a request type can carry a default team, category, impact and urgency
(Admin > Request types, checked like triage: an active category that belongs to the team). A request raised in the portal for
a type with all four set triages itself at creation, through the real triage function with the system as the actor: it becomes
a Service Request in that category and team with that priority, the SLA starts, and approvals start as for an agent's triage
(Awaiting approval with an approval group, otherwise Open and unassigned in the team queue). A type with incomplete defaults,
and every email- or Teams-raised ticket, still goes to New for an agent. The triaged event says "triaged automatically from
request type X" and the audit log has an entry. When an agent picks a kind of request on the triage form its defaults are
prefilled and can be changed.
*Form questions (#118 part 1, built 4 Nov 2026):* an admin can add questions to a request type (short text, pick list, yes/no, date; each optionally required, in an order they set, switched off rather than deleted once answered). The requester answers them on the portal raise form and the required ones must be filled in; for an email or Teams request an agent fills them in on the triage form when picking the kind of request, and triage will not complete while a required one is missing. A portal request self-triages only when its required answers are present. Answers show on the ticket for agents and in the portal for the requester; agents who work the ticket can change them (a ticket event and an audit entry), which marks the ticket's answers as changed without changing anything else. Sub-request templates that use the answers are part 2 (#118).

*Sub-request templates (#118 part 2, built 4 Nov 2026):* a request type can carry templates that make sub-requests automatically (New starter: laptop, phone, account). Each template names the team and category, optionally a kind of request, impact, urgency and an agent (or the parent's assignee), a subject using the answers (`Laptop for {Name}`) and how many days before the parent's Required by it is due. A template can have conditions on the form answers, in groups (every condition in a group, and any group; none means always) using equals, not equals, is one of and is answered. They are made through the splitting machinery, so the derived status, the shared thread and "resolve the original when every part is done" are unchanged: once approval passes, or at triage when the type needs no approval (an automatic portal triage makes everything the rules produce; an agent sees the list ticked and can untick or add to it). The parent's approval covers them unless the sub-request's own kind of request is set to need its own. One admin setting caps the sub-tickets on a ticket, whether split by hand or made automatically. Changing the answers later changes nothing; agents are shown a note.
*Built in 1H-ii (14 Oct 2026, decisions confirmed that day):*
- **A new status, Awaiting approval.** Reached only through triage; it is the one place the plan's
  status list grew. While in it the ticket cannot be moved by hand by anyone (no status buttons;
  the server refuses with a clear message), cannot be split, and cannot be logged as already
  resolved. The SLA clock keeps running. Agents can still triage, assign, write notes and email.
- **A reusable rule** (`app/approvals.py`, written so another kind of ticket, such as change
  control, can use it): since 4C-1 a pass percentage per request type (see "Approval groups" below), copied onto the
  approval when it starts so later edits do not change a request already waiting. Whichever outcome is reached first settles it
  and everyone still pending is marked "not needed". Approved: the ticket goes to Open and the requester gets an
  automatic message. Declined: it is Closed with the approvers' reasons in the message to the requester.
- **It starts at triage**, once the real type and team are known: a catalogue request with
  approvers waits for them instead of opening; an agent who retypes it to an incident ends the
  approval; an agent who raises and classifies a request for someone starts it at once. The
  requester never approves their own request (they are left out; with no one else left, no
  approval is needed).
- **Approvers decide in the end-user portal** ("Waiting for your approval", the request page with
  Approve / Decline and an optional comment) and need not be agents. An approver can read the
  request (the requester's public conversation) but nothing else of the agents' side.
- **The emailed link** (`app/routers/approvals.py`, `app/approval_links.py`): a signed, time-limited
  link per approval that needs no sign-in. Opening it (GET) only shows the request and the
  buttons; deciding is a separate POST from that page, so mail scanners and link previews that
  fetch every URL cannot approve or decline for anyone. How long it lasts is a setting (default 14
  days). The email that carries it comes with phase 3; the link, its page and the token are built.
  It is internet-facing, so it belongs with the public routers when `APP_ROLE` splits them.
- **A super admin can waive** a pending approval (an approver has left); agents cannot.
- **Approval groups (decided 21 and 22 Oct 2026; built in 4C-1).** Approvals go to **approval groups** (Admin > Approval groups:
  create, rename, add and remove members by email, all audited). A request type points at one group and carries one percentage a
  super admin sets: **pass %** (1 to 100; there is no fail %). The voters are the group's members when the approval starts, minus the requester, and
  their number is fixed then. `votes_needed` = ceil(pass % x voters / 100) (5 voters at 60% needs 3; 4 at 60% needs 3; 100% means everyone).
  Approved as soon as the approvals reach `votes_needed` (equal counts); declined as soon as that can no longer be reached
  (declines > voters - votes_needed), so a complete vote always has an outcome. The voter-facing summary reads "3 of 5 must
  approve (60%)". An active request type needs a group with a member. There is one pending approval per ticket and one vote per member;
  each member gets their own emailed link (GET shows, POST decides). Handing over for leave or a leaver is adding or removing a
  person in the group, so there is no per-approval re-assignment and no standing delegate accounts (someone removed mid-request still
  counts as a voter but can no longer answer; a super admin can waive). A group may later map to an Entra security group. Withdrawing
  a request while it waits (4C-2, built: the requester only, closes it as "withdrawn", approval status `withdrawn`, the unanswered approvers told), reminders (4C-3, built: see "Approval reminders" below). The per-person email preference is built (4C-4): approvers get the approval email unless they switch it off under their notification settings (the portal list is unaffected). Each approver also gets a bell entry when a request waits for their vote (#122), with its own setting, so it shows even with the email off; everyone has a bell, and the entry opens the request in the portal. When an agent sets a ticket closed by a decline or a withdrawal back to an open status, the approval restarts as a new round (#122).
- **Approval reminders (4C-3, built).** A job on the database-driven scheduler (`approval-reminders`, hourly) emails each approver who has
  not answered: first after X days, then every Y days, at most N, all admin settings (placeholders 2, 2, 3). It stops when the request is
  approved, declined, waived or withdrawn, or the member answers; it skips a person who switched approval emails off or left the group. The
  reminder counts toward the automatic-email cap (its own allowance per approver). After Z days (placeholder 7) the super admins get one bell
  notification and one email (admin template `approval_overdue`; each super admin can switch the email off, default on) so they can chase or waive. What is due is worked out from each vote's reminder count and last-reminded time, so a late or
  repeated run sends once. Off by default until staging (`approval_reminders_enabled`). Teams reminders wait for the phase 8 bot.

**History.** A `ticket_event` table records every status change,
assignment, priority change, SLA pause, reopen and MSP handoff with time and
actor, for reporting.

**Team activity** (agreed 14 Oct 2026, in addition to the audit log). Agents get an activity
section, `/tickets/activity`, showing everything happening on the tickets they can open, newest
first: ticket events, emails to and replies from the requester, and internal notes. It uses the
ticket list's access (`visible_ticket_condition`), so it is the agent's teams plus the shared
untriaged queue, and a ticket moved to another team leaves the feed of everyone outside that team,
history included. Filters: team and kind (changes, emails and replies, notes). The audit log
(Admin) stays the system-wide record of settings, accounts and categories, and the activity log
the record of sign-ins; this is the work on tickets, for the people doing it. Built (13-14 Oct 2026,
`app/team_activity.py`). Later: per-person filter, and a link from a ticket to its own history.

**Search** across subject, messages, requester and reference using Postgres
full-text search.
*Built in 1F-i (13 Oct 2026):* the search box on the agent ticket list (`app/search.py`). It
matches the number (1042, #1042, INC-1042), subject, requester and every message including
internal notes, and only narrows what the agent can already open. Typing a ticket number that exists finds that ticket alone. No index yet: full-text is
computed per query, fine at this volume; add GIN indexes if the list gets slow.

**Canned responses** for common replies, managed by admins.
*Built in 1F-ii (13 Oct 2026):* Admin > Canned responses (super admin: create, edit, switch off; audited). On a triaged ticket the email form has an "Insert a canned response" picker that only fills the box (appending to what is typed) with {reference}, {subject} and {agent} filled in; the agent edits and sends it as usual. Global, not per team; per-team sets can follow if wanted.

## Outbound email preview
*Built for agent replies in 1G-ii (14 Oct 2026):* the email form now has **Preview email**: a panel
shows To, who is copied (internal followers always), the subject (the family's one thread subject),
the "About:" line for a split request, the text, and what sending will do to the ticket
(for example "moves this ticket to awaiting user and pauses its SLA clock"). **Send email** and
**Keep editing** follow. The preview runs the send's own checks (`app/messages.preview_email`
shares the copy rules with `email_requester`), so what is previewed is what would go. It is a
safety step in the page, not a server lock: the send endpoint still accepts a direct post. The
setting "Tell the requester when an agent raises a ticket for them" (Settings, default on) is in;
the email itself arrives with phase 3.

Every email an agent triggers that reaches an end user (replies,
resolutions, bulk resolves, MSP forwards) shows a preview and an "Are you
sure?" step. Bulk resolves list every linked ticket and recipient.
Automatic emails use admin-managed templates, previewed when edited (built in 4T: `app/email_templates.py`; Admin > Email templates).
A template is plain text with `{placeholders}` from a fixed list per email; the signed approval link is only ever `{link}`, made at send time.

## Mail (Microsoft Graph)

- Shared mailbox address **TBC**, and so are its aliases. The mailbox is not being set up yet. When it is created, the own-mail guard needs the list of aliases (build plan, staging checklist, "Mailbox setup").
- Webhook plus a delta catch-up poll every 5â€“10 minutes. The webhook stores
  the notification and returns `202` within 3 seconds; the worker fetches
  and processes the message. Graph retries for up to 4 hours, and drops
  notifications to persistently slow endpoints.
- Duplicates prevented by a unique constraint on `internetMessageId`.
- Subscriptions last up to 10,080 minutes. The worker renews any with under
  24 hours left and recreates on 404. *Built in 3C (20 Oct 2026):* `app/subscriptions.py`; one
  `GraphSubscription` row (unique resource, locked with `SKIP LOCKED`) plus a check of what Graph itself
  holds before anything is created, so restarts and two replicas never make two; renew margin, lifetime
  and backfill window are settings (the window applies only when Graph forgets the catch-up's place; the very
  first run looks back nothing and imports no existing mail); lifecycle notifications are acted on from the stored rows; the
  catch-up stores its deltaLink in `MailSyncState` and hands new message ids to inbound mail through
  `GraphNotification`; Admin > Mail intake and `/health` warnings show the state. Real creation,
  renewal and lifecycle events are staging-only (see the build plan's staging checklist).
- `lifecycleNotificationUrl` set at creation (it can't be added later):
  `reauthorizationRequired` renews via `PATCH`, `subscriptionRemoved`
  recreates then runs a delta sync, `missed` runs a delta sync. Never
  `/reauthorize` and `PATCH` the same subscription within 10 minutes.
- Replies read with `$select=uniqueBody` to get only the new text.
- Auto-replies filtered on the `Auto-Submitted` header.
- Per-ticket cap on automatic emails to stop mail loops.
- **Mailbox trust** (decided 22 Oct 2026). Mail is trusted on its From address; we rely on Exchange and Mimecast to enforce SPF,
  DKIM and DMARC. The shared mailbox must accept mail only from authenticated senders (internal only, Exchange
  `RequireSenderAuthenticationEnabled`); this is on the staging checklist. Reading Authentication-Results is not planned now
  (optional defence in depth in phase 10).
- **Inbound limits are admin settings** (decided 22 Oct 2026, built as 4B-1). The per-sender hourly limit (30, placeholder, TBC),
  attachments kept per mail (20) and Cc followers made per mail (10) sit next to the automatic email cap under Admin > Settings; the
  Bicep no longer passes them. Mail set aside as `ignored_flood` is shown on Admin > Mail intake. They are counts of 1 to 1000
  (no "off" for the per-sender limit).
- **Acknowledgement and queue** (decided 22 Oct 2026; acknowledgement built as 4B-3). A ticket that arrives by email from a person gets one automatic receipt: "Your issue has been logged as INC-1042 with IT. We'll be in touch." (admin-editable template `acknowledgement`). It is content-free: the reference and that line only, no queue position (positions mislead because a higher priority jumps the queue) and no priority or target time (an emailed ticket has none until triage). It is a deliberate exception to "requester email is hidden until triaged", it counts toward the per-ticket cap on automatic emails, and it is never sent for automatic mail, bounces, our own mail, a flood or a never-follow address. *Open idea:* an
  indication of how busy the team is. The agent-side priority queue (P1 to P4, SLA deadline) is unchanged. *Planned, not built, design
  to be agreed:* service requests get an optional **"Required by"** date on the request form, feeding queue order and the SLA target.
- *Outbound mail, built in 3D (20 Oct 2026):* `app/outbox.py` queues an `OutboundEmail` row in the same
  transaction as what it is about (an agent reply, an automatic notice, an approval request, "a ticket
  was raised for you"); the worker's `mail-out` job (every 30 s, `app/mail.py`) builds each email when
  it goes and sends it through `POST /users/{mailbox}/sendMail` as plain text. The rules are asked again
  at send time: the message must still be one the requester may see and the ticket triaged
  (`app/messages.py`), "raised for you" must still be wanted (an opt-out after queuing wins), an approval
  must still be pending. The approval link is signed at send time and never stored. Subject is
  `[INC-1042] subject` (a split request's parts use their original's reference and subject, plus the
  "About:" line), so a reply is matched back by the reference. The cap is the setting "Automatic emails
  to one ticket's requester, at most per day" (placeholder 10, TBC); agent replies never count. The
  switch is `MAIL_SENDING`: `off` queues nothing (so switching on never sends a backlog), `dev` records
  the text on the row and sends nothing (read it at Admin > Outbound mail), `graph` sends. Delivery is
  at least once; each email is committed as it goes. Graph's 4xx other than 401/403/408/429 is final;
  those four are retried with the Teams outbox's backoff, since they are fixed on the Azure side.
  *Not proved locally (staging):* real `sendMail` with Mimecast in front and the mailbox-scoped
  `Mail.Send` grant; that `X-Auto-Response-Suppress` is accepted as a custom header; that a reply keeps
  the reference in the subject through a mail client's own "Re:" handling. Real `In-Reply-To`
  threading (replying to the Graph message id with `createReply`) is a follow-up: inbound mail (3B) now stores
  the Graph id on each received message; until then threading is by the reference in the subject.
- *Inbound mail, built in 3B (20 Oct 2026):* `app/inbound.py`. The worker's `mail-in` job (`INBOUND_ENABLED`) takes each stored
  `GraphNotification`, fetches the message (`$select` with `uniqueBody` and the headers, `Prefer: outlook.body-content-type="text"`)
  and calls `process_message`, which the 3C catch-up also uses. Every handled mail is recorded in `InboundMail` (unique Graph id and
  Message-ID), so duplicates and re-deliveries do nothing. Our own mail, automatic replies (Auto-Submitted, X-Auto-Response-Suppress,
  Precedence, list and bounce headers), drafts and a sender flooding more than a set number an hour are ignored. A mail joins a ticket
  by Graph conversation, then the Message-ID it answers, then the `[INC-1042]` reference. The requester and followers may always add. Someone
  else midway through the real thread (a strong match: conversation or Message-ID, e.g. a colleague copied who replies-all) is added to the
  ticket as a follower and counts as a follower reply (decided 22 Oct 2026); a match on the subject reference alone is no proof of being in
  the thread, so that sender opens their own ticket (an event names the cited one), as does one when the follower list is full. A reply goes through `record_inbound` and the portal's status rule, so Awaiting user
  returns to In progress and a split original refreshes its parts. A new mail opens an untriaged `email` ticket and the Cc people
  become followers. Files go through `store_attachment` with the size limit and a per-mail count (settings); refused ones leave an event.
  `/webhooks/dev/simulate-inbound` runs the same intake. *Not proved locally (staging):* real Graph and Mimecast behaviour.
  *Not built:* attachments on outgoing mail (3E). (Admin-managed templates were added in 4T.)
- Later: route by sender's Entra attributes; agents forwarding an email to
  the mailbox creates a ticket for the original sender.

## Resolution, reopening and feedback

- Resolution notes required. The requester gets an email with the notes, a
  feedback link and a reopen link.
- Resolved moves to Closed after **N days (TBC)**. A reply after closure
  creates a new ticket linked to the old one (a plain related-ticket link shown on both ticket pages,
  not a parent and child; *built, 4E-1*).
- A reply on a resolved ticket whose new text matches an enabled admin rule
  ("thanks", "cheers"; equals / starts with / contains / ends with, `*` wildcard, a
  maximum length per rule, no regular expressions) and has no attachment is added without
  reopening (*built, 4E-2:* `app/thanks_rules.py`; mail and portal comments alike). Anything else reopens and
  notifies the agent. The emailed reopen link (*built, 4F*) reopens a Resolved ticket after a confirming click; the link expires when the ticket would auto-close (`auto_close_after_days` from resolution, #122); a Closed ticket is never reopened, so the page tells the requester to reply to the email, which opens a linked new ticket.
- Agents reclose with a reason: user said thanks, duplicate, new issue
  raised separately, reopened in error. "User said thanks" sends the reply
  text to an admin review list to add to the ignore list or dismiss. (*built, 4E-2:* Admin > Thanks rules.)
- Chase (*built, 4H:* `app/auto_close_chase.py`): Awaiting user with no reply for **X days** sends "Is this still an
  issue?"; **Y days** after that email went, the ticket is auto-resolved (generic note, resolution email with the reopen link).
  X and Y are admin settings, as is the on/off switch (off until staging). Only Awaiting user is chased (never With supplier or
  With MSP); a split original is chased through its parts; a reply, the reopen link or any status change cancels it; the chase email
  counts toward the automatic-email cap.
- Auto-close (*built, 4H*): a ticket Resolved for **N days** (placeholder 7) is closed by the system (`auto_closed` event). Own switch, off until staging.
- Feedback (*built, 4F:* `app/action_links.py`): random single-use link (only its hash is stored, 30 days by default), 1â€“5 rating and comment, attributed to
  the resolving agent. Admins see all; agents see their own, comments
  included.

## MSP

- Single MSP. Its address is an admin setting (`msp_address`, blank until set; decided 4 Nov 2026, so the build was not
  waiting for it). With no address, Send to MSP is refused with a clear message and the 18:00 job does nothing and
  records why. Built in phase 5A (#138); MSP mail handling and forwarding is 5B (#139).
- Escalating to the MSP was blocked until this handoff was built (decided 22 Oct 2026); testing will use nuisance tickets with the MSP. Not a risk.
- "Send to MSP" button on any ticket. It is the same action as escalating to the MSP
  and the button may carry either label.
- The agent chooses what goes: the **thread** (the default), or **only the original
  request**. The thread holds only useful content: the user's messages, emails sent
  and the comments and notes agents left. Ticket events (triage, assignment, escalation,
  status changes) are never sent. A ticket that has been triaged and escalated with no
  emails sent and no comments left therefore sends only the original request, whichever
  option is chosen. The original request is the first email from the user, with the
  user's details, reference, priority, category and attachments. Use "original only"
  when the ticket was passed between teams and the MSP does not need that history, for
  example 1st line triaged it, escalated it to 2nd line, and 2nd line decided it is MSP
  work. The preview step shows exactly what will be sent. The 18:00 automatic send
  uses the thread. *Built:* the preview is the function that builds the email, and the email is queued with that exact
  text. *Built (5B):* the files are attached to the email up to a total set in Settings (2 MB placeholder, and never more than 2 MB: Graph's request limit is about 4 MB and base64 adds a third); only the files of what is sent go, and the rest are listed by name as too large to attach.
- Each team has an admin setting, "include in the automatic 18:00 MSP handoff"
  (a setting, not a constant). Tickets of a team with it off are never sent
  automatically; an agent can still send them by hand.
- At 18:00 daily, every ticket due to breach before the next business
  morning (08:00, Monday to Friday, UK bank holidays excluded) goes to the
  MSP, unless the assigned agent has put it on hold. A hold lasts until the
  agent releases it. *Built (5A):* the hour (18), the business morning (8) and the bank holidays are admin settings and
  the time is in the organisation time zone. The job (`msp-handoff` in the recurring jobs table, every 5 minutes) works
  from the tickets: open (Open or In progress, triaged), in an included team, not held, not a split
  original, with an unmet first response or the resolution due before that morning (overdue counts; a ticket that arrives or becomes eligible during the night goes at the next run). It only acts between
  the handoff hour and the next business morning, so a missed night is not made up in the day. The assigned agent holds;
  anyone who works the ticket can release it; sending by hand ends a hold.
- The email to the MSP opens with the end user's details and the requesting
  agent, then reference, priority, SLA due time, category, the full thread
  including internal notes, and attachments.
- Status becomes With MSP and the requester is told the ticket has gone to
  the support partner.
- The MSP deals with the user directly with the IT mailbox CC'd, and keeps
  the ticket reference in the subject (agree in writing). MSP messages are
  added to the ticket using `uniqueBody` only.
- When an MSP email leaves the user off To and CC: during business hours the
  agent is prompted to forward it with a preview; out of hours it forwards
  automatically. *Built (5B, #139):* mail from the MSP address is added only to a ticket that was sent to the MSP, never
  makes a ticket, and the MSP is never a requester or follower. A message the user was on is an ordinary public message;
  one they were left off is agents-only until forwarded (the agent previews it in business hours; out of hours it goes to the
  user at once with no preview, and what an agent left unanswered goes when business hours end, the `msp-forwards` job).
  Business hours are admin settings (placeholder 09:00 to 17:30, Monday to Friday, organisation time zone, bank holidays
  from the 5A setting). A message from the MSP changes no status. Handoffs (automatic against manual) and forwards are
  ticket events, which is the reporting data for phase 7.
- *Real MSP mail (#149, decided by the owner from real mail, built):* the MSP's mail is believed by Microsoft's combined
  verdict only, `compauth` in the header literally named `Authentication-Results` whose authserv-id starts `mx.microsoft.com`
  (the first such header; `Authentication-Results-Original` and `ARC-*` are never read; `dkim=fail` is normal through Mimecast and
  is ignored). `compauth=pass` is trusted (out of hours it may be forwarded automatically); `compauth=fail` is set aside and
  audited; anything else (softpass, none, no header) is recorded on the ticket but never forwarded automatically and waits for
  an agent, with the preview, whatever the time. This replaces the old forged-mail open question. The MSP's address is checked
  before the automatic-mail filter (its relay adds List-Unsubscribe), though it still goes through the trust check. The MSP does
  not reply in-thread and uses its own subject, so the ticket is found by the MSP's own ticket number: learned from its
  acknowledgement ("Ticket#7000237/<our subject> -- has been received", read by the admin setting `msp_subject_pattern`),
  which is matched by our `[INC-n]` reference in its title or body, else by the exact title against the subject of an email we
  sent the MSP in the last `msp_ack_match_days` days (only if exactly one matches), and stored as `Ticket.msp_ticket_number`
  (unique). The acknowledgement is an internal note, never forwarded. Later MSP mail carrying the number matches by it first.
  Trusted mail that matches nothing is kept in an agent-only list (`/msp-mail`, "MSP mail" in the agent menu) where an agent
  links it to a ticket or dismisses it. If a ticket sent to the MSP has no acknowledgement `msp_ack_alert_minutes` after its
  handoff email was sent, the assignee (or the team's working members) is told once in the bell: "The MSP hasn't confirmed INC-1042".

## Notifications

- In-app notifications with an unread count, refreshed by htmx polling every
  30 seconds.
- @mentions in notes notify the mentioned agent.
  *Built in 2A (14 Oct 2026):* the bell (`app/notifications.py`, `/notifications`): a link in the
  agent menu that shows the unread count and refreshes every 30 seconds (the poll does not clear
  the flash area). Two things notify so far: a ticket assigned to you (triage, assign, re-assign;
  never to the person who did it) and an @mention in an internal note. `@bob` matches the part of an
  email address before the @, any case; `@bob@example.com` matches the whole address; a name two
  accounts share matches neither; only agents who can open the ticket are told, and never the
  author. Emails to the requester do not mention anyone. Opening a notification and "mark all read"
  are POSTs, so a link scanner or prefetch cannot read them away. Everything that notifies goes
  through `notify()`, where later work adds the Teams copy, per-person settings and skipping
  agents who are out of office. The bell lists only notifications about tickets you can still
  open (one about a ticket that moved to another team disappears), and the text carries the
  ticket number (`#1042`), not a prefix that can change. Mentions match ASCII names only (letters,
  digits, dot, dash, underscore). Not yet notified: the team an escalation lands in, and the agent
  a team change unassigns; both belong with the team notifications of 2E and later.
  *Bell access rule (decided):* a notification about a ticket the agent can no longer open (moved to
  another team, or they left the team; followers and the past notification grant nothing) is hidden
  everywhere: not listed, not counted, opening it is a 404, and "mark all read" leaves it unread.
  It returns if access returns. Ticket numbers and subjects reach the page only through
  `bell_tickets`, which applies `visible_ticket_condition`. The Teams copy checks access again at send
  time (a queued post for a ticket that has since moved is unsendable).
  *Built in 2B (19 Oct 2026):* `/settings/notifications` (a "Settings" link for everyone signed in;
  `app/notification_settings.py`). Each event type lists the channels it can use and which are on by
  default: assigned and mentioned (agents: the bell and Teams, both on), "an agent raises a ticket for
  you" (everyone: email, on). Only differences from the default are stored (`NotificationPreference`),
  so changing a default reaches everyone who never touched it; saving writes an audit entry listing
  what changed. `notify()` skips the bell when the person turned it off; the Teams and email choices
  are recorded now and read through `is_enabled(session, user, kind, channel)` by 2C and phase 3, which
  must not keep settings of their own. `should_tell_requester(session, email)` is the global switch AND
  the person's own choice when they have an account. The per-person Teams delay (default 5 minutes)
  is not here: it arrives with the worker in 2C.
  *Follow-up (20 Oct 2026, from the review of #38):* when an agent raises a ticket for someone, the
  decision "will this person be told by email?" is made then, while their address is known, and recorded
  on the ticket as a `requester_notice` event ("will be told" or "not told", with the reason: switched
  off for everyone in Settings, or they turned it off in their own notification settings); phase 3's
  mail sends only if it asks again at send time and still gets "yes" (the event is history for the agent and can be stale: the switch or the person's choice may change before the email goes). Saving the settings page takes a per-person lock, so a double click or two
  open tabs queue up instead of both inserting the same row. Preferences stored for an event a role
  does not have are harmless: only agents are ever notified, and the choice returns with the role.
- A reply notifies the assigned agent, whatever the ticket's status.
  *Built (4 Nov 2026, #130): the bell for everyone.* The requester, and external followers who have an account, get
  in-app entries about their own tickets: an agent emailed you, the ticket is waiting for your reply (Awaiting user),
  resolved, closed, and the approval outcome (approved, declined or waived). Each is an event on My notification
  settings (in-app only, default on); email is unchanged and there is no Teams copy. Never the person who caused it.
  The entries open the portal ticket (`/portal/{n}`), also for an agent who is the requester. Agent-only kinds are still
  refused for people who are not agents. A sub-ticket created is left for #118.

  *Built (20 Oct 2026):* event type `replied` ("A ticket assigned to you is updated by the requester"; bell and Teams,
  both on by default; Teams label "Ticket updated"; bell text "#42 Printer jam: Ticket updated by Jo Bloggs", the replier's display name, else their email). When the requester or a follower replies, the ticket's assignee is told in every status,
  including Awaiting user, With MSP, Resolved and Closed, where the reply moves nothing and this is the
  only sign of it. It is raised in `record_inbound` (`app/messages.py`) only when the caller passes
  `reply_by`, so the message a ticket is created with (portal raise, raise on behalf, simulate-inbound)
  tells nobody. A reply on a split original also tells the assignees of its open parts, each person once.
  Never the person who replied; only agents who can open the ticket; the per-person channel settings
  apply. A New ticket has no assignee, so nobody is told. A triaged ticket nobody owns also tells
  nobody (decided: no one is notified when there is no assignee).
- A notification still unread after N minutes (user setting, default 5) is
  also sent to Teams.
  *Built in 2C-ii (20 Oct 2026):* a notification is written whenever
  the person wants the bell or Teams for that event (neither: none). It records both wishes; a
  Teams-only one is written already read and hidden from the bell, so the bell never shows or counts it.
  A Teams copy is **due** straight away for a Teams-only row, or once a bell row has stayed unread for
  the person's delay: their own number of minutes (their notification settings, 1 to 1440, blank =
  default) or the admin default, the setting "Unread notifications are also sent to Teams after
  (minutes)", initially 5. Reading it in time means no copy. `hand_to_teams` (run by the worker) hands
  each due row to a sender once, locking the rows so two workers cannot take the same one, and drops it
  instead if the person has since turned Teams off for that event or can no longer open its ticket
  (a Teams message says what the ticket is). Rows written before this change get no Teams copy, so
  switching it on sends no flood of old notifications.
  *Sending:* the worker job `teams-copies` (every minute) calls `hand_to_teams`, which queues each due
  notification as a `TeamsPost` of kind `notification` (one per notification, enforced by a unique
  column) in the same outbox as the new-ticket cards; the existing `teams-posts` job then sends it with
  the shared retry/backoff. The card is an Adaptive Card that **@mentions the person by their email**
  (a `msteams.entities` mention, which is how a channel post can reach one person; messaging a person
  directly is the phase 8 bot), says what happened and who did it, and links to the ticket when
  `PUBLIC_BASE_URL` is set. It goes to the same channel and webhook as the new-ticket cards, behind its
  own switch `TEAMS_NOTIFICATIONS_ENABLED` (off by default, every process needs it). With the switch off
  the due copies are dropped, not kept, so nothing floods out when it is switched on. The text is made
  plain (a requester's subject cannot become a link or markup) and the mention name is stripped of
  anything that could close the tag. The settings page stops saying Teams is "not connected yet" once the
  switch is on.
- Teams is used for assignments, mentions, P1s and SLA breaches. New tickets
  post to a Teams channel through a Teams Workflows webhook (decided 22 Oct 2026: keep that for the new-ticket channel; personal
  notifications move to the phase 8 bot, and there is no separate notifications channel).
  *Built in 2D (19 Oct 2026):* `app/teams.py`. Creating a ticket (`create_ticket`) writes a `TeamsPost` row in the
  ticket's own transaction when `TEAMS_NEW_TICKET_ENABLED` is on, so a ticket is never announced and rolled back or
  created and forgotten, and a Teams outage never slows the request. The worker calls `deliver_pending`, which sends
  the oldest unsent rows (a message carrying an Adaptive Card: number, subject, requester, how it arrived, and an "Open
  ticket" link when `PUBLIC_BASE_URL` is set), marks them sent, and keeps a failed one with its reason ("Teams answered
  401", never the URL). A failure waits 2, 4, 8, 16, 32, 60, 60 minutes before the next try (eight attempts, so a Teams outage of a few hours is ridden out); a 4xx other than a timeout or rate limit is given up on at once. The subject and address on the card are escaped so a requester cannot put a link or formatting in the channel. A worker without the URL logs a warning while posts wait. Delivery is at least once. Every way in announces (email, portal, agent)
  except the parts of a split, which are not new. The webhook URL is only for the process that sends (the worker): web and
  public need just the switch. Test with a fake sender; whether Teams accepts the card is a staging check.
- The requester hears that their ticket was picked up through the channel
  it was raised from, unless their settings say otherwise. Users and agents
  choose channels per event type in portal settings.
- Out-of-office agents are skipped for assignment and team notifications.
  The worker reads `outOfOfficeSettings.isOutOfOffice` from the Graph
  presence API (`Presence.Read.All`, application, admin consent; up to 650
  users per request). It covers Outlook automatic replies, Teams scheduled
  out of office, and calendar events marked out of office.
  *Decision (20 Oct 2026): out of office is a label, never a block or a skip.* Sometimes only one person can fix a problem, so
  they are assigned regardless of availability and deal with it when free. The pickers label them and nothing refuses or
  filters them out; only messages to a whole team leave them out.
  *Built in 2E (20 Oct 2026):* `app/presence.py`. The `presence` job (every 15 minutes, only with `PRESENCE_ENABLED` and the
  GRAPH_* credentials) looks each agent up in the directory once by email (cached as `User.entra_object_id`; an agent Graph
  does not know is never marked), then reads everyone's presence in batches of 650 and stores `out_of_office` and when it was
  read. Readers use `is_out_of_office`, which ignores a reading older than three hours, so a stopped job or an outage never
  leaves anyone out of office for good. *Built in 4B-4:* a person Graph does not know, or who gets no reading, is left alone until the `presence_retry_minutes` setting has passed (`User.last_presence_lookup_at`), and `User.is_active = false` stops polling them (the rest of `is_active` is phase 6). What it changes: an out-of-office agent is labelled "(out of office)" in the assign,
  re-assign and triage pickers (always selectable), and `notifiable_members`
  drops them from any message to a whole team. Nothing sends to a whole team yet (the team an escalation lands in and the agent
  a team change unassigns are still not notified), so that helper is the hook for those. A notification meant for one person
  (assigned, mentioned) still reaches them. Tested with simulated Graph responses; real Graph is a staging check.
  *Planned (decided 21 Oct 2026, a small PR):* add `User.last_presence_lookup_at`. A user whose directory lookup failed or found nobody
  is skipped until a retry interval setting (TBC) has passed, instead of being retried every 15 minutes. Also consider an `is_active`
  flag on users so inactive agents are not polled at all. That flag is shared with the nightly Entra `accountEnabled` check for leavers (phase 6; see Identity and access).

## Admin portal

Users, teams and memberships, categories, SLA targets per priority, business
hours and bank holidays, settings (chase X and Y, auto-close N, automatic
email cap, ignore phrases, Teams delay default), Service Request approval groups,
email templates, canned responses, unlocking locked local accounts, outage mode, the audit log (paginated,
filterable by actor and by subject user), category and ignore-phrase review
queues, all feedback, and reports.

## Reporting

Built on `ticket_event`: volume by category, team, type and source; SLA
compliance for first response and resolution; mean time to first response
and resolution; backlog age; reopen rate and "user said thanks" recloses;
satisfaction by agent, team and category; MSP handoffs split automatic and
manual; Major Incident count and duration; incidents per Problem. CSV export
for all of them.

## Required by date and Due date on service requests (SRP-1, issue #92; Due date, issue #121)

Decided by the owner. A service request can carry a **Required by** date (the day the requester needs it), offered per
request type (`RequestType.offers_required_by`, off by default). A ticket also has a **Due date** (`Ticket.due_on`), and
that is what the SLA measures.

- **Due is the SLA resolution target.** The resolution deadline is the end of the Due day in the requester's time zone,
  stored as UTC (`app/time_zones.py`: `end_of_day_for_email`; "Time zones" below). A ticket with a Due date needs no
  priority for it. The first-response target is unchanged. The SLA is reported against Due, worded "required by" where the
  ticket has a Required by date, otherwise "due".
- **Default.** With a Required by date, Due defaults to Required by minus N days. N is the request type's `due_days_before`;
  blank uses the setting `due_days_before_required_by` (placeholder 1). A room needed on the 23rd is due by the end of
  the 22nd. The default is never earlier than today (a request for tomorrow with a three day lead is due today, not already
  late; decided in the PR) and never later than Required by.
- **Agents can move Due.** Only agents who work the ticket (`visibility.may_set_due`); never the requester. Every move
  needs a reason (setting the first Due date does not), recorded as a `due_changed` ticket event and an audit entry.
  Due can never be later than Required by, never in the past (in the requester's time zone) unless the ticket is backdated,
  and never more than ten years ahead. A blank date puts Due back to the default. Use case: a room only available on the
  morning of the event; the agent sets Due to the Required by day and the ticket is not shown as breached.
- **When Required by changes**, Due is recalculated from the default offset, unless an agent set it by hand
  (`Ticket.due_set_by_hand`); even then it is clamped so it is never after Required by. Clearing Required by clears a
  default Due and keeps a hand-set one. A Scheduled ticket always keeps a Due date.
- **Tickets with no Required by** can still get a Due date from an agent (the Due form on the ticket, or the triage form).
- **Awaiting user always pauses the SLA clock**, including on a ticket with a Due date. A Due date never moves while it is
  paused: the SLA view says "paused" and the date stays.
- **Queue lead time, per request type.** `RequestType.queue_lead_days`, blank meaning the setting `required_by_lead_days`
  (placeholder 1). It only affects sorting: the queue ranks a ticket with a Due date as if it were due that many days
  before Due.
- **Who sets Required by.** The requester in the portal at any time while the ticket is open (no longer only before
  triage); agents who work the ticket can set, change or clear it at any time. A request to change it by email or Teams is
  handled by an agent. A date more than ten years ahead is refused. Past dates are refused unless an agent is backdating
  (raising for someone with Reported/Resolved times, or a ticket already flagged backdated). A date cannot be changed on a
  resolved or closed request. Switching a type's field off keeps the dates already on tickets.
- **The triage form** has Required by and Due (both optional). Required by shows only when the chosen kind of request
  offers it, by an htmx swap like category to agents; Due shows for any ticket. Because triage had no way to choose the kind of
  request, the form also offers a "Kind of request" list (chosen before the approvers are looked up). Blank dates are fine: an
  emailed request with no date is triaged, the agent asks the user and adds the date later.
- **Audit.** Every change to Required by or Due writes a ticket event and an audit entry.

Where it lives: `app/required_by.py` (all the rules), `app/sla.py` (deadline and queue), `app/visibility.py`
(`may_set_required_by`, `may_set_due`), `app/triage.py` (the kind of request at triage), the Due form and triage boxes in
`app/templates/ticket_detail.html` and `_triage_dates.html`.

## Time zones (issue #120, built)

Owner decision: many people travel or are international consultants, so each person sees times in their own time
zone. **Everything is stored and calculated in UTC**; only what a person sees is converted, in one place
(`app/time_zones.py`).

- **Where a person's zone comes from.** (1) Agents and portal users: the browser reports
  `Intl.DateTimeFormat().resolvedOptions().timeZone` from every page (a small script in `base.html`, posted to
  `/settings/time-zone/browser`) and it is saved on the account, updating whenever it changes. A person can choose
  one by hand on their settings page; a manual choice wins until they clear it. (2) Requesters who only use email:
  their Outlook mailbox time zone read through Graph (`GET /users/{id}/mailboxSettings`, `timeZone`; application
  permission `MailboxSettings.Read`, admin consent). **Off by default** (`MAILBOX_TIME_ZONES_ENABLED`, owner decision
  #129: that permission cannot be scoped to the shared mailbox; a feature for after a stable release). When on, looked up by the worker soon after their first ticket is
  created and refreshed after an admin-set interval. Graph answers with a Windows name ("GMT Standard Time"); the
  CLDR windowsZones table kept in `app/windows_zones.py` maps it to an IANA name (no network fetch at run time).
  (3) Everyone else (external senders, failed lookups): the admin setting `default_time_zone` (placeholder
  `Europe/London`).
- **Where it is stored.** `User.time_zone` and `User.time_zone_source` (`browser` or `manual`). A requester with no
  account is stored by lower-cased address in `requestertimezone` (zone, source `mailbox`, last-checked and next-check
  times), the same key a ticket uses for its requester (`Ticket.requester_email`). An account's own zone beats a
  mailbox reading, which beats the default. Only IANA names are stored, checked against the installed tz database;
  an unknown name is ignored (or, if it was stored and later stops being valid, skipped for the default). `tzdata`
  is a requirement so the database does not depend on the container image.
- **Display.** One pair of functions (`format_moment`, `format_date`) and the Jinja filters `local_time` and
  `local_date` (the viewer's zone). No template formats a time itself. Pages show "25 Oct 17:00 GMT" (a year is added
  when it is not this year, seconds only on log pages). An email to a requester is written with `format_moment` and
  `zone_for_email`; none of the current emails carries a time, so none changed. Dates that are dates (Required by) stay
  dates; the backdate and raise-form time boxes are shown and read in the agent's own zone, while the event and audit
  text of a backdate stays UTC and says so.
- **The helper for the Due date (#121):** `end_of_day_utc(day, zone_name)`, and the shortcuts
  `end_of_day_for_email(session, day, email)` and `end_of_day_for_user(session, day, user)`: "end of day D in that
  person's zone, as UTC", the first instant of D+1 there (so a deadline compares with `<`), right across clock changes.
- **Settings.** `default_time_zone` (Europe/London), `time_zone_refresh_days` (30), `time_zone_retry_hours` (24).
  A failed or unmappable lookup keeps any earlier reading and is retried after the retry interval.

## Build order

| Phase | Scope |
|---|---|
| 0 | Security fixes, Alembic, `/health`, audit log pagination and filters, account lockout, list pagination, environment-targeted deploys |
| 1 | Ticket model: types, references, statuses, source, categories, impact Ã— urgency, SLA targets and clock, priority queue, message visibility, followers, linking, team memberships and visibility, reassignment, escalation changes, backdating, `ticket_event`, search, portal new request form, canned responses, preview step, Service Requests and approvers, sign-in follow-ups (unlock button, clear Entra sign-in state, logout as POST) |
| 2 | Worker, in-app notifications, @mentions, notification settings, Teams channel webhook for new tickets, out-of-office detection |
| 3 | Graph mail: intake, threaded replies, `uniqueBody`, delta catch-up, subscription renewal and lifecycle handling, auto-reply filter, attachments in Blob Storage, `public` app mode for the webhook |
| 4 | Approval groups (see Service Requests), resolution, reopen and reclose flow, ignore list and review, chase emails, auto-close, feedback, bulk resolve |
| 5 | MSP handoff, hold, 18:00 job, MSP mail handling and forwarding |
| 6 | Break-glass accounts, TOTP, outage mode, Entra app roles, match users on Entra object id, nightly Entra `accountEnabled` check (`is_active`), linked identities |
| 7 | Reporting and CSV export, subject access export, anonymisation (including requester feedback comments, which are personal data; feedback reports keep the rule that an agent sees only their own) |
| 8 | Teams bot: direct messages to agents and ticket intake from Teams |
| 9 | Google Stitch redesign once the UI is feature-complete, to WCAG 2.2 AA |
| 10 | Security hardening and the go-live gate (see "Security hardening phase"). Nothing here is optional for production; small items are done earlier when their feature is built |

The Azure infrastructure (`infra/`, Bicep) is scaffolded and not deployed.
Staging is deployed when the app is ready for testing, then initial roll-out
on the internal domain, then production before go-live. The code changes it
needs are listed under "Hosting on Azure"; infrastructure work runs
independently of the code phases.

## Security hardening phase

Added 14 Oct 2026 from a review of this plan. A final pass before production. Where the item
belongs to a feature (the link pages in phase 4), build it with that feature and tick it off
here; this list is what the go-live review checks.

1. **Emailed links must survive scanners.** `/reopen/*` and `/feedback/*` GETs only show a
   confirmation page; the action (reopen, rate) happens on POST, with a CSRF-safe form. Mail
   scanners that prefetch links then neither reopen tickets nor use up single-use tokens.
   Test: a GET never changes state.
2. **Session revocation.** Sessions are signed cookies (Starlette `SessionMiddleware`, default
   14-day lifetime, no server-side record). Nothing can end one early. Move to a server-side
   session (a `user_session` table: id in the cookie, user, created, last seen, expiry, revoked
   flag) or add a per-user `session_version` checked on every request, plus a short idle
   timeout. Needed for: outage mode ending password sessions, lockout, role removal, a leaver
   disabled in Entra, and a "sign out everywhere" button. Also add `User.is_active`, which does not
   exist yet, and check it in `get_current_user`.
3. **Match users on the Entra object id (`oid`)** before any real users sign in. Not urgent while
   the app is only run in development; it is a go-live gate item, and may be built with Entra
   app roles in phase 6.
4. **Graph mailbox scoping.** (The by-hand steps are in `docs/azure-setup.md`, section 3; this item is the go-live check that it was done and tested.) Application permissions such as `Mail.ReadWrite` cover every
   mailbox in the tenant. Restrict the app registration to the shared mailbox(es) only
   (Exchange RBAC for Applications, or an application access policy) and verify it on staging
   by trying to read another mailbox. Same check for `Presence.Read.All`: confirm what scoping
   is possible. `MailboxSettings.Read` (requesters' time zones) is not requested while `MAILBOX_TIME_ZONES_ENABLED` is off; it
   cannot be scoped to the shared mailbox, so turning that feature on needs its own decision.
5. **Rate limiting with a shared store** (Postgres table or Redis), not per-process memory:
   `public` runs several uvicorn workers across replicas. Keyed on the real client IP, which
   depends on `FORWARDED_ALLOW_IPS` being right.
6. **Mail loop protection** beyond `Auto-Submitted`: also `Precedence: bulk/junk/list`,
   `X-Auto-Response-Suppress`, `X-Autoreply`, bounce (NDR) messages and `mailer-daemon`
   senders, and ignore mail from our own mailbox. Set `Auto-Submitted: auto-generated` and
   `X-Auto-Response-Suppress: All` on everything the system sends.
   Optional defence in depth (not planned now, 22 Oct 2026): read Authentication-Results on inbound mail; the
   shared mailbox itself accepts authenticated senders only (see Mail).
7. **Secrets that live in settings.** Teams Workflows webhook URLs let anyone post to the
   channel, so keep them in Key Vault (or encrypted), never in a plain settings row or logs.
8. **Anonymisation and erasure are complete.** Besides name, email and attachments: message
   bodies and signatures, followers, the activity log (IP, browser), audit entries that name the
   person. Document what remains (Postgres backups up to the retention window, blob soft-delete
   and version retention) and how long it takes to leave.
9. **Verify the presence API** (`outOfOfficeSettings`) is in Graph v1.0, not beta only, before
   relying on it.
10. **Go-live gate.** Production needs, as a minimum: phases 1 to 6 (including resolution and
    feedback, break-glass and outage mode), this list, the Azure-only verification list passed on
    staging, a restore test, and the retention decision from the data protection lead.

## Later and stretch

Knowledge base (stretch), Projects with Azure DevOps, change control,
automated triage including routing sensitive emails to restricted teams,
routing by sender, forwarded-email intake, portal-only notes.

## Accepted risks

- Indefinite retention, pending the data protection lead.
- An indefinite MSP hold can leave a ticket uncovered overnight if the
  agent leaves without releasing it.
- Out-of-hours MSP forwards go to users without an agent preview.
- Every agent sees untriaged tickets, including sensitive ones, until
  automated routing exists.
- The `public` app is reachable by anyone with no WAF in front of it; the
  protections are in the app (tokens, rate limits, webhook validation).
- Requesters get the portal only from the office network or VPN. Email and
  Teams are their channels.

## TBC values

Shared mailbox address, database restore window (7 to 35
days), upload size limit, SLA targets per priority, triage team, auto-close
days, chase X and Y, automatic email cap per ticket, office and VPN IP
ranges, internal domain name, alert recipients, Azure subscription.

## Scheduled status (SRP-2, issue #93; decided in issue #121)

A ticket status **Scheduled** for work planned for a day. Decided by the owner:

1. **Scheduled never pauses the SLA clock.** Only Awaiting user pauses it. Moving from Awaiting user to Scheduled ends the pause.
2. **Setting Scheduled requires a date: the Due date,** filled in when the ticket already has one. Every way of setting it asks
   for it: the status buttons (and reopening to Scheduled by hand), and the status dropdown on Email user and Add note (and its
   preview, which carries the date to the send). Typing a different date moves Due and needs a reason like any move; a date already
   in the past is refused. `set_status` itself refuses Scheduled on a ticket without a Due date, except for a split original whose
   status follows its parts. Only agents set Scheduled; the system never does.
3. **On the morning of the Due date** (the requester's time zone) a recurring job, `scheduled-due` (`app/due_today.py`, every 15
   minutes on the database-driven scheduler), moves Scheduled tickets back to **Open**, tells the assignee in the bell
   ("#1042 Subject is due today" with the number, as every stored notification text, an agent setting `due_today`, on by default in the bell and Teams), and records a status event and an
   audit entry by `system`. "Morning" is the hour in the setting `scheduled_morning_hour` (placeholder 7). It claims each ticket with
   `FOR UPDATE SKIP LOCKED` and checks again under the lock, so a late or repeated run does nothing twice, and a ticket Due in the
   past (for example when the date was moved back) is taken at the next run.
4. Replies follow the With supplier pattern: a requester or follower reply on a Scheduled ticket changes no status and notifies the
   assigned agent. Emailing the requester also leaves it Scheduled. It is never chased (only Awaiting user is).
5. Wording is the plain label "scheduled". A split original takes Scheduled from its parts by the same rule as every status.

## To-do lists and request checklists (SRP-3, planned, not built)

Asked for by the owner (3 Nov 2026): a service request type can have a **prebuilt to-do list** that appears on every ticket of
that type, and each item can link to the documents needed to do it ("New starter": create the account, assign the licence,
order the laptop, each with a link to its how-to). Nothing below is built. All points were **confirmed by the owner on 3 Nov 2026**:

1. **Decided: defined per request type** in Admin > Request types (the 1H-i catalogue): an ordered
   list of items, each a short label, an optional link (a URL to a document, such as a SharePoint or knowledge base page) and an
   optional note. Only super admins edit it, like the rest of the catalogue, and changes are audited.
2. **Decided: copied onto the ticket when it becomes that request type** (raised from the form, or
   retyped at triage), like the approval's vote percentages: later edits to the template do not change tickets already open.
   Retyping away from the request type keeps the ticket's list (ticked items are history). Sub-tickets from a split do not inherit it.
3. **Decided: agents only.** Shown on the agent ticket page; anyone who works the ticket can tick and
   untick an item, and each tick writes a ticket event (who, when). The requester does not see it in the portal or in email.
4. **Decided: advisory, not blocking.** An unfinished list does not stop Resolve; the resolve step
   says how many items are left. Agents can add a one-off item to a ticket's own list but cannot remove template items.
5. **Decided: links are plain links** to documents stored elsewhere; the app does not host them. If
   hosted documents are wanted later they reuse the 3E storage interface and its visibility check. Links must be `https://`.
6. **Asked for by the owner (3 Nov 2026): a to-do list on any ticket.** Any agent who works a ticket, of any type (incident,
   service request, major incident, problem), can add a to-do list to it, when raising it for someone or later from the ticket
   page. It is the same per-ticket list as above, so a service request with a template simply starts with items in it. Decided:
   agent-added items can be edited and removed by anyone who works the ticket (template items cannot
   be removed); one list per ticket, not several named lists.
7. **Asked for by the owner (3 Nov 2026): entering items in bulk.** Wherever a list is written (the request type template in
   admin, and a ticket's own list) there are two ways to enter items besides one at a time:
   - **One per line:** a text box where each non-blank line becomes an item, in order.
   - **CSV:** paste CSV text or upload a `.csv` file. Decided: columns `label,link,note` in that
     order, only `label` required; a first row reading `label` is treated as a header and skipped; UTF-8, quoted fields allowed.
   Decided: either way the parsed items are shown for checking before they are saved; a bad row
   (empty label, a link that is not `https://`) is reported by row number and nothing is saved until it is fixed; new items are
   added after any existing ones; at most 100 items per list (an admin setting, placeholder value). The CSV file is read and
   thrown away, never stored.

Schema: a template table keyed by request type and a per-ticket item table that flags agent-added items; both need an Alembic migration.

## Loans of shared assets (LOAN, planned, not built)

Asked for by the owner (3 Nov 2026): IT lends out **shared** equipment (chargers, power banks, spare laptops, headsets,
adapters) and needs to know what it has, who has each item and when it is due back. This is **not** a full asset register:
devices issued to one person for keeps are out of scope, and nothing is imported or discovered (no Intune, Entra or CSV
feed). **Every asset is created by hand.** Not in a numbered phase yet; it can be built any time after phase 4, as its own
PRs. The points below are **Proposed - needs owner confirmation** (each can change cheaply):

1. **Proposed - needs owner confirmation: the asset record.** An asset tag (typed in, unique, the label stuck on the item), a
   name, a kind from an admin-managed list (Charger, Power bank, Laptop, Headset, Other; seeded, editable), an optional serial
   number and an optional note. Its state is one of Available, On loan, Under repair or Retired. An asset is never deleted, only
   retired, so its loan history stays.
2. **Proposed - needs owner confirmation: who manages assets.** Any agent can add an asset, edit it and change its state
   (Available, Under repair, Retired); assets are shared across all IT teams, not owned by a team. Kinds are managed by super
   admins in Admin. Every change writes an audit entry.
3. **Proposed - needs owner confirmation: a loan.** Lent to a person (picked from known people or typed as an email address;
   they need no account, like a requester), lent by the agent, out at, an optional due back date, an optional note and an
   optional link to a ticket (for example a loan service request). One open loan per asset; lending an asset that is On loan,
   Under repair or Retired is refused. Returning records who took it back, when, and an optional condition note, and the asset
   goes back to Available (or Under repair if the agent says it is faulty).
4. **Proposed - needs owner confirmation: where it shows.** An agent "Loans" page: all assets with their state and a filter by
   kind and state, the loans that are out, and the overdue ones (past their due date) first. Each asset has a page with its
   loan history. A ticket with linked loans lists them. The requester sees their own open loans, read only, in the portal
   ("My loans"); nobody else's.
5. **Proposed - needs owner confirmation: no automatic emails at first.** Overdue loans are shown, not chased. A reminder to the
   borrower can be added later as a 4A recurring job with an admin template and a days setting, off by default (it needs mail
   proved on staging).
6. **Proposed - needs owner confirmation: reporting** (phase 7) adds loans out, overdue loans and the most borrowed kinds.

Schema: `asset`, `asset_kind` and `asset_loan` tables (an Alembic migration). No Azure-only parts: nothing here needs staging to
prove it, except a later reminder email.

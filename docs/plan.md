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

**Code changes the Azure plan needs:**

- `APP_ROLE` (`web`, `public`, `worker`) selects which routers are mounted.
  Needed before `public` can be deployed; due with the Graph webhook (phase 3).
  Not made yet.
- `RUN_MIGRATIONS=false` makes the entrypoint skip `alembic upgrade head`, so
  replicas do not race; the migrate job runs it instead. Done.
- uvicorn proxy headers: uvicorn trusts `X-Forwarded-*` from the addresses in
  `FORWARDED_ALLOW_IPS`, which the Bicep sets to the Container Apps subnet (where
  the ingress proxies run), not `*`. With `*` uvicorn takes the leftmost
  `X-Forwarded-For` entry as the client address, and a client can forge that.
  No code change needed. Verify on staging: the HTTPS-only cookie works, and a
  request with a forged `X-Forwarded-For` is recorded with the real address.
- Settings check per role: once `APP_ROLE` exists, require `SESSION_SECRET_KEY`
  only for `web`, then drop the internal session key secret.
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
- Storage layer behind an interface, with a local-disk or Azurite
  implementation for development and a Blob implementation using
  `DefaultAzureCredential`, phase 3.
- Application Insights via the `azure-monitor-opentelemetry` package.
- App-level rate limiting on the public routes.
- Write the deploy workflow (the old SSH-to-EC2 one was removed): OIDC login,
  build and push, migrate job, deploy.

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

**Roles.** `super_admin`, `agent`, `end_user`. Moving role assignment to
Entra app roles (the `roles` claim) comes later; assigning whole groups to an
app role needs Entra ID P1.

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
(a service request waiting for its approvers, 14 Oct 2026), With MSP, Resolved, Closed. Reopening is a recorded event, not a status.

**Status follows the conversation** (agreed 12 Oct 2026). The SLA clock pauses in
Awaiting user and chase emails are sent from it, so the status must be right
without anyone remembering to set it by hand.
- **Agent emails the user.** The ticket moves to **Awaiting user** by itself. The
  agent is not asked and cannot opt out: there is no tick-box or choice on the
  form (agents can still set a status by hand with the status buttons). It applies
  to Open and In progress tickets; a ticket With MSP is waiting on someone else, and
  a Resolved or Closed one is finished, so those do not move. It does not apply to
  the resolution email, which is the resolve action in phase 4.
- **The user replies.** When the requester, or a follower, replies while the ticket
  is Awaiting user, it moves back to In progress automatically and the SLA clock
  resumes. This covers portal comments now and email replies once mail intake
  exists. A reply on a New ticket changes nothing; on a With MSP ticket it changes
  nothing (the agent decides); on a Resolved or Closed ticket it is the reopen flow
  in phase 4.
- Both moves are ordinary status events in the timeline and the audit log, with
  the person who caused them.

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
target is left. Times are shown in UTC until the per-viewer timezone work.
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
- The MSP handoff is phase 5. Until it exists a category whose target is the MSP
  (the default) cannot be escalated: the Escalate control says so.
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
reopening a resolved original by hand is refused until the phase 4 reopen flow). Emailing from the
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
before the report; the flag is permanent and removes the SLA deadlines. Times are UTC until
the timezone work. The resolution email (default on) comes with the resolve action, phase 4.

**Service Requests.** Approvers are configured per request type in the
admin portal.
*Built in 1H-i (14 Oct 2026; the plan said no more than the line above, so these are working
defaults to confirm):* a **request catalogue**. Admin > Request types (super admin) lists the
things people can ask for ("New starter", "Software install"): a name, a short description
shown on the form, up to 10 approvers (people with an account, who need not be agents), and an
on/off switch (off hides it from the form; tickets keep it). The raise form (`/portal/new`) asks
"What do you need?" with those types plus "Something is not working"; choosing a type makes the
ticket a Service Request (`SR-`) and records `request_type_id` and a `request_type` event. Agents
raising for someone else can pick one too. The agent page shows the request and its approvers; the
requester sees the request name only. Sub-tickets from a split do not inherit the request type.
*Built in 1H-ii (14 Oct 2026, decisions confirmed that day):*
- **A new status, Awaiting approval.** Reached only through triage; it is the one place the plan's
  status list grew. While in it the ticket cannot be moved by hand by anyone (no status buttons;
  the server refuses with a clear message), cannot be split, and cannot be logged as already
  resolved. The SLA clock keeps running. Agents can still triage, assign, write notes and email.
- **A reusable rule** (`app/approvals.py`, written so another kind of ticket, such as change
  control, can use it): two numbers per request type, *approvals needed* and *declines needed*,
  both defaulting to one (any single approver decides), copied onto the ticket when approvals
  start so later edits do not change a request already waiting. Approved when the approvals reach
  the first number; declined when the declines reach the second, or when so many have declined that
  the approvals can no longer be reached. Whichever happens first settles it and everyone still
  pending is marked "not needed". Approved: the ticket goes to Open and the requester gets an
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
- **Not chasing yet.** Approvers who never answer are not chased before phase 3. When mail and
  Teams exist, asking an approver and chasing them go by **email and Teams**, like the other
  notifications; approvals will want a reminder schedule alongside the auto-close timers.

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
Automatic emails use admin-managed templates, previewed when edited.

## Mail (Microsoft Graph)

- Shared mailbox address **TBC**.
- Webhook plus a delta catch-up poll every 5â€“10 minutes. The webhook stores
  the notification and returns `202` within 3 seconds; the worker fetches
  and processes the message. Graph retries for up to 4 hours, and drops
  notifications to persistently slow endpoints.
- Duplicates prevented by a unique constraint on `internetMessageId`.
- Subscriptions last up to 10,080 minutes. The worker renews any with under
  24 hours left and recreates on 404.
- `lifecycleNotificationUrl` set at creation (it can't be added later):
  `reauthorizationRequired` renews via `PATCH`, `subscriptionRemoved`
  recreates then runs a delta sync, `missed` runs a delta sync. Never
  `/reauthorize` and `PATCH` the same subscription within 10 minutes.
- Replies read with `$select=uniqueBody` to get only the new text.
- Auto-replies filtered on the `Auto-Submitted` header.
- Per-ticket cap on automatic emails to stop mail loops.
- Later: route by sender's Entra attributes; agents forwarding an email to
  the mailbox creates a ticket for the original sender.

## Resolution, reopening and feedback

- Resolution notes required. The requester gets an email with the notes, a
  feedback link and a reopen link.
- Resolved moves to Closed after **N days (TBC)**. A reply after closure
  creates a new ticket linked to the old one.
- A reply on a resolved ticket that is short and matches the ignore list
  ("thanks", "cheers") is added without reopening. Anything else reopens and
  notifies the agent. The reopen link always reopens.
- Agents reclose with a reason: user said thanks, duplicate, new issue
  raised separately, reopened in error. "User said thanks" sends the reply
  text to an admin review list to add to the ignore list or dismiss.
- Chase: Awaiting user with no reply for **X days** sends "Is this still an
  issue?"; **Y days** more auto-resolves. X and Y are admin settings.
- Feedback: signed single-use link, 1â€“5 rating and comment, attributed to
  the resolving agent. Admins see all; agents see their own, comments
  included.

## MSP

- Single MSP, address **TBC**.
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
  uses the thread.
- Each team has an admin setting, "include in the automatic 18:00 MSP handoff"
  (a setting, not a constant). Tickets of a team with it off are never sent
  automatically; an agent can still send them by hand.
- At 18:00 daily, every ticket due to breach before the next business
  morning (08:00, Monday to Friday, UK bank holidays excluded) goes to the
  MSP, unless the assigned agent has put it on hold. A hold lasts until the
  agent releases it.
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
  automatically.

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
  mail reads that event. Saving the settings page takes a per-person lock, so a double click or two
  open tabs queue up instead of both inserting the same row. Preferences stored for an event a role
  does not have are harmless: only agents are ever notified, and the choice returns with the role.
- A notification still unread after N minutes (user setting, default 5) is
  also sent to Teams.
- Teams is used for assignments, mentions, P1s and SLA breaches. New tickets
  post to a Teams channel through a Teams Workflows webhook.
- The requester hears that their ticket was picked up through the channel
  it was raised from, unless their settings say otherwise. Users and agents
  choose channels per event type in portal settings.
- Out-of-office agents are skipped for assignment and team notifications.
  The worker reads `outOfOfficeSettings.isOutOfOffice` from the Graph
  presence API (`Presence.Read.All`, application, admin consent; up to 650
  users per request). It covers Outlook automatic replies, Teams scheduled
  out of office, and calendar events marked out of office.

## Admin portal

Users, teams and memberships, categories, SLA targets per priority, business
hours and bank holidays, settings (chase X and Y, auto-close N, automatic
email cap, ignore phrases, Teams delay default), Service Request approvers,
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

## Build order

| Phase | Scope |
|---|---|
| 0 | Security fixes, Alembic, `/health`, audit log pagination and filters, account lockout, list pagination, environment-targeted deploys |
| 1 | Ticket model: types, references, statuses, source, categories, impact Ã— urgency, SLA targets and clock, priority queue, message visibility, followers, linking, team memberships and visibility, reassignment, escalation changes, backdating, `ticket_event`, search, portal new request form, canned responses, preview step, Service Requests and approvers, sign-in follow-ups (unlock button, clear Entra sign-in state, logout as POST) |
| 2 | Worker, in-app notifications, @mentions, notification settings, Teams channel webhook for new tickets, out-of-office detection |
| 3 | Graph mail: intake, threaded replies, `uniqueBody`, delta catch-up, subscription renewal and lifecycle handling, auto-reply filter, attachments in Blob Storage, `public` app mode for the webhook |
| 4 | Resolution, reopen and reclose flow, ignore list and review, chase emails, auto-close, feedback, bulk resolve |
| 5 | MSP handoff, hold, 18:00 job, MSP mail handling and forwarding |
| 6 | Break-glass accounts, TOTP, outage mode, Entra app roles, match users on Entra object id |
| 7 | Reporting and CSV export, subject access export, anonymisation |
| 8 | Teams bot: direct messages to agents and ticket intake from Teams |
| 9 | Google Stitch redesign once the UI is feature-complete, to WCAG 2.2 AA |

The Azure infrastructure (`infra/`, Bicep) is scaffolded and not deployed.
Staging is deployed when the app is ready for testing, then initial roll-out
on the internal domain, then production before go-live. The code changes it
needs are listed under "Hosting on Azure"; infrastructure work runs
independently of the code phases.

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

Shared mailbox address, MSP address, database restore window (7 to 35
days), upload size limit, SLA targets per priority, triage team, auto-close
days, chase X and Y, automatic email cap per ticket, office and VPN IP
ranges, internal domain name, alert recipients, Azure subscription.

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
  none has arrived within the expected interval.
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
- Worker entrypoint (`python -m app.worker`), phase 2.
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
access" page.

**Per-inbox routing (later addition, agreed 12 Oct 2026).** Each team that is
onboarded can have its own inbox (a shared mailbox, and later a Teams channel).
Mail or a Teams message sent to team B's inbox creates a ticket that arrives
already in team B and is visible only to team B (plus super admins), not to
every agent. Only the shared triage inbox feeds the untriaged queue that
everyone can see. This is how restricted work (HR, security) and other teams
are brought onto the system without exposing their tickets.

Not every team needs its own inbox. A main team can have one inbox that its
sub-teams share, so sub-teams are onboarded without new mailboxes (this needs a
parent team on `team`, and a decision on whether a parent sees its sub-teams'
tickets; **TBC**).

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
own reference prefix (`INC-`, `SR-`, `MI-`, `PRB-`) and a shareable URL.
Projects are a separate entity tickets can link to, with Azure DevOps
integration later. Change control is later.

**Statuses.** New (untriaged), Open, In progress, Awaiting user, With MSP,
Resolved, Closed. Reopening is a recorded event, not a status.

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
sub-category). That puts the ticket in a review queue visible to all super
admins, who either create a new category and move the ticket into it, or move it
into an existing one. The agent who picked "Other" keeps read access while it
waits. This replaces the earlier idea of reviewing only after resolution.

**Priority and SLA.** Impact (number of users affected) × urgency gives
P1–P4. SLA targets (first response and resolution, **TBC** per priority)
come from priority. Every ticket runs on a 24/7 clock that pauses while
Awaiting user. Times display in each viewer's own timezone.

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
portal.

**Assignment.** Any agent who can work a ticket can assign or re-assign it.
"Assign to me" is offered only on an unassigned ticket; once it has an assignee
the control becomes "Re-assign", with a list of the ticket's team members (plus
anyone while the ticket has no team). The assignee is notified. Moving a ticket
to a different team, with an agent in it, is an escalation (below). Taking a
ticket from its assignee needs the explicit re-assign, never the claim button.

**Escalation.** The agent gives a reason and can move the ticket to another
team. The escalating agent becomes an internal follower: CC'd on every
outgoing message, including emails to the MSP, keeps read access to that
ticket, and is notified about notes only when @mentioned.

**Linking.** Tickets link to a parent (Major Incident or Problem). Resolving
the parent offers to resolve every linked ticket with the same notes.

**Splitting.** One request that asks for several things can be split into
sub-tickets, so each part is tracked, assigned and resolved on its own (better
accountability). A sub-ticket has its own reference, type, category, assignee
and status, and is linked to the original, which stays the single conversation
with the requester. The requester is not emailed separately per sub-ticket: they
hear about each part through the parent. The parent's resolution is offered once
every sub-ticket is resolved, and the sub-tickets' notes feed the resolution
email. Splitting is recorded as an event on both sides.

**Backdating.** Agents can log tickets and fixes after the fact.
`reported_at` and `resolved_at` hold the dates entered; `created_at` holds
the real entry time, which the audit log also records. Up to 30 days back,
never in the future. Backdated tickets are excluded from SLA figures but
count towards volume. Sending the user the resolution email is on by
default.

**Service Requests.** Approvers are configured per request type in the
admin portal.

**History.** A `ticket_event` table records every status change,
assignment, priority change, SLA pause, reopen and MSP handoff with time and
actor, for reporting.

**Search** across subject, messages, requester and reference using Postgres
full-text search.

**Canned responses** for common replies, managed by admins.

## Outbound email preview

Every email an agent triggers that reaches an end user (replies,
resolutions, bulk resolves, MSP forwards) shows a preview and an "Are you
sure?" step. Bulk resolves list every linked ticket and recipient.
Automatic emails use admin-managed templates, previewed when edited.

## Mail (Microsoft Graph)

- Shared mailbox address **TBC**.
- Webhook plus a delta catch-up poll every 5–10 minutes. The webhook stores
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
- Feedback: signed single-use link, 1–5 rating and comment, attributed to
  the resolving agent. Admins see all; agents see their own, comments
  included.

## MSP

- Single MSP, address **TBC**.
- "Send to MSP" button on any ticket.
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
| 1 | Ticket model: types, references, statuses, source, categories, impact × urgency, SLA targets and clock, priority queue, message visibility, followers, linking, team memberships and visibility, reassignment, escalation changes, backdating, `ticket_event`, search, portal new request form, canned responses, preview step, Service Requests and approvers, sign-in follow-ups (unlock button, clear Entra sign-in state, logout as POST) |
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

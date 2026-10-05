# Delivery plan

Agreed scope and build order for the ticketing app, as at 5 October 2026.
Values marked **TBC** are decided later and stored as admin settings or
environment config, not hard-coded.

## Platform

**Database.** PostgreSQL stays. Alembic replaces `create_all` before any
schema change, since almost every phase below alters tables.

**Background worker.** A separate `worker` service in `docker-compose.yml`
runs APScheduler. Running jobs inside the web process would execute them
once per uvicorn worker. Jobs: Graph subscription renewal, mail catch-up,
SLA timers, MSP handoff, chase emails, auto-close, delayed Teams
notifications, presence refresh, backups.

**Attachments.** Private, encrypted S3 bucket. Files are served through
short-lived presigned URLs after a ticket visibility check. One upload size
limit (**TBC**) for portal uploads and email attachments, including inline
images. GuardDuty Malware Protection for S3 is optional, to switch on once
costs are known.

**Backups.** `pg_dump` to S3 every **X hours (TBC)**, which sets the maximum
data loss. Bucket versioning on, dumps expire after 30 days, monthly restore
test into a scratch database. A shorter recovery point later needs WAL
archiving or RDS point-in-time recovery.

**Data retention.** Tickets, messages, attachments and the audit log are kept
indefinitely. Raise with the data protection lead: UK GDPR's storage
limitation principle needs a written justification. Regardless of outcome,
build a subject access export (everything held on one person) and requester
anonymisation (keep the ticket, remove name, email and attachments).
Attachments older than a year move to a cheaper S3 storage class.

**Health and monitoring.**

- `/health` checks the app and database. The ALB target health check polls
  it, and a CloudWatch alarm on unhealthy hosts alerts through SNS.
- Dead man's switch: the worker and each scheduled job publish a CloudWatch
  custom metric on success, with alarms that fire on missing data.
- `/health` also warns when: Graph subscription expiry is under 24 hours,
  the last catch-up poll is over 15 minutes old, the last backup is older
  than X hours plus a margin, or EC2 disk free is under 20%.

**Environments.** The current environment is staging until go-live, then a
separate production environment is built. Staging has its own Entra app
registrations and a test mailbox, and never touches the real shared mailbox.
The deploy workflow targets an environment from the start: staging from
`main`, production from a release tag.

## Security and request flooding

Fixes, first:

- Dev login and `/webhooks/dev/simulate-inbound` only exist when an explicit
  `ENV=dev` is set, instead of whenever SSO is unconfigured.
- The app refuses to start outside dev with the default session secret or
  default Graph `clientState`.
- Remove the published Postgres port from `docker-compose.yml`.
- Session cookie set to HTTPS only.

AWS:

- ALB in front of EC2 with an ACM certificate. The EC2 security group only
  accepts the ALB; ports 8000 and 5432 are never reachable from the internet.
- WAF on the ALB: per-IP rate rule across the site (about 300 requests per
  5 minutes), a tighter rule on `/auth/*` and tokenised feedback and reopen
  links, and the AWS managed Core, Known bad inputs and IP reputation groups.
- Access restricted to office and VPN ranges, with the Graph webhook path
  open only to Microsoft's published Graph ranges.

App:

- Account lockout with increasing delay after about 5 failed logins.
- Every list paginated, message body length capped.
- Tokenised links (feedback, reopen) single-use and expiring.
- `uvicorn --workers 4` behind the ALB.

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

## Tickets

**Types.** Incident, Service Request, Major Incident, Problem. Each has its
own reference prefix (`INC-`, `SR-`, `MI-`, `PRB-`) and a shareable URL.
Projects are a separate entity tickets can link to, with Azure DevOps
integration later. Change control is later.

**Statuses.** New (untriaged), Open, In progress, Awaiting user, With MSP,
Resolved, Closed. Reopening is a recorded event, not a status.

**Source.** Email, portal, Teams or agent. Drives the default channel for
notifying the requester.

**Triage.** Manual for now, automated later. New tickets arrive unassigned
in a triage queue owned by a default team (**TBC**), which is notified. The
agent sets type, category, impact and urgency. Category decides the team.
Agents can flag "no good category"; on resolution the ticket goes to a
super admin category review queue.

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
agent-only notes never reach either.

**Followers.** Requesters and agents can add followers by email. Followers
are CC'd on public messages and see the ticket under "Following" in the
portal.

**Assignment.** Any agent can reassign by choosing a team, then an agent in
it. The ticket moves to that team and the new assignee is notified.

**Escalation.** The agent gives a reason and can move the ticket to another
team. The escalating agent becomes an internal follower: CC'd on every
outgoing message, including emails to the MSP, keeps read access to that
ticket, and is notified about notes only when @mentioned.

**Linking.** Tickets link to a parent (Major Incident or Problem). Resolving
the parent offers to resolve every linked ticket with the same notes.

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
email templates, canned responses, outage mode, the audit log (paginated,
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
| 1 | Ticket model: types, references, statuses, source, categories, impact × urgency, SLA targets and clock, priority queue, message visibility, followers, linking, team memberships and visibility, reassignment, escalation changes, backdating, `ticket_event`, search, portal new request form, canned responses, preview step, Service Requests and approvers |
| 2 | Worker, in-app notifications, @mentions, notification settings, Teams channel webhook for new tickets, out-of-office detection |
| 3 | Graph mail: intake, threaded replies, `uniqueBody`, delta catch-up, subscription renewal and lifecycle handling, auto-reply filter, attachments in S3 |
| 4 | Resolution, reopen and reclose flow, ignore list and review, chase emails, auto-close, feedback, bulk resolve |
| 5 | MSP handoff, hold, 18:00 job, MSP mail handling and forwarding |
| 6 | Break-glass accounts, TOTP, outage mode, Entra app roles |
| 7 | Reporting and CSV export, subject access export, anonymisation |
| 8 | Teams bot: direct messages to agents and ticket intake from Teams |
| 9 | Google Stitch redesign once the UI is feature-complete, to WCAG 2.2 AA |

AWS infrastructure (ALB, WAF, IP restriction) and backups land before
go-live, independent of the code phases.

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

## TBC values

Shared mailbox address, MSP address, backup interval, upload size limit,
SLA targets per priority, triage team, auto-close days, chase X and Y,
automatic email cap per ticket.

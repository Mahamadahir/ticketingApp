# ticketingApp

Internal IT support ticketing tool. Requesters email a shared mailbox, each
new message becomes a ticket, and engineers reply from within the app — the
reply goes back out as a normal email in the requester's inbox.

## Architecture

```
Requester ──email──▶ Exchange Online shared mailbox (behind Mimecast)
                              │
                     Microsoft Graph change notification (webhook)
                              │
                              ▼
                    FastAPI app ──▶ PostgreSQL (tickets, users, teams, audit log)
                              │
                     Agent console / end-user portal / admin console (HTMX UI)
                              │
                     Graph sendMail, as the shared mailbox
                              │
                              ▼
                    Requester's inbox (reply in the same thread)
```

Mimecast sits in front of Exchange Online as a mail gateway. It filters
mail before it reaches the mailbox, but once a message is in the mailbox,
Graph API is the only integration point this app needs — no separate
Mimecast integration.

## Stack

- **FastAPI** (Python) — async HTTP, suits webhook-driven mail ingestion
- **PostgreSQL** — tickets, users, teams, audit log, safe for concurrent agents
- **Jinja2 + HTMX** — server-rendered UI, no separate frontend build/deploy
- **Docker Compose** — app + Postgres for local development
- **Azure Container Apps + Bicep** — the hosting target (see `infra/` and `docs/plan.md`); not deployed yet

## Roles and portals

| Role | Portal | Can see |
|---|---|---|
| `super_admin` | `/tickets`, `/admin` | Every ticket, across all teams; manages users, teams, and the audit log |
| `agent` | `/tickets` | Tickets of teams they are a member of (work) or viewer of (read-only), every untriaged ticket, tickets assigned to them, and tickets they escalated (read-only); can self-assign, email the user, add notes, change status, escalate on the ones they can work |
| `end_user` | `/portal` | Only tickets they raised (matched on requester email); can view status and add comments |

A ticket has exactly one assigned agent at a time. Reassignment is a
separate action (`/tickets/{id}/assign`), not a side effect of emailing the user.

**Triage.** A new ticket arrives untriaged in the default triage team (a setting
under Admin > Settings). Any agent can triage it: choose the type and a category,
and the category decides the team. Assigning is optional and separate: choose an
agent in the category's team, or leave the assignment as it is and the team picks
it up. Saving the triage panel moves the ticket from New to Open; nothing else
leaves New, and the status buttons do not offer it. Whoever triages a ticket keeps
read access to it. Every ticket has one number from a counter shared by all
types (1042); its reference is the number with the current type's prefix (INC-1042),
for display. If triage changes the type the prefix follows (SR-1042) but the number
does not, and lookups use the number alone, so `/t/1042`, `/t/INC-1042` and
`/t/sr-1042` all open the same ticket and old links and email subjects keep working. The agent console and the portal are addressed by the number too (/tickets/1042, /portal/1042); the internal id never appears in a URL or page.

**Priority and SLA.** Triage also records impact (how many people) and urgency
(how bad for them), which give a priority P1 to P4 (`app/sla.py`: high/high is P1,
low/low is P4). Any agent who works the ticket can change them later from the ticket
page; the change is an event and an audit entry. Each priority has two targets, first
response and resolution, set under Admin > Settings in minutes. **The values shipped
are placeholders until the real targets are agreed**, and a blank target means not
tracked. The clock is 24/7, starts when the ticket is created, and stops while the
ticket is Awaiting user (emailing the user pauses it, their reply restarts it and
pushes the deadlines out by the time spent waiting). The first email an agent sends
meets the first-response target; internal notes do not. The ticket list is the
priority queue: untriaged tickets first, then by time to breach, then priority, with
finished tickets last. The countdown turns amber when the "at risk" share of the
target (a setting, 25% to start) or less is left and red once overdue. Tickets
triaged before this existed have no priority and no SLA. Requesters never see
either.

**Linking.** A ticket can be linked under a Major Incident or Problem (the parent) from its page, by typing the parent's ticket number (1042, or INC-1042; only the number matters). It is one level deep: a parent is never itself linked, and a ticket with linked tickets cannot go under another. The agent must work the ticket and be able to see the parent. Both tickets get an event and there is an audit entry; unlinking works the same way. The parent lists its linked tickets, but each person sees only the ones they could open anyway (the rest are counted, not named), and a linked ticket does not name a parent its viewer cannot open (its timeline does still show the parent's reference in the link event, never its subject or contents). A closed ticket, a resolved ticket and an untriaged ticket cannot be linked, nor can anything go under a closed parent; two links made at the same moment cannot build a chain. A parent lists at most 50 linked tickets. Offering to resolve every linked ticket with the parent comes with the resolve action in phase 4.

**Backdating.** For a ticket logged after the fact, a worker can open Backdate on the ticket page and enter when it was reported and, on a resolved or closed ticket, when it was fixed. Up to 30 days back, never in the future, never a fix before the report, times in UTC until viewer timezones arrive. `created_at` keeps the real entry time; each change is an event and an audit entry. Once backdated a ticket stays flagged (`is_backdated`): it counts towards volume but has no SLA deadlines and is left out of SLA figures, since its clock would measure when it was logged, not how fast it was handled. The resolution email that goes with a backdated fix comes with the resolve action in phase 4.

**Assign to me** works only on a ticket nobody has; taking a ticket from its assignee always needs the explicit Re-assign.

**Categories.** Managed by super admins under Admin > Categories: a main category
(owned by a team) with optional sub-categories (one level, using the parent's
team). A super admin can also create one while triaging. "Other" is for tickets
nothing fits: they are worked and resolved normally, and afterwards appear under
Admin > Category review, where a super admin gives them a proper category.

**Re-assign.** For a triaged ticket, choose a team and then an agent in it, or
leave it unassigned in that team. No reason is needed. It is not an escalation,
which is a separate action that flags the ticket and needs a reason.

**Followers.** An agent who works a ticket, or the requester on their own
ticket, can copy other people on it by email (up to 20). Followers are copied on
every email to the requester, and each email records exactly who was copied.
When an agent writes an email, every follower is ticked under *Copy this email
to*; unticking someone leaves them off that one email without removing them as
a follower (removing is separate, and is logged in the timeline and the audit
log). A follower with a portal account sees the ticket under *Following* (public
messages only) and can reply there; their reply is added to the ticket and shown
to agents as *From follower*. They cannot manage the follower list. Escalating a
ticket makes the escalating agent an *internal follower*: always copied;
they reply from the agent console, not the portal. Following gives no access to the
ticket (`app/followers.py`; access is `app/visibility.py` and follows the team), and
neither does the ticket's escalated-by
field. Nothing is emailed until Graph is wired up in phase 3.

**Status follows the conversation.** Emailing the user moves an Open or In progress
ticket to *Awaiting user* automatically, with no option for the agent to skip it. A
reply from the requester or a follower while it is Awaiting user moves it back to
*In progress*. Both are recorded in the timeline and audit log. (The SLA clock that
pauses in Awaiting user is built in phase 1D-ii.)

**Email user and notes.** An agent has two ways to write on a ticket. **Email
user** sends the requester a message and is available once the ticket is
triaged (for now: has left New). **Add note** records an internal note any time,
including on an untriaged ticket the agent has not claimed, and never triages
it. The ticket page shows one timeline of emails, notes and events in time order.
Notes are stored as `agents_only` and the portal and outbound mail read
messages only through `requester_visible_messages` in `app/messages.py`, which
returns public messages, so a note cannot reach the requester.

**Escalation** is manual: an agent opens the "Escalate" panel on a ticket,
gives a reason, and optionally reassigns it to a different team. This sets
`is_escalated`, records who escalated it and why, and writes an audit log
entry. There's no automatic SLA-based escalation.

**Audit log** (`/admin/audit-log`, super admin only) records every status
change, assignment, escalation, comment, and user/team/role change — actor,
action, entity, and a short detail string, newest first.

**Activity log** (`/admin/activity-log`, super admin only) is separate from the
audit log. It records security and usage events: sign-ins, sign-outs, failed
sign-ins, account lockouts, SSO failures, access-denied responses, and every
state-changing request (POST/PUT/PATCH/DELETE), each with who, when, the IP
address and the user agent. Paths are stored as route templates
(`/tickets/{ticket_id}/email`), so ids and tokens never land in the log. Request
bodies, passwords, tokens, one-time codes and query strings are never recorded.
Failed sign-ins are stored only when the address matches a real account; guesses at unknown addresses, attempts on an already-locked account, and calls with no sign-in started go to the application log only, so outsiders cannot grow the table. A request that crashes is recorded as a 500. Filter by event type, part of an address, date range, or "problems only". A failure to write a
row is logged and swallowed, so it can never break a request. The IP shown is
only trustworthy once `FORWARDED_ALLOW_IPS` is set correctly behind the Azure
ingress (a staging check).

## Signing in

Two ways in, both resolve to a `User` row with a `role` (team access is separate, see Teams below):

1. **Microsoft SSO** (primary) — set `SSO_TENANT_ID`, `SSO_CLIENT_ID`,
   `SSO_CLIENT_SECRET` in `.env` and it's the default option on the login
   page. First-time sign-in auto-creates the user as `end_user`, unless
   their email is listed in `SUPER_ADMIN_EMAILS`.
2. **Local account** (alternative) — for anyone without a tenant identity
   (e.g. a contractor). A super admin creates these from `/admin/users`
   with an email, password, role, and optionally a first team. Always available regardless of
   whether SSO is configured.

When `ENV=dev`, the login page also links to `/auth/dev-login`, a plain
form that signs in as any email with no password, for local development
only. In any other environment the route returns 404, so it can't be left
switched on by accident.

### Bootstrapping the first super admin

Put your email in `SUPER_ADMIN_EMAILS` (comma-separated) before first
sign-in. The first time that email logs in — via SSO or dev-login — it's
created with `role=super_admin`, which is what lets you reach `/admin` to
create teams and promote other users.

### Teams

Teams are created from `/admin/teams` by a super admin; there's no fixed/seeded
list. People are put into teams from `/admin/users`, with a level per team:

- **member** sees and works the team's tickets.
- **viewer** can read them but not change anything.

A user can belong to several teams. Agents also work every *untriaged* ticket
(status New, or no team yet) so someone can triage it. Otherwise access comes from the ticket's team alone: being
assigned to a ticket, having triaged or escalated it, or following it grants nothing, so
when a ticket moves to another team everyone outside that team loses it.
Opening a ticket without access shows a "no access" page (403). The rules live
in `app/visibility.py`; routes never decide access themselves. Super admins can
unlock a locked local account from the same users page.

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

Then open http://localhost:8000. You'll land on `/auth/login`.

`.env.example` sets `ENV=dev`. Staging and production must set
`ENV=staging` or `ENV=production`: outside dev the app refuses to start
while `SESSION_SECRET_KEY` (or, once Graph is configured,
`GRAPH_WEBHOOK_CLIENT_STATE`) is still a default value, and the session
cookie is HTTPS-only.

The background worker (`app/worker.py`, the scheduled jobs in `app/jobs`) is a separate
service: `docker compose up worker` (or `python -m app.worker` with the database running).
Run exactly one: a second copy refuses to start. It logs `heartbeat job=heartbeat status=ok`
every `WORKER_HEARTBEAT_SECONDS` (default 60).

Postgres is published on 127.0.0.1:5432 only (so the tests can reach it). To get a shell on it:

```bash
docker compose exec db psql -U ticketing
```

Graph credentials are optional to start the app — email ingestion and
sending are stubbed until an app registration exists (see below). Until
then, use the dev helper (available only when `ENV=dev`) to create tickets
manually:

```bash
curl -X POST http://localhost:8000/webhooks/dev/simulate-inbound \
  -d "requester_email=jane@example.com" \
  -d "subject=Can't connect to VPN" \
  -d "body=I get a timeout every time I try to connect."
```

## Database migrations

Schema changes go through Alembic. The container runs `alembic upgrade head`
before starting the app, so a deploy applies pending migrations on its own.

After changing a model, generate and review a migration:

```bash
docker compose run --rm app alembic revision --autogenerate -m "describe the change"
```

CI runs `alembic check` and fails if a model changed without a migration.

A database created before migrations existed (by the old `create_all` at
startup) already has the baseline tables. Mark it as migrated once, before
the first deploy that includes Alembic:

```bash
docker compose run --rm app alembic stamp a575769b8ea7
```

## Running tests

```bash
pip install --require-hashes -r requirements-dev.lock
pytest -n auto
```

`requirements-dev.lock` pins every package with hashes and works on Windows and Linux;
`requirements.txt` and `requirements-dev.txt` are the human-edited inputs (how to
regenerate the lock: `docs/build-plan.md`, "Dependency lock").

Needs Python 3.14 and the compose database running (`docker compose up -d db`).
The tests run against Postgres 16, never SQLite: they create and wipe a separate
`ticketing_test` database (see `tests/conftest.py`), and refuse any database whose
name does not end in `_test`. The tables are built once per run and emptied before each
test. `-n auto` runs one worker per core, each on its own database
(`ticketing_gw0_test`, ...); plain `pytest` runs them one at a time. Set `TEST_DATABASE_URL` to point elsewhere. Covers
role-scoped ticket visibility, ticket numbering, assignment, escalation, audit and
ticket history, and both login paths.

## CI/CD

- `.github/workflows/ci.yml` runs `pytest -n auto` and the migration drift check on
  every pull request to `main` and on `v*` tags (not on the push to `main` after a
  merge, which the pull request's run already tested). A newer push to a pull request
  cancels its run still in progress, and a run stops after 30 minutes. A pull request
  that only touches markdown files or `docs/` does not run it. Do not make this check required in branch
  protection, or a docs-only pull request would wait for a run that never starts.
- There is no deploy workflow yet. The old EC2 one was removed when hosting
  moved to Azure (see `docs/plan.md`, "Hosting on Azure", and `infra/README.md`),
  and nothing is deployed. The new one will build and push the image, run the
  migrate job and update the Container Apps, authenticating with OIDC.

The container runs uvicorn with 4 workers; set `UVICORN_WORKERS` in `.env`
to change it.

## Wiring up Microsoft Graph

1. In Entra ID, register an app (client-credentials / app-only flow).
2. Grant it application permissions `Mail.Read` and `Mail.Send`, admin-consented,
   and scope access to only the shared mailbox via an application access policy
   (`New-ApplicationAccessPolicy` in Exchange Online PowerShell) — don't grant
   tenant-wide mailbox access for this app.
3. Fill in `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, and
   `GRAPH_SHARED_MAILBOX` in `.env`.
4. Implement the TODOs in [app/graph_client.py](app/graph_client.py):
   `send_mail`, `create_subscription`, `get_message`.
5. Call `create_subscription` once (e.g. from a one-off script or startup
   task) pointing at `https://<your-host>/webhooks/graph/notifications`.
   Mail subscriptions last up to 10,080 minutes (under 7 days) and need a
   renewal job.
6. Fill in the notification-handling TODO in
   [app/routers/webhooks.py](app/routers/webhooks.py) to fetch the new
   message via Graph and create or update a ticket.

## Wiring up Microsoft SSO (sign-in)

This is a separate Entra ID app registration from the Graph mail one above
— this one is delegated (user sign-in), not app-only.

1. Register an app with platform type "Web", redirect URI matching
   `SSO_REDIRECT_URI` (e.g. `https://your-host/auth/callback`).
2. Add delegated permission `User.Read` (default Microsoft Graph scope is
   fine, no admin consent needed for this one).
3. Create a client secret, fill in `SSO_TENANT_ID`, `SSO_CLIENT_ID`,
   `SSO_CLIENT_SECRET`, `SSO_REDIRECT_URI` in `.env`.
4. Restart the app — the login page switches from dev-login to the
   "Sign in with Microsoft" button automatically.

## Project layout

```
app/
  main.py            FastAPI app, session middleware, router wiring, startup
  config.py          Settings from environment / .env
  database.py        SQLModel engine/session
  models.py          Ticket, Message, User, Team, AuditLog
  auth.py            SSO (MSAL) + local password auth, session, role checks
  audit.py           Audit log helper
  backdating.py      Enter earlier reported/resolved dates on agent-raised tickets only; flags the ticket out of SLA figures
  linking.py         Link tickets under a Major Incident or Problem
  splitting.py       Split a ticket into sub-tickets, each triaged from the original
  sla.py             Priority from impact x urgency, SLA deadlines and clock, queue order
  activity.py        Activity log: sign-in events and request-logging middleware
  graph_client.py    Microsoft Graph mail integration (stubbed)
  routers/
    health.py        Unauthenticated /health check (app + database) for monitors
    auth.py          Login page, SSO callback, local login, dev login, logout
    tickets.py       Agent console: list/view/email/note/assign/escalate/status
    portal.py        End-user portal: list/view own tickets, comment
    admin.py         Super admin: users, teams, audit log
    webhooks.py      Graph change-notification endpoint + dev helper
  templates/         Jinja2 + HTMX views
  static/            CSS
migrations/          Alembic environment and versioned schema migrations
tests/               pytest suite (Postgres)
infra/               Bicep for the Azure environments (not deployed yet)
docs/                Delivery plan
.github/workflows/   CI (tests); no deploy workflow yet
```

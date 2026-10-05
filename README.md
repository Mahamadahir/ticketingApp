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
- **Docker Compose** — app + Postgres, runs on one internal box or EC2

## Roles and portals

| Role | Portal | Can see |
|---|---|---|
| `super_admin` | `/tickets`, `/admin` | Every ticket, across all teams; manages users, teams, and the audit log |
| `agent` | `/tickets` | Tickets belonging to their own team; can self-assign, reply, change status, escalate |
| `end_user` | `/portal` | Only tickets they raised (matched on requester email); can view status and add comments |

A ticket has exactly one assigned agent at a time. Reassignment is a
separate action (`/tickets/{id}/assign`), not a side effect of replying.

**Escalation** is manual: an agent opens the "Escalate" panel on a ticket,
gives a reason, and optionally reassigns it to a different team. This sets
`is_escalated`, records who escalated it and why, and writes an audit log
entry. There's no automatic SLA-based escalation.

**Audit log** (`/admin/audit-log`, super admin only) records every status
change, assignment, escalation, comment, and user/team/role change — actor,
action, entity, and a short detail string, newest first.

## Signing in

Two ways in, both resolve to a `User` row with a `role` and optional `team`:

1. **Microsoft SSO** (primary) — set `SSO_TENANT_ID`, `SSO_CLIENT_ID`,
   `SSO_CLIENT_SECRET` in `.env` and it's the default option on the login
   page. First-time sign-in auto-creates the user as `end_user`, unless
   their email is listed in `SUPER_ADMIN_EMAILS`.
2. **Local account** (alternative) — for anyone without a tenant identity
   (e.g. a contractor). A super admin creates these from `/admin/users`
   with an email, password, role, and team. Always available regardless of
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

Teams are created and managed from `/admin/teams` by a super admin — there's
no fixed/seeded list. Create at least one team before assigning agents to
it; a ticket with no team shows as "Unassigned" in the agent console.

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

Postgres isn't published on the host. To get a shell on it:

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

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against an in-memory SQLite database (see `tests/conftest.py`),
not Postgres — fast and dependency-free, at the cost of not exercising
Postgres-specific behaviour. Covers role-scoped ticket visibility,
assignment, escalation, audit log writes, and both login paths.

## CI/CD

- `.github/workflows/ci.yml` runs `pytest` on every push and pull request.
- `.github/workflows/deploy.yml` runs after CI passes on `main`, SSHes into
  an EC2 box, and runs `docker compose up -d --build`.

The deploy workflow needs these repo secrets (Settings → Secrets and
variables → Actions), none of which this repo can set for you:

- `EC2_HOST` — public IP or hostname of the instance
- `EC2_USER` — SSH user (e.g. `ubuntu`)
- `EC2_SSH_KEY` — private key with access to that user, PEM contents
- `EC2_APP_DIR` — path on the instance where this repo is checked out (e.g. `/home/ubuntu/ticketingApp`)

One-time setup on the EC2 box itself: install Docker + Docker Compose,
`git clone` this repo into `EC2_APP_DIR`, and create a real `.env` there
(copied from `.env.example` with production values) — the deploy workflow
only pulls and rebuilds, it doesn't create the environment file.

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
  graph_client.py    Microsoft Graph mail integration (stubbed)
  routers/
    auth.py          Login page, SSO callback, local login, dev login, logout
    tickets.py       Agent console: list/view/reply/assign/escalate/status
    portal.py        End-user portal: list/view own tickets, comment
    admin.py         Super admin: users, teams, audit log
    webhooks.py      Graph change-notification endpoint + dev helper
  templates/         Jinja2 + HTMX views
  static/            CSS
tests/               pytest suite (SQLite-backed)
.github/workflows/   CI (tests) and CD (deploy to EC2)
```

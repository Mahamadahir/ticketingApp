# Ticketing app: notes for Claude

Internal IT support ticketing tool. FastAPI + SQLModel + PostgreSQL, server
rendered with Jinja2 and htmx. Read `docs/plan.md` for the agreed scope, build
order and hosting design before starting a phase, and `README.md` for how to
run it.

## State of play

- Phase 0 is done and merged (security fixes, Alembic, `/health`, lockout,
  pagination, audit log filters). Phase 1 is agreed as eight PRs (1A to 1H):
  1A ticket core (types, per-type references, statuses, source, ticket events),
  1B teams and visibility, 1C messages (Email user vs internal notes, email
  hidden until triaged, one timeline of messages and events) and followers,
  1D triage as its own action separate from assignment (categories with an
  "Other" review queue, impact/urgency, SLA; TBC values become admin
  settings), 1E assignment UX (assign to me only when unassigned, anyone can
  re-assign), escalation, linking, splitting into sub-tickets, backdating,
  1F search and canned responses, 1G portal form and email preview,
  1H Service Requests and approvers (may slip to after Phase 3). Ticket numbers
  come from a `before_insert` hook in `app/models.py`; use
  `app/ticket_service.py` for creating tickets and changing status. Who can see
  or work a ticket is decided only in `app/visibility.py`, never in a route;
  what the requester may see, and whether a ticket can be emailed, is decided
  only in `app/messages.py` (portal and outbound mail use
  `requester_visible_messages`, never `ticket.messages`). Followers live in
  `app/followers.py`; an internal follower row is what keeps an escalator's read
  access, and `cc_list` is what the sender will copy on every email.
- **Nothing is deployed anywhere.** No Azure resources exist, no database has
  had migrations applied outside this laptop. Everything is developed and
  tested locally with `docker compose` and `pytest`.
- Hosting is Azure (Container Apps, PostgreSQL Flexible Server, Blob Storage,
  Key Vault), defined in Bicep under `infra/`. It compiles and lints but has
  never been run against a subscription. The earlier AWS/EC2 plan is dead and
  its deploy workflow was removed; there is no deploy workflow yet.

## Working rules

- Develop and test locally. Do not deploy, create Azure resources or run
  `az deployment` unless the user asks.
- Frontend stays server-rendered Jinja2 + htmx. No SPA, no Static Web Apps.
  The web app is mainly for agents; requesters mostly use email and Teams.
- Keep the UI plain and functional: semantic HTML, minimal CSS, no styling
  polish. The whole UI gets redesigned with Google Stitch in Phase 9 once it is
  feature-complete, so visual effort now is wasted.
- Schema changes need an Alembic migration (CI runs `alembic check`).
- Run `pytest` before saying work is done. Tests run against
  Postgres 16 (compose `db` locally, a service container in CI), never SQLite.
  Start it with `docker compose up -d db`; the tests create and wipe a separate
  `ticketing_test` database. Python is 3.14.
- No secrets in the repo, Bicep parameter files or app definitions. Secrets
  come from the environment at deploy time and live in Key Vault.
- Values marked TBC in the plan are settings or environment config, never
  hard-coded.
- Do not add AI attribution lines to commit messages or PR descriptions unless
  the user asks for them.
- Before committing, validate Bicep changes with
  `bicep lint infra/main.bicep` and `bicep build-params infra/staging.bicepparam`
  (set dummy values first for `POSTGRES_ADMIN_PASSWORD`, `SESSION_SECRET_KEY`,
  `INTERNAL_SESSION_SECRET_KEY`, `GRAPH_WEBHOOK_CLIENT_STATE`, `ALERT_EMAIL` and `WEB_ALLOWED_CIDRS`). There is
  no Bicep CLI on the PATH on this laptop; the standalone `bicep-win-x64.exe` from
  the Azure/bicep GitHub releases works.

## Flag Azure-only checks when you reach them

Some things cannot be proved locally. When a task touches one of these, say so
explicitly, say what was and was not tested, and add it to the user's
staging checklist. The full list is under "Azure-only verification" in
`docs/plan.md`. The ones most likely to come up:

- Entra sign-in against a real redirect URI (the dev login is not the same code path).
- Graph webhooks: need a public HTTPS URL, so they only work on staging
  (or via a tunnel). Test the handler logic locally with simulated payloads.
- Managed identity access to Blob Storage and Key Vault, and user-delegation
  SAS links (the Azurite emulator may not support them).
- Proxy and forwarded headers behind Container Apps ingress, HTTPS-only cookies.
- IP restrictions, the migrate job, revision rollout, scale rules.
- Postgres SSL and private networking, point-in-time restore.
- Alerts, heartbeat monitoring, Application Insights data.
- Teams bot and Teams Workflows webhooks.
- Custom domain and certificates at roll-out.

## Code the Azure plan still needs

Listed with phases in `docs/plan.md` ("Code changes the Azure plan needs"):
`APP_ROLE` router split (`web` / `public` / `worker`), worker entrypoint,
storage interface with local and Blob implementations, Application Insights,
rate limiting on public routes, and a new deploy workflow. The `RUN_MIGRATIONS`
switch is done; proxy headers need only `FORWARDED_ALLOW_IPS`, set in the Bicep
to the Container Apps subnet (never `*`, which lets clients forge their IP).

## Layout

`app/` application code, `migrations/` Alembic, `tests/` pytest, `docs/plan.md`
the plan, `infra/` Bicep (see `infra/README.md`), `.github/workflows/` CI.

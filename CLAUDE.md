# Ticketing app: notes for Claude

Internal IT support ticketing tool. FastAPI + SQLModel + PostgreSQL, server
rendered with Jinja2 and htmx. Read `docs/plan.md` for the agreed scope, build
order and hosting design before starting a phase, and `README.md` for how to
run it.

## State of play

- Phase 0 is done and merged (security fixes, Alembic, `/health`, lockout,
  pagination, audit log filters). Phase 1 is next; agree its approach with the
  user before coding, because it touches far more than a few files.
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
- Schema changes need an Alembic migration (CI runs `alembic check`).
- Run `pytest` before saying work is done. Locally tests use SQLite and CI
  uses Postgres 16 (set `TEST_DATABASE_URL` to run against Postgres locally), so
  anything Postgres-specific (full-text search, constraints, locking) is not
  proved by a local green run; say so when it matters. Python is 3.14.
- No secrets in the repo, Bicep parameter files or app definitions. Secrets
  come from the environment at deploy time and live in Key Vault.
- Values marked TBC in the plan are settings or environment config, never
  hard-coded.
- Do not add AI attribution lines to commit messages or PR descriptions unless
  the user asks for them.
- Before committing, validate Bicep changes with
  `bicep lint infra/main.bicep` and `bicep build-params infra/staging.bicepparam`
  (set the three required secret variables to dummy values first).

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
`APP_ROLE` router split (`web` / `public` / `worker`), `RUN_MIGRATIONS` switch
in the entrypoint, uvicorn proxy headers, worker entrypoint, storage interface
with local and Blob implementations, Application Insights, rate limiting on
public routes, and a new deploy workflow.

## Layout

`app/` application code, `migrations/` Alembic, `tests/` pytest, `docs/plan.md`
the plan, `infra/` Bicep (see `infra/README.md`), `.github/workflows/` CI.

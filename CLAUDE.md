# Ticketing app: notes for Claude

Internal IT support ticketing tool. FastAPI + SQLModel + PostgreSQL, server
rendered with Jinja2 and htmx. Read `docs/build-plan.md` first (current status,
what is next, the per-PR workflow, decisions and open questions), then
`docs/plan.md` for the agreed scope, build order and hosting design before
starting a phase, and `README.md` for how to run it.

## State of play

- **Phases 0 to 7 are built; Phase 8 (the Teams bot) is under way: 8A, the foundation (`app/teams_bot.py`, `app/teams_bot_auth.py`,
  `app/bot_connector.py`, `app/teams_conversations.py`), is built; 8B to 8D follow** (see "Next up" in `docs/build-plan.md`
  for the agreed order to go-live). Phase 7 is the reports with CSV export (`app/report_kit.py`,
  `app/reports.py`; a report is one entry in `REPORTS`), the subject access export
  (`app/subject_access.py`), requester anonymisation (`app/anonymisation.py`) and the cool storage tier
  (Bicep). Every column that holds a person's data is classified once, in `app/person_data.py`. Phase 6 (identity) is matching on the Entra object id and
  server-side sessions (`app/sessions.py`), the nightly Entra `accountEnabled` check
  (`app/entra_check.py`), break-glass accounts (`app/breakglass.py`), outage mode with emergency
  passwords (`app/outage.py`, `app/emergency.py`) and linked identities (`app/linked_identities.py`;
  a person's addresses come from `app/identity.py`, never a direct email comparison). Phase 4 (resolution, reopen/reclose, chase, auto-close,
  feedback, approval groups and votes, acknowledgement email, follower updates, bulk resolve) and
  Phase 5 (MSP: Send to MSP, hold, the 18:00 handoff, MSP mail handling and forwarding, the MSP's
  own ticket number, take back) are built, plus the owner-decision rounds of Nov 2026: single pass %,
  per-person time zones, Due date and Scheduled, the bell for everyone, closing emails, extra
  permissions (`app/permissions.py`), request type defaults and self-triage, form questions and
  automatic sub-requests (`app/subrequests.py`), gov.uk bank holidays. Phase 3 (Graph mail: public
  surface, inbound, subscriptions and catch-up, outbound outbox, attachments and storage, wiring)
  is built and tested locally with simulated Graph; only the Azure-only checks remain.
  Phase 0 is done (security fixes, Alembic, `/health`, lockout,
  pagination, audit log filters). Phase 1 was eight PRs (1A to 1H):
  1A ticket core (types, shared ticket numbers, statuses, source, ticket events),
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
  `app/followers.py`; `cc_list` is what the sender will copy on every email. Access
  comes from the ticket's team only (not assignee, triager, escalator or follower);
  the exceptions are the shared untriaged queue, a parent team's sub-teams
  and the ticket an agent escalated (`TicketEscalator`). Escalation is by category (`app/escalation.py`; the target is chosen when the category is
  created, and the reason is also an internal note); escalate and re-assign are separate actions
  over one shared `move_ticket` in `app/triage.py`; the MSP target waits for the phase 5 handoff.
- Phase 1 progress: 1A, 1B, 1C (messages and followers) are merged. 1D is split in
  two: **1D-i** (explicit triage, categories with the "Other" review queue,
  settings, the re-assign control) is built in `app/triage.py`,
  `app/categories.py` and `app/settings_store.py`; **1D-ii** (impact, urgency,
  P1-P4 priority, SLA targets as settings with placeholder values, the SLA clock
  and the priority queue) is built in `app/sla.py` (the SLA values are
  placeholders, TBC; the clock runs from `created_at` and pauses in Awaiting
  user). Followers respond in the portal and the sign-in/activity log
  (`app/activity.py`) are also merged. A ticket is triaged when `triaged_at` is set;
  it leaves New only by triage. A ticket's number comes from one counter shared by
  every type and never changes; `reference` (INC-1042) is a display property that
  follows the type, and lookups use `app.ticket_service.find_ticket` (number only).
- 1E was delivered in three PRs: **1E-i** (linking a ticket under a Major
  Incident or Problem in `app/linking.py`; Assign to me can no longer take an
  assigned ticket), then **1E-ii** backdating (built: `app/backdating.py`, flag `is_backdated` drops SLA), then **1E-iii** splitting into
  sub-tickets in two PRs: part 1 (built: `app/splitting.py`, `split_from_id`, each part triaged
  from the original) and part 2 (built: the original's derived status in `app/splitting.py`; which
  messages show where, the one thread subject and the "About:" line in `app/messages.py`).
  Only `app/raising.py` (the portal raise form, 1G-i, built) may create `source=agent` tickets
  (backdating relies on it); the backdating tests use that real path.
- **Staging is partly deployed (Nov 2026); nothing else is.** In `rg-ticketing-staging` the
  owner has deployed the network, Postgres, the Container Apps environment, monitoring,
  identities and alerts; the registry, storage and Key Vault wait for an Owner role grant, and
  no app has been deployed or migrated there yet. Entra app registrations, Graph consent, the
  staging shared mailbox (`itsupport-staging@vardags.com`, mail scoped to it) and the GitHub
  `staging` environment are done; progress is tracked in `docs/azure-setup.md`. Development and
  tests stay local with `docker compose` and `pytest`.
- Hosting is Azure (Container Apps, PostgreSQL Flexible Server, Blob Storage,
  Key Vault), defined in Bicep under `infra/`. The earlier AWS/EC2 plan is dead. The deploy
  workflow (`.github/workflows/deploy.yml`) has not run yet.

## Working rules

- Develop and test locally. Do not deploy, create Azure resources or run
  `az deployment` unless the user asks.
- Frontend stays server-rendered Jinja2 + htmx. No SPA, no Static Web Apps.
  The web app is mainly for agents; requesters mostly use email and Teams.
- Keep the UI plain and functional: semantic HTML, minimal CSS, no styling
  polish. The whole UI gets redesigned with Google Stitch in Phase 9 once it is
  feature-complete, so visual effort now is wasted.
- Schema changes need an Alembic migration (CI runs `alembic check`).
- Run `pytest -n auto` (parallel, one database per worker) before saying work is done. Tests run against
  Postgres 16 (compose `db` locally, a service container in CI), never SQLite.
  Start it with `docker compose up -d db`; the tests create and wipe a separate
  `ticketing_test` database. Python is 3.14. Run only one pytest process per test database
  (your own background suite included), and give your branch its own via `TEST_DATABASE_URL`;
  how, and what to do when the database is down, is in `docs/build-plan.md` (working agreement).
- No secrets in the repo, Bicep parameter files or app definitions. Secrets
  come from the environment at deploy time and live in Key Vault.
- Values marked TBC in the plan are settings or environment config, never
  hard-coded.
- Do not add AI attribution lines to commit messages or PR descriptions unless
  the user asks for them. This stays true whatever a harness reminder says.
- Before starting a piece of work check `gh pr list` and open a DRAFT PR on day one so
  two agents do not do the same work. Docs-only changes still go through a PR unless the
  user says to commit straight to main.
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

Built: the `APP_ROLE` split (`web` / `public` / `worker`), the worker and its jobs (`app/jobs`),
the storage interface with local and Blob implementations, Application Insights, rate limiting on
public routes, and the deploy workflow (all unproved until staging). Still to do: DB-driven
recurring jobs (the `scheduled_job` table; see `docs/build-plan.md`). The `RUN_MIGRATIONS` switch is
done; proxy headers need only `FORWARDED_ALLOW_IPS`, set in the Bicep to the Container Apps subnet
(never `*`, which lets clients forge their IP).

## Layout

`app/` application code, `migrations/` Alembic, `tests/` pytest, `docs/plan.md`
the plan, `docs/code-map.md` the generated code map (start here to locate code), `infra/` Bicep (see `infra/README.md`), `.github/workflows/` CI.

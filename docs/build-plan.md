# Build plan (working document)

The live status, the next steps and the working agreement for whoever is building
this next, human or agent. `docs/plan.md` is the agreed scope and design; this file
says where the build is, what to do next and how to do it. Update it in the same PR
as the work, so it never drifts: move the item, add the decision, close the question.

Read in this order: `CLAUDE.md` (rules), this file, then the sections of
`docs/plan.md` your item names. `README.md` covers running the app.

## Status

Nothing is deployed. Everything is built and tested locally; Azure resources do
not exist (`infra/` compiles but has never run against a subscription).

The app is not live and has no users; open questions are design choices, not live risks.

| Item | State | Where |
|---|---|---|
| Phase 0 (security, Alembic, health, lockout, pagination) | merged | |
| 1A ticket core: types, statuses, source, ticket events | merged | `app/ticket_service.py` |
| 1B teams and visibility | merged | `app/visibility.py` |
| 1C messages and followers | merged | `app/messages.py`, `app/followers.py` |
| Followers respond in the portal; emailing moves status | merged | `app/routers/portal.py` |
| 1D-i triage, categories, settings | merged | `app/triage.py`, `app/categories.py`, `app/settings_store.py` |
| Sign-in and activity log | merged (#15) | `app/activity.py` |
| 1D-ii impact, urgency, priority, SLA clock and queue | merged (#16) | `app/sla.py` |
| 1E-i linking under a Major Incident / Problem | merged (#17) | `app/linking.py` |
| Shared ticket numbers | merged (#18) | `app/models.py`, `NumberCounter` |
| 1E-ii backdating (agent-raised tickets only) | merged (#19, #20) | `app/backdating.py` |
| 1E-iii part 1 splitting into sub-tickets; access follows the team | merged (#21) | `app/splitting.py` |
| 1E-iii part 2 derived status and single mail thread | merged (#23) | `app/splitting.py`, `app/messages.py` |
| 1F-i search | merged (#25) | `app/search.py` |
| 1F-ii canned responses | merged (#26) | `app/canned.py` |
| 1B-ii sub-teams and escalation by category | merged (#29, #32) | `app/visibility.py`, `app/escalation.py`, `app/routers/admin.py` |
| Escalation tidy-up: target chosen at category creation, reason also an internal note, escalate and re-assign share `move_ticket` | merged (#37) | `app/triage.py`, `app/escalation.py`, `app/categories.py` |
| 1G-i raise a ticket: portal form, agents raising for someone, optional triage and backdating at creation, source labels | merged (#28) | `app/raising.py` |
| 1G-ii outbound email preview step; "tell the requester a ticket was raised for them" setting | merged (#31) | `app/messages.py` (`preview_email`), `app/raising.py` |
| Team activity feed (agents see all activity in their teams) | merged (#30) | `app/team_activity.py` |
| 1H-i request catalogue: request types with approvers, chosen on the raise form | merged (#34) | `app/request_types.py` |
| 1H-ii approvals: Awaiting approval status, thresholds, approvers decide in the portal or from an emailed link | merged (#35) | `app/approvals.py`, `app/routers/approvals.py` |
| Phase 1 | complete | |
| 2A in-app notifications (the bell): assignment and @mentions | merged (#36) | `app/notifications.py` |
| 2B notification settings (channels per event type per person) | merged (#38) | `app/notification_settings.py`, `app/routers/my_settings.py` |
| 2C-i worker skeleton: `python -m app.worker`, APScheduler, heartbeat line, single-instance lock, compose service | merged (#39) | `app/worker.py`, `app/jobs/` |
| 2C-ii unread-after-N-minutes notification to Teams: which notifications want a copy and when (Teams-only rows, per-person delay, admin default), then queued in the Teams outbox and sent by the worker | merged (#42) | `app/notifications.py`, `app/teams.py`, `app/notification_settings.py` |
| 2D Teams channel webhook for new tickets: outbox, card, sender, worker job (Bicep wired) | merged (#40) | `app/teams.py` |
| 2E out-of-office detection (Graph presence): the worker's `presence` job stores each agent's flag; OOO agents are labelled in the assignee pickers and left out of team-wide notifications | merged (#44) | `app/presence.py` |
| Reply notification: the assigned agent is told when the requester or a follower replies, whatever the status | PR open | `app/notifications.py` (`notify_reply`), `app/messages.py` |
| Phase 2 | complete | |
| With supplier status (waiting on an outside supplier or vendor; set by hand, SLA clock keeps running) | merged (#53) | `app/models.py`, migration `a9d3c6e1f275` |
| 3A public surface: `APP_ROLE` split (`build_app` in `app/main.py`), Graph webhook stores each notification and returns 202, rate limiting, approval tokens kept out of the access log | built, in review | `app/main.py`, `app/routers/webhooks.py`, `app/rate_limit.py`, `app/log_redaction.py` |
| Dependency hygiene 1: dropped unused `pytest-cov` and the `uvicorn[standard]` extras; Dependabot (pip, docker, actions; weekly, minor and patch grouped); `pip-audit` on requirements changes and nightly (`.github/workflows/pip-audit.yml`) | merged (#55) | `.github/dependabot.yml`, `.github/workflows/pip-audit.yml` |
| Dependency hygiene 2: hashed lock (`requirements.lock`, `requirements-dev.lock`) installed with `--require-hashes`, base image pinned by digest, every action pinned by SHA, htmx 2.0.11 vendored into `app/static/vendor/` | built, in review | `Dockerfile`, `.github/workflows/`, `app/static/vendor/README.md`, `tests/test_static.py` |
| 3C subscriptions and delta catch-up: the worker creates, renews and recreates the mailbox subscription, acts on lifecycle notifications, and catches up missed mail with an inbox delta query; Admin > Mail intake; `/health` warnings | built, in review | `app/subscriptions.py`, migration `c3b7e1a94d20` |
| 3B inbound mail: the `mail-in` worker job turns stored notifications into tickets (source email) and replies, de-duplicated, own-mail and auto-reply filter, Cc becomes followers, files saved through the attachment rules; the dev simulate-inbound runs the same intake (`INBOUND_ENABLED`, off by default) | built, in review | `app/inbound.py`, `app/graph_client.py`, migration `e4b7c1a9d052` |
| 3D outbound mail: outbox, real send, approval and "raised for you" emails (`MAIL_SENDING` off / dev / graph; Admin > Outbound mail) | merged (#52) | `app/outbox.py`, `app/mail.py` |
| 3E-storage attachments: storage interface (local directory, Azure Blob with user-delegation SAS), `attachment` table, `/attachments/{id}`, content-type detection and safe serving | built, in review (#49) | `app/storage.py`, `app/attachments.py`, `app/routers/attachments.py` |
| 3F wiring: Bicep and deploy workflow for the public app, mailbox (`MAIL_SENDING`), the two base URLs, Application Insights started from the connection string (approval links kept out of request telemetry) | built, in review | `infra/main.bicep`, `.github/workflows/deploy.yml`, `app/telemetry.py` |
| Phases 4 to 9 | not started | `docs/plan.md`, "Build order" |

Tests: 953 passing on Postgres 16.

## Next up

**Phase 3 (Graph mail) is next:** intake, threaded replies, `uniqueBody`, delta catch-up, subscription renewal and lifecycle
handling, the auto-reply filter, attachments in Blob Storage, and the `public` app mode for the webhook (`docs/plan.md`, "Build
order"). Phase 3 also carries the approval emails with the link from `app/approval_links.py`, and the email channel for the
notification settings that 2B stores and nothing yet sends. The Graph client in `app/graph_client.py` still has `send_mail`,
`create_subscription` and `get_message` stubbed; the presence calls are the first real ones.

**Small PR planned (decided 21 Oct 2026): out-of-office lookup bookkeeping (2E follow-up).** Add `User.last_presence_lookup_at`, skip a user whose lookup failed or found nobody until a retry interval setting has passed, and consider an `is_active` flag so inactive agents are not polled (`docs/plan.md`, "Notifications"). Own migration, tested with simulated Graph responses. Independent of 3B to 3F, so it can go in any gap; decide whether `is_active` is in or out when it starts.

**Phase 3 plan (agreed 20 Oct 2026; order: 3A, 3B, then 3D with 3C and 3E alongside).**
- *3A Public surface:* the `APP_ROLE` split (web / public / worker) with per-role settings checks (the public app and worker need no
  session or SSO secrets; see the `INTERNAL_SESSION_SECRET_KEY` note in `infra/README.md`). `/webhooks/graph/notifications` and the
  approval-link route move to public. The webhook stores each notification in a table and returns 202 inside 3 seconds
  (`validationToken` echo, constant-time `clientState` check as today). Rate limiting on public routes. The approval link carries its token in the URL path, so the public app must also keep that path
  out of its own request logs (and, with 3F, out of Application Insights). Agent 2.
- *3B Inbound mail:* a worker job fetches each stored message with `$select=uniqueBody` through `app/graph_client.py`. A reply is
  added to the ticket (match Graph `conversationId`, then the reference in the subject); anything else is a new ticket in the shared
  untriaged queue (source email). Unique constraint on `internetMessageId`. Auto-reply filter (`Auto-Submitted` and the usual headers).
  The dev simulate-inbound is rebuilt on the same path. Tested with a fake Graph client and simulated payloads. A reply on a
  resolved or closed ticket is only recorded here; reopen and ignore-list rules are Phase 4. Agent 2.
- *3C Subscriptions and catch-up:* create, renew (under 24 hours left), recreate on 404; the lifecycle endpoint
  (`reauthorizationRequired` -> PATCH, `subscriptionRemoved` -> recreate then delta sync, `missed` -> delta sync; never
  `/reauthorize` and PATCH within 10 minutes); delta catch-up every 5 to 10 minutes; `/health` warns when expiry is under 24 hours.
  *Built (PR 3C):* see "Mail intake seam (3C)" below.
- *3D Outbound mail:* an outbox with retry and backoff (like `TeamsPost`), real send through Graph for agent replies, automatic
  notices, approval emails with the signed link and the "raised for you" email; threading, CC from followers, a per-ticket cap on
  automatic emails, the preview step reused. Agent 1.
- *3E Attachments:* a storage interface (local and Blob), an attachment table, inbound attachments saved, `/attachments/{id}` with a
  visibility check and a short-lived SAS redirect, server-side type detection, `nosniff`, inline only PNG, JPEG, GIF and WebP.
  Agent 3 does the storage interface; ingestion joins 3B later.
- *3F Wiring:* Bicep and the deploy workflow for the public app, mailbox variables, storage identity, Application Insights. Agent 3.
- `docs/azure-setup.md` (a separate PR by Agent 1) is the by-hand Azure setup checklist.

**3B seam with 3C (agreed in code).** `app.inbound.process_message(session, graph_message_id, client=None, storage=None)` reads one
message (and its files) from Graph and handles it; it is idempotent (unique Graph id and Message-ID in `InboundMail`, claimed in the
same transaction as the ticket) and does not commit, so the delta catch-up job (3C) calls it for every message id it finds and the
duplicates fall away. It returns `Handled(outcome, ticket, message)`; outcomes are `ticket_created`, `reply_added`, `duplicate`, `gone`
and `ignored_*`. The `mail-in` job (`process_notifications`) handles only `GraphNotification` rows with no `lifecycle_event` and
`change_type=created`; other change types are marked done, and lifecycle rows are left untouched for 3C to process and mark. Retries use
the new `attempts` / `next_attempt_at` columns and give up (marked done with the reason) after the outbox's limit; the catch-up is what
recovers a given-up mail. `Message.graph_message_id` now holds the Graph id of each received mail, so a later change can reply to it
with `createReply` (sendMail returns no id, so a sent message has none).

**Deferred from the escalation work (documented, not built).**
- *MSP handoff (phase 5):* Send to MSP / escalate to the MSP, the "thread only has emails
  and comments, or original request only" choice and its preview, the per-team toggle for
  the automatic 18:00 handoff, and the 18:00 job itself. Until then a category whose target
  is the MSP (the default for all of them) shows "Cannot escalate" and the admin
  categories page labels it "MSP (not available yet)". An admin enables escalation per
  category by picking a team.
- *Target by (category, current team):* today the target is the category's, and a ticket
  already with that team would go to the MSP. A table keyed by category and current team
  would let a ticket chain 1st line -> 2nd line -> dev team. Build it if a real chain needs it.
- *Per-inbox routing for sub-teams* (a sub-team sharing its parent's inbox): with per-inbox
  routing (`docs/plan.md`).
- *Re-assign needs visibility of the target team?* Today any agent who works a ticket can
  re-assign it to any team and then loses sight of it. Whether re-assign should be limited to
  teams the agent can see is undecided.

**Small PRs from the decisions below (not started):** unassign tickets when an agent is removed from a team, category change after triage, per-user timezone display, and the status dropdown on Email user / Add note (agreed 14 Oct, replaces "emailing always moves to Awaiting user": the agent picks the status, default Awaiting user; the preview must show the chosen status; see "Status follows the conversation" in `docs/plan.md`). Today an email still always moves an Open or In progress ticket to Awaiting user.


**Email preview for the other emails.** The step exists for agent replies (1G-ii). Resolution emails (phase 4), MSP forwards and bulk resolves (listing every linked ticket and recipient) must reuse it, and automatic emails need admin-managed templates previewed when edited. Per-person opt-out of the "raised for you" email joins the setting when user notification settings exist.

**Phase 2 (complete), in five PRs:** 2A the bell (#36); 2B notification settings (#38; `/settings/notifications`, anyone signed in chooses channels per event type; the bell, Teams and email choices are read through `is_enabled`, and email is stored for phase 3); 2C the worker, in two parts (#39 the skeleton: `python -m app.worker`, APScheduler, heartbeat, a compose service; #42 the first job that matters: an unread notification is also sent to Teams after the person's delay, default 5 minutes, through the Teams outbox; `notify()` writes a Teams-only row already read when the bell is off, so the copy still has a row to read); 2D the Teams Workflows webhook for new tickets (#40; the URL is a secret from the environment, never a setting); 2E out-of-office detection (#44; Graph presence read by the `presence` job, tested with simulated responses, Azure-only to prove; decision: out of office is a label in the pickers, never a block or a skip).

**Still to do after Phase 2.** (1) Bicep: done (wired in `infra/main.bicep` for `TEAMS_NEW_TICKET_ENABLED`, `TEAMS_NOTIFICATIONS_ENABLED` and `PRESENCE_ENABLED`, with the webhook URL as a worker-only Key Vault secret; documented in `infra/README.md`, "Teams and out-of-office"; unproved until staging, see the staging checklist). (2) Prune old sent `TeamsPost` rows when a housekeeping job exists. The `teams-posts` job (every 60 seconds) is in `app/jobs/__init__.py`. Another kind of post is one builder in `app.teams.CARD_BUILDERS` (it turns the row into the payload, or raises `Unsendable`); sending, backoff and giving up are shared.

**Mail intake seam (3C, for 3B).** `app/subscriptions.py` owns keeping mail arriving; 3B owns turning it into tickets.
- The webhook (3A) and the catch-up both end the same way: a `GraphNotification` row with `resource_id` = the Graph message id and
  `processed_at` NULL. 3B's job processes rows where `lifecycle_event IS NULL`. **Rows with a `lifecycle_event` belong to 3C**
  (`graph-subscription` job marks them processed); 3B must skip them.
- The catch-up (`catch_up`, job `mail-catch-up`, every 5 minutes) hands ids to a `MailIntake` callable
  `(session, message_ids, source) -> new count`. The default `record_for_processing` inserts a `GraphNotification` row for each id
  that has no row yet (any state), so 3B needs no code change; it dedupes on `resource_id`, and 3B's unique `internetMessageId` is
  the backstop (a webhook and a catch-up can race and write two rows for one id). If 3B wants a different hand-over it passes its own
  function to `catch_up(intake=...)`.
- **Race to be aware of:** a webhook and a catch-up run can both insert a `GraphNotification` row for one message id (the table has
  no unique constraint on `resource_id`, so the webhook stays a plain insert inside Graph's 3 seconds). Two rows must never mean
  two tickets: 3B's processing has to be idempotent per message, with the unique constraint on the message's `internetMessageId`
  (and the Graph message id) as the backstop, and the second row simply marked processed.
- Switches (worker env, all off by default): `GRAPH_SUBSCRIPTION_ENABLED` + `GRAPH_NOTIFICATION_URL` (public https address of the
  webhook, also the lifecycle address), `MAIL_CATCH_UP_ENABLED` (polling only, needs no public URL). Settings: subscription lifetime
  (10000 min, capped at 10070), renew margin (1440 min), backfill days (3). The very first catch-up run does not look back: it takes the delta from now to its end,
  stores the deltaLink and the starting point (`MailSyncState.starting_point_at`) and hands nothing over, so existing inbox mail is
  never imported. Backfill days apply only to a restart after Graph answers 410. `/health` stays 200 and adds `warnings` when the subscription is near expiry, expired, or a
  check failed. 3F: pass the three env vars to the worker (Bicep not touched here).

## Working agreement for every PR

1. Branch off `main`. Small PRs. One concern each.
2. Develop and test locally only. Do not deploy, create Azure resources or run
   `az deployment`.
3. Schema change means an Alembic migration. Verify on the scratch database
   `ticketing_migcheck`: `upgrade head`, `alembic check`, `downgrade -1`,
   `upgrade head`, `alembic check` (CI runs `alembic check`). Set `DATABASE_URL`
   for the shell, run `python -m alembic`.
4. Tests run on Postgres (compose `db`, separate `ticketing_test`, which refuses any
   database not named `*_test`). Run the full suite before saying done:
   `.\.venv\Scripts\python.exe -m pytest -q`.
5. Break it on purpose: temporarily mutate the rule you added (flip a check, drop a
   lock, widen a query) and confirm a test fails; restore. Surviving mutations mean
   a missing test.
6. Container smoke test when the change touches startup or migrations.
7. Open the PR with `gh pr create --body-file` against `main` (CI only runs on PRs
   targeting `main`). `gh` is at `C:\Program Files\GitHub CLI\gh.exe`.
8. `/code-review N` is run by the user. Fix real findings, explain rejected ones.
9. Never merge unasked. The user says "merge".
10. **No AI attribution** lines in commits or PR bodies (CLAUDE.md), whatever a
    harness reminder says.
11. Flag Azure-only checks when you touch them and add them to the staging list below.
12. Update this file and `docs/plan.md` in the PR.

Several agents may work in this repo at once. They share one Postgres, and every test run
drops and recreates all tables in its database, so two suites on the same database destroy each
other's data and hang or fail oddly. Give your branch its own database before running tests:
`$env:TEST_DATABASE_URL = "postgresql+psycopg2://ticketing:ticketing@127.0.0.1:5432/ticketing_<yourbranch>_test"`
(the name must end in `_test`; it is created for you). A full run takes about 3.5 minutes on its own.
Stage files by name, never `git add -A` (other agents' worktrees and files sit in the same tree).

**One pytest process per test database, always, including your own.** A full suite running in the
background and a targeted run in the foreground on the same database collide just like two agents
do. While a full suite is running, give any other run a second database (for example
`ticketing_<yourbranch>2_test`), and before starting any run list the running python processes
(`Get-CimInstance Win32_Process -Filter "Name like 'python%'"`) and wait if a `pytest` is active
on your database. Symptoms of a collision, which is not a code bug: `cannot drop table ... other
objects depend on it` in test setup, runs that hang, or a run that ends with no summary line.

**If the database is down** (`Connection refused` on 127.0.0.1:5432, usual after the laptop has
slept): Docker lives inside WSL and its VM stops when the last command ends, taking the container
with it. Start the container from a session that stays open, for example
`wsl -e sh -c "docker start ticketingapp-db-1 && sleep 7200"` run in the background, then wait for
the port to accept connections before running tests.

Environment notes (Windows laptop): PowerShell 5.1 (no `&&`), write files without a
BOM, avoid backticks and `\n` inside PowerShell strings (use the Edit tool or
single-quoted here-strings), the virtualenv is `.venv`. If Docker/WSL is down after
sleep, restart it and `docker start ticketingapp-db-1`.

## Invariants (where rules live)

- **Cross-site protection is `SameSite=Lax` on the session cookie** (`app/main.py`), not per-form
  tokens. Browsers do not send that cookie on a cross-site POST, which is what keeps another site
  from submitting our forms as the signed-in person (for example "untick everything" on the
  notification settings). A test pins the setting. Anything that would weaken it (`SameSite=None`, an
  embedded or cross-site login flow) needs form tokens first.

- **Access** is decided only in `app/visibility.py` and comes from the ticket's
  team: member works, viewer reads. Assignee, triager, escalator and follower grant
  nothing. The exceptions are the shared untriaged queue and a parent
  team's view of its sub-teams, and the escalator's access to the one ticket they
  escalated (`TicketEscalator`). `visible_ticket_condition`
  is the SQL twin of `ticket_access`; a test keeps them in step. Get what a user can
  reach only from `access_context(session, user)`: it loads once per database session and
  is dropped on every flush, commit and rollback, so never build or keep your own copy.
- **What the requester sees** and whether a ticket can be emailed: only
  `app/messages.py` (`requester_visible_messages`, never `ticket.messages`).
- **Ticket numbers**: one shared counter, never change. `reference` (INC-12) is a
  display property that follows the type. Routes and links use the number. Look
  tickets up with `find_ticket` / `get_by_number`. Stored history uses `ticket.tag`
  (`#12`), because the prefix can change.
- **Triage** is separate from assignment; a ticket leaves New only by triage.
- **SLA**: deadlines are fixed when set (triage, priority change, end of a pause)
  and changed targets never touch existing tickets. Targets are settings with
  placeholder values. Backdated tickets have no SLA.
- **Settings, not constants**, for anything marked TBC in the plan.
- Frontend is server-rendered Jinja2 and htmx, plain HTML. The Stitch redesign is
  phase 9, so no styling effort now. 4xx errors reach htmx through the global
  handlers into `#flash`.
- Every state-changing action writes a ticket event and an audit entry, and nothing
  in the service functions commits; the route commits.

## Decisions log

- 21 Oct 2026: With supplier does **not** pause the SLA clock (only Awaiting user does). A requester or follower reply on a With supplier ticket changes nothing in status but notifies the assigned agent, whatever the status. Chase emails to the supplier are a later feature.
- 21 Oct 2026: a reply on a triaged ticket with no assignee notifies nobody. The first mail catch-up starts from now and back-fills nothing; the backfill-days setting applies only after a Graph 410 reset. So create the subscription first and send test mail only after the first catch-up run; mail from before that point is never ingested.
- 21 Oct 2026: approvals will let an approver hand an approval to someone else when they are away, and let the requester withdraw a request while it waits (both to build with the approvals follow-up, phase 4). How handing over works is still open (Open questions).
- 21 Oct 2026: the out-of-office "last looked up" column is planned now as a small PR, not left until the logs show a problem (see "Next up").
- 21 Oct 2026: dependency hygiene 2. The lock is compiled with `uv pip compile --universal` (one file for Linux and Windows, markers for platform-only packages such as `colorama`), once for `requirements.txt` and once for `requirements-dev.txt`; `requirements.txt` and `requirements-dev.txt` stay the human-edited inputs. The Dockerfile installs `requirements.lock`, CI and local dev install `requirements-dev.lock`, both with `--require-hashes`. Dependabot only edits the inputs and cannot regenerate a uv lock, so the `lock-current` job in `pip-audit.yml` fails a PR whose lock is stale; recompile (below) and push. `pip-audit` now audits the dev lock, so transitives are covered. The base image is pinned to the `python:3.14-slim` multi-arch index digest; Dependabot's docker ecosystem proposes new digests, and a rule ignores Python major and minor bumps (3.15 is a deliberate upgrade, patches still arrive). Actions are pinned to full SHAs with a `# vX.Y.Z` comment: checkout v7.0.1 (v7 only blocks fork PR checkouts under `pull_request_target`/`workflow_run`, which we do not use), setup-python v7.0.0 (drops the unused `pip-install` input), upload-artifact v7.0.2, download-artifact v8.0.2, azure/login v3.1.0; this supersedes Dependabot #59 and #60. htmx is vendored at 2.0.11 (`app/static/vendor/`, hash in its README, long immutable caching because the version is in the file name): our templates use only `hx-get/post/target/swap/trigger/include/disabled-elt` and the `hx-on:` syntax, none of which changed in 2.x; the pages send no Content-Security-Policy, so the local script needs no change there. `pip-audit` itself is still installed unhashed in its workflow (a pinned version, outside the app).
- 20 Oct 2026: dependency hygiene is two PRs. PR 1 (this one): kept `httpx2` (nothing in our code imports it, but Starlette's `TestClient` prefers it and warns "install httpx2 instead" when only `httpx` is present, so it is the test client's backend), removed `pytest-cov` (no `--cov` in pytest.ini, CI or docs) and the `uvicorn[standard]` extras (the entrypoint uses `--workers`, never `--reload`; plain uvicorn falls back to asyncio and h11); Dependabot; `pip-audit` in its own workflow so the `test` check is untouched (a requirements change on a PR, nightly at 05:17 UTC, manual run). PR 2 (after PR 1 merges): a lock file with hashes installed with `--require-hashes`, the base image pinned by digest, actions pinned by SHA (azure/login first) and htmx vendored into `app/static`. Dependabot cannot yet bump transitives; PR 2 fixes that. The nightly failure notice goes to whoever last edited the cron line; if that is not the right person, edit the line.
- 20 Oct 2026: out of office is a label in the assignee pickers, never a block or a skip. Sometimes only one person can fix a
  problem, so they are assigned regardless of availability and deal with it when free. Do not turn it into a refusal or a filter.
- 12 Oct 2026: per-inbox routing and sub-teams agreed (`docs/plan.md`).
- 13 Oct: changed SLA targets never affect existing tickets. First response will
  later include Teams.
- 13 Oct: one shared ticket number; display prefix follows the type; lookups by
  number only; agent and portal routes use the number.
- 13 Oct: activity log stores failed sign-ins only for real accounts (no unknown or
  locked accounts), so bots cannot grow the table.
- 13 Oct: backdating is only for tickets an agent raised on someone's behalf (source
  `agent`), e.g. the person came to the desk and it was fixed in person. Never for
  tickets the requester opened.
- 13 Oct: access follows the team and nothing else.
- 13 Oct: `app/raising.py` is the only code that creates `source=agent` tickets (a test scans for it); the backdating tests now raise their tickets through the real form.
- 13 Oct: each sub-ticket is triaged from the original at split time rather than
  left for the agent to triage.
- 13 Oct: the original's conversation shows on a part to an agent only if their team can open the original; the requester's own portal and mail view is unaffected.
- 13 Oct: the original's status is derived from its parts; agents cannot set it by
  hand (only resolve or close once all parts are finished). Reopening a resolved
  original by hand waits for the phase 4 reopen flow.

- 13 Oct: removing an agent from a team leaves their tickets in the team but
  unassigned. (To build.)
- 13 Oct: a ticket's category can be changed after triage, so a split part can take a
  different category from the original (parts are separate issues). The category
  change is a ticket event. (To build; unblocks splits when the original's category
  has been switched off.)
- 13 Oct: times display in each user's own timezone; stored values stay UTC. (To build.)
- 13 Oct: team visibility links (an admin-set way for one team to see another's tickets)
  were designed and built, then **dropped from 1B-ii on 14 Oct before merging**: viewer or
  member memberships do the same job for now. Build them (a one-way team-to-team link, one
  hop, never above the agent's own level) when a real case needs a whole team to see
  another's tickets without being assignable there. Access stays team-only otherwise.
- 13 Oct: a first version of escalation had each team escalate to one team. Replaced on 14 Oct
  before it merged (never in `main`).
- 14 Oct: **escalation is by ticket category** (built, 1B-ii).
  A ticket must be triaged to escalate. Each main category has an escalation target
  (default: the MSP); a sub-category inherits or overrides. A target is a sub-team of
  the agent's team (1st to 2nd line), another team (support to the in-house dev team) or
  the MSP. Built as: the category's target, or the MSP if the ticket is already with that team
  (a (category, current team) table is deferred, see Next up). The agent does not choose a team. Targets, and the sub-teams
  themselves, are created by an admin in settings and the portal; nothing is seeded.
- 14 Oct: the escalator keeps working access to the one ticket they escalated to another
  team (and only that ticket), as an explicit rule in `app/visibility.py`. This
  reverses "an escalator grants nothing" for escalators only. Followers, assignees
  and triagers still grant nothing. Any follower can be removed by whoever works the ticket.
- 14 Oct: re-assign is not escalation. It hands a ticket to a team, unassigned, needs
  no reason, and the agent loses sight of it unless they already have visibility of
  that team (membership or a parent team).
- 19 Oct: escalate and re-assign are two actions (different rules for who picks the team and
  what else happens) over one shared move, `move_ticket` in `app/triage.py`, not one action with
  a type switch. The escalation target is set when a category is created. The escalation reason is
  also saved as an internal note, so @mentions in it work and the receiving team sees it in the
  conversation. One event per move (`team_changed` or `escalated`).
- 14 Oct: Send to MSP and escalating to the MSP are the same action. It lets the agent
  send the thread (default) or only the original request. The thread holds only emails
  and comments, never ticket events, so an escalated ticket nobody wrote on sends just
  the original request. Teams also get an admin toggle for the automatic 18:00 handoff
  (`docs/plan.md`, "MSP").
- 20 Oct: a reply from the requester or a follower notifies the ticket's assignee only, as "Ticket updated by <display name>"
  (their email when they have no name). Decided: when there is no assignee, no one is notified (not the team).
- 20 Oct (3B): a mail is added to an existing ticket only if its sender is the requester or an external follower, however the ticket was
  found (conversation, the Message-ID it answers, then the `[INC-n]` reference in the subject). The reference is on every email, so
  anyone who saw one could otherwise write on that ticket; anyone else gets a ticket of their own with an event naming the one they cited.
  Only the Cc of the mail that opens a ticket makes followers. A sender making more than `INBOUND_MAX_PER_SENDER_HOUR` (30, TBC) tickets
  and replies in an hour is set aside as a probable loop. A reply on a Resolved or Closed ticket is only recorded (phase 4 does reopen).
- 14 Oct: added phase 10 (security hardening and go-live gate) to `docs/plan.md`, and agreed that
  background work is driven by the database (rows with leases and retries) rather than in-memory
  timers. Where that stands (20 Oct): sending is built that way (the `TeamsPost` and `OutboundEmail`
  outboxes are rows the worker claims with `SKIP LOCKED` and retries with backoff). The recurring-jobs
  table (`scheduled_job`, with `next_run_at` and a lease) is not built; the worker still runs its jobs
  from APScheduler, which is acceptable only while each job is safe to run late and twice. Build the
  table before the 18:00 MSP handoff (phase 5), which must not be missed.

## Dependency lock

Change a dependency by editing `requirements.txt` (runtime) or `requirements-dev.txt` (tests), then from the
repo root with `uv` (`pip install uv==0.13.4`, in a venv outside the project is fine):

```
uv pip compile requirements.txt --universal --python-version 3.14 --generate-hashes -o requirements.lock
uv pip compile requirements-dev.txt --universal --python-version 3.14 --generate-hashes -o requirements-dev.lock
```

Add `--upgrade` to move transitive packages forward (otherwise the existing lock is kept). Install with
`pip install --require-hashes -r requirements-dev.lock`. Commit the inputs and both locks together. Refresh the
base image digest by taking Dependabot's docker PR, or by hand with `docker buildx imagetools inspect python:3.14-slim`
(use the top-level `Digest`, the index).

## Open questions

- **Inbound mail (3B), to confirm.** (1) A person CC'd on a thread who then replies without being a follower becomes a new ticket, not a
  follower of the old one; should a reply-all from an unknown address join as a follower when the thread (not just the reference) matches?
  (2) Exchange may give two separate mails with the same subject the same `conversationId`; a new mail from the requester could then join
  an old (even closed) ticket until phase 4's "reply after closure makes a new ticket". (3) Nothing yet acknowledges a new ticket ("your ticket
  is open"); that automatic notice is not built. (4) Mail is trusted on its From address (Mimecast / Exchange are expected to enforce
  SPF, DKIM and DMARC); the code does not read Authentication-Results. (5) The Cc of an alias or distribution list becomes a follower like a person. (6) The own-mail guard compares the exact From address, so mail from an alias of the mailbox is caught only by its headers; the aliases are listed when the mailbox is created (staging checklist, "Mailbox setup").
- **Out-of-office lookups (2E).** One failing batch (only above 650 agents) rolls back the whole run; the three-hour stale cutoff makes that safe. The retry and inactive-agent items are planned (see "Next up").
- **How a Teams copy reaches a person (built on working defaults; say if any should change).** A Workflows webhook posts to a channel, not to one person, so the copy is a post in the support channel that @mentions the person by email (messaging a person directly is the phase 8 bot). Defaults taken: the row is always written when either channel is wanted (Teams-only rows hidden from the bell); a nullable per-person delay with an admin default of 5 minutes; the same channel and webhook as the new-ticket cards, behind its own switch; the outbox's retries (backoff, give up after 8 tries or at once on a refused request). Only staging can prove the mention resolves.
- **Approvals, remaining: how an approver hands over.** Approval emails and chasing (email and Teams) wait for phase 3. Decided 21 Oct 2026: an approver may hand an approval to someone else when away, and a requester may withdraw a request while it waits (both with the approvals follow-up, phase 4). Still open, the model for handing over:
  (a) The approver, or a super admin, re-assigns that one approval to another eligible approver for that request. It is audited.
      No standing delegate accounts. Recommended.
  (b) Each approver names a standing delegate with a date range, and approvals in that range go to the delegate.
      More to build and more to get wrong (stale ranges, who sees what). Until then a super admin waiving the approval covers an absent approver.
- **MSP escalation (assumption to confirm).** Until the MSP handoff is built (phase 5) a
  category whose target is the MSP cannot be escalated.

## Staging checklist (Azure-only, cannot be proved locally)

- Mailbox setup (not started): the shared mailbox address and its aliases are not defined yet. When the mailbox is created, list every alias (and any distribution list that delivers into it) so the own-mail guard in `app/inbound.py` can recognise mail from the mailbox itself; today it compares the exact From address and otherwise relies on headers. The app has no setting for aliases yet (`GRAPH_SHARED_MAILBOX` holds one address), so a small code change (an alias list setting) is needed once the aliases are known. Set `GRAPH_SHARED_MAILBOX` to the address and check that mail sent from an alias is not turned into a ticket.

- Outbound mail (3D): set `MAIL_SENDING=graph` on the worker with the Graph app and `GRAPH_SHARED_MAILBOX` (Mail.Send, admin-consented and limited to that mailbox). Reply to a ticket from the app and check the requester receives it from the shared mailbox with `[INC-n] subject` as the subject and the followers copied; check the mail is not caught by Mimecast or spam, that `X-Auto-Response-Suppress` is accepted as a custom header, and that a reply from the requester (their mail client adds "Re:") still carries the reference. Raise a ticket for someone and check the "raised for you" email; send an approval request and check the link opens the page and decides nothing on GET. Force a 403 (remove the grant) and check the email waits and goes once it is fixed. Try `MAIL_SENDING=dev` locally first and read the text at Admin > Outbound mail.

- Teams new-ticket cards (2D): create a Workflows webhook ("Post to a channel when a webhook request is received") in the support
  channel, store its URL in Key Vault, switch `TEAMS_NEW_TICKET_ENABLED` on, raise a ticket and check the card arrives with the
  right number, subject and (if `PUBLIC_BASE_URL` is set) a working "Open ticket" link. Keep the channel IT-only: the card shows each new ticket's subject and requester before triage. Locally only the payload shape and the
  outbox were tested, with a fake sender, so confirm Teams accepts the Adaptive Card as sent, and what it answers (this code
  treats any 2xx as sent). Also confirm a wrong URL leaves the post unsent with "Teams answered 4xx" and no URL in the log.

- Mail subscription and catch-up (3C; tested locally with a fake Graph client and a faked HTTP layer only): with `GRAPH_SUBSCRIPTION_ENABLED=true`, `GRAPH_NOTIFICATION_URL` set to the public app's `/webhooks/graph/notifications` and the worker deployed (public app up first, since Graph calls it during creation), grant Mail.Read (mailbox-scoped) and check the worker creates exactly one subscription (Admin > Mail intake, and `GET /subscriptions`), that Graph accepts a lifetime of 10000 minutes (limit believed to be 10070 for mail), that a new mail produces a `graphnotification` row, and that `clientState` matches. Restart the worker and scale a second one: still one subscription. Shorten the lifetime setting to ~2 hours with a margin of 60 to watch a real renewal. Delete the subscription in Graph and check it is recreated within 5 minutes. Check lifecycle notifications really arrive at the same address (`reauthorizationRequired`, `subscriptionRemoved`, `missed`) and are handled without `/reauthorize`. Stop the public app for an hour, send mail, and check the catch-up finds it; check the delta query accepts `$filter=receivedDateTime ge` with `$select=id,receivedDateTime`, that the stored deltaLink works on the next run, and that a 410 restarts cleanly. `/health` warnings show on staging.
- Inbound mail (3B; tested locally with a fake Graph and simulated payloads only): after 3C creates the subscription and with `INBOUND_ENABLED=true`
  on the worker (it needs the GRAPH_* values and `GRAPH_SHARED_MAILBOX`, `Mail.Read` admin-consented and limited to the mailbox): send a
  mail from a test account and check a ticket (source email) appears within about 30 seconds with the right requester, subject, text (a
  plain-text `uniqueBody`, no quoted thread on a reply) and Cc people as followers; reply to our own email from the requester's mail client and
  check it joins the ticket (by the reference and by the thread), wakes an Awaiting user ticket, and keeps only the new text; check
  `Prefer: outlook.body-content-type="text"` is honoured for HTML-only and Outlook mail; send an Outlook out-of-office reply, a bounce and a
  mailing-list mail and check each is ignored (`inboundmail.outcome`), and that mail from the mailbox itself is; check `internetMessageHeaders`
  really contains `Auto-Submitted` and `In-Reply-To` after Mimecast; attach a PDF, a picture, an `.exe`, a file over the limit and an attached
  email and check each is stored or noted; kill the worker mid-run and check no ticket is doubled; check a stranger quoting a `[INC-n]`
  reference gets a new ticket, not a message on the old one; check a `Mail.Read` 403 shows as `last_error` on the notification and retries.
  Not proved locally: real Graph payload shapes (`from`, `uniqueBody`, header names and casing), `conversationId` behaviour, attachment
  download for files over 3 MB, Mimecast rewriting or stripping headers.

The full list is "Azure-only verification" in `docs/plan.md`. Added by work so far:
- Dependency hygiene 2: after merge, confirm Dependabot opens its next PRs as expected (inputs only, no Python 3.15 image PR, action bumps keep the SHA plus comment form, a digest PR for the base image) and that GitHub's Dependabot tab shows no config error. First staging image build: the `--require-hashes` install runs on the real runner (verified here in a local WSL Docker build only).
- `FORWARDED_ALLOW_IPS` set to the Container Apps subnet, so the activity log records
  real client IPs and cannot be forged.
- htmx behaviour and `datetime-local` inputs in a real browser (backdating, split, link,
  canned-response picker and raise-a-ticket forms, including the `HX-Redirect` after raising);
  tests use the HTTP layer only.
- Entra sign-in and the SSO activity events against a real redirect URI.
- Out-of-office detection (2E): with the GRAPH_* credentials set and `PRESENCE_ENABLED=true` on the worker, grant the app registration `Presence.Read.All` and `User.Read.All` (application, admin consent). Put an agent out of office (an Outlook automatic reply, a Teams scheduled out of office, or a calendar event marked out of office) and within 15 minutes check the worker log shows `heartbeat job=presence`, the agent shows "(out of office)" in the assign, re-assign and triage pickers, and is unmarked again once back. Check an agent whose sign-in name differs from their email is simply never marked (the directory lookup treats the email as the user principal name), and that a 403 from Graph shows up as `job=presence status=failed` rather than silently marking nobody. The Bicep (`PRESENCE_ENABLED` on the worker) is wired and documented in `infra/README.md`; the Entra permissions are granted by hand and the template has not been deployed, so also check the `PRESENCE_ENABLED=true` env var arrives on the worker revision.
- Public app (3A): deploy with `APP_ROLE=public` and check: `/health` answers; every agent route (`/tickets`, `/admin/users`, `/auth/login`, `/`) is 404 on the public address; Graph's validation POST to `/webhooks/graph/notifications?validationToken=...` is echoed within 3 seconds, including from a cold start; a real notification becomes one `graphnotification` row and a 202; a forged `clientState` stores nothing. The access log shows `/approvals/{token}`, never a token, and check the Container Apps ingress logs and Application Insights do not record the token either (they log the path themselves; if they do, shorten the retention or drop that table for the public app). A caller hammering `/approvals/x` is answered 429 with `Retry-After` (the limit is per replica, so with two replicas the effective limit is double, and the real client address depends on `FORWARDED_ALLOW_IPS`). `APP_ROLE` unset outside dev makes the app refuse to start. Settings: `SESSION_SECRET_KEY` is still required on `public` and `worker` because it signs the emailed approval links (there is no cookie); it must be the same value on the process that mints a link and the one that checks it.
- Teams notification copies (2C-ii): with TEAMS_NOTIFICATIONS_ENABLED on and the webhook URL set, assign a ticket to a test agent who has Teams ticked and leave it unread past their delay: check the card arrives in the support channel and the agent is actually @mentioned (the mention id is their email; an agent whose Teams sign-in differs from their email, or a local account with no Teams user, will show plain text and no ping). Check Teams accepts the msteams.entities mention in a Workflows card. The channel then shows each notified ticket's subject to everyone in it, so keep it IT-only (a dedicated notifications channel and second webhook is a later option). The Bicep for `TEAMS_NOTIFICATIONS_ENABLED` is wired (infra/README.md), not deployed.
- The worker (deployWorker, currently false in Bicep): set it true once real jobs exist, and add the log-query dead man's switch alert on the heartbeat job=heartbeat line (docs/plan.md, Health and monitoring). Its single-instance lock is a *session* advisory lock held on one connection: if staging Postgres is reached through PgBouncer in transaction-pooling mode the lock would not hold, so connect the worker to the server directly (or use session pooling). Verify on staging by starting a second worker and seeing it exit with Another worker is already running.
- The emailed approval link carries its token in the URL path, so web server, ingress and
  Application Insights request logs will record a working credential for the link's lifetime
  (14 days by default). Redact `/approvals/*` paths in access logs and telemetry on staging, and
  keep the lifetime short once real mail is sent. The portal ("Waiting for your approval") is the
  signed-in alternative that has no such token.
- Approve / Decline pages (portal and emailed link) in a real browser, including that a mail
  security scanner opening the link does not decide anything (phase 3, with real mail).
- Deploy workflow (`.github/workflows/deploy.yml`, drafted, linted with actionlint only): nothing in it has run. On the first
  staging run check: the federated credential subject matches the environment name; `azure/login` v3 and the role assignments
  (including the conditioned RBAC Administrator) let `what-if` and the deployment pass; `az acr login` and the push work for the
  AcrPush identity; the `docker save` / `load` / push gives the same image in both registries; `az containerapp job update --image`,
  `job start`, `job execution show` (status values, the `job logs show` flags) behave as `run-migrate-job.sh` assumes; the first
  image deploy path (no job yet, migrate after the template); `latestReadyRevisionName`, `healthState` and `runningState` read as
  `check-rollout.sh` expects, and a deliberately bad image fails the run while the old revision keeps serving; `/health` is
  reachable on the public app; the `production` required-reviewers gate pauses the second matrix leg and `max-parallel: 1` runs
  staging first; secrets left unset really arrive as empty strings in `readEnvironmentVariable`.
- Teams/presence Bicep wiring (linted and built with `build-params` only): the `teams-new-ticket-webhook-url` secret is created in Key Vault from `TEAMS_NEW_TICKET_WEBHOOK_URL` and is readable by the worker identity only (try reading it with the web and public tokens: it must be refused); the worker revision gets `TEAMS_NEW_TICKET_WEBHOOK_URL`, the three `*_ENABLED` flags arrive as `true`/`false` on web, public and worker (`PRESENCE_ENABLED` on the worker only), and the app reads them; supplying the secret on a later deploy without it drops it from the revision; the deploy workflow passes the new variables and secret through (unset variables become empty strings and mean off).
- Attachment storage (3E-storage; tested locally with a directory and a fake Blob client only): with `STORAGE_ACCOUNT_NAME` set and no keys anywhere, the app's managed identity can create a user-delegation key and read/write the `attachments` container (the Bicep gives web and worker Storage Blob Data Contributor and the Storage Blob Delegator role; check the delegator role is really present, since signing a delegation key needs it); a `/attachments/{id}` link redirects to a SAS URL that works for about 90 seconds and then fails with 403; the browser shows a PNG inline and downloads a PDF, an `.html` and an `.svg` from the storage origin (check the forced `Content-Disposition` and `Content-Type` in the response headers, whether the storage origin sends `X-Content-Type-Options: nosniff` on its own, since the redirect path cannot add it or the sandboxing policy, and that the SVG never renders); the public app and migrate job, which have no blob role, cannot fetch a blob. Azurite probably cannot issue user-delegation SAS, so this is staging-only. Also decide the app's image content-security policy once attachments show in the page: it must allow the storage domain (`img-src https://<account>.blob.core.windows.net`); today no page shows attachments, so nothing sets it.
- 3F wiring (linted and built with `build-params` only; nothing deployed): with `DEPLOY_PUBLIC_APP`/`DEPLOY_WORKER` true the public and worker apps start and `check-rollout.sh` finds them healthy (the worker has no probes, so only running is checked); `APP_ROLE=public` really leaves the agent routes at 404 on its address and `/health` answers from outside; the public identity can read only its three secrets (try the Graph secret and the session key with its token); the worker's `PUBLIC_BASE_URL` is the public app and `WEB_BASE_URL` the web app, so an approval email links to the public address and a Teams card to the web address; with `MAIL_SENDING=graph` the worker sends and the other apps only queue; Application Insights receives requests, dependencies and logs from all three apps with the connection string from the monitoring module, and **no `/approvals/` request or token appears in it** (also in the Container Apps ingress logs, which are Azure's, not the app's); the extra image size and start time of `azure-monitor-opentelemetry` is acceptable on the Consumption profile.
- Worker mail switches (3F wiring for 3B/3C; the settings land with those PRs, nothing here is deployed): `GRAPH_SUBSCRIPTION_ENABLED`, `MAIL_CATCH_UP_ENABLED` and `INBOUND_ENABLED` arrive as `true`/`false` on the worker only, `GRAPH_NOTIFICATION_URL` is the public app's `https://.../webhooks/graph/notifications` and is empty when no public app exists, and the `INBOUND_MAX_*` limits appear only when set. Check the worker revision's environment, and that Graph accepts that address when the subscription is created.

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
| 2C-ii unread-after-N-minutes notification to Teams: which notifications want a copy and when (Teams-only rows, per-person delay, admin default), then queued in the Teams outbox and sent by the worker | built, in review | `app/notifications.py`, `app/teams.py`, `app/notification_settings.py` |
| 2D Teams channel webhook for new tickets: outbox, card, sender, worker job (the Bicep wiring follows, see Next up) | merged (#40) | `app/teams.py` |
| 2E out-of-office detection (Graph presence): the worker's `presence` job stores each agent's flag; OOO agents are labelled in the assignee pickers and left out of team-wide notifications | built, in review | `app/presence.py` |
| Phases 2 to 9 | not started | `docs/plan.md`, "Build order" |

Tests: 769 passing on Postgres 16.

## Next up
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

**Small PRs from the decisions below (not started):** unassign tickets when an agent is removed from a team, category change after triage, per-user timezone display.


**Email preview for the other emails.** The step exists for agent replies (1G-ii). Resolution emails (phase 4), MSP forwards and bulk resolves (listing every linked ticket and recipient) must reuse it, and automatic emails need admin-managed templates previewed when edited. Per-person opt-out of the "raised for you" email joins the setting when user notification settings exist.

**Phase 2**, in five PRs: 2A the bell (merged, #36), 2B notification settings (built: `/settings/notifications`, anyone signed in chooses channels per event type; only the bell delivers, Teams and email choices are stored for 2C and phase 3 to read through `is_enabled`), 2C the worker (python -m app.worker, APScheduler, heartbeat, a compose service; first job: an unread notification is also sent to Teams after N minutes, a per-person setting, default 5). Note for 2C: notify() writes no Notification row when the bell is off, so a Teams copy that reads unread rows would never fire for someone with the bell off and Teams on; 2C must decide that (for example, write the row already read, or send Teams from notify() itself), 2D the Teams Workflows webhook for new tickets (the URL is a secret from the environment, never a setting; test with a mocked sender), 2E out-of-office detection (Graph presence, tested with simulated responses; Azure-only to prove) and skipping out-of-office agents for assignment and team notifications. Phase 3 (mail) will carry the approval emails with the link from pp/approval_links.py.

**Still to do for 2D.** (1) Bicep: `TEAMS_NEW_TICKET_ENABLED` as a plain environment variable on web, public and worker; the webhook URL as a Key Vault secret (`teams-new-ticket-webhook-url` -> `TEAMS_NEW_TICKET_WEBHOOK_URL`) readable by the worker identity only. Validate with `bicep lint` and `build-params`. Until then the worker has no URL, so posts wait in the `teamspost` table and the worker logs a warning every minute. (2) Prune old sent `TeamsPost` rows when a housekeeping job exists. The `teams-posts` job (every 60 seconds) is in `app/jobs/__init__.py`. Another kind of post is one builder in `app.teams.CARD_BUILDERS` (it turns the row into the payload, or raises `Unsendable`); sending, backoff and giving up are shared.

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

## Open questions

- **Out-of-office lookups (2E).** A directory lookup that fails or finds nobody is retried on every 15-minute run, and inactive agents are
  polled (there is no active flag). A "last looked up" column to stop the retries is worth doing if the logs show repeated failed
  lookups. One failing batch (only above 650 agents) rolls back the whole run; the three-hour stale cutoff makes that safe.
- **How a Teams copy reaches a person (built on working defaults; say if any should change).** A Workflows webhook posts to a channel, not to one person, so the copy is a post in the support channel that @mentions the person by email (messaging a person directly is the phase 8 bot). Defaults taken: the row is always written when either channel is wanted (Teams-only rows hidden from the bell); a nullable per-person delay with an admin default of 5 minutes; the same channel and webhook as the new-ticket cards, behind its own switch; the outbox's retries (backoff, give up after 8 tries or at once on a refused request). Only staging can prove the mention resolves.
- **Approvals, remaining.** Approval emails and chasing (email and Teams) wait for phase 3. Not decided: whether an approver may delegate (someone on leave), and whether a request should be able to be withdrawn by its requester while it waits. Today an absent approver is handled by a super admin waiving the approval.
- **MSP escalation (assumption to confirm).** Until the MSP handoff is built (phase 5) a
  category whose target is the MSP cannot be escalated.

## Staging checklist (Azure-only, cannot be proved locally)

- Teams new-ticket cards (2D): create a Workflows webhook ("Post to a channel when a webhook request is received") in the support
  channel, store its URL in Key Vault, switch `TEAMS_NEW_TICKET_ENABLED` on, raise a ticket and check the card arrives with the
  right number, subject and (if `PUBLIC_BASE_URL` is set) a working "Open ticket" link. Keep the channel IT-only: the card shows each new ticket's subject and requester before triage. Locally only the payload shape and the
  outbox were tested, with a fake sender, so confirm Teams accepts the Adaptive Card as sent, and what it answers (this code
  treats any 2xx as sent). Also confirm a wrong URL leaves the post unsent with "Teams answered 4xx" and no URL in the log.

The full list is "Azure-only verification" in `docs/plan.md`. Added by work so far:
- `FORWARDED_ALLOW_IPS` set to the Container Apps subnet, so the activity log records
  real client IPs and cannot be forged.
- htmx behaviour and `datetime-local` inputs in a real browser (backdating, split, link,
  canned-response picker and raise-a-ticket forms, including the `HX-Redirect` after raising);
  tests use the HTTP layer only.
- Entra sign-in and the SSO activity events against a real redirect URI.
- Out-of-office detection (2E): with the GRAPH_* credentials set and `PRESENCE_ENABLED=true` on the worker, grant the app registration `Presence.Read.All` and `User.Read.All` (application, admin consent). Put an agent out of office (an Outlook automatic reply, a Teams scheduled out of office, or a calendar event marked out of office) and within 15 minutes check the worker log shows `heartbeat job=presence`, the agent shows "(out of office)" in the assign, re-assign and triage pickers, and is unmarked again once back. Check an agent whose sign-in name differs from their email is simply never marked (the directory lookup treats the email as the user principal name), and that a 403 from Graph shows up as `job=presence status=failed` rather than silently marking nobody. Bicep wiring for PRESENCE_ENABLED and the extra Graph permissions is not built.
- Teams notification copies (2C-ii): with TEAMS_NOTIFICATIONS_ENABLED on and the webhook URL set, assign a ticket to a test agent who has Teams ticked and leave it unread past their delay: check the card arrives in the support channel and the agent is actually @mentioned (the mention id is their email; an agent whose Teams sign-in differs from their email, or a local account with no Teams user, will show plain text and no ping). Check Teams accepts the msteams.entities mention in a Workflows card. The channel then shows each notified ticket's subject to everyone in it, so keep it IT-only (a dedicated notifications channel and second webhook is a later option). Bicep wiring for TEAMS_NOTIFICATIONS_ENABLED is not built.
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

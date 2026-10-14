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
| 1B-ii team links, sub-teams and escalation by category | built, in review (#29) | `app/visibility.py`, `app/escalation.py`, `app/routers/admin.py` |
| 1G raise-a-ticket form, agents raising for someone, preview step | not started | |
| 1H Service Requests and approvers | not started (may slip past phase 3) | |
| Phases 2 to 9 | not started | `docs/plan.md`, "Build order" |

Tests: 463 passing on Postgres 16.

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

**1G.** Portal raise-a-ticket form; agents raising a ticket for someone else (the
only thing allowed to create `source=agent` tickets, which backdating relies on);
creation-time backdating; the outbound email preview step. When it lands, switch
the backdating tests off their hand-set `source` to the real creation path.

**1H.** Service Requests and per-type approvers.

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

Environment notes (Windows laptop): PowerShell 5.1 (no `&&`), write files without a
BOM, avoid backticks and `\n` inside PowerShell strings (use the Edit tool or
single-quoted here-strings), the virtualenv is `.venv`. If Docker/WSL is down after
sleep, restart it and `docker start ticketingapp-db-1`.

## Invariants (where rules live)

- **Access** is decided only in `app/visibility.py` and comes from the ticket's
  team: member works, viewer reads. Assignee, triager, escalator and follower grant
  nothing. The exceptions are the shared untriaged queue and admin-set team
  visibility links (`TeamLink`: one way, one hop, never above the agent's own level in
  the linking team), a parent team's view of its sub-teams, and the escalator's access to
  the one ticket they escalated (`TicketEscalator`). `visible_ticket_condition`
  is the SQL twin of `ticket_access`; a test keeps them in step.
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
- 13 Oct: **team visibility links** (built, 1B-ii; links come only from teams the agent belongs to, not from sub-teams). Access stays team-only by
  default; an admin setting can link one team to another so the first can see the
  second's tickets. Links are directional, not transitive, "view" or "work" level,
  admin-only and written to the activity log. The rule lives only in
  `app/visibility.py`. Used for same-role teams in different locations; unrelated
  departments (e.g. Finance and IT support) get no link.
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
  that team (membership, parent team or a team link).
- 14 Oct: Send to MSP and escalating to the MSP are the same action. It lets the agent
  send the thread (default) or only the original request. The thread holds only emails
  and comments, never ticket events, so an escalated ticket nobody wrote on sends just
  the original request. Teams also get an admin toggle for the automatic 18:00 handoff
  (`docs/plan.md`, "MSP").

## Open questions

None at the moment. Assumptions to confirm: a "view" team link can only read, not add a note; until the MSP
handoff is built (phase 5) a category whose target is the MSP cannot be escalated.

## Staging checklist (Azure-only, cannot be proved locally)

The full list is "Azure-only verification" in `docs/plan.md`. Added by work so far:
- `FORWARDED_ALLOW_IPS` set to the Container Apps subnet, so the activity log records
  real client IPs and cannot be forged.
- htmx behaviour and `datetime-local` inputs in a real browser (backdating, split and
  link forms); tests use the HTTP layer only.
- Entra sign-in and the SSO activity events against a real redirect URI.

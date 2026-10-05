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
                    FastAPI app ──▶ PostgreSQL (tickets, messages)
                              │
                     Engineer views/replies in browser (HTMX UI)
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
- **PostgreSQL** — tickets and message threads, safe for concurrent engineers
- **Jinja2 + HTMX** — server-rendered UI, no separate frontend build/deploy
- **Docker Compose** — app + Postgres, runs on one internal box

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

Then open http://localhost:8000/tickets.

Graph credentials are optional to start the app — email ingestion and
sending are stubbed until an app registration exists (see below). Until
then, use the dev helper to create tickets manually:

```bash
curl -X POST http://localhost:8000/webhooks/dev/simulate-inbound \
  -d "requester_email=jane@example.com" \
  -d "subject=Can't connect to VPN" \
  -d "body=I get a timeout every time I try to connect."
```

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
   Mail subscriptions expire after ~3 days and need a renewal job.
6. Fill in the notification-handling TODO in
   [app/routers/webhooks.py](app/routers/webhooks.py) to fetch the new
   message via Graph and create or update a ticket.

## Project layout

```
app/
  main.py            FastAPI app, router wiring, startup
  config.py          Settings from environment / .env
  database.py        SQLModel engine/session
  models.py          Ticket, Message
  graph_client.py     Microsoft Graph integration (stubbed)
  routers/
    tickets.py       List/view tickets, post replies
    webhooks.py      Graph change-notification endpoint + dev helper
  templates/         Jinja2 + HTMX views
  static/            CSS
```

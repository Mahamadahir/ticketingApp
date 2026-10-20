from pydantic_settings import BaseSettings, SettingsConfigDict


INSECURE_SECRETS = {"", "change-me", "dev-secret-change-me"}

# Which routes a process serves (app/main.py build_app), and for the worker which settings it needs.
# web: the agent console, portal and admin. public: only the Graph webhook, the emailed approval
# link and /health, open to the internet. worker: no routes (python -m app.worker). all: every route
# in one process, for local development and tests only.
APP_ROLES = ("all", "web", "public", "worker")


class Settings(BaseSettings):
    # dev, staging or production. Defaults to production so a missing
    # setting can never switch on dev-only routes.
    env: str = "production"
    # What this process is for (APP_ROLES). "all" serves everything and is refused outside dev, so a
    # deployment that forgets to say what it is does not put the agent console on the internet.
    app_role: str = "all"

    database_url: str = "postgresql+psycopg2://ticketing:ticketing@db:5432/ticketing"

    # Entra ID (Azure AD) app registration, client-credentials flow against
    # the shared mailbox. Mimecast sits in front of Exchange Online, but by
    # the time mail lands in the mailbox it's just Graph API from here.
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_shared_mailbox: str = ""
    graph_webhook_client_state: str = "change-me"

    # Separate Entra ID app registration for user sign-in (delegated,
    # authorization-code flow). Used by both the agent console and the
    # end-user portal. If left blank, the app falls back to a dev-only
    # login form so the role/team model can be exercised without a real
    # app registration.
    sso_tenant_id: str = ""
    sso_client_id: str = ""
    sso_client_secret: str = ""
    sso_redirect_uri: str = "http://localhost:8000/auth/callback"

    session_secret_key: str = "dev-secret-change-me"

    # Announcing new tickets in a Teams channel (phase 2D). The web and public apps only queue the
    # announcement, so they need just the switch; the worker sends it, so only the worker needs the
    # URL. The URL is a credential (anyone holding it can post to the channel): it comes from the
    # environment (Key Vault in Azure), never from a setting or the database.
    teams_new_ticket_enabled: bool = False
    teams_new_ticket_webhook_url: str = ""
    # Sending an agent's unread notifications to Teams (phase 2C-ii). Every process needs the switch
    # (the worker queues copies only when it is on; with it off, due copies are dropped rather than
    # left to pile up). They go to the same channel as the new-ticket cards, so the one webhook URL
    # above is the only credential, and it is needed only by the worker.
    teams_notifications_enabled: bool = False
    # Out-of-office detection (phase 2E): the worker reads agents' presence from Graph
    # (Presence.Read.All) and marks those who are out of office. Needs the GRAPH_* credentials.
    presence_enabled: bool = False
    # Keeping a Graph change-notification subscription on the shared mailbox alive (phase 3C), run by the
    # worker. GRAPH_NOTIFICATION_URL is the public address of POST /webhooks/graph/notifications on the
    # public app (https, reachable from Microsoft); lifecycle notifications come to the same address.
    # Not set in dev: a subscription needs a public URL, so only staging can prove it.
    graph_subscription_enabled: bool = False
    graph_notification_url: str = ""
    # The catch-up sync (delta query on the inbox) finds mail a webhook missed. It needs no public URL,
    # so it can run without the subscription (polling only).
    mail_catch_up_enabled: bool = False
    # Email out of the shared mailbox (phase 3D). "off": nothing is queued. "dev": emails are queued and
    # the worker records them as sent without contacting Graph (dev only; see /admin/outbound-mail).
    # "graph": the worker sends them through Graph, so it needs the GRAPH_* values and the mailbox.
    mail_sending: str = "off"
    # Where the app is reached, so a Teams card can link to the ticket. Blank: no link on the card.
    public_base_url: str = ""

    # Rate limits on the public routes, per caller address per minute, per replica (app/rate_limit.py).
    # The approval link is a credential in an address, so guessing it is slowed down; the Graph webhook
    # gets a high limit because Microsoft sends from few addresses and a refused notification is lost
    # until the catch-up sync finds the mail.
    rate_limit_approvals_per_minute: int = 30
    rate_limit_webhook_per_minute: int = 600
    # Attachments (phase 3E, app/storage.py). STORAGE_BACKEND is "local" (a directory, for development
    # and tests) or "blob" (Azure Blob Storage through the app's managed identity: no account keys).
    # Left blank it is blob when STORAGE_ACCOUNT_NAME is set (the Bicep sets it) and local otherwise.
    storage_backend: str = ""
    storage_local_path: str = "data/attachments"
    storage_account_name: str = ""
    storage_container: str = "attachments"
    # A download link lives this long: long enough for the browser to follow the redirect.
    attachment_link_seconds: int = 90
    attachment_max_bytes: int = 10 * 1024 * 1024
    # An inline picture smaller than this (a signature logo, a tracking pixel) is not kept.
    attachment_inline_min_bytes: int = 5 * 1024

    # Inbound mail (phase 3B, app/inbound.py). All placeholders, TBC. INBOUND_ENABLED switches the worker's
    # mail-in job on (it also needs the GRAPH_* values and the mailbox). A mail keeps at most this many
    # attachments (the rest are noted on the ticket), and a sender may make at most this many tickets or
    # replies an hour before the rest are set aside as a probable mail loop (0 turns the limit off).
    inbound_enabled: bool = False
    inbound_max_attachments: int = 20
    inbound_max_per_sender_hour: int = 30
    # Followers made from the Cc line of a mail that opens a ticket (followers.MAX_EXTERNAL_FOLLOWERS caps all).
    inbound_max_cc: int = 10

    # Comma-separated emails granted super_admin on first login, so there's
    # a way into /admin before any roles have been assigned there.
    super_admin_emails: str = ""

    # How often the background worker (app/worker.py) writes its heartbeat line. The dead man's
    # switch alert is set to a few multiples of this.
    worker_heartbeat_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def graph_configured(self) -> bool:
        return bool(self.graph_tenant_id and self.graph_client_id and self.graph_client_secret)

    @property
    def storage_kind(self) -> str:
        if self.storage_backend:
            return self.storage_backend.lower()
        return "blob" if self.storage_account_name else "local"

    @property
    def teams_new_ticket_configured(self) -> bool:
        """Whether this process can send: it holds the webhook URL (the worker)."""
        return bool(self.teams_new_ticket_webhook_url)

    @property
    def sso_configured(self) -> bool:
        return bool(self.sso_tenant_id and self.sso_client_id and self.sso_client_secret)

    @property
    def super_admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.super_admin_emails.split(",") if e.strip()]


MAIL_SENDING_MODES = ("off", "dev", "graph")


def mail_sync_errors(config: Settings) -> list[str]:
    errors = []
    if (config.graph_subscription_enabled or config.mail_catch_up_enabled) and not (
        config.graph_configured and config.graph_shared_mailbox
    ):
        errors.append("GRAPH_SUBSCRIPTION_ENABLED / MAIL_CATCH_UP_ENABLED need the GRAPH_* values and GRAPH_SHARED_MAILBOX")
    if config.graph_subscription_enabled and not config.graph_notification_url.lower().startswith("https://"):
        errors.append("GRAPH_SUBSCRIPTION_ENABLED needs GRAPH_NOTIFICATION_URL, an https address Microsoft can reach")
    return errors


def unsafe_setting_errors(config: Settings) -> list[str]:
    errors = mail_sync_errors(config)
    if config.mail_sending not in MAIL_SENDING_MODES:
        errors.append(f"MAIL_SENDING must be one of {', '.join(MAIL_SENDING_MODES)}")
    if config.app_role not in APP_ROLES:
        errors.append(f"APP_ROLE must be one of {', '.join(APP_ROLES)}")
    if config.is_dev:
        return errors
    if config.mail_sending == "dev":
        errors.append("MAIL_SENDING=dev only works in the dev environment (it sends nothing)")

    if config.app_role == "all":
        errors.append("APP_ROLE is unset: say web, public or worker, so the process serves only its own routes")
    # On web this signs the session cookie. On public and the worker there is no cookie; it signs and
    # checks the emailed approval links, which web never does, so it is the one secret they share.
    if config.session_secret_key in INSECURE_SECRETS:
        errors.append("SESSION_SECRET_KEY is unset or a default value")
    # The public app is the one that receives the webhook, so it needs the state to check it against.
    if (config.graph_configured or config.app_role == "public") and config.graph_webhook_client_state in INSECURE_SECRETS:
        errors.append("GRAPH_WEBHOOK_CLIENT_STATE is unset or a default value")
    # The public app is open to the internet: it must not be able to sign anyone in.
    if config.app_role == "public" and config.sso_client_secret:
        errors.append("The public app must not hold the SSO secret (SSO_CLIENT_SECRET)")
    if config.storage_kind not in ("local", "blob"):
        errors.append("STORAGE_BACKEND must be 'local' or 'blob'")
    elif config.storage_kind == "blob" and not config.storage_account_name:
        errors.append("STORAGE_BACKEND is blob but STORAGE_ACCOUNT_NAME is unset")
    return errors


settings = Settings()

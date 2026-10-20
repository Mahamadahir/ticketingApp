from pydantic_settings import BaseSettings, SettingsConfigDict


INSECURE_SECRETS = {"", "change-me", "dev-secret-change-me"}


class Settings(BaseSettings):
    # dev, staging or production. Defaults to production so a missing
    # setting can never switch on dev-only routes.
    env: str = "production"

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
    # Email out of the shared mailbox (phase 3D). "off": nothing is queued. "dev": emails are queued and
    # the worker records them as sent without contacting Graph (dev only; see /admin/outbound-mail).
    # "graph": the worker sends them through Graph, so it needs the GRAPH_* values and the mailbox.
    mail_sending: str = "off"
    # Where the app is reached, so a Teams card can link to the ticket. Blank: no link on the card.
    public_base_url: str = ""

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


def unsafe_setting_errors(config: Settings) -> list[str]:
    errors = []
    if config.mail_sending not in MAIL_SENDING_MODES:
        errors.append(f"MAIL_SENDING must be one of {', '.join(MAIL_SENDING_MODES)}")
    if config.is_dev:
        return errors
    if config.mail_sending == "dev":
        errors.append("MAIL_SENDING=dev only works in the dev environment (it sends nothing)")
    if config.session_secret_key in INSECURE_SECRETS:
        errors.append("SESSION_SECRET_KEY is unset or a default value")
    if config.graph_configured and config.graph_webhook_client_state in INSECURE_SECRETS:
        errors.append("GRAPH_WEBHOOK_CLIENT_STATE is unset or a default value")
    return errors


settings = Settings()

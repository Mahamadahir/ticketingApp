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

    # Comma-separated emails granted super_admin on first login, so there's
    # a way into /admin before any roles have been assigned there.
    super_admin_emails: str = ""

    model_config = SettingsConfigDict(env_file=".env")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def graph_configured(self) -> bool:
        return bool(self.graph_tenant_id and self.graph_client_id and self.graph_client_secret)

    @property
    def sso_configured(self) -> bool:
        return bool(self.sso_tenant_id and self.sso_client_id and self.sso_client_secret)

    @property
    def super_admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.super_admin_emails.split(",") if e.strip()]


def unsafe_setting_errors(config: Settings) -> list[str]:
    if config.is_dev:
        return []

    errors = []
    if config.session_secret_key in INSECURE_SECRETS:
        errors.append("SESSION_SECRET_KEY is unset or a default value")
    if config.graph_configured and config.graph_webhook_client_state in INSECURE_SECRETS:
        errors.append("GRAPH_WEBHOOK_CLIENT_STATE is unset or a default value")
    return errors


settings = Settings()

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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

    class Config:
        env_file = ".env"

    @property
    def sso_configured(self) -> bool:
        return bool(self.sso_tenant_id and self.sso_client_id and self.sso_client_secret)

    @property
    def super_admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.super_admin_emails.split(",") if e.strip()]


settings = Settings()

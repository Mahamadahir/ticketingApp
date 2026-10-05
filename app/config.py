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

    class Config:
        env_file = ".env"


settings = Settings()

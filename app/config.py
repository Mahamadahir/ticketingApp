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

    # bcrypt cost for new password hashes (4 to 16). Tests set 4 for speed; outside dev it must be at least 12.
    # Existing hashes keep the cost they were made with.
    bcrypt_rounds: int = 12
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
    # The Teams bot (phase 8, app/teams_bot.py): people message it and it messages them. Off by default. The
    # public app serves POST /api/teams/messages (404 while off); the public app and the worker send. APP_ID is
    # the bot's Entra application (client) id, which is also the audience Teams puts in its token; TENANT_ID is
    # our directory, the only one whose users are served. The bot signs in to the Bot Connector with
    # TEAMS_BOT_CLIENT_SECRET (a secret, from Key Vault) when it has one, otherwise as the user-assigned managed
    # identity whose client id is APP_ID. Only the public app and the worker hold the secret.
    teams_bot_enabled: bool = False
    teams_bot_app_id: str = ""
    teams_bot_tenant_id: str = ""
    teams_bot_client_secret: str = ""
    # Scheduled mailbox changes through Azure Automation (app/mailbox_executor_azure.py). Only the worker uses them.
    # RESOURCE_ID is the Automation account (/subscriptions/.../automationAccounts/<name>), TENANT_DOMAIN the
    # tenant's <name>.onmicrosoft.com that the runbooks sign in to Exchange with. Neither is a secret: the worker
    # reaches Azure Resource Manager as its managed identity. TIMEOUT is how long one runbook job may take
    # before the answer is "no answer" (the caller then reads the mailbox, so it is not a failure by itself).
    mailbox_automation_resource_id: str = ""
    mailbox_tenant_domain: str = ""
    mailbox_automation_timeout_seconds: int = 600
    mailbox_automation_poll_seconds: int = 5
    # The public route, per caller address per minute, per replica. Teams sends from few addresses.
    rate_limit_teams_bot_per_minute: int = 600
    rate_limit_legal_per_minute: int = 60
    # Messages one person may send the bot in a minute before it stops answering (app/teams_intake.py), per replica; the
    # hourly cap on tickets and replies is the admin setting.
    rate_limit_teams_person_per_minute: int = 20
    # Where a file a person sent the bot may be downloaded from without the bot's token (Teams puts uploads on the
    # sender's OneDrive/SharePoint): host name suffixes, comma separated. Anything else is refused, so a crafted
    # activity cannot make the app fetch from inside its network. Check on staging that real uploads are on this list.
    teams_file_host_suffixes: str = ".sharepoint.com,.sharepoint-df.com,.1drv.com"
    # Out-of-office detection (phase 2E): the worker reads agents' presence from Graph
    # (Presence.Read.All) and marks those who are out of office. Needs the GRAPH_* credentials.
    presence_enabled: bool = False
    # The nightly Entra accountEnabled check (phase 6B, app/entra_check.py): the worker makes agents whose
    # account Entra has disabled or deleted inactive. Needs the GRAPH_* credentials and User.Read.All. OFF
    # by default like presence; with it off the job does nothing and makes no Graph call.
    entra_account_check_enabled: bool = False
    # Reading a requester's Outlook mailbox time zone from Graph (GET /users/{id}/mailboxSettings, application
    # permission MailboxSettings.Read), run by the worker. OFF: that permission cannot be scoped to the shared
    # mailbox, so for now requesters use their account zone or the default_time_zone setting. The code stays
    # for a later release; intake still notes each address, so turning it on picks everyone up.
    mailbox_time_zones_enabled: bool = False
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
    # Where the public app is reached (the emailed approval link is built on it). Blank: no link, so
    # the approval email waits.
    public_base_url: str = ""
    # Where the web app (the agent console) is reached, for the "Open ticket" link on a Teams card.
    # Blank: falls back to PUBLIC_BASE_URL, which suits one address serving both (development).
    # In Azure the two apps have different addresses, so set both.
    web_base_url: str = ""
    # Application Insights (the Bicep sets it from the monitoring module). Blank: no telemetry.
    applicationinsights_connection_string: str = ""

    # Rate limits on the public routes, per caller address per minute, per replica (app/rate_limit.py).
    # The approval link is a credential in an address, so guessing it is slowed down; the Graph webhook
    # gets a high limit because Microsoft sends from few addresses and a refused notification is lost
    # until the catch-up sync finds the mail.
    rate_limit_approvals_per_minute: int = 30
    rate_limit_action_links_per_minute: int = 30
    rate_limit_webhook_per_minute: int = 600
    # The break-glass sign-in form on the web app (#163), per caller address per minute, per replica.
    rate_limit_break_glass_per_minute: int = 10
    # The emergency password form (outage mode, #164), the same way.
    rate_limit_emergency_per_minute: int = 10
    # The people picker on the portal's mailbox forms (#309): one search per keystroke pause, per caller address per minute.
    rate_limit_people_search_per_minute: int = 60
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

    # Inbound mail (phase 3B, app/inbound.py). INBOUND_ENABLED switches the worker's mail-in job on (it
    # also needs the GRAPH_* values and the mailbox). The intake limits (attachments, Cc, mails per sender
    # an hour) are admin settings, not environment values (app/settings_store.py).
    inbound_enabled: bool = False

    # Comma-separated emails granted super_admin on first login, so there's
    # a way into /admin before any roles have been assigned there.
    super_admin_emails: str = ""

    # How often the background worker (app/worker.py) writes its heartbeat line. The dead man's
    # switch alert is set to a few multiples of this.
    worker_heartbeat_seconds: int = 60

    # Who makes mailbox changes in Exchange (app/mailbox_executor.py). Unset: the feature is OFF (the mailbox-changes job does
    # nothing and a change cannot be created), so an environment that has not been set up for it still starts. "fake" keeps them in memory (local development and tests;
    # the worker refuses it anywhere but dev) and "azure" is the Azure Automation account (#308). Only the worker uses it.
    mailbox_executor: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")  # ignore keys left in an old .env (e.g. the retired INBOUND_MAX_*)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def graph_configured(self) -> bool:
        return bool(self.graph_tenant_id and self.graph_client_id and self.graph_client_secret)

    @property
    def ticket_link_base(self) -> str:
        return self.web_base_url or self.public_base_url

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
    def teams_file_hosts(self) -> tuple[str, ...]:
        return tuple(s.strip().lower() for s in self.teams_file_host_suffixes.split(",") if s.strip())

    @property
    def teams_bot_configured(self) -> bool:
        return bool(self.teams_bot_enabled and self.teams_bot_app_id and self.teams_bot_tenant_id)

    @property
    def sso_configured(self) -> bool:
        return bool(self.sso_tenant_id and self.sso_client_id and self.sso_client_secret)

    @property
    def super_admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.super_admin_emails.split(",") if e.strip()]


BCRYPT_ROUNDS_RANGE = (4, 16)
BCRYPT_MIN_ROUNDS_OUTSIDE_DEV = 12
MAIL_SENDING_MODES = ("off", "dev", "graph")
MAILBOX_EXECUTORS = ("", "fake", "azure")  # "" is off


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
    if config.mailbox_executor not in MAILBOX_EXECUTORS:
        errors.append("MAILBOX_EXECUTOR must be unset (off), fake or azure")
    if config.mailbox_executor == "azure" and config.app_role in ("worker", "all"):
        # Only the worker calls Azure; web and public just need the variable set so they can create a change.
        from app.mailbox_executor_azure import azure_executor_config_errors

        errors.extend(azure_executor_config_errors(config))
    if config.teams_bot_enabled and not (config.teams_bot_app_id and config.teams_bot_tenant_id):
        errors.append("TEAMS_BOT_ENABLED needs TEAMS_BOT_APP_ID and TEAMS_BOT_TENANT_ID")
    if config.teams_bot_client_secret and config.app_role == "web":
        errors.append("Only the public app and the worker may hold the Teams bot secret (TEAMS_BOT_CLIENT_SECRET)")
    if not BCRYPT_ROUNDS_RANGE[0] <= config.bcrypt_rounds <= BCRYPT_ROUNDS_RANGE[1]:
        errors.append(f"BCRYPT_ROUNDS must be between {BCRYPT_ROUNDS_RANGE[0]} and {BCRYPT_ROUNDS_RANGE[1]}")
    if config.is_dev:
        return errors
    if config.bcrypt_rounds < BCRYPT_MIN_ROUNDS_OUTSIDE_DEV:
        errors.append(f"BCRYPT_ROUNDS below {BCRYPT_MIN_ROUNDS_OUTSIDE_DEV} only works in the dev environment (it makes passwords cheap to crack)")
    if config.mail_sending == "dev":
        errors.append("MAIL_SENDING=dev only works in the dev environment (it sends nothing)")
    if config.mailbox_executor == "fake" and config.app_role in ("worker", "all"):
        # The worker is the one process that applies mailbox changes. A fake executor outside dev would tell people their
        # forwarding is on when Exchange was never touched.
        errors.append("MAILBOX_EXECUTOR=fake only works in the dev environment (it changes nothing in Exchange): unset it or set azure")

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
